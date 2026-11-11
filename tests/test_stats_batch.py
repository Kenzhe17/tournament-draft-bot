"""P3: пакетное получение статистики (get_many) — N+1 → 1 запрос.

Проверяет:
- JSON-fallback путь get_many (дедупликация, фильтрация id);
- PG-путь: ровно ОДИН fetch на произвольное число user_ids;
- embed-билдеры используют get_many вместо N×get.
"""

from types import SimpleNamespace

import pytest


def _stats_store_json():
    from storage.player_stats_store import PlayerStatsStore
    from models.player_stats import PlayerStats

    store = PlayerStatsStore.__new__(PlayerStatsStore)
    store._use_db = False
    store._stats = {}
    for uid in (11, 12, 13):
        store._stats[f"1:{uid}"] = PlayerStats(
            guild_id=1, user_id=uid, name=f"P{uid}", elo=1000 + uid
        )
    return store


class TestGetManyJsonFallback:
    @pytest.mark.asyncio
    async def test_returns_map_for_known_users(self):
        store = _stats_store_json()
        out = await store.get_many(1, [11, 12, 99, "bad", 11])
        assert set(out) == {11, 12}
        assert out[11].elo == 1011
        assert out[12].elo == 1012

    @pytest.mark.asyncio
    async def test_empty_and_invalid_input(self):
        store = _stats_store_json()
        assert await store.get_many(1, []) == {}
        assert await store.get_many(1, [None, "x", -1]) == {}
        # Другой guild_id — пусто.
        assert await store.get_many(2, [11]) == {}


class _FakeConn:
    def __init__(self):
        self.fetch_calls = 0
        self.fetchrow_calls = 0

    async def fetch(self, query, *args):
        self.fetch_calls += 1
        return []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls += 1
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return self.conn


class TestGetManyPgQueryCount:
    @pytest.mark.asyncio
    async def test_single_fetch_for_many_users(self, monkeypatch):
        from storage.player_stats_store import PlayerStatsStore
        import storage.db as db

        store = PlayerStatsStore.__new__(PlayerStatsStore)
        store._use_db = True
        pool = _FakePool()
        monkeypatch.setattr(db, "get_pool", lambda: _async_ret(pool))

        uids = list(range(1, 33))  # 32 игрока
        out = await store.get_many(1, uids)
        assert out == {}
        assert pool.conn.fetch_calls == 1
        assert pool.conn.fetchrow_calls == 0

    @pytest.mark.asyncio
    async def test_get_still_single_row(self, monkeypatch):
        from storage.player_stats_store import PlayerStatsStore
        import storage.db as db

        store = PlayerStatsStore.__new__(PlayerStatsStore)
        store._use_db = True
        pool = _FakePool()
        monkeypatch.setattr(db, "get_pool", lambda: _async_ret(pool))
        await store.get(1, 5)
        assert pool.conn.fetchrow_calls == 1


async def _async_ret(value):
    return value


class TestEmbedBatchStats:
    @pytest.mark.asyncio
    async def test_setup_embed_uses_get_many_once(self, monkeypatch):
        """build_setup_embed: 1 вызов get_many на всех игроков,
        get() не вызывается вообще."""
        import utils.embeds as embeds
        from models.player_stats import PlayerStats

        calls = {"get": 0, "get_many": 0}

        class _Store:
            async def get(self, guild_id, user_id):
                calls["get"] += 1
                return None

            async def get_many(self, guild_id, user_ids):
                calls["get_many"] += 1
                return {
                    int(u): PlayerStats(
                        guild_id=guild_id, user_id=int(u),
                        name=f"P{u}", elo=1200,
                    )
                    for u in user_ids
                }

        monkeypatch.setattr(embeds, "player_stats_store", _Store())

        t = SimpleNamespace(
            guild_id=1, channel_id=2, organizer_id=7,
            formation_mode=SimpleNamespace(value="RANDOM"),
            registration=SimpleNamespace(
                value="OPEN", __eq__=lambda s, o: False
            ),
            players_pool=["A", "B", "C", "D"],
            player_user_ids={"A": 11, "B": 12, "C": 13, "D": 14},
            teams=[
                {"captain": "A", "players": ["A", "B", "C", "D"]},
                {"captain": "B", "players": ["B", "A", "C", "D"]},
            ],
            size=SimpleNamespace(value="8"),
            circle1=[], circle2=[], circle3=[], circle4=[],
            circle_limits_enabled={},
            get_circle_counts=lambda: {},
        )
        # RANDOM-режим: пул игроков, без кругов.
        from models.tournament import FormationMode, RegistrationState
        t.formation_mode = FormationMode.RANDOM
        t.registration = RegistrationState.OPEN

        guild = SimpleNamespace(id=1)
        embed = await embeds.build_setup_embed(t, guild)
        assert calls["get_many"] == 1
        assert calls["get"] == 0
        # ELO подставлены из пакетной выборки.
        assert "1200 ELO" in (embed.description or "")
