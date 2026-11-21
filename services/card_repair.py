"""Диагностика и восстановление публичных карточек турниров.

Инцидент: служебные ответы room_view («Изменение отменено.»
и т.п.) перезаписывали главное сообщение турнира из-за
DEFERRED_MESSAGE_UPDATE на modal_submit (исправлено в
views/room_view.py — defer теперь thinking=True).

Здесь — read-only ``scan`` и контролируемый ``repair``:
- scan: для каждого турнира из JsonStore проверить, что
  message_id указывает на карточку турнира, а не на
  служебный текст. Ничего не меняет.
- repair: для подтверждённо повреждённых сообщений вызвать
  ``bot.update_tournament_message(..., force=True)`` —
  штатный renderer, то же message_id, без пересоздания
  турнира и без изменений данных.

MESSAGE_MISSING (сообщение удалено) — только репорт,
новое сообщение автоматически НЕ создаётся.
"""

from __future__ import annotations

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


async def _fetch_tournament_message(
    bot: discord.Client, tournament,
) -> tuple[str, discord.Message | None]:
    """(status, message). status: OK | DAMAGED | MESSAGE_MISSING | ERROR."""
    gid = int(getattr(tournament, "guild_id", 0) or 0)
    cid = int(getattr(tournament, "channel_id", 0) or 0)
    mid = int(getattr(tournament, "message_id", 0) or 0)
    if not (gid and cid and mid):
        return "ERROR:no_ids", None
    guild = bot.get_guild(gid)
    if guild is None:
        try:
            guild = await bot.fetch_guild(gid)
        except Exception:
            return "ERROR:no_guild", None
    channel = guild.get_channel(cid)
    if channel is None:
        try:
            channel = await guild.fetch_channel(cid)
        except Exception:
            return "ERROR:no_channel", None
    try:
        message = await channel.fetch_message(mid)
    except discord.NotFound:
        return "MESSAGE_MISSING", None
    except Exception as exc:
        return f"ERROR:fetch:{exc}", None
    reason = diagnose_message(message)
    if reason:
        return f"DAMAGED:{reason}", message
    return "OK", message


async def scan(bot: discord.Client) -> list[dict[str, Any]]:
    """Read-only диагностика всех активных турниров.

    Возвращает список записей:
    {tournament_id, guild_id, channel_id, message_id, phase,
     status, detail}.
    """
    report: list[dict[str, Any]] = []
    tournaments = list(tournament_store.all())
    for t in tournaments:
        tid = str(getattr(t, "id", "?"))
        phase = str(getattr(getattr(t, "phase", ""), "value", "?"))
        status, _msg = await _fetch_tournament_message(bot, t)
        report.append({
            "tournament_id": tid,
            "guild_id": int(getattr(t, "guild_id", 0) or 0),
            "channel_id": int(getattr(t, "channel_id", 0) or 0),
            "message_id": int(getattr(t, "message_id", 0) or 0),
            "phase": phase,
            "status": status.split(":")[0],
            "detail": status,
        })
        logger.info(
            "cardrepair scan: %s phase=%s status=%s",
            tid, phase, status,
        )
    return report


async def repair(bot: discord.Client) -> dict[str, Any]:
    """Восстановить подтверждённо повреждённые карточки.

    Только DAMAGED-статусы; MESSAGE_MISSING и ERROR — репорт.
    Идемпотентно: повторный запуск не создаёт дублей
    (force-edit того же message_id). Данные турнира не меняются.
    """
    report = await scan(bot)
    repaired: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for entry in report:
        tid = entry["tournament_id"]
        if entry["status"] != "DAMAGED":
            skipped.append(entry)
            continue
        tournament = next(
            (t for t in tournament_store.all()
             if str(t.id) == tid), None)
        if tournament is None:
            entry["detail"] = "tournament vanished"
            skipped.append(entry)
            continue
        guild = bot.get_guild(entry["guild_id"])
        if guild is None:
            try:
                guild = await bot.fetch_guild(entry["guild_id"])
            except Exception:
                entry["detail"] = "no guild"
                skipped.append(entry)
                continue
        ok = False
        try:
            ok = await bot.update_tournament_message(
                guild, tournament, force=True)
        except Exception as exc:
            logger.error("cardrepair repair %s failed: %s", tid, exc)
        entry["detail"] = "repaired" if ok else "update_failed"
        (repaired if ok else skipped).append(entry)
        logger.info("cardrepair repair: %s -> %s", tid, entry["detail"])
    return {
        "scanned": len(report),
        "damaged": sum(1 for e in report if e["status"] == "DAMAGED"),
        "missing": sum(
            1 for e in report if e["status"] == "MESSAGE_MISSING"),
        "ok": sum(1 for e in report if e["status"] == "OK"),
        "repaired": [e["tournament_id"] for e in repaired],
        "skipped": skipped,
    }


def format_report(result: dict[str, Any]) -> str:
    """Текстовый отчёт для ephemeral-ответа команды."""
    lines = [
        "**Card repair report**",
        f"Проверено турниров: {result['scanned']}",
        f"OK: {result['ok']} · Повреждено: {result['damaged']} "
        f"· Сообщение удалено: {result['missing']}",
    ]
    if result.get("repaired"):
        lines.append("Восстановлено: " + ", ".join(
            f"`{t}`" for t in result["repaired"]))
    for e in result.get("skipped", []):
        if e["status"] != "OK":
            lines.append(
                f"- `{e['tournament_id']}` "
                f"({e['phase']}): {e['detail']}")
    return "\n".join(lines)[:1900]
