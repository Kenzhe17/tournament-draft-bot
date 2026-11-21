"""Диагностика и восстановление публичных карточек турниров.

Инцидент: служебные ответы room_view («Изменение отменено.»
и т.п.) перезаписывали главное сообщение турнира из-за
DEFERRED_MESSAGE_UPDATE на modal_submit (исправлено в
views/room_view.py — defer теперь thinking=True).

Здесь — read-only ``scan`` и контролируемый ``repair``:
- scan: для каждого турнира из JsonStore и активного Cash Cup
  (PostgreSQL) проверить, что message_id указывает на карточку
  турнира, а не на служебный текст. Ничего не меняет.
- repair: для подтверждённо повреждённых сообщений вызвать
  штатный renderer (``update_tournament_message(force=True)``
  для classic-турниров, ``CashCupCog._refresh`` для Cash Cup) —
  то же message_id, без пересоздания турнира и без изменений
  данных. Каждый запуск пишется в аудит ``card_repair_runs``.

Статусы scan: HEALTHY | DAMAGED | MESSAGE_MISSING | FORBIDDEN |
UNKNOWN | ERROR. Недоступное или непроверенное сообщение никогда
не считается исправным.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import discord

from storage.json_store import store as tournament_store

logger = logging.getLogger(__name__)

# Служебные тексты, которые room_view мог записать в
# публичную карточку (дефектный defer без thinking=True).
SERVICE_MARKERS = (
    "Изменение отменено",
    "Данные комнаты сохранены для",
    "Турнир не найден",
    "Нет прав на изменение этой комнаты",
    "Сессия подтверждения истекла",
    "Турнир больше не активен",
)

# Итоговые статусы scan (detail может содержать ":причину").
HEALTHY = "HEALTHY"
DAMAGED = "DAMAGED"
MESSAGE_MISSING = "MESSAGE_MISSING"
FORBIDDEN = "FORBIDDEN"
UNKNOWN = "UNKNOWN"
ERROR = "ERROR"


# ---------------------------------------------------------------------------
# Диагностика сообщения
# ---------------------------------------------------------------------------


def _message_text(message: discord.Message) -> str:
    """Весь видимый текст сообщения, включая CV2-компоненты."""
    parts: list[str] = [message.content or ""]

    def _walk(item: Any) -> None:
        c = getattr(item, "content", None)
        if isinstance(c, str):
            parts.append(c)
        for ch in getattr(item, "children", []) or []:
            _walk(ch)

    for comp in getattr(message, "components", []) or []:
        _walk(comp)
    return "\n".join(parts)


def diagnose_message(message: discord.Message) -> str | None:
    """None — карточка в порядке; иначе причина повреждения."""
    text = _message_text(message)
    for marker in SERVICE_MARKERS:
        if marker in text:
            return f"SERVICE_TEXT:{marker}"
    return None


async def _fetch_message(
    bot: discord.Client, guild_id: int, channel_id: int, message_id: int,
) -> tuple[str, discord.Message | None]:
    """(status, message). Только читаем, ничего не меняем."""
    if not (guild_id and channel_id and message_id):
        return f"{ERROR}:no_ids", None
    guild = bot.get_guild(guild_id)
    if guild is None:
        try:
            guild = await bot.fetch_guild(guild_id)
        except discord.Forbidden:
            return f"{FORBIDDEN}:guild", None
        except Exception:
            return f"{UNKNOWN}:no_guild", None
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.Forbidden:
            return f"{FORBIDDEN}:channel", None
        except Exception:
            return f"{UNKNOWN}:no_channel", None
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        return MESSAGE_MISSING, None
    except discord.Forbidden:
        return f"{FORBIDDEN}:message", None
    except Exception as exc:
        return f"{UNKNOWN}:fetch:{exc}", None
    reason = diagnose_message(message)
    if reason:
        return f"{DAMAGED}:{reason}", message
    return HEALTHY, message


# ---------------------------------------------------------------------------
# Scan: classic-турниры (JsonStore) + Cash Cup (PostgreSQL)
# ---------------------------------------------------------------------------


async def _scan_classic(bot: discord.Client) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for t in tournament_store.all():
        tid = str(getattr(t, "id", "?"))
        phase = str(getattr(getattr(t, "phase", ""), "value", "?"))
        gid = int(getattr(t, "guild_id", 0) or 0)
        cid = int(getattr(t, "channel_id", 0) or 0)
        mid = int(getattr(t, "message_id", 0) or 0)
        status, _msg = await _fetch_message(bot, gid, cid, mid)
        report.append({
            "kind": "tournament",
            "tournament_id": tid,
            "guild_id": gid,
            "channel_id": cid,
            "message_id": mid,
            "phase": phase,
            "status": status.split(":")[0],
            "detail": status,
        })
        logger.info("cardrepair scan: tournament %s phase=%s status=%s",
                    tid, phase, status)
    return report


async def _scan_cashcup(bot: discord.Client) -> list[dict[str, Any]]:
    """Активный Cash Cup каждой гильдии — read-only через PG."""
    report: list[dict[str, Any]] = []
    from services.cashcup_store import cashcup_store
    for guild in list(getattr(bot, "guilds", []) or []):
        gid = int(guild.id)
        try:
            state = await cashcup_store.active(gid)
        except Exception as exc:
            # PG недоступна — фиксируем UNKNOWN, не считаем OK.
            report.append({
                "kind": "cashcup",
                "tournament_id": f"cashcup:{gid}",
                "guild_id": gid,
                "channel_id": 0,
                "message_id": 0,
                "phase": "pg_error",
                "status": UNKNOWN,
                "detail": f"{UNKNOWN}:pg:{exc}",
            })
            continue
        if not state:
            continue
        tid = str(state.get("tournament_id") or state.get("id") or "?")
        cid = int(state.get("channel_id") or 0)
        mid = int(state.get("message_id") or 0)
        status, _msg = await _fetch_message(bot, gid, cid, mid)
        report.append({
            "kind": "cashcup",
            "tournament_id": f"cashcup:{tid}",
            "guild_id": gid,
            "channel_id": cid,
            "message_id": mid,
            "phase": str(state.get("status") or "?"),
            "status": status.split(":")[0],
            "detail": status,
        })
        logger.info("cardrepair scan: cashcup %s status=%s", tid, status)
    return report


async def scan(bot: discord.Client) -> list[dict[str, Any]]:
    """Read-only диагностика всех актуальных карточек.

    Возвращает список записей:
    {kind, tournament_id, guild_id, channel_id, message_id, phase,
     status, detail}.
    """
    report = await _scan_classic(bot)
    try:
        report += await _scan_cashcup(bot)
    except Exception as exc:
        logger.warning("cardrepair cashcup scan failed: %s", exc)
    return report


# ---------------------------------------------------------------------------
# Repair: только подтверждённый DAMAGED, штатный renderer
# ---------------------------------------------------------------------------


async def _repair_classic(bot: discord.Client, entry: dict) -> bool:
    tid = entry["tournament_id"]
    tournament = next(
        (t for t in tournament_store.all()
         if str(t.id) == tid), None)
    if tournament is None:
        entry["detail"] = "tournament vanished"
        return False
    guild = bot.get_guild(entry["guild_id"])
    if guild is None:
        try:
            guild = await bot.fetch_guild(entry["guild_id"])
        except Exception:
            entry["detail"] = "no guild"
            return False
    try:
        return bool(await bot.update_tournament_message(
            guild, tournament, force=True))
    except Exception as exc:
        logger.error("cardrepair repair %s failed: %s", tid, exc)
        entry["detail"] = f"update_failed:{exc}"
        return False


async def _repair_cashcup(bot: discord.Client, entry: dict) -> bool:
    """Cash Cup — штатный refresh панели через cog."""
    gid = entry["guild_id"]
    cog = bot.get_cog("CashCupCog")
    if cog is None:
        entry["detail"] = "cashcup cog unavailable"
        return False
    try:
        from services.cashcup_store import cashcup_store
        state = await cashcup_store.active(gid) \
            or await cashcup_store.latest(gid)
        if not state:
            entry["detail"] = "no state"
            return False
        await cog._refresh(gid, state)
        return True
    except Exception as exc:
        logger.error("cardrepair cashcup repair %s failed: %s",
                     entry["tournament_id"], exc)
        entry["detail"] = f"update_failed:{exc}"
        return False


async def _audit_run(bot: discord.Client, result: dict[str, Any]) -> None:
    """Аудит запуска repair в PostgreSQL (card_repair_runs)."""
    try:
        from storage.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS card_repair_runs ("
                " id BIGSERIAL PRIMARY KEY,"
                " ran_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " scanned INT NOT NULL, damaged INT NOT NULL,"
                " repaired JSONB NOT NULL DEFAULT '[]'::jsonb,"
                " skipped JSONB NOT NULL DEFAULT '[]'::jsonb)")
            await conn.execute(
                "INSERT INTO card_repair_runs"
                " (scanned, damaged, repaired, skipped)"
                " VALUES ($1,$2,$3::jsonb,$4::jsonb)",
                result["scanned"], result["damaged"],
                json.dumps(result["repaired"]),
                json.dumps([
                    {"tournament_id": e["tournament_id"],
                     "detail": e["detail"],
                     "status": e["status"]}
                    for e in result["skipped"]
                ]))
    except Exception as exc:
        # Аудит не должен ломать repair — только лог.
        logger.warning("cardrepair audit write failed: %s", exc)


async def repair(bot: discord.Client) -> dict[str, Any]:
    """Восстановить подтверждённо повреждённые карточки.

    Только DAMAGED-статусы; MESSAGE_MISSING/FORBIDDEN/UNKNOWN/ERROR —
    репорт. Идемпотентно: повторный запуск не создаёт дублей
    (force-edit того же message_id). Данные турнира не меняются.
    """
    report = await scan(bot)
    repaired: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for entry in report:
        if entry["status"] != DAMAGED:
            skipped.append(entry)
            continue
        if entry["kind"] == "cashcup":
            ok = await _repair_cashcup(bot, entry)
        else:
            ok = await _repair_classic(bot, entry)
        entry["detail"] = "repaired" if ok else entry["detail"]
        (repaired if ok else skipped).append(entry)
        logger.info("cardrepair repair: %s -> %s",
                    entry["tournament_id"], entry["detail"])
    result = {
        "scanned": len(report),
        "damaged": sum(1 for e in report if e["status"] == DAMAGED),
        "missing": sum(
            1 for e in report if e["status"] == MESSAGE_MISSING),
        "forbidden": sum(
            1 for e in report if e["status"] == FORBIDDEN),
        "unknown": sum(
            1 for e in report if e["status"] == UNKNOWN),
        "healthy": sum(1 for e in report if e["status"] == HEALTHY),
        "repaired": [e["tournament_id"] for e in repaired],
        "skipped": skipped,
    }
    await _audit_run(bot, result)
    return result


def format_report(result: dict[str, Any]) -> str:
    """Текстовый отчёт для ephemeral-ответа команды."""
    lines = [
        "**Card repair report**",
        f"Проверено карточек: {result['scanned']}",
        f"HEALTHY: {result['healthy']} · "
        f"Повреждено: {result['damaged']} · "
        f"MESSAGE_MISSING: {result['missing']} · "
        f"FORBIDDEN: {result['forbidden']} · "
        f"UNKNOWN: {result['unknown']}",
    ]
    if result.get("repaired"):
        lines.append("Восстановлено: " + ", ".join(
            f"`{t}`" for t in result["repaired"]))
    for e in result.get("skipped", []):
        if e["status"] not in (HEALTHY,):
            lines.append(
                f"- `{e['tournament_id']}` "
                f"({e['kind']}/{e['phase']}): {e['detail']}")
    return "\n".join(lines)[:1900]
