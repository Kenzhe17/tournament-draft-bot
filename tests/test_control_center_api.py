"""API-тесты Control Center: auth override, RBAC, CSRF, settings CRUD."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import UTC

from control_center import rbac
from control_center.auth import PanelUser, current_user
from control_center.main import create_app
from storage import admin_config_store as store

# ------------------------------------------------------------ fake store

class FakeBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[int, str], dict] = {}
        self.history: list[dict] = []
        self.audit: list[dict] = []
        self.actions: dict[str, dict] = {}
        self.services: list[dict] = []
        self.permissions: dict[tuple[int, int], set[str]] = {}

    async def get_all_values(self):
        return [
            {"guild_id": gid, "key": k, "value": v["value"],
             "version": v["version"], "updated_by": 0,
             "updated_at": None}
            for (gid, k), v in self.values.items()
        ]

    async def get_value(self, guild_id, key):
        v = self.values.get((guild_id, key))
        if v is None:
            return None
        return {"guild_id": guild_id, "key": key, "value": v["value"],
                "version": v["version"], "updated_by": 0,
                "updated_at": None}

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
            "old_value": old, "new_value": value, "changed_by": actor_id,
            "correlation_id": correlation_id, "changed_at": None,
        })
        return {"guild_id": guild_id, "key": key, "value": value,
                "version": new_v, "updated_by": actor_id,
                "updated_at": None}

    async def get_history(self, guild_id, key, *, limit=50,
                          before_version=None):
        rows = [h for h in self.history
                if h["guild_id"] == guild_id and h["key"] == key
                and (before_version is None
                     or h["version"] < before_version)]
        rows.sort(key=lambda h: h["version"], reverse=True)
        return rows[:limit]

    async def rollback_to_version(self, guild_id, key, target_version, *,
                                  actor_id=0, correlation_id=""):
        for h in self.history:
            if (h["guild_id"] == guild_id and h["key"] == key
                    and h["version"] == target_version):
                return await self.set_value(
                    guild_id, key, h["new_value"], actor_id=actor_id)
        raise ValueError("not found")

    async def list_audit(self, *, guild_id=None, actor_id=None,
                         action=None, limit=100, offset=0):
        rows = self.audit
        if guild_id is not None:
            rows = [r for r in rows if r["guild_id"] == guild_id]
        return rows[offset:offset + limit]

    async def create_action_request(self, *, guild_id, action,
                                    parameters=None, idempotency_key,
                                    actor_id=0, max_attempts=3):
        for r in self.actions.values():
            if (r["guild_id"] == guild_id and r["action"] == action
                    and r["idempotency_key"] == idempotency_key):
                return None
        import uuid as _u
        from datetime import datetime
        rid = str(_u.uuid4())
        row = {
            "id": rid, "guild_id": guild_id, "action": action,
            "parameters": parameters or {},
            "idempotency_key": idempotency_key, "status": "pending",
            "result": None, "error": "", "actor_id": actor_id,
            "attempts": 0, "max_attempts": max_attempts,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "finished_at": None,
        }
        self.actions[rid] = row
        return dict(row)

    async def get_action_request(self, action_id):
        return self.actions.get(action_id)

    async def list_pending_actions(self, limit=50):
        return [r for r in self.actions.values()
                if r["status"] in ("pending", "running")][:limit]

    async def list_service_status(self):
        return self.services

    async def user_permissions(self, guild_id, user_id):
        return self.permissions.get((guild_id, user_id), set())


@pytest.fixture
def backend(monkeypatch):
    fb = FakeBackend()
    for name in ("get_all_values", "get_value", "set_value",
                 "get_history", "rollback_to_version", "list_audit",
                 "create_action_request", "get_action_request",
                 "list_pending_actions", "list_service_status",
                 "user_permissions"):
        monkeypatch.setattr(store, name, getattr(fb, name))
    return fb


def make_user(perms, *, uid=1001, gid=555, role="admin"):
    payload = {"sub": str(uid), "username": "tester", "avatar": "",
               "guild_id": gid, "role": role}
    return PanelUser(payload, set(perms))


@pytest.fixture
def client(backend):
    app = create_app()
    return TestClient(app)


def auth_as(app_client, user):
    app_client.app.dependency_overrides[current_user] = lambda: user
    return app_client


ALL_ADMIN = rbac.permissions_for_role("admin")


# ------------------------------------------------------------ tests

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["service"] == "control-center-api"


def test_unauthorized_without_session(client):
    r = client.get("/api/settings")
    assert r.status_code == 401


def test_me(client):
    user = make_user(ALL_ADMIN)
    c = auth_as(client, user)
    r = c.get("/api/me")
    assert r.status_code == 200
    assert r.json()["username"] == "tester"
    assert "settings.manage" in r.json()["permissions"]


def test_settings_list_and_get(client):
    user = make_user(ALL_ADMIN)
    c = auth_as(client, user)
    r = c.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    keys = {s["key"] for s in body["settings"]}
    assert "elo.kill_weight" in keys
    assert "feature.tournaments_enabled" in keys

    r = c.get("/api/settings/elo.kill_weight")
    assert r.status_code == 200
    assert r.json()["value"] == 1.0  # default
    assert r.json()["is_default"] is True


def test_settings_put_requires_csrf_and_perm(client, backend):
    viewer = make_user({"panel.view", "settings.view"}, role="viewer")
    c = auth_as(client, viewer)
    # viewer не имеет settings.manage → 403
    r = c.put("/api/settings/elo.kill_weight",
              json={"value": 2.0}, headers={"X-CC-Request": "1"})
    assert r.status_code == 403

    admin = make_user(ALL_ADMIN)
    c2 = auth_as(client, admin)
    # Без CSRF-заголовка → 403
    r = c2.put("/api/settings/elo.kill_weight", json={"value": 2.0})
    assert r.status_code == 403
    # С заголовком → 200
    r = c2.put("/api/settings/elo.kill_weight",
               json={"value": 2.0}, headers={"X-CC-Request": "1"})
    assert r.status_code == 200
    assert r.json()["version"] == 1
    assert backend.values[(0, "elo.kill_weight")]["value"] == 2.0


def test_settings_put_validation(client):
    c = auth_as(client, make_user(ALL_ADMIN))
    r = c.put("/api/settings/elo.kill_weight",
              json={"value": "abc"}, headers={"X-CC-Request": "1"})
    assert r.status_code == 422
    r = c.put("/api/settings/elo.kill_weight",
              json={"value": 999.0}, headers={"X-CC-Request": "1"})
    assert r.status_code == 422  # max=100


def test_settings_version_conflict(client):
    c = auth_as(client, make_user(ALL_ADMIN))
    h = {"X-CC-Request": "1"}
    c.put("/api/settings/elo.kill_weight",
          json={"value": 1.5}, headers=h)
    r = c.put("/api/settings/elo.kill_weight",
              json={"value": 2.0, "expected_version": 99}, headers=h)
    assert r.status_code == 409


def test_settings_history_and_rollback(client, backend):
    c = auth_as(client, make_user(ALL_ADMIN))
    h = {"X-CC-Request": "1"}
    c.put("/api/settings/elo.kill_weight",
          json={"value": 1.5}, headers=h)
    c.put("/api/settings/elo.kill_weight",
          json={"value": 2.0}, headers=h)

    r = c.get("/api/settings/elo.kill_weight/history")
    assert r.status_code == 200
    hist = r.json()["history"]
    assert [x["version"] for x in hist] == [2, 1]

    r = c.post("/api/settings/elo.kill_weight/rollback",
               json={"target_version": 1}, headers=h)
    assert r.status_code == 200
    assert r.json()["value"] == 1.5


def test_economy_finance_not_in_admin_bundle():
    """Administrator НЕ получает economy.finance по умолчанию."""
    perms = rbac.permissions_for_role("admin")
    assert not rbac.has_permission(perms, "economy.finance")
    assert rbac.has_permission(perms, "economy.manage")


def test_grant_wildcard_and_prefix():
    assert rbac.has_permission({"*"}, "anything.x")
    assert rbac.has_permission({"economy.*"}, "economy.finance")
    assert not rbac.has_permission({"economy.manage"}, "economy.finance")
    assert not rbac.has_permission({"settings.view"}, "settings.manage")


def test_audit_scoped_to_user_guild(client, backend):
    backend.audit = [
        {"id": 1, "guild_id": 555, "actor_id": 1, "action": "config.set",
         "object_type": "config", "object_id": "k", "old_value": None,
         "new_value": 1, "result": "success", "config_version": 1,
         "correlation_id": "", "created_at": None},
        {"id": 2, "guild_id": 999, "actor_id": 2, "action": "config.set",
         "object_type": "config", "object_id": "k", "old_value": None,
         "new_value": 1, "result": "success", "config_version": 1,
         "correlation_id": "", "created_at": None},
    ]
    # viewer-role user с guild_id=555 не должен видеть guild 999
    user = make_user({"panel.view", "audit.view"}, gid=555,
                     role="viewer")
    c = auth_as(client, user)
    r = c.get("/api/audit")
    assert r.status_code == 200
    assert all(i["guild_id"] == 555 for i in r.json()["items"])
    # явный запрос чужого guild → 403
    r = c.get("/api/audit", params={"guild_id": 999})
    assert r.status_code == 403


def test_actions_unregistered_rejected(client):
    user = make_user(ALL_ADMIN | {"actions.execute"})
    c = auth_as(client, user)
    r = c.post("/api/actions",
               json={"action": "tournament.cancel",
                     "parameters": {}, "idempotency_key": "k1"},
               headers={"X-CC-Request": "1"})
    assert r.status_code == 400


def test_actions_idempotency(client, backend):
    # Зарегистрируем фейковое действие через monkeypatch ALLOWED_ACTIONS_V1
    from control_center.routers import ops_api
    ops_api.ALLOWED_ACTIONS_V1.add("test.noop")
    try:
        user = make_user(ALL_ADMIN | {"actions.execute"})
        c = auth_as(client, user)
        body = {"action": "test.noop", "parameters": {},
                "idempotency_key": "same-key"}
        r1 = c.post("/api/actions", json=body,
                    headers={"X-CC-Request": "1"})
        assert r1.status_code == 200
        assert r1.json()["deduplicated"] is False
        r2 = c.post("/api/actions", json=body,
                    headers={"X-CC-Request": "1"})
        assert r2.status_code == 200
        assert r2.json()["deduplicated"] is True
        assert len(backend.actions) == 1
    finally:
        ops_api.ALLOWED_ACTIONS_V1.discard("test.noop")


def test_dashboard_shape(client, backend):
    backend.services = [{
        "service": "discord-bot",
        "version_info": {"commit": "abc123"},
        "extra": {}, "last_seen": None,
    }]
    user = make_user(ALL_ADMIN)
    c = auth_as(client, user)
    r = c.get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert "services" in body and "bot" in body and "counts" in body
