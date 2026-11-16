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
    """Атомарная корректировка: баланс + admin_finance_ops в одной
    транзакции; op_id = action request id → защита от повторов."""
    from storage.user_balance_store import user_balance_store
    user_id = int(params["user_id"])
    amount = int(params["amount"])
    reason = str(params.get("reason") or "")
    if amount == 0:
        raise ValueError("amount must be non-zero")
    op_id = str(params.get("_action_id") or "") or None
    res = await user_balance_store.admin_adjust(
        int(gid), user_id, amount,
        op_id or f"manual:{gid}:{user_id}:{amount}:{actor}",
        actor_id=int(actor or 0), reason=reason)
    try:
        from storage import admin_config_store as store
        await store.insert_audit(
            guild_id=int(gid), actor_id=int(actor or 0),
            action="economy.adjust", object_type="user_balance",
            object_id=str(user_id),
            new_value={"status": res["status"],
                       "applied": res["applied"],
                       "requested": amount,
                       "reason": reason[:200],
                       "new_balance": res["new_balance"]})
    except Exception as exc:  # noqa: BLE001
        log.warning("economy.adjust audit write failed: %s", exc)
    return {"user_id": user_id, **res}


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
            params = dict(row.get("parameters") or {})
            params["_action_id"] = str(row.get("id") or "")
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


# ------------------------------------------------------------------
# Players: корректировка ELO через player_stats_store (аудит old→new)
# ------------------------------------------------------------------

@register_action("player.adjust_elo")
async def _h_player_elo(gid, params, actor):
    from storage.player_stats_store import player_stats_store
    user_id = int(params["user_id"])
    new_elo = int(params["elo"])
    reason = str(params.get("reason") or "")
    stats = await player_stats_store.get(int(gid), user_id)
    old_elo = getattr(stats, "elo", None) if stats else None
    name = getattr(stats, "name", None) or str(user_id)
    await player_stats_store.update_player(
        int(gid), user_id, name, set_elo=new_elo)
    try:
        from storage import admin_config_store as store
        await store.insert_audit(
            guild_id=int(gid), actor_id=int(actor or 0),
            action="player.adjust_elo", object_type="player_stats",
            object_id=str(user_id),
            old_value={"elo": old_elo},
            new_value={"elo": new_elo, "reason": reason[:200]})
    except Exception as exc:  # noqa: BLE001
        log.warning("player.adjust_elo audit failed: %s", exc)
    return {"user_id": user_id, "old_elo": old_elo, "new_elo": new_elo}


# ------------------------------------------------------------------
# Tournaments: регистрация open/close через json_store state-machine
# ------------------------------------------------------------------

@register_action("tournament.set_registration")
async def _h_t_registration(gid, params, actor):
    channel_id = int(params["channel_id"])
    state = str(params["state"]).upper()  # OPEN | CLOSED
    if state not in ("OPEN", "CLOSED"):
        raise ValueError("state must be OPEN or CLOSED")
    from models.tournament import RegistrationState, TournamentPhase
    from storage.json_store import store as json_store
    t = json_store.get(int(gid), channel_id)
    if not t:
        raise RuntimeError("no active tournament in channel")
    # Регистрацию можно менять только до старта матчей.
    if t.phase not in (TournamentPhase.SETUP,
                       TournamentPhase.RULES_VOTING,
                       TournamentPhase.DRAFT,
                       TournamentPhase.TEAMS):
        raise RuntimeError(
            f"registration locked in phase {t.phase.value}")
    t.registration = (RegistrationState.OPEN if state == "OPEN"
                      else RegistrationState.CLOSED)
    json_store.set(t)
    return {"channel_id": channel_id, "registration": state,
            "phase": t.phase.value}


# ------------------------------------------------------------------
# Design: публикация CV2-шаблона в канал через bot
# ------------------------------------------------------------------

@register_action("design.publish")
async def _h_design_publish(gid, params, actor):
    bot = _require_bot()
    key = str(params["template_key"])
    channel_id = int(params["channel_id"])
    guild = bot.get_guild(int(gid))
    if guild is None:
        raise RuntimeError(f"guild {gid} not available")
    from storage import admin_config_store as store
    payload = await store.get_template(int(gid), key) or         await store.get_template(0, key)
    if not payload:
        raise RuntimeError(f"template {key} not found")
    from services.cv2_templates import render_template
    view = render_template(payload["payload"])
    channel = guild.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    msg = await channel.send(view=view)
    return {"published": True, "template": key,
            "channel_id": channel_id, "message_id": msg.id}
