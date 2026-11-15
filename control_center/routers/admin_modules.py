# ruff: noqa: B008
"""Admin modules API: snapshot, economy, templates, moderation reads.

Read paths query PostgreSQL directly (VAC, balances — bot and panel
share the same DB). Mutations go through /api/actions →
admin_action_requests → bot-side handlers (state-machine safe).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from control_center import rbac
from control_center.auth import PanelUser, require_perm, verify_csrf
from storage import admin_config_store as store
from storage.db import get_pool

router = APIRouter(prefix="/api/admin", tags=["admin-modules"])


def _gid(user: PanelUser, guild_id: int | None) -> int:
    gid = guild_id or user.guild_id or 0
    if user.role != "owner" and user.guild_id and gid != user.guild_id:
        raise HTTPException(403, "guild scope violation")
    return gid


# ---------------- bot state snapshot ----------------

@router.get("/snapshot")
async def snapshot(
    user: PanelUser = Depends(require_perm(rbac.PERM_PANEL_VIEW)),
) -> dict[str, Any]:
    """Снимок состояния, опубликованный ботом (admin_service_status)."""
    rows = await store.list_service_status()
    row = next((r for r in rows if r["service"] == "bot_state"), None)
    if not row:
        return {"available": False,
                "reason": "bot snapshot not published yet"}
    extra = row.get("extra") or {}
    if isinstance(extra, str):
        import json
        extra = json.loads(extra)
    gid = str(user.guild_id) if user.guild_id else None
    out: dict[str, Any] = {"available": True, "last_seen": row.get("last_seen")}
    for section in ("tournaments", "matchmaking", "vac_active_count"):
        data = extra.get(section) or {}
        out[section] = ({gid: data[gid]} if gid and gid in data
                        else data if user.role == "owner" else {})
    guilds = extra.get("guilds") or {}
    out["guilds"] = ({gid: guilds[gid]} if gid and gid in guilds
                     else guilds if user.role == "owner" else {})
    return out


# ---------------- economy reads ----------------

@router.get("/economy/player/{user_id}")
async def economy_player(
    user_id: int,
    user: PanelUser = Depends(require_perm(rbac.PERM_PANEL_VIEW)),
) -> dict[str, Any]:
    gid = _gid(user, None)
    pool = await get_pool()
    async with pool.acquire() as conn:
        bal = await conn.fetchrow(
            "SELECT balance FROM user_balance WHERE guild_id=$1 AND user_id=$2",
            gid, user_id)
        grants = await conn.fetch(
            """
            SELECT match_id, amount, granted_at FROM match_coin_grants
            WHERE guild_id=$1 AND user_id=$2 ORDER BY granted_at DESC LIMIT 50
            """, gid, user_id)
    return {
        "user_id": user_id, "guild_id": gid,
        "balance": bal["balance"] if bal else None,
        "recent_grants": [dict(g) for g in grants],
    }


@router.get("/economy/grants")
async def economy_grants(
    limit: int = Query(default=50, le=200),
    user: PanelUser = Depends(require_perm(rbac.PERM_PANEL_VIEW)),
) -> dict[str, Any]:
    """Журнал финансовых начислений (match_coin_grants)."""
    gid = _gid(user, None)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT match_id, user_id, amount, granted_at
            FROM match_coin_grants WHERE guild_id=$1
            ORDER BY granted_at DESC LIMIT $2
            """, gid, limit)
    return {"items": [dict(r) for r in rows]}


# ---------------- moderation reads (VAC) ----------------

@router.get("/moderation/vac/active")
async def vac_active(
    user: PanelUser = Depends(require_perm(rbac.PERM_PANEL_VIEW)),
) -> dict[str, Any]:
    gid = _gid(user, None)
    from storage.vac_store import vac_store
    rows = await vac_store.list_active(gid)
    return {"items": rows}


@router.get("/moderation/vac/history")
async def vac_history(
    user_id: int | None = None,
    limit: int = Query(default=50, le=200),
    user: PanelUser = Depends(require_perm(rbac.PERM_PANEL_VIEW)),
) -> dict[str, Any]:
    gid = _gid(user, None)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if user_id is not None:
            rows = await conn.fetch(
                """
                SELECT * FROM vac_bans WHERE guild_id=$1 AND user_id=$2
                ORDER BY issued_at DESC LIMIT $3
                """, gid, user_id, limit)
        else:
            rows = await conn.fetch(
                """
                SELECT * FROM vac_bans WHERE guild_id=$1
                ORDER BY issued_at DESC LIMIT $2
                """, gid, limit)
    return {"items": [dict(r) for r in rows]}


# ---------------- CV2 templates ----------------

class TemplateBody(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


@router.get("/templates")
async def list_templates(
    user: PanelUser = Depends(require_perm(rbac.PERM_PANEL_VIEW)),
) -> dict[str, Any]:
    gid = _gid(user, None)
    rows = await store.list_templates(gid or None)
    return {"items": rows}


@router.get("/templates/{key}")
async def get_template(
    key: str,
    user: PanelUser = Depends(require_perm(rbac.PERM_PANEL_VIEW)),
) -> dict[str, Any]:
    gid = _gid(user, None)
    row = await store.get_template(gid or 0, key)
    if not row and gid:
        row = await store.get_template(0, key)  # global fallback
    if not row:
        raise HTTPException(404, "template not found")
    return row


@router.put("/templates/{key}")
async def put_template(
    key: str,
    body: TemplateBody,
    user: PanelUser = Depends(require_perm("design.manage")),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    gid = _gid(user, None)
    if not key or len(key) > 100:
        raise HTTPException(422, "invalid key")
    row = await store.set_template(
        gid or 0, key, body.payload, actor_id=user.id)
    return {"key": key, "version": row["version"], "status": "saved"}
