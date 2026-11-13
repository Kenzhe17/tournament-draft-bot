"""PG-тесты жизненного цикла Ultimatum Solo: атомарный идемпотентный старт.

pgserver поднимает изолированный PostgreSQL; схема применяется из
ultimatum/__init__.py. Проверяем:
- переход REGISTRATION -> IN_PROGRESS -> ROUND_IN_PROGRESS;
- ровно 8 матчей 1-го раунда, без дублей при повторных стартах;
- конкурентные «Старт» (FOR UPDATE);
- возобновление после сбоя между созданием матчей и публикацией;
- идемпотентность complete_match (без повторного ELO);
- миграцию VAC на старой схеме vac_bans;
- fail-closed check_vac при недоступности хранилища.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent))

import pgserver

from ultimatum import ELO_SCHEMA, ULTIMATUM_SCHEMA
from ultimatum.models import TournamentStatus
from ultimatum.store import ultimatum_store
from ultimatum.tournament import UltimatumTournament

GUILD = 777001


def _logic() -> UltimatumTournament:
    return UltimatumTournament(ultimatum_store)


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_ultstart")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    import storage.db as dbmod

    pool = await asyncpg.create_pool(pg_uri, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(ULTIMATUM_SCHEMA)
        await conn.execute(ELO_SCHEMA)

    prev_url = dbmod.DATABASE_URL
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    try:
        yield pool
    finally:
        dbmod._pool = None
        dbmod.DATABASE_URL = prev_url
        await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def clean(pg_pool):
    async with pg_pool.acquire() as conn:
        for t in (
            "ultimatum_matches", "ultimatum_rounds", "ultimatum_players",
            "ultimatum_match_history", "ultimatum_audit_log", "ultimatum_votes",
            "ultimatum_tournaments", "elo_history", "elo_ratings",
        ):
            await conn.execute(f"DELETE FROM {t}")
    yield


async def _mk_tournament(players: int = 16) -> str:
    t = await ultimatum_store.create_tournament("Ult #1", GUILD, 999)
    await ultimatum_store.update_tournament_status(t.id, TournamentStatus.REGISTRATION)
    for i in range(players):
        await ultimatum_store.add_player(t.id, 1000 + i, f"P{i}")
    return t.id


async def _match_count(tid: str, rnd: int | None = None) -> int:
    if rnd is None:
        return await ultimatum_store.round_match_count(tid, 1)
    return await ultimatum_store.round_match_count(tid, rnd)


# --------------------------------------------------------------- lifecycle

@pytest.mark.asyncio
async def test_create_register_start_round1(pg_pool):
    tid = await _mk_tournament(16)
    logic = _logic()

    await logic.start_tournament(tid, bot=None)

    t = await ultimatum_store.get_tournament(tid)
    assert t.status == TournamentStatus.ROUND_IN_PROGRESS
    assert t.current_round == 1
    assert t.started_at is not None
    assert await _match_count(tid, 1) == 8

    # Раунд в БД — IN_PROGRESS
    async with pg_pool.acquire() as conn:
        st = await conn.fetchval(
            "SELECT status FROM ultimatum_rounds WHERE tournament_id=$1 AND round_number=1",
            tid,
        )
    assert st == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_concurrent_start_single_transition(pg_pool):
    tid = await _mk_tournament(16)

    results = await asyncio.gather(
        ultimatum_store.begin_tournament(tid),
        ultimatum_store.begin_tournament(tid),
    )
    assert sorted(results) == ["resumed", "started"]

    # Параллельное создание раунда — без дублей матчей.
    logic = _logic()
    await asyncio.gather(
        logic.create_round(tid, 1, bot=None),
        logic.create_round(tid, 1, bot=None),
    )
    assert await _match_count(tid, 1) == 8


@pytest.mark.asyncio
async def test_repeat_start_no_duplicates(pg_pool):
    tid = await _mk_tournament(16)
    logic = _logic()

    await logic.start_tournament(tid, bot=None)
    await logic.start_tournament(tid, bot=None)
    await logic.start_tournament(tid, bot=None)

    assert await _match_count(tid, 1) == 8
    t = await ultimatum_store.get_tournament(tid)
    assert t.status == TournamentStatus.ROUND_IN_PROGRESS
    assert t.current_round == 1


@pytest.mark.asyncio
async def test_start_requires_exactly_16(pg_pool):
    tid = await _mk_tournament(15)
    with pytest.raises(ValueError, match="16 players"):
        await ultimatum_store.begin_tournament(tid)
    # Статус не изменился
    t = await ultimatum_store.get_tournament(tid)
    assert t.status == TournamentStatus.REGISTRATION


@pytest.mark.asyncio
async def test_resume_after_crash_before_matches(pg_pool):
    """begin зафиксирован, процесс упал до создания матчей —
    повторный старт доводит создание до конца."""
    tid = await _mk_tournament(16)
    assert await ultimatum_store.begin_tournament(tid) == "started"
    assert await _match_count(tid, 1) == 0

    await _logic().start_tournament(tid, bot=None)
    assert await _match_count(tid, 1) == 8
    t = await ultimatum_store.get_tournament(tid)
    assert t.status == TournamentStatus.ROUND_IN_PROGRESS


@pytest.mark.asyncio
async def test_begin_invalid_status(pg_pool):
    tid = await _mk_tournament(16)
    await ultimatum_store.update_tournament_status(tid, TournamentStatus.COMPLETED)
    assert await ultimatum_store.begin_tournament(tid) == "invalid"


@pytest.mark.asyncio
async def test_completed_tournament_stays(pg_pool):
    """Завершённый турнир не возвращается к регистрации."""
    tid = await _mk_tournament(16)
    await _logic().start_tournament(tid, bot=None)
    await ultimatum_store.update_tournament_status(tid, TournamentStatus.COMPLETED)
    assert await ultimatum_store.begin_tournament(tid) == "invalid"
    t = await ultimatum_store.get_tournament(tid)
    assert t.status == TournamentStatus.COMPLETED
    assert await _match_count(tid, 1) == 8


# ------------------------------------------------------------- messages ids

@pytest.mark.asyncio
async def test_message_ids_persist(pg_pool):
    tid = await _mk_tournament(4)
    await ultimatum_store.set_setup_message(tid, 111, 222)
    t = await ultimatum_store.get_tournament(tid)
    assert t.setup_channel_id == 111 and t.setup_message_id == 222

    await ultimatum_store.set_round_message(tid, 333, 444)
    t = await ultimatum_store.get_tournament(tid)
    assert t.round_channel_id == 333 and t.round_message_id == 444

    await ultimatum_store.clear_setup_message(tid)
    t = await ultimatum_store.get_tournament(tid)
    assert t.setup_message_id is None
    assert t.round_message_id == 444


@pytest.mark.asyncio
async def test_unfinished_listing(pg_pool):
    tid_reg = await _mk_tournament(2)
    tid_done = await _mk_tournament(2)
    await ultimatum_store.update_tournament_status(tid_done, TournamentStatus.COMPLETED)

    ids = {t.id for t in await ultimatum_store.list_unfinished_tournaments()}
    assert tid_reg in ids and tid_done not in ids


# -------------------------------------------------------------------- ELO

@pytest.mark.asyncio
async def test_complete_match_twice_no_double_elo(pg_pool):
    tid = await _mk_tournament(16)
    logic = _logic()
    await logic.start_tournament(tid, bot=None)

    matches = await ultimatum_store.get_matches(tid, 1)
    m = matches[0]
    w, l = m.player1_id, m.player2_id

    await logic.complete_match(m.id, w, l, bot=None)

    async with pg_pool.acquire() as conn:
        hist_after_first = await conn.fetchval(
            "SELECT COUNT(*) FROM elo_history WHERE match_id = $1", m.id
        )
        wins_w = await conn.fetchval(
            "SELECT wins FROM ultimatum_players WHERE tournament_id=$1 AND discord_id=$2",
            tid, w,
        )

    # Повторное завершение того же матча — без повторных начислений.
    await logic.complete_match(m.id, w, l, bot=None)

    async with pg_pool.acquire() as conn:
        hist_after_second = await conn.fetchval(
            "SELECT COUNT(*) FROM elo_history WHERE match_id = $1", m.id
        )
        wins_w2 = await conn.fetchval(
            "SELECT wins FROM ultimatum_players WHERE tournament_id=$1 AND discord_id=$2",
            tid, w,
        )
        mstatus = await conn.fetchval(
            "SELECT status FROM ultimatum_matches WHERE id=$1", m.id
        )

    assert hist_after_first == hist_after_second == 2  # по строке на игрока
    assert wins_w == wins_w2 == 1
    assert mstatus == "COMPLETED"


# ------------------------------------------------------------- VAC миграция

_OLD_VAC_DDL = """
CREATE TABLE IF NOT EXISTS vac_bans (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    issued_by BIGINT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoked_by BIGINT,
    expired_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT vac_expiry_check CHECK (expires_at > issued_at)
);
"""


@pytest.mark.asyncio
async def test_vac_migration_old_schema(pg_pool):
    """Старая vac_bans без level/reason/event_key — миграция добавляет
    колонки до индексов, история сохраняется."""
    from services import vac_service
    from storage.vac_store import VacStore, vac_store

    async with pg_pool.acquire() as conn:
        await conn.execute(_OLD_VAC_DDL)
        # Старая активная блокировка — должна сохраниться.
        await conn.execute(
            """
            INSERT INTO vac_bans (guild_id, user_id, issued_by, expires_at)
            VALUES ($1, $2, $3, $4)
            """,
            GUILD, 555, 1,
            datetime.now(UTC) + timedelta(hours=1),
        )

    VacStore._ready = False  # сброс process-флага для чистой миграции
    await vac_store.ensure_schema()

    async with pg_pool.acquire() as conn:
        cols = {
            r["column_name"]
            for r in await conn.fetch(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'vac_bans'
                """
            )
        }
    assert {"level", "reason", "event_key"} <= cols

    # Старая запись читается, VAC активен.
    vac = await vac_service.check_vac(GUILD, 555)
    assert vac is not None
    assert vac["reason"] is None


@pytest.mark.asyncio
async def test_vac_fail_closed_on_store_error(pg_pool, monkeypatch):
    """Недоступность VAC-хранилища — VacStoreUnavailable, а не «не заблокирован»."""
    from services import vac_service

    async def _boom(*a, **k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(vac_service.vac_store, "get_active", _boom)
    with pytest.raises(vac_service.VacStoreUnavailable):
        await vac_service.check_vac(GUILD, 1)
    with pytest.raises(vac_service.VacStoreUnavailable):
        await vac_service.vac_active_ids(GUILD, [1, 2])
