# ruff: noqa: B008
"""API аудита и очереди административных операций."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from control_center import rbac
from control_center.auth import (
    PanelUser,
    require_perm,
    verify_csrf,
)
from storage import admin_config_store as store

router = APIRouter(prefix="/api", tags=["audit-actions"])


# ---------------- audit ----------------

@router.get("/audit")
async def list_audit(
    guild_id: int | None = Query(default=None),
    actor_id: int | None = Query(default=None),
    action: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    user: PanelUser = Depends(require_perm(rbac.PERM_AUDIT_VIEW)),
) -> dict[str, Any]:
    # Не-owners видят только свой guild (IDOR-защита).
    gid = guild_id
    if user.role != "owner" and user.guild_id:
        if gid is not None and gid != user.guild_id:
            raise HTTPException(403, "guild scope violation")
        gid = user.guild_id
    rows = await store.list_audit(
        guild_id=gid, actor_id=actor_id, action=action,
        limit=limit, offset=offset,
    )
    return {"items": rows, "limit": limit, "offset": offset}


# ---------------- action requests ----------------

try:
    from services.admin_actions import allowed_actions as _bot_actions
    ALLOWED_ACTIONS_V1: set[str] = _bot_actions()
except Exception:  # noqa: BLE001 — бот может быть недоступен в тестах
    ALLOWED_ACTIONS_V1 = set()


def _action_perm(action: str) -> str | None:
    """Модульное разрешение для действия (None — только actions.execute)."""
    prefix = action.split(".", 1)[0]
    return {
        "economy": "economy.finance",
        "vac": "vac.manage",
        "moderation": "moderation.manage",
        "tournament": "tournament.manage",
        "player": "players.manage",
        "design": "design.manage",
        "shop": "shop.manage",
        "games": "games.manage",
        "settings": "settings.manage",
    }.get(prefix)


def _allowed_actions() -> set[str]:
    """Whitelist = bot-side handlers ∪ тестовый override ALLOWED_ACTIONS_V1."""
    try:
        from services.admin_actions import allowed_actions
        return allowed_actions() | ALLOWED_ACTIONS_V1
    except Exception:  # noqa: BLE001
        return set(ALLOWED_ACTIONS_V1)
# Действия, исполняемые ботом через зарегистрированные handlers.
# Расширяется по мере миграции модулей (Этап 5).


class ActionBody(BaseModel):
    action: str = Field(min_length=1, max_length=100)
    guild_id: int | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=200)


@router.post("/actions")
async def create_action(
    body: ActionBody,
    user: PanelUser = Depends(require_perm(rbac.PERM_ACTIONS_EXECUTE)),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    if body.action not in _allowed_actions():
        raise HTTPException(
            400, f"unknown or unregistered action: {body.action}"
        )
    # Действие требует и actions.execute, и модульное разрешение —
    # иначе moderator с actions.execute мог бы двигать экономику.
    needed = _action_perm(body.action)
    if needed and not user.can(needed):
        raise HTTPException(403, f"missing permission: {needed}")
    gid = body.guild_id or user.guild_id or 0
    # Не-owner не может отправлять действия от имени чужого guild.
    if user.role != "owner" and user.guild_id:
        if body.guild_id and body.guild_id != user.guild_id:
            raise HTTPException(403, "guild scope violation")
        gid = user.guild_id
    row = await store.create_action_request(
        guild_id=gid,
        action=body.action,
        parameters=body.parameters,
        idempotency_key=body.idempotency_key,
        actor_id=user.id,
    )
    if row is None:
        # Дубликат idempotency_key — возвращаем существующий.
        existing = await store.list_pending_actions(limit=500)
        match = next(
            (r for r in existing
             if r["action"] == body.action
             and r["idempotency_key"] == body.idempotency_key),
            None,
        )
        if match:
            return {"deduplicated": True, "action": _ser(match)}
        raise HTTPException(409, "duplicate action request")
    return {"deduplicated": False, "action": _ser(row)}


@router.get("/actions/{action_id}")
async def get_action(
    action_id: str,
    user: PanelUser = Depends(require_perm(rbac.PERM_AUDIT_VIEW)),
) -> dict[str, Any]:
    try:
        uuid.UUID(action_id)
    except ValueError:
        raise HTTPException(400, "invalid action id") from None
    row = await store.get_action_request(action_id)
    if row is None:
        raise HTTPException(404, "action not found")
    if user.role != "owner" and int(row["guild_id"]) != user.guild_id:
        raise HTTPException(403, "guild scope violation")
    return {"action": _ser(row)}


def _ser(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "guild_id": row["guild_id"],
        "action": row["action"],
        "parameters": row["parameters"],
        "idempotency_key": row["idempotency_key"],
        "status": row["status"],
        "result": row["result"],
        "error": row["error"],
        "actor_id": row["actor_id"],
        "attempts": row["attempts"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finished_at": row.get("finished_at"),
    }
