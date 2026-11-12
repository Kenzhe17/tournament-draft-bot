"""Real-PostgreSQL concurrency tests for the economy bridge.

Runs only when TEST_DATABASE_URL (or DATABASE_URL) points at a disposable
database — never production. Exercises true parallel settle/settle,
settle/refund and refund/refund through the actual HTTP handlers and row
locks, which the in-memory fake cannot prove.

    TEST_DATABASE_URL=postgresql://... pytest tests/test_economy_bridge_pg.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DSN = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set — skipping real-PG bridge tests"),
]

GUILD = 999000111222333444
USERS = [999000111222333001, 999000111222333002]


class Req:
    def __init__(self, body=None, query=None, token="tok") -> None:
        self._body = body
        self.query = {"get": lambda k, d=None: (query or {}).get(k, d)}
        self.headers = {"get": lambda k, d=None: token if k == "X-Bridge-Token" else d}
        self.remote = "127.0.0.1"

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest_asyncio.fixture
async def pool(monkeypatch):
    import asyncpg
    import services.economy_bridge as bridge
    import storage.db as storage_db

    monkeypatch.setenv("ECONOMY_BRIDGE_TOKEN", "tok")
    pg = await asyncpg.connect(DSN)
    await pg.execute(bridge._SCHEMA)
    await pg.execute("""
        CREATE TABLE IF NOT EXISTS user_balance (
            guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, balance BIGINT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    real_pool = await asyncpg.create_pool(DSN, min_size=4, max_size=8)

    @asynccontextmanager
    async def acquire():
        async with real_pool.acquire() as conn:
            yield conn

    class P:
        acquire = staticmethod(lambda: acquire())

    async def fake_get_pool():
        return P()

    monkeypatch.setattr(storage_db, "get_pool", fake_get_pool)
    yield pg, bridge
    await real_pool.close()
    await pg.close()


async def _reset(pg, op_prefix: str):
    await pg.execute("DELETE FROM arcade_escrow WHERE op_id LIKE $1", op_prefix + "%")
    await pg.execute("DELETE FROM arcade_ledger WHERE op_id LIKE $1", op_prefix + "%")
    await pg.execute("DELETE FROM arcade_pending_credit WHERE op_id LIKE $1", op_prefix + "%")
    await pg.execute("DELETE FROM user_balance WHERE guild_id = $1", GUILD)


async def _fund(pg, amount: int):
    for u in USERS:
        await pg.execute(
            "INSERT INTO user_balance (guild_id, user_id, balance) VALUES ($1,$2,$3) ON CONFLICT (guild_id,user_id) DO UPDATE SET balance = $3",
            GUILD, u, amount,
        )


async def _balance(pg, user: int) -> int:
    return int(await pg.fetchval("SELECT balance FROM user_balance WHERE guild_id=$1 AND user_id=$2", GUILD, user))


async def test_parallel_settle_credits_exactly_once(pool):
    pg, bridge = pool
    await _reset(pg, "pg-settle")
    await _fund(pg, 1_000)
    op = "pg-settle-1"
    stakes = [{"user_id": str(USERS[0]), "amount": 100}, {"user_id": str(USERS[1]), "amount": 100}]
    r = await bridge.escrow(Req({"op_id": op, "stakes": stakes}, {"guild_id": str(GUILD)}))
    assert r.status == 200
    payouts = [{"user_id": str(USERS[0]), "amount": 200}]
    body = {"op_id": op, "payouts": payouts}
    results = await asyncio.gather(*[bridge.settle(Req(body, {"guild_id": str(GUILD)})) for _ in range(4)])
    assert all(r.status == 200 for r in results)
    # Exactly one non-replayed settlement; winner credited the pool once.
    assert sum(1 for r in results if json.loads(await r.text()).get("replayed") is not True) == 1
    assert await _balance(pg, USERS[0]) == 1_100  # 1000 - 100 + 200
    assert await _balance(pg, USERS[1]) == 900    # 1000 - 100


async def test_parallel_settle_vs_refund_exactly_one_wins(pool):
    pg, bridge = pool
    await _reset(pg, "pg-race")
    await _fund(pg, 1_000)
    op = "pg-race-1"
    stakes = [{"user_id": str(USERS[0]), "amount": 100}, {"user_id": str(USERS[1]), "amount": 100}]
    await bridge.escrow(Req({"op_id": op, "stakes": stakes}, {"guild_id": str(GUILD)}))
    settle_r, refund_r = await asyncio.gather(
        bridge.settle(Req({"op_id": op, "payouts": [{"user_id": str(USERS[0]), "amount": 200}]}, {"guild_id": str(GUILD)})),
        bridge.refund(Req({"op_id": op}, {"guild_id": str(GUILD)})),
    )
    state = await pg.fetchval("SELECT state FROM arcade_escrow WHERE op_id=$1", op)
    if state == "settled":
        assert settle_r.status == 200 and refund_r.status == 409
        assert await _balance(pg, USERS[0]) == 1_100
        assert await _balance(pg, USERS[1]) == 900
    else:
        assert state == "refunded"
        assert refund_r.status == 200 and settle_r.status == 409
        assert await _balance(pg, USERS[0]) == 1_000
        assert await _balance(pg, USERS[1]) == 1_000


async def test_parallel_refunds_credit_once(pool):
    pg, bridge = pool
    await _reset(pg, "pg-refund")
    await _fund(pg, 1_000)
    op = "pg-refund-1"
    stakes = [{"user_id": str(USERS[0]), "amount": 100}, {"user_id": str(USERS[1]), "amount": 100}]
    await bridge.escrow(Req({"op_id": op, "stakes": stakes}, {"guild_id": str(GUILD)}))
    results = await asyncio.gather(*[bridge.refund(Req({"op_id": op}, {"guild_id": str(GUILD)})) for _ in range(4)])
    assert all(r.status == 200 for r in results)
    assert sum(1 for r in results if json.loads(await r.text()).get("replayed") is not True) == 1
    assert await _balance(pg, USERS[0]) == 1_000
    assert await _balance(pg, USERS[1]) == 1_000
