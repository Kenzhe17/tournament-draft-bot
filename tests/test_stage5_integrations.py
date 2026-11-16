"""Stage 5 integration tests: runtime settings reach real consumers.

Verifies that module code reads dynamic config via settings_service.peek
with correct fallback when the service is unavailable.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def patched_settings(monkeypatch):
    """Patch settings_service.peek with a dict-backed fake."""
    from services import settings_service as mod

    values: dict[tuple[int | None, str], object] = {}

    from services import settings_registry

    def fake_peek(guild_id, key):
        if (guild_id, key) in values:
            return values[(guild_id, key)]
        if (None, key) in values:
            return values[(None, key)]
        d = settings_registry.get(key)
        return d.default if d else None

    monkeypatch.setattr(mod.settings_service, "peek", fake_peek)
    return values


def test_rating_base_change_defaults(patched_settings):
    from utils.rating_calculator import get_base_elo_change
    assert get_base_elo_change(1, True) == 10
    assert get_base_elo_change(4, False) == -10


def test_rating_base_change_override(patched_settings):
    from utils.rating_calculator import get_base_elo_change
    patched_settings[(None, "rating.base_win_change")] = 15
    patched_settings[(None, "rating.base_loss_change")] = -12
    assert get_base_elo_change(1, True) == 15
    assert get_base_elo_change(4, False) == -12


def test_rating_kd_bonus_weights(patched_settings):
    from utils.rating_calculator import calculate_kd_bonus
    assert calculate_kd_bonus(10, 5) == 15  # 10*2 - 5*1
    patched_settings[(None, "rating.kill_weight")] = 3
    patched_settings[(None, "rating.death_weight")] = 2
    assert calculate_kd_bonus(10, 5) == 20  # 10*3 - 5*2


def test_smart_balance_weights(patched_settings):
    from utils.smart_balance import _cfg_weights
    w = _cfg_weights()
    assert w["sum"] == pytest.approx(0.40)
    patched_settings[(None, "matchmaking.balance_weight_sum")] = 0.9
    assert _cfg_weights()["sum"] == pytest.approx(0.9)


def test_smart_balance_tolerance(patched_settings):
    from utils.smart_balance import _cfg_sum_tolerance
    assert _cfg_sum_tolerance() == pytest.approx(1.0)
    patched_settings[(None, "matchmaking.balance_sum_tolerance")] = 5.0
    assert _cfg_sum_tolerance() == pytest.approx(5.0)


def test_reputation_amount_dynamic(patched_settings):
    from services.reputation import _setting_int
    assert _setting_int("reputation.manual_amount", 1) == 1
    patched_settings[(None, "reputation.manual_amount")] = 7
    assert _setting_int("reputation.manual_amount", 1) == 7


def test_vac_decay_seconds(patched_settings):
    from storage.vac_store import _decay_seconds
    assert _decay_seconds() == pytest.approx(24 * 3600)
    patched_settings[(None, "vac.decay_hours")] = 48
    assert _decay_seconds() == pytest.approx(48 * 3600)


def test_vac_max_days_parse(patched_settings):
    """parse_duration respects vac.max_days."""
    from datetime import timedelta

    from services.vac_service import parse_duration
    patched_settings[(None, "vac.max_days")] = 1
    assert parse_duration("2d") is None          # > 1 day → rejected
    assert parse_duration("12h") == timedelta(hours=12)


def test_admin_actions_allowed_set():
    from services import admin_actions
    assert "service.ping" in admin_actions.allowed_actions()
    assert "settings.notify" in admin_actions.allowed_actions()
    assert "shop.resync_catalog" in admin_actions.allowed_actions()
    assert "games.resync" in admin_actions.allowed_actions()


@pytest.mark.asyncio
async def test_admin_actions_execute_ping(monkeypatch):
    """Executor runs a registered handler and marks success."""
    from services import admin_actions
    from storage import admin_config_store as store

    calls: list[tuple[str, str, Any]] = []

    async def fake_update(action_id, *, status=None, result=None,
                          error=None, increment_attempts=False):
        calls.append((action_id, status, result))

    async def fake_list(limit=50):
        return [{
            "id": "a1", "action": "service.ping",
            "guild_id": 0, "parameters": {}, "actor_id": 1,
        }]

    monkeypatch.setattr(store, "list_pending_actions", fake_list)
    monkeypatch.setattr(store, "update_action_request", fake_update)

    ran = await admin_actions.execute_pending_actions()
    assert ran == 1
    assert calls and calls[0][1] == "success"
    assert calls[0][2] == {"pong": True}


@pytest.mark.asyncio
async def test_admin_actions_unknown_action_fails(monkeypatch):
    from services import admin_actions
    from storage import admin_config_store as store

    calls: list[tuple[str, str, Any]] = []

    async def fake_update(action_id, *, status=None, result=None,
                          error=None, increment_attempts=False):
        calls.append((action_id, status, error))

    async def fake_list(limit=50):
        return [{
            "id": "a2", "action": "nonexistent.action",
            "guild_id": 0, "parameters": {}, "actor_id": 1,
        }]

    monkeypatch.setattr(store, "list_pending_actions", fake_list)
    monkeypatch.setattr(store, "update_action_request", fake_update)

    ran = await admin_actions.execute_pending_actions()
    assert ran == 0
    assert calls[0][1] == "failed"
    assert "no handler" in (calls[0][2] or "")


def test_shop_item_is_active_roundtrip():
    from models.shop_item import (
        CosmeticRarity,
        CosmeticType,
        ShopItem,
    )
    item = ShopItem(
        id="test_item", name="Test", description="d",
        price=10, cosmetic_type=CosmeticType.TAG,
        rarity=CosmeticRarity.BASIC, value="T",
        category="misc", is_active=False,
    )
    d = item.to_dict()
    assert d["is_active"] is False
    restored = ShopItem.from_dict(d)
    assert restored.is_active is False
