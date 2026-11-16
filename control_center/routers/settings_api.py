# ruff: noqa: B008
"""API настроек: реестр, чтение, запись, история, rollback.

Запись идёт напрямую в admin_config_values через store —
бот подхватывает через LISTEN/NOTIFY. Чтение — из БД (свежие
данные, без кэша бота).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from control_center import rbac
from control_center.auth import (
    PanelUser,
    require_perm,
    verify_csrf,
)
from services import settings_registry as registry
from storage import admin_config_store as store

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SetBody(BaseModel):
    value: Any
    guild_id: int | None = None
    expected_version: int | None = None
    correlation_id: str = ""


class RollbackBody(BaseModel):
    target_version: int
    guild_id: int | None = None
    correlation_id: str = ""


def _check_manage(user: PanelUser, d) -> None:
    user.require(d.permission)
    user.require(rbac.PERM_SETTINGS_MANAGE)


@router.get("")
async def list_settings(
    module: str | None = Query(default=None),
    user: PanelUser = Depends(require_perm(rbac.PERM_SETTINGS_VIEW)),
) -> dict[str, Any]:
    defs = registry.all_defs()
    if module:
        defs = [d for d in defs if d.module == module]
    return {
        "modules": sorted({d.module for d in registry.all_defs()}),
        "settings": [
            {
                "key": d.key,
                "type": d.type,
                "module": d.module,
                "description": d.description,
                "scope": d.scope,
                "default": d.default,
                "min": d.min,
                "max": d.max,
                "permission": d.permission,
                "runtime_editable": d.runtime_editable,
                "consumers_pending": d.consumers_pending,
                "applies_to": d.applies_to,
            }
            for d in defs
        ],
    }


@router.get("/values")
async def list_values(
    guild_id: int = Query(default=0),
    user: PanelUser = Depends(require_perm(rbac.PERM_SETTINGS_VIEW)),
) -> dict[str, Any]:
    """Текущие значения всех настроек (PG + дефолты реестра)."""
    rows = await store.get_all_values()
    stored = {
        (int(r["guild_id"]), r["key"]): r for r in rows
    }
    out = []
    for d in registry.all_defs():
        effective_gid = 0 if d.scope == "global" else guild_id
        row = stored.get((effective_gid, d.key))
        out.append({
            "key": d.key,
            "module": d.module,
            "value": row["value"] if row else d.default,
            "version": row["version"] if row else 0,
            "is_default": row is None,
            "updated_at": row["updated_at"] if row else None,
            "updated_by": row["updated_by"] if row else None,
        })
    return {"guild_id": guild_id, "values": out}


@router.get("/{key}")
async def get_setting(
    key: str,
    guild_id: int = Query(default=0),
    user: PanelUser = Depends(require_perm(rbac.PERM_SETTINGS_VIEW)),
) -> dict[str, Any]:
    d = registry.get(key)
    if d is None:
        raise HTTPException(404, "unknown setting")
    gid = 0 if d.scope == "global" else guild_id
    row = await store.get_value(gid, key)
    return {
        "key": d.key,
        "type": d.type,
        "module": d.module,
        "description": d.description,
        "scope": d.scope,
        "default": d.default,
        "min": d.min,
        "max": d.max,
        "permission": d.permission,
        "runtime_editable": d.runtime_editable,
        "consumers_pending": d.consumers_pending,
        "applies_to": d.applies_to,
        "value": row["value"] if row else d.default,
        "version": row["version"] if row else 0,
        "is_default": row is None,
        "updated_at": row["updated_at"] if row else None,
        "updated_by": row["updated_by"] if row else None,
    }


@router.put("/{key}")
async def set_setting(
    key: str,
    body: SetBody,
    user: PanelUser = Depends(require_perm(rbac.PERM_SETTINGS_MANAGE)),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    d = registry.get(key)
    if d is None:
        raise HTTPException(404, "unknown setting")
    _check_manage(user, d)
    try:
        value = registry.coerce(d, body.value)
    except registry.SettingValidationError as exc:
        raise HTTPException(422, str(exc)) from exc

    gid = 0 if d.scope == "global" else (body.guild_id or user.guild_id)
    try:
        row = await store.set_value(
            gid, key, value,
            actor_id=user.id,
            expected_version=body.expected_version,
            correlation_id=body.correlation_id,
        )
    except store.VersionConflictError as exc:
        raise HTTPException(
            409,
            {
                "detail": "version conflict",
                "current_version": exc.current_version,
            },
        ) from exc
    return {
        "key": key,
        "guild_id": gid,
        "value": row["value"],
        "version": row["version"],
    }


@router.get("/{key}/history")
async def setting_history(
    key: str,
    guild_id: int = Query(default=0),
    limit: int = Query(default=50, le=200),
    before_version: int | None = Query(default=None),
    user: PanelUser = Depends(require_perm(rbac.PERM_SETTINGS_VIEW)),
) -> dict[str, Any]:
    d = registry.get(key)
    if d is None:
        raise HTTPException(404, "unknown setting")
    gid = 0 if d.scope == "global" else guild_id
    rows = await store.get_history(
        gid, key, limit=limit, before_version=before_version
    )
    return {"key": key, "guild_id": gid, "history": rows}


@router.post("/{key}/rollback")
async def rollback_setting(
    key: str,
    body: RollbackBody,
    user: PanelUser = Depends(require_perm(rbac.PERM_SETTINGS_MANAGE)),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    d = registry.get(key)
    if d is None:
        raise HTTPException(404, "unknown setting")
    _check_manage(user, d)
    gid = 0 if d.scope == "global" else (body.guild_id or user.guild_id)
    try:
        row = await store.rollback_to_version(
            gid, key, body.target_version,
            actor_id=user.id, correlation_id=body.correlation_id,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "key": key,
        "guild_id": gid,
        "value": row["value"],
        "version": row["version"],
        "rolled_back_to": body.target_version,
    }
