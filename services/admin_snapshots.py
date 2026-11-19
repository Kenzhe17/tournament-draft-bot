"""Bot-state snapshots for the Control Center panel.

The panel runs in a separate process/container and cannot read the
bot's JSON tournament store or Discord guild objects. This module
publishes a periodic, read-only snapshot into admin_service_status
(service='bot_state') so the panel can render real data. Mutations
still go through admin_action_requests executed here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

SERVICE = "bot_state"


def _tournament_row(t) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "channel_id": t.channel_id,
        "size": getattr(t.size, "value", str(t.size)),
        "phase": getattr(t.phase, "value", str(t.phase)),
        "registration": getattr(t.registration, "value",
                                str(t.registration)),
        "formation_mode": getattr(t.formation_mode, "value",
                                  str(t.formation_mode)),
        "organizer_id": t.organizer_id,
        "is_test": t.is_test,
        "created_at": t.created_at,
        "players": {
            "captains": len(t.captains or []),
            "circle1": len(t.circle1 or []),
            "circle2": len(t.circle2 or []),
            "circle3": len(t.circle3 or []),
            "circle4": len(t.circle4 or []),
            "pool": len(t.players_pool or []),
        },
    }


def _mm_row(s) -> dict[str, Any]:
    return {
        "channel_id": s.channel_id,
        "format": getattr(s.format, "value", str(s.format)),
        "state": s.state,
        "players": len(s.players or []),
        "player_names": list(s.players or [])[:16],
        "required": getattr(s, "required_players", None),
        "timer_started": s.timer_started,
        "tournament_channel_id": s.tournament_channel_id,
        "match_num": s.match_num,
    }


def _guild_resources(guild) -> dict[str, Any]:
    channels = [
        {"id": str(c.id), "name": getattr(c, "name", ""),
         "type": str(getattr(c, "type", "")),
         "category_id": str(getattr(c, "category_id", 0) or 0),
         "position": getattr(c, "position", 0)}
        for c in getattr(guild, "text_channels", [])
    ]
    roles = [
        {"id": str(r.id), "name": r.name,
         "position": getattr(r, "position", 0),
         "managed": bool(getattr(r, "managed", False)),
         "mentionable": bool(getattr(r, "mentionable", False)),
         "permissions": int(getattr(
             getattr(r, "permissions", None), "value", 0) or 0)}
        for r in (getattr(guild, "roles", []) if guild else [])
        if r.name != "@everyone"
    ]
    return {"channels": channels, "roles": roles}


async def collect_snapshot(bot) -> dict[str, Any]:
    """Собрать read-only снимок состояния бота."""
    from services import vac_service
    from storage.json_store import store as json_store
    from storage.mm_search_store import mm_search_store

    tournaments: dict[str, list[dict[str, Any]]] = {}
    try:
        for t in json_store.all():
            tournaments.setdefault(str(t.guild_id), []).append(
                _tournament_row(t))
    except Exception as exc:  # noqa: BLE001
        log.warning("snapshot: tournaments failed: %s", exc)

    mm: dict[str, list[dict[str, Any]]] = {}
    try:
        for s in mm_search_store.all():
            mm.setdefault(str(s.guild_id), []).append(_mm_row(s))
    except Exception as exc:  # noqa: BLE001
        log.warning("snapshot: mm searches failed: %s", exc)

    guilds: dict[str, Any] = {}
    for g in getattr(bot, "guilds", []) or []:
        try:
            guilds[str(g.id)] = {
                "name": g.name,
                **_guild_resources(g),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("snapshot: guild %s failed: %s", g.id, exc)

    vac_active: dict[str, int] = {}
    for gid_s in tournaments.keys() | mm.keys():
        try:
            rows = await vac_service.list_active_vacs(int(gid_s))
            vac_active[gid_s] = len(rows)
        except Exception:  # noqa: BLE001
            vac_active[gid_s] = -1

    return {
        "tournaments": tournaments,
        "matchmaking": mm,
        "vac_active_count": vac_active,
        "guilds": guilds,
    }


async def publish_snapshot(bot) -> None:
    try:
        from storage import admin_config_store as store
        snap = await collect_snapshot(bot)
        await store.report_service_status(
            SERVICE,
            version_info={"kind": "state_snapshot"},
            extra=snap,
        )
    except Exception as exc:  # noqa: BLE001 — snapshot не критичен
        log.warning("snapshot publish failed: %s", exc)


async def snapshot_loop(bot, interval: float = 60.0) -> None:
    """Периодическая публикация снимка состояния."""
    while True:
        try:
            await publish_snapshot(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("snapshot loop error")
        await asyncio.sleep(interval)
