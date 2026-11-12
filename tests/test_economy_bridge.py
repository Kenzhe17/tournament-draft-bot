"""Economy bridge state-machine tests against an in-memory asyncpg fake.

These exercise the real HTTP handlers (escrow / settle / refund / escrows)
including idempotent replays, exactly-once transitions, balance-cap overflow
and full-restoration refunds. True row-lock concurrency requires a real
PostgreSQL; the FOR UPDATE + advisory-lock design is what guarantees it
there — here we verify the sequential state machine cannot double-credit.
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GUILD = 123456789012345678
U1 = "123456789012345671"
U2 = "123456789012345672"
U3 = "123456789012345673"

pytestmark = pytest.mark.asyncio


class FakeConn:
    """Minimal asyncpg.Connection stand-in for the bridge SQL."""

    def __init__(self, db: "FakeDB") -> None:
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql: str, *args):
        s = " ".join(sql.split())
        if "pg_advisory_xact_lock" in s or s.startswith("CREATE "):
            return "OK"
        if "INSERT INTO user_balance" in s:
            guild, user = args[0], args[1]
            self.db.balances.setdefault((guild, user), 100)
            return "INSERT 0 1"
        if "balance = balance - $3" in s:
            guild, user, amount = args
            key = (guild, user)
            if self.db.balances.get(key, 0) >= amount:
                self.db.balances[key] -= amount
                return "UPDATE 1"
            return "UPDATE 0"
        if "UPDATE user_balance SET balance = $3" in s:
            guild, user, balance = args
            self.db.balances[(guild, user)] = balance
            return "UPDATE 1"
        if "INSERT INTO arcade_pending_credit" in s:
            self.db.pending.append({"id": len(self.db.pending) + 1, "op_id": args[0], "guild_id": args[1], "user_id": args[2], "amount": args[3]})
            return "INSERT 0 1"
        if "UPDATE arcade_pending_credit SET amount" in s:
            for credit in self.db.pending:
                if credit["id"] == args[0]:
                    credit["amount"] = args[1]
            return "UPDATE 1"
        if "DELETE FROM arcade_pending_credit" in s:
            self.db.pending = [c for c in self.db.pending if c["id"] != args[0]]
            return "DELETE 1"
        if "INSERT INTO arcade_ledger" in s:
            self.db.ledger.append(args)
            return "INSERT 0 1"
        if "INSERT INTO arcade_escrow" in s:
            op_id, guild, entries, _state = args[0], args[1], args[2], args[3] if len(args) > 3 else "reserved"
            self.db.escrows[op_id] = {
                "op_id": op_id, "guild_id": guild,
                "entries": json.loads(entries) if isinstance(entries, str) else entries,
                "payouts": None, "state": "reserved",
                "created_at": datetime.now(timezone.utc),
            }
            return "INSERT 0 1"
        if "UPDATE arcade_escrow SET state = 'settled'" in s:
            self.db.escrows[args[0]]["state"] = "settled"
            self.db.escrows[args[0]]["payouts"] = json.loads(args[1])
            return "UPDATE 1"
        if "UPDATE arcade_escrow SET state = 'refunded'" in s:
            self.db.escrows[args[0]]["state"] = "refunded"
            return "UPDATE 1"
        raise AssertionError("unexpected execute: " + s[:80])

    async def fetchrow(self, sql: str, *args):
        s = " ".join(sql.split())
        if "FROM arcade_escrow" in s and "state, entries" in s:
            row = self.db.escrows.get(args[0])
            return {"state": row["state"], "entries": json.dumps(row["entries"])} if row else None
        if "FROM arcade_escrow" in s and "SELECT *" in s:
            row = self.db.escrows.get(args[0])
            if not row or row["guild_id"] != args[1]:
                return None
            return dict(row, entries=json.dumps(row["entries"]), payouts=json.dumps(row["payouts"]) if row["payouts"] else None)
        if "FROM user_balance" in s:
            balance = self.db.balances.get((args[0], args[1]))
            return {"balance": balance} if balance is not None else None
        raise AssertionError("unexpected fetchrow: " + s[:80])

    async def fetch(self, sql: str, *args):
        s = " ".join(sql.split())
        if "FROM arcade_escrow" in s and "state = 'reserved'" in s:
            return [
                {"op_id": r["op_id"], "created_at": r.get("created_at")}
                for r in self.db.escrows.values()
                if r["guild_id"] == args[0] and r["state"] == "reserved"
            ]
        if "FROM arcade_pending_credit" in s:
            return [dict(c) for c in self.db.pending if c["op_id"] == args[0] and c["guild_id"] == args[1]]
        raise AssertionError("unexpected fetch: " + s[:80])


class FakeDB:
    def __init__(self) -> None:
        self.balances: dict = {}
        self.escrows: dict = {}
        self.ledger: list = []
        self.pending: list = []

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self)


class FakeRequest:
    def __init__(self, body=None, query=None, token="tok") -> None:
        self._body = body
        self.query = {"get": lambda k, d=None: (query or {}).get(k, d)}
        self.headers = {"get": lambda k, d=None: token if k == "X-Bridge-Token" else d}
        self.remote = "127.0.0.1"

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ECONOMY_BRIDGE_TOKEN", "tok")
    db = FakeDB()
    import services.economy_bridge as bridge
    import storage.db as storage_db

    class FakePool:
        @asynccontextmanager
        async def acquire(self):
            yield FakeConn(db)

    async def fake_get_pool():
        return FakePool()

    monkeypatch.setattr(storage_db, "get_pool", fake_get_pool)
    return db


def _stakes_payload(op_id="op1", stakes=None):
    return {"op_id": op_id, "stakes": stakes if stakes is not None else [
        {"user_id": U1, "amount": 100}, {"user_id": U2, "amount": 100},
    ]}


async def test_escrow_settle_settle_replay_credits_once(env):
    from services.economy_bridge import escrow, settle

    env.balances[(GUILD, int(U1))] = 500
    env.balances[(GUILD, int(U2))] = 500
    r = await escrow(FakeRequest(_stakes_payload(), {"guild_id": str(GUILD)}))
    assert r.status == 200
    # After escrow: 400 / 400.

    payouts = [{"user_id": U1, "amount": 200}]
    body = {"op_id": "op1", "payouts": payouts}
    r1 = await settle(FakeRequest(body, {"guild_id": str(GUILD)}))
    assert r1.status == 200
    assert env.balances[(GUILD, int(U1))] == 600
    assert env.balances[(GUILD, int(U2))] == 400  # loser keeps stake-minus amount

    # Replay must not credit again.
    r2 = await settle(FakeRequest(body, {"guild_id": str(GUILD)}))
    assert json.loads(await r2.text())["replayed"] is True
    assert env.balances[(GUILD, int(U1))] == 600


async def test_settle_then_refund_conflicts(env):
    from services.economy_bridge import escrow, refund, settle

    env.balances[(GUILD, int(U1))] = 300
    env.balances[(GUILD, int(U2))] = 300
    await escrow(FakeRequest(_stakes_payload(), {"guild_id": str(GUILD)}))
    await settle(FakeRequest({"op_id": "op1", "payouts": [{"user_id": U1, "amount": 200}]}, {"guild_id": str(GUILD)}))
    assert env.balances[(GUILD, int(U1))] == 400
    r = await refund(FakeRequest({"op_id": "op1"}, {"guild_id": str(GUILD)}))
    assert r.status == 409
    assert env.balances[(GUILD, int(U1))] == 400
    assert env.balances[(GUILD, int(U2))] == 200


async def test_refund_refund_replay_and_full_amount(env):
    from services.economy_bridge import escrow, refund

    env.balances[(GUILD, int(U1))] = 50_000
    env.balances[(GUILD, int(U2))] = 50_000
    await escrow(FakeRequest(_stakes_payload(), {"guild_id": str(GUILD)}))
    r1 = await refund(FakeRequest({"op_id": "op1"}, {"guild_id": str(GUILD)}))
    assert r1.status == 200
    assert env.balances[(GUILD, int(U1))] == 50_000  # full restitution
    r2 = await refund(FakeRequest({"op_id": "op1"}, {"guild_id": str(GUILD)}))
    assert json.loads(await r2.text())["replayed"] is True
    assert env.balances[(GUILD, int(U1))] == 50_000  # not double-refunded


async def test_refund_near_cap_restores_full_amount(env):
    from services.economy_bridge import escrow, refund

    env.balances[(GUILD, int(U1))] = 100_000
    await escrow(FakeRequest(_stakes_payload("op9", [{"user_id": U1, "amount": 500}]), {"guild_id": str(GUILD)}))
    env.balances[(GUILD, int(U1))] = 100_000  # winnings arrived while escrowed
    await refund(FakeRequest({"op_id": "op9"}, {"guild_id": str(GUILD)}))
    # 100_000 + 500 exceeds MAX_BALANCE=100_000 — refund must still be full.
    assert env.balances[(GUILD, int(U1))] == 100_500


async def test_payout_cap_overflow_is_reported_and_ledgered(env):
    from services.economy_bridge import escrow, settle

    env.balances[(GUILD, int(U1))] = 100_000
    await escrow(FakeRequest(_stakes_payload("op2", [{"user_id": U1, "amount": 1000}]), {"guild_id": str(GUILD)}))
    env.balances[(GUILD, int(U1))] = 99_500  # near cap when payout lands
    r = await settle(FakeRequest({"op_id": "op2", "payouts": [{"user_id": U1, "amount": 1000}]}, {"guild_id": str(GUILD)}))
    data = json.loads(await r.text())
    assert data["payouts"][0]["credited"] == 500
    assert data["payouts"][0]["overflow"] == 500
    assert env.balances[(GUILD, int(U1))] == 100_000
    kinds = [entry[3] for entry in env.ledger]
    assert "payout_overflow" in kinds


async def test_insufficient_balance_rolls_everything_back(env):
    from services.economy_bridge import escrow

    env.balances[(GUILD, int(U1))] = 50
    env.balances[(GUILD, int(U2))] = 10_000
    r = await escrow(FakeRequest(_stakes_payload(), {"guild_id": str(GUILD)}))
    assert r.status == 409
    body = json.loads(await r.text())
    assert body["error"] == "insufficient"
    assert env.balances[(GUILD, int(U2))] == 10_000  # no partial debit
    assert "op1" not in env.escrows


async def test_escrow_replay_and_op_conflict(env):
    from services.economy_bridge import escrow

    await escrow(FakeRequest(_stakes_payload(), {"guild_id": str(GUILD)}))
    before = dict(env.balances)
    r = await escrow(FakeRequest(_stakes_payload(), {"guild_id": str(GUILD)}))
    assert json.loads(await r.text())["replayed"] is True
    assert env.balances == before  # replay never re-debits
    # Same op_id with a different roster is a conflict, not a silent replay.
    r2 = await escrow(FakeRequest(_stakes_payload(stakes=[{"user_id": U3, "amount": 100}]), {"guild_id": str(GUILD)}))
    assert r2.status == 409


async def test_settle_rejects_payouts_above_pool(env):
    from services.economy_bridge import escrow, settle

    await escrow(FakeRequest(_stakes_payload(), {"guild_id": str(GUILD)}))
    r = await settle(FakeRequest({"op_id": "op1", "payouts": [{"user_id": U1, "amount": 999}]}, {"guild_id": str(GUILD)}))
    assert r.status == 400
    assert env.escrows["op1"]["state"] == "reserved"  # still settleable later


async def test_escrows_listing_and_auth(env):
    from services.economy_bridge import escrows, escrow

    await escrow(FakeRequest(_stakes_payload(), {"guild_id": str(GUILD)}))
    r = await escrows(FakeRequest(query={"guild_id": str(GUILD)}))
    data = json.loads(await r.text())
    assert [e["op_id"] for e in data["escrows"]] == ["op1"]
    denied = await escrows(FakeRequest(query={"guild_id": str(GUILD)}, token="wrong"))
    assert denied.status == 401


async def test_settle_replay_returns_persisted_payouts(env):
    from services.economy_bridge import escrow, settle

    env.balances[(GUILD, int(U1))] = 100_000
    await escrow(FakeRequest(_stakes_payload("op3", [{"user_id": U1, "amount": 1000}]), {"guild_id": str(GUILD)}))
    env.balances[(GUILD, int(U1))] = 99_500
    body = {"op_id": "op3", "payouts": [{"user_id": U1, "amount": 1000}]}
    r1 = await settle(FakeRequest(body, {"guild_id": str(GUILD)}))
    first = json.loads(await r1.text())
    assert first["payouts"][0]["overflow"] == 500
    # A retried settle after a lost response returns the same financial result.
    r2 = await settle(FakeRequest(body, {"guild_id": str(GUILD)}))
    replay = json.loads(await r2.text())
    assert replay["replayed"] is True
    assert replay["payouts"][0]["credited"] == 500
    assert replay["payouts"][0]["overflow"] == 500
    assert env.balances[(GUILD, int(U1))] == 100_000  # credited exactly once


async def test_settle_rejects_partial_or_foreign_distribution(env):
    from services.economy_bridge import escrow, settle

    await escrow(FakeRequest(_stakes_payload(), {"guild_id": str(GUILD)}))
    # Pool is 200; allocating only 100 must be rejected — the pool is conserved.
    r = await settle(FakeRequest({"op_id": "op1", "payouts": [{"user_id": U1, "amount": 100}]}, {"guild_id": str(GUILD)}))
    assert r.status == 400
    assert json.loads(await r.text())["error"] == "invalid_distribution"
    # A recipient who never escrowed cannot receive pool coins.
    r2 = await settle(FakeRequest({"op_id": "op1", "payouts": [{"user_id": U1, "amount": 100}, {"user_id": U3, "amount": 100}]}, {"guild_id": str(GUILD)}))
    assert r2.status == 400
    assert env.escrows["op1"]["state"] == "reserved"
    # The correct full distribution still settles.
    r3 = await settle(FakeRequest({"op_id": "op1", "payouts": [{"user_id": U1, "amount": 200}]}, {"guild_id": str(GUILD)}))
    assert r3.status == 200


async def test_pending_credit_claim_pays_when_balance_has_room(env):
    from services.economy_bridge import claim_pending, escrow, settle

    env.balances[(GUILD, int(U1))] = 100_000
    await escrow(FakeRequest(_stakes_payload("op4", [{"user_id": U1, "amount": 1000}]), {"guild_id": str(GUILD)}))
    env.balances[(GUILD, int(U1))] = 99_500
    await settle(FakeRequest({"op_id": "op4", "payouts": [{"user_id": U1, "amount": 1000}]}, {"guild_id": str(GUILD)}))
    assert len(env.pending) == 1 and env.pending[0]["amount"] == 500
    # At the cap nothing is credited but the credit stays queued.
    r = await claim_pending(FakeRequest({"op_id": "op4"}, {"guild_id": str(GUILD)}))
    data = json.loads(await r.text())
    assert data["claimed"][0]["credited"] == 0
    assert env.pending[0]["amount"] == 500
    # Once balance drops, the remainder is claimable — never destroyed.
    env.balances[(GUILD, int(U1))] = 90_000
    r2 = await claim_pending(FakeRequest({"op_id": "op4"}, {"guild_id": str(GUILD)}))
    data2 = json.loads(await r2.text())
    assert data2["claimed"][0]["credited"] == 500
    assert env.balances[(GUILD, int(U1))] == 90_500
    assert env.pending == []
