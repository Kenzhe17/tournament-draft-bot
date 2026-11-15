"""Bot-side executor for Control Center admin action requests.

The panel writes rows into admin_action_requests; this module polls them
and executes registered handlers. Each handler receives (guild_id, params,
actor_id) and must return a result dict. Failures are recorded back into
the request row so the panel can show real status — nothing is left
"queued forever" silently.

Handlers v1 (all read/write existing stores, no arbitrary code):
  - settings.notify        : force-refresh settings cache for a guild
  - shop.resync_catalog    : reload PG shop catalog into bot cache
  - games.resync           : resync minigame rows from PG
  - service.ping           : heartbeat marker (diagnostics)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

ActionHandler = Callable[[int | None, dict[str, Any], int | None],
                         Awaitable[dict[str, Any]]]

_HANDLERS: dict[str, ActionHandler] = {}


def register_action(name: str):
    def deco(fn: ActionHandler) -> ActionHandler:
        _HANDLERS[name] = fn
        return fn
    return deco


def allowed_actions() -> set[str]:
    return set(_HANDLERS)


@register_action("settings.notify")
async def _h_notify(gid, params, actor):
    from services.settings_service import settings_service
    n = await settings_service.refresh_all()
    return {"refreshed_keys": n}


@register_action("shop.resync_catalog")
async def _h_shop_resync(gid, params, actor):
    from storage.shop_catalog import sync_to_store
    from storage.shop_store import shop_store
    mode = await sync_to_store(shop_store)
    return {"mode": mode}


@register_action("games.resync")
async def _h_games_resync(gid, params, actor):
    from storage.minigame_store import minigame_store
    games = await minigame_store.get_available_games()
    return {"available_games": len(games)}


@register_action("service.ping")
async def _h_ping(gid, params, actor):
    return {"pong": True}


async def execute_pending_actions(limit: int = 10) -> int:
    """Claim and run pending admin action requests. Returns count run."""
    try:
        from storage import admin_config_store as store
    except ImportError:
        return 0
    rows = await store.list_pending_actions(limit=limit)
    ran = 0
    for row in rows:
        rid = row.get("id")
        action = row.get("action")
        handler = _HANDLERS.get(action)
        try:
            if handler is None:
                await store.update_action_request(
                    rid, status="failed",
                    error=f"no handler for {action}",
                )
                continue
            gid = row.get("guild_id")
            params = row.get("parameters") or {}
            actor = row.get("actor_id")
            result = await handler(gid, params, actor)
            await store.update_action_request(
                rid, status="success", result=result)
            ran += 1
        except Exception as exc:  # noqa: BLE001 — record failure, keep loop alive
            log.warning("admin action %s failed: %s", rid, exc)
            try:
                await store.update_action_request(
                    rid, status="failed",
                    error=str(exc)[:500])
            except Exception:
                log.exception("failed to record action failure")
    return ran


async def actions_loop(interval: float = 15.0) -> None:
    """Background task: poll pending actions until cancelled."""
    while True:
        try:
            await execute_pending_actions()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("admin actions loop error")
        await asyncio.sleep(interval)
