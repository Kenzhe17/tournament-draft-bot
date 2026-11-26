"""Регрессия: banner_store._ensure должен быть безопасен при
параллельной инициализации схемы (pg_type_typname_nsp_index duplicate
key). Проверяется на временной pgserver-БД.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

import storage.db as dbmod
from storage.banner_store import BannerStore, banner_store


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_banner")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=4, max_size=10)
    prev_url = dbmod.DATABASE_URL
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    banner_store._tables_ready = False
    # Чистый инстанс: общий banner_store может быть «прогрет» другими
    # тестами — для проверки гонки нужен свежий флаг.
    store = BannerStore()
    store._tables_ready = False
    try:
        yield pool, store
    finally:
        dbmod._pool = None
        dbmod.DATABASE_URL = prev_url
        await pool.close()


@pytest.mark.asyncio
async def test_concurrent_ensure_no_ddl_race(pg_pool):
    _, store = pg_pool
    day = date(2026, 1, 1)

    async def worker(i):
        # Каждый воркер «забывает» флаг готовности, чтобы все прошли
        # через ветку DDL одновременно.
        store._tables_ready = False
        await store.add_voice_seconds(1, 100 + i, day, 10)
        return await store.get_voice_seconds(1, 100 + i, day)

    results = await asyncio.gather(*(worker(i) for i in range(12)))
    assert all(r == 10 for r in results)


@pytest.mark.asyncio
async def test_ensure_idempotent_repeated(pg_pool):
    _, store = pg_pool
    store._tables_ready = False
    for _ in range(3):
        await store.add_voice_seconds(2, 5, date(2026, 1, 2), 7)
    assert await store.get_voice_seconds(2, 5, date(2026, 1, 2)) == 21
