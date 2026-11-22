"""PG-интеграция: очередь восстановления tournament_journal.

Реальная временная PostgreSQL (pgserver) — проверяет семантику
list_pending/set_retry/set_processing/update_ids: gating
retry_wait по next_attempt_at, исключение error/synced,
обработка зависшего processing, сброс retry_count при успехе.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

import storage.db as dbmod
from storage import tournament_journal_store as store


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_journal")
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
        await conn.execute("DELETE FROM tournament_journal")
    try:
        yield pool
    finally:
        dbmod._pool = prev_pool
        dbmod.DATABASE_URL = prev_url
        store._table_ready = False
        await pool.close()


async def _main(tid: str, **kw):
    entry = await store.upsert_main(
        guild_id=1, channel_id=100, tournament_id=tid)
    if kw:
        await store.update_ids(int(entry["id"]), **kw)
    return entry


@pytest.mark.asyncio
async def test_list_pending_statuses(pg_pool):
    """pending/partial — всегда; synced/error — никогда;
    retry_wait — только по наступлении next_attempt_at;
    processing — только устаревший."""
    await _main("t_pending")
    await _main("t_partial", sync_status="partial")
    await _main("t_synced", sync_status="synced",
                audit_message_id=1, thread_id=2,
                summary_message_id=3)
    await _main("t_error", sync_status="error")
    await _main("t_retry_future", sync_status="retry_wait")
    await store.set_retry(
        int((await store.get_main("t_retry_future"))["id"]),
        delay_seconds=3600, last_error="429")
    await _main("t_retry_past", sync_status="retry_wait")
    await store.set_retry(
        int((await store.get_main("t_retry_past"))["id"]),
        delay_seconds=-10, last_error="429")

    tids = {e["tournament_id"]
            for e in await store.list_pending(limit=100)}
    assert "t_pending" in tids
    assert "t_partial" in tids
    assert "t_synced" not in tids
    assert "t_error" not in tids
    assert "t_retry_future" not in tids   # ещё не время
    assert "t_retry_past" in tids         # next_attempt_at <= now


@pytest.mark.asyncio
async def test_set_retry_increments_and_caps(pg_pool):
    entry = await _main("t_retry")
    eid = int(entry["id"])
    for _ in range(3):
        await store.set_retry(eid, delay_seconds=60,
                              last_error="boom")
    row = await store.get_main("t_retry")
    assert row["sync_status"] == "retry_wait"
    assert row["retry_count"] == 3
    assert row["next_attempt_at"] is not None
    # После 10 попыток list_pending запись больше не подбирает.
    for _ in range(8):
        await store.set_retry(eid, delay_seconds=-1)
    row = await store.get_main("t_retry")
    assert row["retry_count"] == 11
    tids = {e["tournament_id"]
            for e in await store.list_pending(limit=100)}
    assert "t_retry" not in tids


@pytest.mark.asyncio
async def test_processing_stale_is_pending_fresh_is_not(pg_pool):
    fresh = await _main("t_proc_fresh")
    await store.set_processing(int(fresh["id"]))
    stale = await _main("t_proc_stale")
    await store.set_processing(int(stale["id"]))
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE tournament_journal SET "
            "updated_at = now() - interval '1 hour' "
            "WHERE tournament_id = 't_proc_stale'")
    tids = {e["tournament_id"]
            for e in await store.list_pending(limit=100)}
    assert "t_proc_fresh" not in tids   # свежий processing — жив
    assert "t_proc_stale" in tids       # зависший — на повтор


@pytest.mark.asyncio
async def test_success_resets_retry_state(pg_pool):
    entry = await _main("t_ok")
    eid = int(entry["id"])
    await store.set_retry(eid, delay_seconds=60, last_error="x")
    await store.update_ids(
        eid, sync_status="synced", audit_message_id=55,
        thread_id=56, summary_message_id=57)
    row = await store.get_main("t_ok")
    assert row["sync_status"] == "synced"
    assert row["retry_count"] == 0
    assert row["next_attempt_at"] is None
    tids = {e["tournament_id"]
            for e in await store.list_pending(limit=100)}
    assert "t_ok" not in tids


@pytest.mark.asyncio
async def test_missing_ids_still_pending(pg_pool):
    """Запись без thread_id/summary_message_id подбирается
    даже при sync_status='synced' — защита от частичной
    публикации."""
    entry = await _main("t_partial_ids", sync_status="synced",
                        audit_message_id=10)
    tids = {e["tournament_id"]
            for e in await store.list_pending(limit=100)}
    assert "t_partial_ids" in tids
    await store.update_ids(
        int(entry["id"]), thread_id=20, summary_message_id=30)
    tids = {e["tournament_id"]
            for e in await store.list_pending(limit=100)}
    assert "t_partial_ids" not in tids
