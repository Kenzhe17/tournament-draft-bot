"""PG-интеграция: organizer.* исключены из выборок журнала аудита.

Реальная временная PostgreSQL (pgserver) — проверяет SQL-фильтрацию
list_events/count_events/unpublished_events, включая граничные
случаи source=NULL и action='organizer.*' под другим source.
Сами записи в tournament_audit_log не удаляются.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

import storage.db as dbmod
from storage import tournament_audit_store as store


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_audit")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=1, max_size=4)
    prev_url = dbmod.DATABASE_URL
    prev_pool = dbmod._pool
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    store._table_ready = False
    async with pool.acquire() as conn:
        await store.ensure_table(conn)
        await conn.execute("DELETE FROM tournament_audit_log")
    try:
        yield pool
    finally:
        dbmod._pool = prev_pool
        dbmod.DATABASE_URL = prev_url
        store._table_ready = False
        await pool.close()


async def _add(**kw):
    base = dict(
        event_id=__import__("uuid").uuid4().hex,
        guild_id=1, channel_id=100, tournament_id="1_1",
        action="player.join", source="tournament",
    )
    base.update(kw)
    return await store.add_event(**base)


@pytest.mark.asyncio
async def test_list_events_excludes_organizer_before_pagination(pg_pool):
    # 3 organizer + 3 обычных, вставляем вперемешку.
    await _add(action="organizer.assign", source="organizer")
    await _add(action="player.join")
    await _add(action="organizer.role_grant", source="organizer")
    await _add(action="match.start")
    await _add(action="organizer.reset", source="organizer")
    await _add(action="room.submit", source="room")
    # organizer.* action под ДРУГИМ source — тоже скрывается.
    await _add(action="organizer.transfer", source="system")

    all_ev = await store.list_events(guild_id=1, channel_id=100,
                                     tournament_id="1_1", limit=50)
    vis = await store.list_events(guild_id=1, channel_id=100,
                                  tournament_id="1_1", limit=50,
                                  exclude_organizer=True)
    assert len(all_ev) == 7
    assert len(vis) == 3
    assert {e["action"] for e in vis} == {
        "player.join", "match.start", "room.submit"}

    # Пагинация: limit=2 → вторая страница добирает остаток,
    # organizer в выборке отсутствуют.
    p1 = await store.list_events(guild_id=1, channel_id=100,
                                 tournament_id="1_1", limit=2,
                                 offset=0, exclude_organizer=True)
    p2 = await store.list_events(guild_id=1, channel_id=100,
                                 tournament_id="1_1", limit=2,
                                 offset=2, exclude_organizer=True)
    assert len(p1) == 2 and len(p2) == 1
    assert all("organizer" not in e["action"] for e in p1 + p2)


@pytest.mark.asyncio
async def test_count_events_exact_visible(pg_pool):
    await _add(action="organizer.assign", source="organizer")
    await _add(action="player.join")
    await _add(action="organizer.role_grant", source="organizer")
    await _add(action="player.exit")

    assert await store.count_events(
        guild_id=1, channel_id=100, tournament_id="1_1") == 4
    assert await store.count_events(
        guild_id=1, channel_id=100, tournament_id="1_1",
        exclude_organizer=True) == 2


@pytest.mark.asyncio
async def test_unpublished_events_skips_organizer(pg_pool):
    await _add(action="organizer.assign", source="organizer",
               tournament_id=None)
    await _add(action="organizer.role_grant", source="system",
               tournament_id=None)
    await _add(action="mm.result_confirm", source="mm",
               tournament_id=None)
    await _add(action="player.join", source="tournament",
               tournament_id="1_1")  # tournament_id → журнал, не карточка

    events = await store.unpublished_events(limit=50)
    actions = {e["action"] for e in events}
    assert "mm.result_confirm" in actions
    assert not any(a.startswith("organizer.") for a in actions)
    assert "player.join" not in actions  # tournament_id ≠ NULL → исключён


@pytest.mark.asyncio
async def test_list_events_limit_applies_after_organizer_filter(pg_pool):
    """Регрессия P1: LIMIT должен применяться ПОСЛЕ исключения
    organizer.* — иначе журнал теряет видимые события.

    Среди последних 5 записей — 4 organizer.*; ниже лежат 6 обычных.
    limit=5 с exclude_organizer=True обязан вернуть 5 обычных событий
    (без SQL-фильтра вернулось бы только 1).
    """
    for i in range(6):
        await _add(action=f"match.result_{i}")
    # Последние 5 записей: 4 organizer.* + 1 обычное.
    await _add(action="organizer.assign", source="organizer")
    await _add(action="match.result_6")
    await _add(action="organizer.role_grant", source="organizer")
    await _add(action="organizer.role_revoke", source="organizer")
    await _add(action="organizer.reset", source="organizer")

    rows = await store.list_events(guild_id=1, channel_id=100,
                                   tournament_id="1_1", limit=5,
                                   exclude_organizer=True)
    assert len(rows) == 5
    assert all(not e["action"].startswith("organizer.")
               and e["source"] != "organizer" for e in rows)
    # Самое свежее обычное событие — match.result_6.
    assert rows[0]["action"] == "match.result_6"


@pytest.mark.asyncio
async def test_organizer_events_still_written(pg_pool):
    """PG-запись organizer.* не меняется — аудит для расследований."""
    ok = await _add(action="organizer.assign", source="organizer",
                    target_user_id=55)
    assert ok is True
    rows = await store.list_events(guild_id=1, channel_id=100,
                                   tournament_id="1_1", limit=10)
    assert any(e["action"] == "organizer.assign"
               and e["target_user_id"] == 55 for e in rows)
