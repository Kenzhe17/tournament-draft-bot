"""P1 admin modules: action handlers, deduct_balance, admin API."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import timedelta

# ------------------------------------------------------------ deduct_balance

def test_deduct_balance_async():
    import asyncio

    from storage.user_balance_store import UserBalanceStore
    store = UserBalanceStore()
    store._use_db = False
    store._balances.clear()
    store._balances["1:42"] = 500

    async def go():
        t1 = await store.deduct_balance(1, 42, 200)
        bal1 = store._balances["1:42"]
        t2 = await store.deduct_balance(1, 42, 9999)  # больше баланса
        bal2 = store._balances["1:42"]
        return t1, bal1, t2, bal2

    t1, bal1, t2, bal2 = asyncio.run(go())
    assert (t1, bal1, t2, bal2) == (200, 300, 300, 0)


def test_deduct_balance_rejects_nonpositive():
    import asyncio

    from storage.user_balance_store import UserBalanceStore
    store = UserBalanceStore()
    store._use_db = False
    with pytest.raises(ValueError):
        asyncio.run(store.deduct_balance(1, 1, 0))


# ------------------------------------------------------------ action handlers

@pytest.mark.asyncio
async def test_economy_adjust_credit(monkeypatch):
    from services import admin_actions
    from storage.user_balance_store import user_balance_store

    calls = []

    async def fake_admin_adjust(gid, uid, amount, op_id, *,
                                actor_id=0, reason=""):
        calls.append(("admin_adjust", gid, uid, amount, op_id))
        return {"status": "applied", "applied": amount,
                "new_balance": 600}

    async def fake_audit(**kw):
        calls.append(("audit", kw["action"], kw["object_id"]))

    monkeypatch.setattr(user_balance_store, "admin_adjust",
                        fake_admin_adjust)
    from storage import admin_config_store as store
    monkeypatch.setattr(store, "insert_audit", fake_audit)

    res = await admin_actions._h_economy_adjust(
        555, {"user_id": 42, "amount": 100, "reason": "test",
              "_action_id": "act-1"}, 7)
    assert res["applied"] == 100 and res["new_balance"] == 600
    assert calls[0] == ("admin_adjust", 555, 42, 100, "act-1")
    assert calls[1][0] == "audit"


@pytest.mark.asyncio
async def test_economy_adjust_debit_partial(monkeypatch):
    from services import admin_actions
    from storage.user_balance_store import user_balance_store

    async def fake_admin_adjust(gid, uid, amount, op_id, *,
                                actor_id=0, reason=""):
        return {"status": "applied", "applied": -50,
                "new_balance": 50}

    async def fake_audit(**kw):
        pass

    monkeypatch.setattr(user_balance_store, "admin_adjust",
                        fake_admin_adjust)
    from storage import admin_config_store as store
    monkeypatch.setattr(store, "insert_audit", fake_audit)

    res = await admin_actions._h_economy_adjust(
        555, {"user_id": 42, "amount": -100}, 7)
    assert res["applied"] == -50
    assert res["new_balance"] == 50


@pytest.mark.asyncio
async def test_economy_adjust_rejects_zero():
    from services import admin_actions
    with pytest.raises(ValueError):
        await admin_actions._h_economy_adjust(
            555, {"user_id": 1, "amount": 0}, 7)


@pytest.mark.asyncio
async def test_vac_issue_and_revoke(monkeypatch):
    from services import admin_actions, vac_service

    issued = {}

    async def fake_issue(gid, uid, by, duration, reason=None):
        issued.update(gid=gid, uid=uid, duration=duration, reason=reason)
        return {"expires_at": "2025-01-01"}, None

    revoked = {}

    async def fake_revoke(gid, uid, by):
        revoked.update(gid=gid, uid=uid)
        return {"ok": True}

    monkeypatch.setattr(vac_service, "issue_vac", fake_issue)
    monkeypatch.setattr(vac_service, "revoke_vac", fake_revoke)

    r1 = await admin_actions._h_vac_issue(
        555, {"user_id": 9, "duration": "7d", "reason": "spam"}, 7)
    assert r1["issued"] and issued["duration"] == timedelta(days=7)

    r2 = await admin_actions._h_vac_revoke(555, {"user_id": 9}, 7)
    assert r2["revoked"] and revoked["uid"] == 9


@pytest.mark.asyncio
async def test_vac_issue_error_propagates(monkeypatch):
    from services import admin_actions, vac_service

    async def fake_issue(gid, uid, by, duration, reason=None):
        return None, "already has active VAC"

    monkeypatch.setattr(vac_service, "issue_vac", fake_issue)
    with pytest.raises(RuntimeError, match="already has active VAC"):
        await admin_actions._h_vac_issue(
            555, {"user_id": 9, "duration": "1d"}, 7)


def test_allowed_actions_include_admin_ops():
    from services import admin_actions
    allowed = admin_actions.allowed_actions()
    for a in ("vac.issue", "vac.revoke", "economy.adjust",
              "tournament.cancel", "service.ping"):
        assert a in allowed


# ------------------------------------------------------------ admin API

def test_admin_snapshot_endpoint(monkeypatch):
    """GET /api/admin/snapshot читает admin_service_status 'bot_state'."""
    from fastapi.testclient import TestClient

    from control_center import rbac
    from control_center.auth import PanelUser, current_user
    from control_center.main import create_app
    from storage import admin_config_store as store

    async def fake_status():
        return [{
            "service": "bot_state",
            "version_info": {},
            "extra": {
                "tournaments": {"555": [{"id": "t1", "channel_id": 1,
                                         "phase": "DRAFT"}]},
                "matchmaking": {},
                "vac_active_count": {"555": 2},
                "guilds": {"555": {"name": "Test", "channels": [],
                                   "roles": []}},
            },
            "last_seen": "2025-01-01T00:00:00",
        }]

    monkeypatch.setattr(store, "list_service_status", fake_status)
    app = create_app()
    user = PanelUser(
        {"sub": "1", "username": "o", "avatar": "", "guild_id": 555,
         "role": "owner"},
        set(rbac.permissions_for_role("owner")))
    app.dependency_overrides[current_user] = lambda: user
    try:
        r = TestClient(app).get("/api/admin/snapshot")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["tournaments"]["555"][0]["id"] == "t1"
    finally:
        app.dependency_overrides.clear()


def test_admin_snapshot_scoped_to_guild(monkeypatch):
    """Не-owner видит только свой guild."""
    from fastapi.testclient import TestClient

    from control_center import rbac
    from control_center.auth import PanelUser, current_user
    from control_center.main import create_app
    from storage import admin_config_store as store

    async def fake_status():
        return [{
            "service": "bot_state", "version_info": {},
            "extra": {"tournaments": {"555": [{"id": "a"}],
                                      "999": [{"id": "b"}]},
                      "guilds": {"555": {}, "999": {}}},
            "last_seen": "x",
        }]

    monkeypatch.setattr(store, "list_service_status", fake_status)
    app = create_app()
    user = PanelUser(
        {"sub": "2", "username": "m", "avatar": "", "guild_id": 555,
         "role": "moderator"},
        set(rbac.permissions_for_role("moderator")))
    app.dependency_overrides[current_user] = lambda: user
    try:
        body = TestClient(app).get("/api/admin/snapshot").json()
        assert "555" in body["tournaments"]
        assert "999" not in body["tournaments"]
        assert "555" in body["guilds"] and "999" not in body["guilds"]
    finally:
        app.dependency_overrides.clear()


def test_template_put_and_list(monkeypatch):
    """PUT /api/admin/templates/{key} → set_template с версией."""
    from fastapi.testclient import TestClient

    from control_center import rbac
    from control_center.auth import PanelUser, current_user
    from control_center.main import create_app
    from storage import admin_config_store as store

    saved = {}

    async def fake_set(gid, key, payload, *, actor_id=0):
        saved.update(gid=gid, key=key, payload=payload)
        return {"guild_id": gid, "key": key, "payload": payload,
                "version": 3, "updated_by": actor_id,
                "updated_at": None}

    async def fake_list(gid=None):
        return [saved] if saved else []

    monkeypatch.setattr(store, "set_template", fake_set)
    monkeypatch.setattr(store, "list_templates", fake_list)

    app = create_app()
    user = PanelUser(
        {"sub": "3", "username": "a", "avatar": "", "guild_id": 555,
         "role": "admin"},
        set(rbac.permissions_for_role("admin")))
    app.dependency_overrides[current_user] = lambda: user
    try:
        client = TestClient(app)
        r = client.put("/api/admin/templates/welcome.card",
                       json={"payload": {"title": "Hi"}},
                       headers={"X-CC-Request": "1"})
        assert r.status_code == 200
        assert saved["key"] == "welcome.card"
        assert saved["payload"] == {"title": "Hi"}
    finally:
        app.dependency_overrides.clear()


def test_template_put_requires_design_perm():
    """Организатор без design.manage не может писать шаблоны."""
    from fastapi.testclient import TestClient

    from control_center import rbac
    from control_center.auth import PanelUser, current_user
    from control_center.main import create_app
    from storage import admin_config_store as store

    async def fake_set(*a, **kw):  # pragma: no cover
        raise AssertionError("must not be called")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(store, "set_template", fake_set)
    app = create_app()
    user = PanelUser(
        {"sub": "4", "username": "o", "avatar": "", "guild_id": 555,
         "role": "organizer"},
        set(rbac.permissions_for_role("organizer")))
    app.dependency_overrides[current_user] = lambda: user
    try:
        r = TestClient(app).put(
            "/api/admin/templates/x", json={"payload": {}},
            headers={"X-CC-Request": "1"})
        assert r.status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()
        monkeypatch.undo()


# ------------------------------------------------------------ cv2 templates

def test_cv2_validate_rejects_custom_id():
    from services.cv2_templates import TemplateError, validate_payload
    with pytest.raises(TemplateError, match="custom_id"):
        validate_payload({"custom_id": "x", "blocks": []})


def test_cv2_validate_rejects_bad_button():
    from services.cv2_templates import TemplateError, validate_payload
    with pytest.raises(TemplateError):
        validate_payload({"blocks": [
            {"type": "button_link", "label": "x", "url": "javascript:bad"}]})


def test_cv2_render_and_preview():
    from services.cv2_templates import preview_payload, render_template
    payload = {
        "title": "Test",
        "blocks": [
            {"type": "heading", "text": "H"},
            {"type": "text", "text": "body"},
            {"type": "separator", "large": True},
            {"type": "fields", "fields": [{"name": "A", "value": "B"}]},
            {"type": "button_link", "label": "Go", "url": "https://x"},
        ],
    }
    view = render_template(payload)
    assert view is not None
    prev = preview_payload(payload)
    assert prev["block_count"] == 5
    assert any("Go" in l for l in prev["lines"])


# ------------------------------------------------------------ discord_cfg

def test_discord_cfg_fallback(monkeypatch):
    from services import discord_cfg
    # без инициализированного сервиса → default
    assert discord_cfg.channel_id(1, "discord.notification_channel_id", 42) == 42
    assert discord_cfg.channel_ids(1, "discord.tournament_channels", [7]) == [7]


def test_discord_cfg_dynamic(monkeypatch):
    from services import discord_cfg
    from services import settings_service as mod
    monkeypatch.setattr(
        mod.settings_service, "peek",
        lambda gid, key: {"discord.notification_channel_id": 999}.get(key))
    assert discord_cfg.channel_id(1, "discord.notification_channel_id", 42) == 999


# ------------------------------------------------------------ new handlers

@pytest.mark.asyncio
async def test_player_adjust_elo(monkeypatch):
    from services import admin_actions
    from storage.player_stats_store import player_stats_store
    from storage import admin_config_store as store

    class FakeStats:
        elo = 1000
        name = "Player"

    calls = {}

    async def fake_get(gid, uid):
        return FakeStats()

    async def fake_update(gid, uid, name, **kw):
        calls.update(set_elo=kw.get("set_elo"), name=name)

    async def fake_audit(**kw):
        calls["audit"] = (kw["old_value"], kw["new_value"])

    monkeypatch.setattr(player_stats_store, "get", fake_get)
    monkeypatch.setattr(player_stats_store, "update_player", fake_update)
    monkeypatch.setattr(store, "insert_audit", fake_audit)

    res = await admin_actions._h_player_elo(
        555, {"user_id": 9, "elo": 1200, "reason": "boost"}, 7)
    assert res["old_elo"] == 1000 and res["new_elo"] == 1200
    assert calls["set_elo"] == 1200
    assert calls["audit"][0] == {"elo": 1000}


@pytest.mark.asyncio
async def test_tournament_set_registration_guards(monkeypatch):
    from services import admin_actions
    from models.tournament import TournamentPhase

    class FakeT:
        def __init__(self, phase):
            self.phase = phase
            self.registration = None

    store_data = {"t": FakeT(TournamentPhase.SETUP)}

    class FakeJsonStore:
        def get(self, gid, cid):
            return store_data.get("t")

        def set(self, t):
            pass

    import storage.json_store as js
    monkeypatch.setattr(js, "store", FakeJsonStore())

    # SETUP → OPEN разрешено
    r = await admin_actions._h_t_registration(
        555, {"channel_id": 1, "state": "open"}, 7)
    assert r["registration"] == "OPEN"

    # COMPLETE → запрет
    store_data["t"] = FakeT(TournamentPhase.COMPLETE)
    with pytest.raises(RuntimeError, match="locked"):
        await admin_actions._h_t_registration(
            555, {"channel_id": 1, "state": "closed"}, 7)

    # нет турнира → ошибка
    store_data.clear()
    with pytest.raises(RuntimeError, match="no active tournament"):
        await admin_actions._h_t_registration(
            555, {"channel_id": 1, "state": "open"}, 7)


@pytest.mark.asyncio
async def test_executor_injects_action_id(monkeypatch):
    """Executor добавляет _action_id в params — нужен для финансовой
    идемпотентности (op_id)."""
    from services import admin_actions
    from storage import admin_config_store as store

    seen = {}

    @admin_actions.register_action("test.capture_params")
    async def _cap(gid, params, actor):
        seen.update(params)
        return {"ok": True}

    async def fake_list(limit=50):
        return [{"id": "uuid-123", "action": "test.capture_params",
                 "guild_id": 1, "parameters": {"x": 1}, "actor_id": 2}]

    async def fake_update(*a, **kw):
        pass

    monkeypatch.setattr(store, "list_pending_actions", fake_list)
    monkeypatch.setattr(store, "update_action_request", fake_update)
    await admin_actions.execute_pending_actions()
    assert seen.get("_action_id") == "uuid-123"
    assert seen.get("x") == 1
