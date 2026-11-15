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
_bot = None


def bind_bot(bot) -> None:
    """Привязать экземпляр бота для handlers, которым нужен guild/каналы."""
    global _bot
    _bot = bot


def _require_bot():
    if _bot is None:
        raise RuntimeError("bot not bound to admin_actions")
    return _bot


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


# ------------------------------------------------------------------
# Moderation: VAC
# ------------------------------------------------------------------

def _parse_duration(text: str):
    """'7d'/'12h'/'30m' → timedelta (валидация через vac_service)."""
    import re
    from datetime import timedelta
    m = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?", text.strip())
    if not m or not any(m.groups()):
        raise ValueError("duration must look like 7d / 12h / 30m")
    return timedelta(
        days=int(m.group(1) or 0), hours=int(m.group(2) or 0),
        minutes=int(m.group(3) or 0))


@register_action("vac.issue")
async def _h_vac_issue(gid, params, actor):
    from services import vac_service
    user_id = int(params["user_id"])
    duration = _parse_duration(str(params["duration"]))
    reason = params.get("reason")
    rec, err = await vac_service.issue_vac(
        int(gid), user_id, int(actor or 0), duration, reason=reason)
    if err:
        raise RuntimeError(err)
    return {"issued": True, "user_id": user_id,
            "expires_at": rec.get("expires_at") if rec else None}


@register_action("vac.revoke")
async def _h_vac_revoke(gid, params, actor):
    from services import vac_service
    user_id = int(params["user_id"])
    rec = await vac_service.revoke_vac(int(gid), user_id, int(actor or 0))
    if not rec:
        raise RuntimeError("no active VAC for user")
    return {"revoked": True, "user_id": user_id}


# ------------------------------------------------------------------
# Economy: корректировка баланса через транзакционный store
# ------------------------------------------------------------------

@register_action("economy.adjust")
async def _h_economy_adjust(gid, params, actor):
    from storage.user_balance_store import user_balance_store
    user_id = int(params["user_id"])
    amount = int(params["amount"])
    reason = str(params.get("reason") or "")
    if amount == 0:
        raise ValueError("amount must be non-zero")
    if amount > 0:
        new_balance = await user_balance_store.add_balance(
            int(gid), user_id, amount)
        applied = amount
    else:
        taken = await user_balance_store.deduct_balance(
            int(gid), user_id, -amount)
        applied = -taken
        new_balance = await user_balance_store.get_balance(
            int(gid), user_id)
    # Аудит финансовой операции в admin_audit_log.
    try:
        from storage import admin_config_store as store
        await store.insert_audit(
            guild_id=int(gid), actor_id=int(actor or 0),
            action="economy.adjust", object_type="user_balance",
            object_id=str(user_id),
            new_value={"amount": applied, "requested": amount,
                       "reason": reason[:200],
                       "new_balance": new_balance})
    except Exception as exc:  # noqa: BLE001 — аудит не должен ронять операцию
        log.warning("economy.adjust audit write failed: %s", exc)
    return {"applied": applied, "new_balance": new_balance,
            "user_id": user_id}


# ------------------------------------------------------------------
# Tournaments: отмена через существующий state-machine бота
# ------------------------------------------------------------------

@register_action("tournament.cancel")
async def _h_tournament_cancel(gid, params, actor):
    bot = _require_bot()
    channel_id = int(params["channel_id"])
    guild = bot.get_guild(int(gid))
    if guild is None:
        raise RuntimeError(f"guild {gid} not available to bot")
    from models.tournament import TournamentPhase
    from storage.json_store import store as json_store
    tournament = json_store.get(int(gid), channel_id)
    if not tournament:
        raise RuntimeError("no active tournament in channel")
    if tournament.phase == TournamentPhase.COMPLETE:
        raise RuntimeError("tournament already COMPLETE — protected")
    from cogs.tournament import _perform_tournament_delete
    await _perform_tournament_delete(
        bot, guild, channel_id, tournament,
        actor_user_id=int(actor or 0))
    return {"cancelled": True, "channel_id": channel_id,
            "tournament_id": str(tournament.id)}


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
