# ruff: noqa: B008
"""System API: health, me, dashboard, статусы сервисов."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import APIRouter, Depends

from control_center import config, rbac
from control_center.auth import PanelUser, current_user, require_perm
from storage import admin_config_store as store

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict[str, Any]:
    db_ok = False
    try:
        await store.list_service_status()
        db_ok = True
    except Exception:  # noqa: BLE001, S110 — health не должен падать
        pass
    missing = config.missing_required()
    return {
        "service": config.SERVICE_NAME,
        "status": "ok" if db_ok and not missing else "degraded",
        "database": "up" if db_ok else "down",
        "config": "ok" if not missing else f"missing: {', '.join(missing)}",
    }


@router.get("/me")
async def me(user: PanelUser = Depends(current_user)) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "username": user.username,
        "avatar": user.avatar,
        "guild_id": user.guild_id,
        "role": user.role,
        "permissions": sorted(user.permissions),
    }


@router.get("/dashboard")
async def dashboard(
    user: PanelUser = Depends(require_perm(rbac.PERM_PANEL_VIEW)),
) -> dict[str, Any]:
    """Реальные данные: статусы сервисов + счётчики из PG.

    Турниры/очереди MM живут в JSON-хранилищах бота — панель их
    не читает напрямую (контейнеры разделены); бот публикует
    сводку через admin_service_status.extra при наличии данных.
    """
    services: list[dict[str, Any]] = []
    db_ok = True
    try:
        services = await store.list_service_status()
    except Exception:  # noqa: BLE001
        db_ok = False

    counts: dict[str, Any] = {}
    if db_ok:
        try:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                counts["players"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM player_stats"
                )
                counts["balances"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM user_balance"
                )
                counts["mm_queues"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM mm_queues WHERE state = 'open'"
                )
                counts["pending_actions"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM admin_action_requests"
                    " WHERE status IN ('pending','running')"
                )
                counts["audit_events"] = await conn.fetchval(
                    "SELECT COUNT(*) FROM admin_audit_log"
                )
        except Exception:  # noqa: BLE001
            db_ok = False

    bot_hb = next(
        (s for s in services if s["service"] == "discord-bot"), None
    )
    return {
        "database": "up" if db_ok else "down",
        "services": [
            {
                "service": s["service"],
                "version_info": s["version_info"],
                "extra": s["extra"],
                "last_seen": s["last_seen"],
                "stale": _is_stale(s["last_seen"]),
            }
            for s in services
        ],
        "bot": {
            "online": bot_hb is not None and not _is_stale(
                bot_hb["last_seen"]
            ),
            "last_seen": bot_hb["last_seen"] if bot_hb else None,
            "version_info": bot_hb["version_info"] if bot_hb else {},
        },
        "counts": counts,
    }


def _is_stale(last_seen, threshold_seconds: int = 120) -> bool:
    from datetime import datetime
    if last_seen is None:
        return True
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    return (
        datetime.now(UTC) - last_seen
    ).total_seconds() > threshold_seconds
