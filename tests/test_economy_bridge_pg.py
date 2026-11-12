"""Real-PostgreSQL concurrency tests for the economy bridge.

Runs only when TEST_DATABASE_URL points at a disposable test database —
never production. The DSN is validated fail-closed: Railway hosts,
production-looking identifiers and non-test database names are rejected,
and all bridge tables live in a unique per-run schema that is dropped on
teardown. Exercises true parallel settle/settle, settle/refund,
refund/refund and pending-credit claims through the actual handlers.

    TEST_DATABASE_URL=postgresql://user:pass@host:5432/mydb_test \
        pytest tests/test_economy_bridge_pg.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set — skipping real-PG bridge tests"),
]

GUILD = 999000111222333444
USERS = [999000111222333001, 999000111222333002, 999000111222333003]

_FORBIDDEN_HOST = re.compile(r"railway|prod|production|neon\.tech|amazonaws|render\.com", re.I)
_ALLOWED_DB = re.compile(r"(^|[_\-])test([_\-]|$)|(_test$|^test_)", re.I)


def _assert_safe_dsn(dsn: str) -> None:
    """Fail closed: only an explicitly named, obviously disposable database."""
    parsed = urlparse(dsn)
    host = (parsed.hostname or "").lower()
    db = (parsed.path or "").lstrip("/")
    assert parsed.scheme in {"postgres", "postgresql"}, "TEST_DATABASE_URL must be a postgres DSN"
    assert host and not _FORBIDDEN_HOST.search(host), f"refusing non-test host: {host}"
    assert host not in {"localhost", "127.0.0.1"} or os.getenv("ALLOW_LOCAL_TEST_DB") == "1", \
        "localhost test DBs require ALLOW_LOCAL_TEST_DB=1"
    assert _ALLOWED_DB.search(db), f"database name must look like a test database, got: {db!r}"


class Req:
    """aiohttp-request stand-in with real mapping semantics."""

    def __init__(self, body=None, query=None, token="tok") -> None:
        self._body = body
        self.query = query or {}
        self.headers = {"X-Bridge-Token": token}
        self.remote = "127.0.0.1"

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest_asyncio.fixture
async def bridge_env(monkeypatch):
    """Real asyncpg pool inside a unique schema; dropped on teardown."""
    _assert_safe_dsn(DSN)
    import asyncpg
    import services.economy_bridge as bridge
    import storage.db as storage_db

    monkeypatch.setenv("ECONOMY_BRIDGE_TOKEN", "tok")
    monkeypatch.setenv("ECONOMY_BRIDGE_WRITES_ENABLED", "true")
    schema = "bridge_test_" + uuid.uuid4().hex[:12]
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        DSN, min_size=4, max_size=8,
        server_settings={"search_path": f'"{schema}"'},
    )

    class P:
        @asynccontextmanager
        async def acquire(self):
            async with pool.acquire() as conn:
                yield conn

    async def fake_get_pool():
        return P()

    monkeypatch.setattr(storage_db, "get_pool", fake_get_pool)
    # Materialize bridge tables inside the throwaway schema up front.
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_balance (
                guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, balance BIGINT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await conn.execute(bridge._SCHEMA)
    yield bridge, pool, admin, schema
    await pool.close()
    await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await admin.close()


async def _reset(pool, prefix: str):
    async with pool.acquire() as conn:
        for table in ("arcade_escrow", "arcade_ledger", "arcade_pending_credit"):
            await conn.execute(f"DELETE FROM {table} WHERE op_id LIKE $1", prefix + "%")
        await conn.execute("DELETE FROM user_balance WHERE guild_id = $1", GUILD)


async def _exec(pool, sql, *args):
    async with pool.acquire() as conn:
        return await conn.execute(sql, *args)


async def _fetchval(pool, sql, *args):
    async with pool.acquire() as conn:
        return await conn.fetchval(sql, *args)


async def _fund(pool, amount: int, users=USERS):
    for u in users:
        await _exec(pool,
            "INSERT INTO user_balance (guild_id, user_id, balance) VALUES ($1,$2,$3) "
            "ON CONFLICT (guild_id,user_id) DO UPDATE SET balance = $3",
            GUILD, u, amount)


async def _balance(pool, user: int) -> int:
    return int(await _fetchval(pool, "SELECT balance FROM user_balance WHERE guild_id=$1 AND user_id=$2", GUILD, user))


def _stakes(op_users=None, amount=100):
    users = op_users or USERS[:2]
    return [{"user_id": str(u), "amount": amount} for u in users]


def _q(guild=GUILD):
    return {"guild_id": str(guild)}


async def test_parallel_settle_credits_exactly_once(bridge_env):
    bridge, pool, _admin, _schema = bridge_env
    await _reset(pool, "pg-settle")
    await _fund(pool, 1_000)
    op = "pg-settle-1"
    r = await bridge.escrow(Req({"op_id": op, "stakes": _stakes()}, _q()))
    assert r.status == 200, r.text
    body = {"op_id": op, "payouts": [{"user_id": str(USERS[0]), "amount": 200}]}
    results = await asyncio.gather(*[bridge.settle(Req(body, _q())) for _ in range(4)])
    assert all(r.status == 200 for r in results)
    bodies = [json.loads(r.text) for r in results]  # aiohttp .text is a property
    assert sum(1 for b in bodies if b.get("replayed") is not True) == 1
    # Replay returns the persisted financial result.
    replay = next(b for b in bodies if b.get("replayed") is True)
    assert replay["payouts"][0]["credited"] == 200
    assert await _balance(pool, USERS[0]) == 1_100
    assert await _balance(pool, USERS[1]) == 900
    # Pool conservation + no duplicate ledger rows.
    ledger = await _fetchval(pool, "SELECT count(*) FROM arcade_ledger WHERE op_id=$1 AND kind='payout'", op)
    assert ledger == 1


async def test_parallel_settle_vs_refund_exactly_one_wins(bridge_env):
    bridge, pool, _admin, _schema = bridge_env
    await _reset(pool, "pg-race")
    await _fund(pool, 1_000)
    op = "pg-race-1"
    await bridge.escrow(Req({"op_id": op, "stakes": _stakes()}, _q()))
    settle_r, refund_r = await asyncio.gather(
        bridge.settle(Req({"op_id": op, "payouts": [{"user_id": str(USERS[0]), "amount": 200}]}, _q())),
        bridge.refund(Req({"op_id": op}, _q())),
    )
    state = await _fetchval(pool, "SELECT state FROM arcade_escrow WHERE op_id=$1", op)
    if state == "settled":
        assert settle_r.status == 200 and refund_r.status == 409
        assert await _balance(pool, USERS[0]) == 1_100
        assert await _balance(pool, USERS[1]) == 900
    else:
        assert state == "refunded"
        assert refund_r.status == 200 and settle_r.status == 409
        assert await _balance(pool, USERS[0]) == 1_000
        assert await _balance(pool, USERS[1]) == 1_000


async def test_parallel_refunds_credit_once(bridge_env):
    bridge, pool, _admin, _schema = bridge_env
    await _reset(pool, "pg-refund")
    await _fund(pool, 1_000)
    op = "pg-refund-1"
    await bridge.escrow(Req({"op_id": op, "stakes": _stakes()}, _q()))
    results = await asyncio.gather(*[bridge.refund(Req({"op_id": op}, _q())) for _ in range(4)])
    assert all(r.status == 200 for r in results)
    bodies = [json.loads(r.text) for r in results]
    assert sum(1 for b in bodies if b.get("replayed") is not True) == 1
    assert await _balance(pool, USERS[0]) == 1_000
    assert await _balance(pool, USERS[1]) == 1_000
    refunds = await _fetchval(pool, "SELECT count(*) FROM arcade_ledger WHERE op_id=$1 AND kind='refund'", op)
    assert refunds == 2  # one per escrowed player, exactly once


async def test_settle_rejects_invalid_distribution_under_lock(bridge_env):
    bridge, pool, _admin, _schema = bridge_env
    await _reset(pool, "pg-dist")
    await _fund(pool, 1_000)
    op = "pg-dist-1"
    await bridge.escrow(Req({"op_id": op, "stakes": _stakes(amount=100)}, _q()))
    # Partial allocation must fail even when raced.
    partial = {"op_id": op, "payouts": [{"user_id": str(USERS[0]), "amount": 100}]}
    results = await asyncio.gather(*[bridge.settle(Req(partial, _q())) for _ in range(3)])
    assert all(r.status == 400 for r in results)
    assert await _fetchval(pool, "SELECT state FROM arcade_escrow WHERE op_id=$1", op) == "reserved"
    # Foreign recipient rejected.
    foreign = {"op_id": op, "payouts": [{"user_id": str(USERS[0]), "amount": 100}, {"user_id": str(USERS[2]), "amount": 100}]}
    r = await bridge.settle(Req(foreign, _q()))
    assert r.status == 400
    # Full authorized distribution succeeds.
    full = {"op_id": op, "payouts": [{"user_id": str(USERS[0]), "amount": 200}]}
    r2 = await bridge.settle(Req(full, _q()))
    assert r2.status == 200


async def test_pending_credit_concurrent_claims_and_partial_crash_retry(bridge_env):
    bridge, pool, _admin, _schema = bridge_env
    await _reset(pool, "pg-claim")
    await _fund(pool, 1_000)
    op = "pg-claim-1"
    # Single-player escrow of 1000; balance near cap so payout overflows.
    await _exec(pool, "UPDATE user_balance SET balance=100000 WHERE guild_id=$1 AND user_id=$2", GUILD, USERS[0])
    await bridge.escrow(Req({"op_id": op, "stakes": [{"user_id": str(USERS[0]), "amount": 1000}]}, _q()))
    await _exec(pool, "UPDATE user_balance SET balance=99500 WHERE guild_id=$1 AND user_id=$2", GUILD, USERS[0])
    settle = await bridge.settle(Req({"op_id": op, "payouts": [{"user_id": str(USERS[0]), "amount": 1000}]}, _q()))
    assert settle.status == 200
    assert await _balance(pool, USERS[0]) == 100_000
    pending = await _fetchval(pool, "SELECT amount FROM arcade_pending_credit WHERE op_id=$1", op)
    assert pending == 500

    # At the cap, concurrent claims credit nothing but stay queued.
    claims = await asyncio.gather(*[bridge.claim_pending(Req({"op_id": op}, _q())) for _ in range(3)])
    assert all(json.loads(r.text)["claimed"][0]["credited"] == 0 for r in claims)
    assert await _fetchval(pool, "SELECT amount FROM arcade_pending_credit WHERE op_id=$1", op) == 500

    # Balance drops: a "crash" mid-claim is simulated by a partial-cap claim,
    # then a retry completes it — ledger shows each credit exactly once.
    await _exec(pool, "UPDATE user_balance SET balance=90000 WHERE guild_id=$1 AND user_id=$2", GUILD, USERS[0])
    first = await bridge.claim_pending(Req({"op_id": op}, _q()))
    assert json.loads(first.text)["claimed"][0]["credited"] == 500
    assert await _balance(pool, USERS[0]) == 90_500
    assert await _fetchval(pool, "SELECT count(*) FROM arcade_pending_credit WHERE op_id=$1", op) == 0
    # Retry after completion is a no-op.
    again = await bridge.claim_pending(Req({"op_id": op}, _q()))
    assert json.loads(again.text)["claimed"] == []
    ledger_sum = await _fetchval(pool, "SELECT COALESCE(sum(amount),0) FROM arcade_ledger WHERE op_id=$1 AND kind='pending_credit'", op)
    assert ledger_sum == 500  # no duplicate credit entries


async def test_writes_disabled_blocks_mutations_on_real_db(bridge_env, monkeypatch):
    bridge, pool, _admin, _schema = bridge_env
    monkeypatch.setenv("ECONOMY_BRIDGE_WRITES_ENABLED", "false")
    await _reset(pool, "pg-writes")
    await _fund(pool, 1_000)
    op = "pg-writes-1"
    r = await bridge.escrow(Req({"op_id": op, "stakes": _stakes()}, _q()))
    assert r.status == 503
    r = await bridge.settle(Req({"op_id": op, "payouts": [{"user_id": str(USERS[0]), "amount": 200}]}, _q()))
    assert r.status == 503
    r = await bridge.refund(Req({"op_id": op}, _q()))
    assert r.status == 503
    r = await bridge.claim_pending(Req({"op_id": op}, _q()))
    assert r.status == 503
    assert await _balance(pool, USERS[0]) == 1_000
    assert await _balance(pool, USERS[1]) == 1_000
    assert await _fetchval(pool, "SELECT count(*) FROM arcade_escrow",) == 0
    # Read path still works with writes disabled.
    r = await bridge.balance(Req(query={"guild_id": str(GUILD), "user_id": str(USERS[0])}))
    assert r.status == 200 and json.loads(r.text)["balance"] == 1_000
