"""Тесты движка динамической конфигурации Control Center.

PostgreSQL-слой подменяется in-memory fake с той же семантикой
(values/history/versioning/VersionConflictError). Реальная схема
проверяется отдельным PG-тестом (TEST_DATABASE_URL).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import settings_registry as registry
from services.settings_service import (
    SettingsService,
    SettingsUnavailableError,
)
from storage import admin_config_store as store

# ------------------------------------------------------------ fake store

class FakeBackend:
    """In-memory реализация контракта admin_config_store."""

    def __init__(self) -> None:
        self.values: dict[tuple[int, str], dict] = {}
        self.history: list[dict] = []

    async def init_schema(self) -> None:
        pass

    async def get_all_values(self):
        return [
            {
                "guild_id": gid, "key": key,
                "value": v["value"], "version": v["version"],
                "updated_by": 0, "updated_at": None,
            }
            for (gid, key), v in self.values.items()
        ]

    async def get_value(self, guild_id: int, key: str):
        v = self.values.get((guild_id, key))
        if v is None:
            return None
        return {
            "guild_id": guild_id, "key": key,
            "value": v["value"], "version": v["version"],
            "updated_by": 0, "updated_at": None,
        }

    async def set_value(self, guild_id, key, value, *, actor_id=0,
                        expected_version=None, correlation_id=""):
        k = (guild_id, key)
        cur = self.values.get(k)
        cur_v = cur["version"] if cur else None
        if expected_version is not None and cur_v != expected_version:
            raise store.VersionConflictError(cur_v)
        new_v = (cur_v or 0) + 1
        old = cur["value"] if cur else None
        self.values[k] = {"value": value, "version": new_v}
        self.history.append({
            "guild_id": guild_id, "key": key, "version": new_v,
            "old_value": old, "new_value": value,
            "changed_by": actor_id,
        })
        return {
            "guild_id": guild_id, "key": key, "value": value,
            "version": new_v, "updated_by": actor_id,
            "updated_at": None,
        }

    async def get_history(self, guild_id, key, *, limit=50,
                          before_version=None):
        rows = [
            h for h in self.history
            if h["guild_id"] == guild_id and h["key"] == key
            and (before_version is None or h["version"] < before_version)
        ]
        rows.sort(key=lambda h: h["version"], reverse=True)
        return rows[:limit]

    async def rollback_to_version(self, guild_id, key, target_version, *,
                                  actor_id=0, correlation_id=""):
        for h in self.history:
            if (h["guild_id"] == guild_id and h["key"] == key
                    and h["version"] == target_version):
                return await self.set_value(
                    guild_id, key, h["new_value"], actor_id=actor_id,
                    correlation_id=correlation_id,
                )
        raise ValueError("history version not found")


@pytest.fixture
def backend(monkeypatch):
    fb = FakeBackend()
    monkeypatch.setattr(store, "init_schema", fb.init_schema)
    monkeypatch.setattr(store, "get_all_values", fb.get_all_values)
    monkeypatch.setattr(store, "get_value", fb.get_value)
    monkeypatch.setattr(store, "set_value", fb.set_value)
    monkeypatch.setattr(store, "get_history", fb.get_history)
    monkeypatch.setattr(
        store, "rollback_to_version", fb.rollback_to_version)
    return fb


@pytest.fixture
def service(backend):
    return SettingsService(poll_interval=3600)


# ------------------------------------------------------------ registry

def test_registry_defaults_present():
    d = registry.get("feature.tournaments_enabled")
    assert d is not None and d.default is True
    d = registry.get("elo.max_match_elo")
    assert d.default == 30
    # Реальные env-driven дефолты совпадают с config.py
    import config
    assert registry.get("economy.max_balance").default == config.MAX_BALANCE
    assert registry.get("economy.min_bet").default == config.MIN_BET
    assert registry.get("economy.max_bet").default == config.MAX_BET


def test_coerce_types():
    d = registry.get("elo.kill_weight")
    assert registry.coerce(d, "0.5") == 0.5
    with pytest.raises(registry.SettingValidationError):
        registry.coerce(d, "abc")

    d = registry.get("feature.welcome_enabled")
    assert registry.coerce(d, "false") is False
    assert registry.coerce(d, 1) is True
    with pytest.raises(registry.SettingValidationError):
        registry.coerce(d, "maybe")

    d = registry.get("elo.min_match_elo")
    with pytest.raises(registry.SettingValidationError):
        registry.coerce(d, -1)  # min=0
    with pytest.raises(registry.SettingValidationError):
        registry.coerce(d, 5000)  # max=1000


def test_no_secrets_in_registry():
    forbidden = ("token", "secret", "password", "database_url",
                 "webhook", "api_key")
    for d in registry.all_defs():
        assert not any(f in d.key for f in forbidden), d.key


# ------------------------------------------------------------ service

@pytest.mark.asyncio
async def test_get_returns_default_without_db_value(service):
    assert await service.get(None, "elo.kill_weight") == 1.0
    assert service.peek(None, "elo.kill_weight") == 1.0


@pytest.mark.asyncio
async def test_set_get_roundtrip_and_version(service, backend):
    await service.init()
    r = await service.set(None, "elo.kill_weight", 1.5,
                          actor_id=42)
    assert r["version"] == 1
    assert await service.get(None, "elo.kill_weight") == 1.5
    assert service.peek(None, "elo.kill_weight") == 1.5

    r2 = await service.set(None, "elo.kill_weight", 2.0,
                           actor_id=42, expected_version=1)
    assert r2["version"] == 2
    assert len(backend.history) == 2
    assert backend.history[0]["old_value"] is None
    assert backend.history[1]["old_value"] == 1.5


@pytest.mark.asyncio
async def test_optimistic_concurrency(service):
    await service.init()
    await service.set(None, "elo.kill_weight", 1.5)
    with pytest.raises(store.VersionConflictError):
        await service.set(None, "elo.kill_weight", 9.9,
                          expected_version=99)
    # Значение не перезаписано
    assert await service.get(None, "elo.kill_weight") == 1.5


@pytest.mark.asyncio
async def test_invalid_value_rejected(service, backend):
    await service.init()
    with pytest.raises(registry.SettingValidationError):
        await service.set(None, "elo.kill_weight", "not-a-number")
    with pytest.raises(registry.SettingValidationError):
        await service.set(None, "elo.kill_weight", 999.0)  # max=100
    assert backend.values == {}


@pytest.mark.asyncio
async def test_unknown_key_rejected(service):
    await service.init()
    with pytest.raises(KeyError):
        await service.set(None, "no.such.key", 1)
    with pytest.raises(KeyError):
        await service.get(None, "no.such.key")


@pytest.mark.asyncio
async def test_degraded_mode_without_db():
    svc = SettingsService()
    svc.available = False
    # get → default, set → ошибка
    assert await svc.get(None, "feature.tournaments_enabled") is True
    with pytest.raises(SettingsUnavailableError):
        await svc.set(None, "feature.tournaments_enabled", False)


@pytest.mark.asyncio
async def test_guild_scope_resolution(service, backend):
    # Все текущие ключи scope=global: guild_id игнорируется,
    # пишется в guild_id=0.
    await service.init()
    await service.set(12345, "elo.kill_weight", 1.25)
    assert (0, "elo.kill_weight") in backend.values
    assert await service.get(99999, "elo.kill_weight") == 1.25


@pytest.mark.asyncio
async def test_rollback(service, backend):
    await service.init()
    await service.set(None, "elo.kill_weight", 1.5, actor_id=1)
    await service.set(None, "elo.kill_weight", 2.0, actor_id=1)
    await service.set(None, "elo.kill_weight", 3.0, actor_id=1)
    r = await service.rollback(None, "elo.kill_weight", 1,
                               actor_id=2)
    assert r["value"] == 1.5
    assert r["version"] == 4
    assert await service.get(None, "elo.kill_weight") == 1.5
    # Откат сам записан в history
    assert backend.history[-1]["new_value"] == 1.5


@pytest.mark.asyncio
async def test_refresh_picks_external_change(service, backend):
    """Изменение, записанное мимо сервиса (другой процесс/панель),
    подхватывается через refresh_all/refresh_key."""
    await service.init()
    assert await service.get(None, "elo.kill_weight") == 1.0
    # «Чужая» запись напрямую в store
    await backend.set_value(0, "elo.kill_weight", 2.5, actor_id=7)
    # Кэш ещё старый
    assert service.peek(None, "elo.kill_weight") == 1.0
    await service.refresh_all()
    assert await service.get(None, "elo.kill_weight") == 2.5


@pytest.mark.asyncio
async def test_feature_flag_toggle_live(service):
    """Доказательство runtime-изменения: feature flag применяется
    без рестарта (consumer читает через peek)."""
    await service.init()
    assert service.peek(None, "feature.coin_drop_enabled") is True
    await service.set(None, "feature.coin_drop_enabled", False)
    assert service.peek(None, "feature.coin_drop_enabled") is False
    await service.set(None, "feature.coin_drop_enabled", True)
    assert service.peek(None, "feature.coin_drop_enabled") is True


@pytest.mark.asyncio
async def test_history_listing(service):
    await service.init()
    await service.set(None, "elo.kill_weight", 1.5)
    await service.set(None, "elo.kill_weight", 2.0)
    hist = await service.history(None, "elo.kill_weight")
    assert [h["version"] for h in hist] == [2, 1]
    hist2 = await service.history(None, "elo.kill_weight",
                                  before_version=2)
    assert [h["version"] for h in hist2] == [1]
