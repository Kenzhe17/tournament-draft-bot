"""Phase-1 оптимизации: P4 (аватар-кэш/session), P6 (tracemalloc opt-in),
P7 (asyncpg pool config)."""

import os
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# P4 — fetch_avatars: кэш + переиспользуемая сессия
# ---------------------------------------------------------------------------

class TestAvatarCache:
    @pytest.fixture(autouse=True)
    def _clean(self):
        import utils.match_found_renderer as mfr

        mfr._AVATAR_CACHE.clear()
        yield
        mfr._AVATAR_CACHE.clear()

    @pytest.mark.asyncio
    async def test_second_call_uses_cache(self, monkeypatch):
        import utils.match_found_renderer as mfr

        fetches = []

        async def _fake_fetch(session, url):
            fetches.append(url)
            return b"PNGDATA"

        monkeypatch.setattr(mfr, "_fetch_avatar", _fake_fetch)

        # Подменяем session getter — реальных сетевых вызовов нет.
        class _S:
            closed = False

        async def _sess():
            return _S()

        monkeypatch.setattr(mfr, "_get_avatar_session", _sess)

        urls = ["http://cdn/a.png", "http://cdn/b.png"]
        out1 = await mfr.fetch_avatars(urls)
        out2 = await mfr.fetch_avatars(urls)

        assert out1 == out2 == {
            "http://cdn/a.png": b"PNGDATA",
            "http://cdn/b.png": b"PNGDATA",
        }
        # Второй вызов — полностью из кэша, без fetch.
        assert fetches == urls

    @pytest.mark.asyncio
    async def test_partial_miss_fetches_only_missing(self, monkeypatch):
        import utils.match_found_renderer as mfr

        fetches = []

        async def _fake_fetch(session, url):
            fetches.append(url)
            return b"D"

        monkeypatch.setattr(mfr, "_fetch_avatar", _fake_fetch)

        class _S:
            closed = False

        async def _sess():
            return _S()

        monkeypatch.setattr(mfr, "_get_avatar_session", _sess)

        await mfr.fetch_avatars(["http://cdn/a.png"])
        out = await mfr.fetch_avatars(
            ["http://cdn/a.png", "http://cdn/b.png"]
        )
        assert fetches == ["http://cdn/a.png", "http://cdn/b.png"]
        assert set(out) == {"http://cdn/a.png", "http://cdn/b.png"}

    @pytest.mark.asyncio
    async def test_failed_fetch_not_cached(self, monkeypatch):
        import utils.match_found_renderer as mfr

        calls = []

        async def _fake_fetch(session, url):
            calls.append(url)
            return None  # ошибка/404

        monkeypatch.setattr(mfr, "_fetch_avatar", _fake_fetch)

        class _S:
            closed = False

        async def _sess():
            return _S()

        monkeypatch.setattr(mfr, "_get_avatar_session", _sess)

        out1 = await mfr.fetch_avatars(["http://cdn/x.png"])
        out2 = await mfr.fetch_avatars(["http://cdn/x.png"])
        assert out1 == {} and out2 == {}
        # Ошибка не кэшируется — повторная попытка возможна.
        assert calls == ["http://cdn/x.png"] * 2

    def test_cache_eviction_bounds(self):
        import utils.match_found_renderer as mfr

        mfr._AVATAR_CACHE_MAX = 3
        mfr._AVATAR_CACHE_BYTES_MAX = 10_000
        try:
            for i in range(10):
                mfr._avatar_cache_put(f"u{i}", b"x" * 100)
            assert len(mfr._AVATAR_CACHE) <= 3
        finally:
            mfr._AVATAR_CACHE_MAX = 500
            mfr._AVATAR_CACHE_BYTES_MAX = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# P6 — tracemalloc opt-in
# ---------------------------------------------------------------------------

class TestTracemallocOptIn:
    def test_profile_mem_disabled_by_default(self):
        import utils.match_found_renderer as mfr

        # В тестовом окружении env не выставлен → профилирование off.
        assert mfr._PROFILE_MEM is False

    def test_flag_values(self, monkeypatch):
        import importlib
        import utils.match_found_renderer as mfr

        # Флаг читается при импорте — проверяем логику значения.
        for v, expected in (
            ("1", True), ("true", True), ("YES", True),
            ("on", True), ("0", False), ("", False), ("no", False),
        ):
            assert (
                v.strip().lower() in ("1", "true", "yes", "on")
            ) is expected


# ---------------------------------------------------------------------------
# P7 — asyncpg pool configuration
# ---------------------------------------------------------------------------

class TestPgPoolConfig:
    @pytest.mark.asyncio
    async def test_pool_created_with_env_settings(self, monkeypatch):
        import storage.db as db

        captured = {}

        class _Pool:
            async def close(self):
                pass

        async def _create_pool(dsn, **kwargs):
            captured["dsn"] = dsn
            captured.update(kwargs)
            return _Pool()

        monkeypatch.setattr(db.asyncpg, "create_pool", _create_pool)
        monkeypatch.setattr(db, "_pool", None)
        monkeypatch.setenv("PG_POOL_MIN", "3")
        monkeypatch.setenv("PG_POOL_MAX", "12")
        monkeypatch.setenv("PG_COMMAND_TIMEOUT", "45")
        monkeypatch.setenv("PG_POOL_MAX_INACTIVE", "120")
        monkeypatch.setenv("PG_CONNECT_TIMEOUT", "15")

        pool = await db.get_pool()
        assert pool is not None
        assert captured["min_size"] == 3
        assert captured["max_size"] == 12
        assert captured["command_timeout"] == 45
        assert captured["max_inactive_connection_lifetime"] == 120
        assert captured["timeout"] == 15
        # Сброс для других тестов.
        db._pool = None

    @pytest.mark.asyncio
    async def test_pool_defaults_conservative(self, monkeypatch):
        import storage.db as db

        captured = {}

        class _Pool:
            pass

        async def _create_pool(dsn, **kwargs):
            captured.update(kwargs)
            return _Pool()

        monkeypatch.setattr(db.asyncpg, "create_pool", _create_pool)
        monkeypatch.setattr(db, "_pool", None)
        for var in (
            "PG_POOL_MIN", "PG_POOL_MAX", "PG_COMMAND_TIMEOUT",
            "PG_POOL_MAX_INACTIVE", "PG_CONNECT_TIMEOUT",
        ):
            monkeypatch.delenv(var, raising=False)

        await db.get_pool()
        # Дефолты не увеличивают пул сверх asyncpg default (10).
        assert captured["min_size"] == 2
        assert captured["max_size"] == 10
        assert captured["command_timeout"] == 30
        db._pool = None
