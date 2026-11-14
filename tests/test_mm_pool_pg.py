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


# ---------------------------------------------------------------
# Идемпотентные монеты и конкурентная финализация (real PG)
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_coin_grants_idempotent(pg_pool, monkeypatch):
    """add_balance_once: повтор с тем же match_id не начисляет
    монеты дважды; другой match_id — начисляет; частичный сбой
    между игроками безопасен (грант атомарен с балансом)."""
    import storage.db as dbmod
    from storage.user_balance_store import user_balance_store

    prev = dbmod._pool
    dbmod._pool = pg_pool
    try:
        await dbmod.init_db()
        gid, uid = 880001, 424242

        credited1 = await user_balance_store.add_balance_once(
            gid, uid, 75, "m_idem_1"
        )
        bal1 = await user_balance_store.get_balance(gid, uid)
        credited2 = await user_balance_store.add_balance_once(
            gid, uid, 75, "m_idem_1"
        )
        bal2 = await user_balance_store.get_balance(gid, uid)

        assert credited1 == 75 and credited2 == 75  # сумма для stats
        assert bal2 == bal1  # баланс НЕ изменился при повторе

        # Частичный сбой: «другие игроки» уже получили гранты,
        # повторная финализация возвращает те же суммы без дублей.
        for other in (424243, 424244):
            await user_balance_store.add_balance_once(
                gid, other, 60, "m_idem_1"
            )
        again = await user_balance_store.add_balance_once(
            gid, 424243, 60, "m_idem_1"
        )
        assert again == 60
        bal_other = await user_balance_store.get_balance(gid, 424243)
        assert bal_other == 160  # 100 старт + 60 ровно один раз

        # Другой матч — новое начисление
        await user_balance_store.add_balance_once(gid, uid, 75, "m_idem_2")
        bal3 = await user_balance_store.get_balance(gid, uid)
        assert bal3 == bal1 + 75
    finally:
        dbmod._pool = prev


@pytest.mark.asyncio
async def test_two_concurrent_finalizes_real_pg(pg_pool, monkeypatch):
    """Два одновременных подтверждения на РЕАЛЬНОМ PostgreSQL:
    ровно один успех, ELO/монеты/история применены один раз,
    match_finalizations = 'done'."""
    import storage.db as dbmod
    import views.mm_match_view as mmv
    from models.tournament import FormationMode, Tournament
    from types import SimpleNamespace

    prev = dbmod._pool
    dbmod._pool = pg_pool
    gid = 880002
    try:
        await dbmod.init_db()

        async def _noop(*a, **kw):
            return None

        monkeypatch.setattr(mmv, "_refresh_card", _noop)
        monkeypatch.setattr(mmv, "_release_match_channel", _noop)

        t = Tournament(
            guild_id=gid, channel_id=555,
            formation_mode=FormationMode.RANDOM_AUTO,
        )
        t.teams = [
            {"players": [f"P{i}" for i in range(1, 5)], "captain": "P1"},
            {"players": [f"P{i}" for i in range(5, 9)], "captain": "P5"},
        ]
        t.player_user_ids = {f"P{i}": 880000 + i for i in range(1, 9)}
        t.team_names = {0: "TeamA", 1: "TeamB"}
        t.final_teams = [0, 1]
        mid = mmv.match_id_of(t)
        t.temp_match_stats[mid] = {
            f"P{i}": {"kills": 5, "deaths": 2} for i in range(1, 9)
        }
        t.match_channels = {
            "555": {
                "state": "check", "match_num": 1,
                "roster": [f"P{i}" for i in range(1, 9)],
                "card_message_id": 0,
            }
        }

        guild = SimpleNamespace(
            id=gid,
            get_channel=lambda cid: None,
            fetch_channel=lambda cid: None,
        )
        ix = SimpleNamespace(guild=guild, user=SimpleNamespace(id=1))

        results = await asyncio.gather(
            mmv.finalize_mm_match(ix, t, "a", (5, 3)),
            mmv.finalize_mm_match(ix, t, "a", (5, 3)),
        )
        oks = [r[0] for r in results]
        assert oks.count(True) == 1
        assert mmv.meta_of(t)["state"] == "done"
        assert mmv.meta_of(t)["winner"] == "a"

        async with pg_pool.acquire() as conn:
            stats_rows = await conn.fetch(
                "SELECT games, elo FROM player_stats WHERE guild_id = $1",
                gid,
            )
            assert len(stats_rows) == 8
            for r in stats_rows:
                assert r["games"] == 1  # один матч — не два

            grants = await conn.fetchval(
                "SELECT COUNT(*) FROM match_coin_grants WHERE match_id = $1",
                mid,
            )
            assert grants == 8  # по одному гранту на игрока

            # Сумма балансов выросла ровно на выданные гранты
            grant_sum = await conn.fetchval(
                "SELECT COALESCE(SUM(amount),0) FROM match_coin_grants "
                "WHERE match_id = $1",
                mid,
            )
            bal_sum = await conn.fetchval(
                "SELECT COALESCE(SUM(balance),0) FROM user_balance "
                "WHERE guild_id = $1",
                gid,
            )
            # 8 игроков × 100 стартовых + grant_sum
            assert bal_sum == 800 + grant_sum

            hist = await conn.fetchval(
                "SELECT COUNT(*) FROM match_history "
                "WHERE guild_id = $1 AND match_id = $2",
                gid, mid,
            )
            assert hist == 8

            fin = await conn.fetchval(
                "SELECT status FROM match_finalizations WHERE match_id = $1",
                mid,
            )
            assert fin == "done"
    finally:
        dbmod._pool = prev
