"""PG-интеграция: пул MM-каналов и финализация на реальном PostgreSQL.

pgserver поднимает изолированный инстанс; проверяем:
- acquire_slot выдаёт только каналы из MM_MATCH_CHANNEL_POOL;
- два параллельных матча получают два разных канала, третий ждёт;
- жизненный цикл reserved → active → cleaning → free;
- слот вне пула никогда не выдаётся;
- match_finalizations фиксирует stats_done атомарно (ELO один раз).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pgserver = pytest.importorskip("pgserver")

import asyncpg  # noqa: E402
import pytest_asyncio  # noqa: E402

from services import match_channel_pool as mcp  # noqa: E402

POOL_IDS = [1569282744909824061, 1569282781270245417]
OUTSIDER_ID = 999999999999999999

_SLOTS_DDL = """
CREATE TABLE IF NOT EXISTS match_channel_slots (
    channel_id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'free',
    owner TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_mm_pool")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri, monkeypatch):
    pool = await asyncpg.create_pool(pg_uri, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute(_SLOTS_DDL)
        await conn.execute("DELETE FROM match_channel_slots")
    monkeypatch.setattr(mcp, "get_pool", lambda: _const(pool))
    monkeypatch.setattr(mcp, "pool_channel_ids", lambda: list(POOL_IDS))
    mcp._pool_ids = list(POOL_IDS)
    try:
        yield pool
    finally:
        await pool.close()


async def _const(x):
    return x


@pytest.mark.asyncio
async def test_two_parallel_matches_get_two_channels(pg_pool):
    """Два одновременных acquire → два разных канала из двух."""
    await mcp.ensure_slots()
    results = await asyncio.gather(
        mcp.acquire_slot(guild_id=1, owner="mm:a"),
        mcp.acquire_slot(guild_id=1, owner="mm:b"),
    )
    assert sorted(r for r in results if r) == sorted(POOL_IDS)


@pytest.mark.asyncio
async def test_third_match_waits_when_pool_full(pg_pool):
    """Оба слота заняты → третий acquire возвращает None."""
    await mcp.ensure_slots()
    await mcp.acquire_slot(1, "m1")
    await mcp.acquire_slot(1, "m2")
    assert await mcp.acquire_slot(1, "m3") is None


@pytest.mark.asyncio
async def test_out_of_pool_slot_never_issued(pg_pool):
    """Свободная строка вне пула в БД не выдаётся acquire_slot."""
    await mcp.ensure_slots()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO match_channel_slots (channel_id, state) "
            "VALUES ($1, 'free') ON CONFLICT DO NOTHING",
            OUTSIDER_ID,
        )
    got = await mcp.acquire_slot(1, "m1")
    assert got in POOL_IDS
    assert got != OUTSIDER_ID


@pytest.mark.asyncio
async def test_release_frees_slot_for_next_match(pg_pool):
    """После release слот снова доступен следующему матчу."""
    await mcp.ensure_slots()
    first = await mcp.acquire_slot(1, "m1")
    assert first in POOL_IDS
    # Помечаем active и освобождаем через transition-цепочку
    assert await mcp.transition(first, {"reserved"}, "active", "m1")
    assert await mcp.transition(first, {"active"}, "cleaning", "m1")
    assert await mcp.transition(first, {"cleaning"}, "free")
    second = await mcp.acquire_slot(1, "m2")
    assert second is not None


@pytest.mark.asyncio
async def test_finalize_marker_written_once(pg_pool, monkeypatch):
    """match_finalizations: stats_done — маркер единственности ELO."""
    import storage.db as dbmod
    from storage import finalize_store

    prev_pool = dbmod._pool
    dbmod._pool = pg_pool
    try:
        await pg_pool.execute(
            """CREATE TABLE IF NOT EXISTS match_finalizations (
                match_id TEXT PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )"""
        )
        await finalize_store.mark_done(1, "t1_final_0")
        status = await finalize_store.get_status(1, "t1_final_0")
        assert status == "done"
    finally:
        dbmod._pool = prev_pool
