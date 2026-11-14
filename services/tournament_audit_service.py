"""Сервис аудита турниров — единая точка записи событий.

Все обработчики (setup/matches/match_stats/room/organizer/cogs/MM)
вызывают ``audit.record(...)`` — логика создания событий не
дублируется.

Гарантии:
- событие пишется в PostgreSQL ``tournament_audit_log``;
- при недоступности БД событие уходит в ``audit_pending.json``
  (status=pending) и повторяется через ``flush_pending()`` —
  «тихой» потери записи нет;
- ``dedup_key`` исключает дубли при двойном нажатии/повторной
  доставке Interaction/рестарте;
- before/after проходят через ``_sanitize`` — только скаляры и
  плоские структуры, ключи password/token/secret отбрасываются;
- после успешной записи событие может публиковаться компактной
  CV2-карточкой в канал ``TOURNAMENT_AUDIT_CHANNEL_ID`` —
  идемпотентно по ``discord_message_id``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any

import discord

from config import DATA_DIR
from storage.json_atomic import atomic_write_json, load_json_file
from storage import tournament_audit_store as audit_store

logger = logging.getLogger(__name__)

PENDING_FILE = DATA_DIR / "audit_pending.json"

# Ключи, которые никогда не попадают в журнал.
_FORBIDDEN_KEY = re.compile(
    r"(?i)(password|passwd|pwd|token|secret|env|authorization|cookie)"
)
_MAX_DEPTH = 4
_MAX_ITEMS = 50

# Действия (action) — единый словарь для фильтров и форматирования.
class A:
    # Турнир
    TOURNAMENT_CREATE = "tournament.create"
    TOURNAMENT_DELETE = "tournament.delete"
    TOURNAMENT_ARCHIVED = "tournament.archived"
    TOURNAMENT_START = "tournament.start"
    TOURNAMENT_NEXT_PHASE = "tournament.next_phase"
    TOURNAMENT_CANCEL = "tournament.cancel"
    # Участники
    PLAYER_JOIN = "player.join"
    PLAYER_ADD = "player.add"
    PLAYER_REMOVE = "player.remove"
    PLAYER_MOVE = "player.move"
    PLAYER_SWAP = "player.swap"
    PLAYER_EXIT = "player.exit"
    REGISTRATION_TOGGLE = "registration.toggle"
    LIMIT_CHANGE = "limit.change"
    AUTO_DISTRIBUTE = "auto_distribute.run"
    DRAFT_START = "draft.start"
    DRAFT_PICK = "draft.pick"
    # Матчи
    MATCH_START = "match.start"
    MATCH_GENERATE = "match.generate"
    KD_CAPTAIN_SUBMIT = "match.kd_captain"
    KD_ADMIN_SUBMIT = "match.kd_admin"
    KD_ADMIN_EDIT = "match.kd_edit"
    WINNER_SELECT = "match.winner_select"
    WINNER_CHANGE = "match.winner_change"
    RESULT_CONFIRM = "match.result_confirm"
    FINAL_CONFIRM = "final.confirm"
    RESULT_RESET = "match.result_reset"
    EARNINGS_AWARDED = "earnings.awarded"
    # Комнаты
    ROOM_SUBMIT = "room.submit"
    ROOM_UPDATE = "room.update"
    # Организаторы
    ORG_ASSIGN = "organizer.assign"
    ORG_REMOVE = "organizer.remove"
    ORG_TRANSFER = "organizer.transfer"
    ORG_ROLE_GRANT = "organizer.role_grant"
    ORG_ROLE_REVOKE = "organizer.role_revoke"
    ORG_RESET = "organizer.reset"
    # MM
    MM_MATCH_START = "mm.match_start"
    MM_KD_SUBMIT = "mm.kd_submit"
    MM_RESULT_CONFIRM = "mm.result_confirm"


# ---------------------------------------------------------------------------
# Санитизация before/after
# ---------------------------------------------------------------------------


def _sanitize(value: Any, depth: int = 0) -> Any:
    """Рекурсивно оставить только JSON-безопасные скаляры.

    Словари с запрещёнными ключами (password/token/…) вырезаются
    целиком; строки усекаются; вложенность и размер ограничены.
    """
    if depth > _MAX_DEPTH:
        return "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_ITEMS:
                out["…"] = "truncated"
                break
            key = str(k)
            if _FORBIDDEN_KEY.search(key):
                continue
            out[key] = _sanitize(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:_MAX_ITEMS]
        return [_sanitize(v, depth + 1) for v in items]
    # Discord-объекты и прочее — только безопасное строковое представление
    # для id-подобных полей; иначе отбрасываем.
    return None


# ---------------------------------------------------------------------------
# Pending-файл (fallback при недоступности PostgreSQL)
# ---------------------------------------------------------------------------


def _load_pending() -> list[dict[str, Any]]:
    raw = load_json_file(PENDING_FILE)
    return raw if isinstance(raw, list) else []


def _save_pending(items: list[dict[str, Any]]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_json(PENDING_FILE, items)
    except Exception as exc:  # pragma: no cover - диск недоступен
        logger.error("audit: не удалось сохранить pending-файл: %s", exc)


def _append_pending(event: dict[str, Any]) -> None:
    items = _load_pending()
    items.append(event)
    _save_pending(items)


# ---------------------------------------------------------------------------
# Публикация в TOURNAMENT_AUDIT_CHANNEL_ID
# ---------------------------------------------------------------------------

_bot: discord.Client | None = None


def bind_bot(bot: discord.Client) -> None:
    """Привязать клиента бота для публикации карточек событий."""
    global _bot
    _bot = bot


def _audit_channel_id() -> int:
    try:
        from config import TOURNAMENT_AUDIT_CHANNEL_ID
        return int(TOURNAMENT_AUDIT_CHANNEL_ID or 0)
    except Exception:
        return 0


_ACTION_TITLES = {
    A.TOURNAMENT_CREATE: "ТУРНИР СОЗДАН",
    A.TOURNAMENT_DELETE: "ТУРНИР УДАЛЁН",
    A.TOURNAMENT_ARCHIVED: "ТУРНИР АРХИВИРОВАН",
    A.TOURNAMENT_START: "ТУРНИР ЗАПУЩЕН",
    A.TOURNAMENT_NEXT_PHASE: "ЭТАП ПЕРЕЙДЕН",
    A.TOURNAMENT_CANCEL: "ТУРНИР ОТМЕНЁН",
    A.PLAYER_JOIN: "УЧАСТНИК ВСТУПИЛ",
    A.PLAYER_ADD: "УЧАСТНИК ДОБАВЛЕН",
    A.PLAYER_REMOVE: "УЧАСТНИК УДАЛЁН",
    A.PLAYER_MOVE: "УЧАСТНИК ПЕРЕМЕЩЁН",
    A.PLAYER_SWAP: "ОБМЕН УЧАСТНИКОВ",
    A.PLAYER_EXIT: "УЧАСТНИК ВЫШЕЛ",
    A.REGISTRATION_TOGGLE: "РЕГИСТРАЦИЯ ИЗМЕНЕНА",
    A.LIMIT_CHANGE: "ЛИМИТ ИЗМЕНЁН",
    A.AUTO_DISTRIBUTE: "АВТОРАСПРЕДЕЛЕНИЕ",
    A.DRAFT_START: "ДРАФТ ЗАПУЩЕН",
    A.DRAFT_PICK: "ВЫБОР ИГРОКА КАПИТАНОМ",
    A.MATCH_START: "МАТЧ НАЧАТ",
    A.MATCH_GENERATE: "МАТЧИ СГЕНЕРИРОВАНЫ",
    A.KD_CAPTAIN_SUBMIT: "K/D ОТПРАВЛЕН",
    A.KD_ADMIN_SUBMIT: "K/D ЗАПОЛНЕН",
    A.KD_ADMIN_EDIT: "СТАТИСТИКА ИСПРАВЛЕНА",
    A.WINNER_SELECT: "ПОБЕДИТЕЛЬ ВЫБРАН",
    A.WINNER_CHANGE: "РЕЗУЛЬТАТ ИЗМЕНЁН",
    A.RESULT_CONFIRM: "РЕЗУЛЬТАТ ПОДТВЕРЖДЁН",
    A.FINAL_CONFIRM: "ФИНАЛ ПОДТВЕРЖДЁН",
    A.RESULT_RESET: "РЕЗУЛЬТАТ СБРОШЕН",
    A.EARNINGS_AWARDED: "НАГРАДЫ НАЧИСЛЕНЫ",
    A.ROOM_SUBMIT: "ДАННЫЕ КОМНАТЫ ОТПРАВЛЕНЫ",
    A.ROOM_UPDATE: "ДАННЫЕ КОМНАТЫ ИЗМЕНЕНЫ",
    A.ORG_ASSIGN: "ОРГАНИЗАТОР НАЗНАЧЕН",
    A.ORG_REMOVE: "ОРГАНИЗАТОР СНЯТ",
    A.ORG_TRANSFER: "ОРГАНИЗАТОР ПЕРЕДАН",
    A.ORG_ROLE_GRANT: "ORG-РОЛЬ ВЫДАНА",
    A.ORG_ROLE_REVOKE: "ORG-РОЛЬ СНЯТА",
    A.ORG_RESET: "ОРГ РОЛИ СБРОШЕНЫ",
    A.MM_MATCH_START: "MM-МАТЧ НАЧАТ",
    A.MM_KD_SUBMIT: "MM K/D ОТПРАВЛЕН",
    A.MM_RESULT_CONFIRM: "MM РЕЗУЛЬТАТ ПОДТВЕРЖДЁН",
}

_SOURCE_LABELS = {
    "tournament": "Турнир",
    "mm": "MM",
    "room": "Комната",
    "organizer": "Организатор",
    "system": "Система",
}


def _fmt_dt(dt) -> str:
    try:
        import datetime as _dt
        if dt is None:
            return "—"
        if isinstance(dt, (int, float)):
            dt = _dt.datetime.fromtimestamp(float(dt), tz=_dt.timezone.utc)
        elif isinstance(dt, str):
            dt = _dt.datetime.fromisoformat(dt.replace("Z", "+00:00"))
        return discord.utils.format_dt(dt, style="t")
    except Exception:
        return "—"


def _actor_text(ev: dict[str, Any]) -> str:
    uid = ev.get("actor_user_id")
    if uid:
        return f"<@{uid}>"
    return "система"


def build_event_card(ev: dict[str, Any]) -> discord.ui.LayoutView:
    """Компактная CV2-карточка события для канала журнала."""
    title = _ACTION_TITLES.get(ev.get("action"), ev.get("action", "?"))
    lines = [f"### {title}"]
    tid = ev.get("tournament_id")
    if tid:
        lines.append(f"**Турнир:** `{tid}`")
    mid = ev.get("match_id")
    if mid:
        lines.append(f"**Матч:** `{mid}`")
    lines.append(f"**Инициатор:** {_actor_text(ev)}")
    tgt = ev.get("target_user_id")
    if tgt:
        lines.append(f"**Участник:** <@{tgt}>")
    # Изменение до/после — повторно санитизируем: события из
    # pending-файла или старых записей могли попасть в БД до
    # появления фильтра секретов.
    before = _sanitize(ev.get("before_data"))
    after = _sanitize(ev.get("after_data"))
    diff_bits = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in after:
            if key in ("…",):
                continue
            b, a = before.get(key), after.get(key)
            if b != a:
                diff_bits.append(f"{key}: `{b}` → `{a}`")
    elif after:
        diff_bits.append(f"`{json.dumps(after, ensure_ascii=False)[:200]}`")
    if diff_bits:
        lines.append("**Изменение:** " + "; ".join(diff_bits[:4]))
    lines.append(f"-# {_fmt_dt(ev.get('created_at'))} · {_SOURCE_LABELS.get(ev.get('source'), ev.get('source'))}")

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Colour.blurple())
    for i, line in enumerate(lines):
        if i:
            container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(content=line))
    view.add_item(container)
    return view


async def _publish(ev: dict[str, Any]) -> None:
    """Отправить карточку события в TOURNAMENT_AUDIT_CHANNEL_ID.

    Идемпотентно: discord_message_id проставляется один раз;
    при ошибке отправки запись остаётся в БД и будет повторена
    через unpublished_events/flush.
    """
    channel_id = _audit_channel_id()
    if not channel_id or _bot is None:
        return
    channel = _bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await _bot.fetch_channel(channel_id)
        except Exception as exc:
            logger.warning("audit: канал журнала недоступен: %s", exc)
            return
    try:
        message = await channel.send(
            view=build_event_card(ev),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception as exc:
        logger.warning("audit: публикация события не удалась: %s", exc)
        return
    try:
        from storage.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await audit_store.mark_discord_posted(
                conn, ev["event_id"], message.id)
    except Exception as exc:
        logger.warning("audit: не удалось пометить публикацию: %s", exc)


async def republish_unpublished(limit: int = 50) -> int:
    """Повторить публикацию событий без discord_message_id."""
    if _audit_channel_id() == 0 or _bot is None:
        return 0
    try:
        events = await audit_store.unpublished_events(limit)
    except Exception as exc:
        logger.warning("audit: не удалось получить неопубликованные: %s", exc)
        return 0
    sent = 0
    for ev in events:
        await _publish(ev)
        sent += 1
    return sent


# ---------------------------------------------------------------------------
# Основная точка записи
# ---------------------------------------------------------------------------


async def record(
    *,
    guild_id: int,
    channel_id: int,
    tournament_id: str | None = None,
    match_id: str | None = None,
    actor_user_id: int | None = None,
    target_user_id: int | None = None,
    action: str,
    source: str = "tournament",
    before: Any = None,
    after: Any = None,
    status: str = "success",
    interaction_id: str | None = None,
    dedup_key: str | None = None,
) -> dict[str, Any] | None:
    """Записать событие аудита.

    Возвращает dict события при успешной записи в PG, None —
    при дедупликации. При недоступности БД событие сохраняется
    в pending-файл и возвращается с ``persisted="pending"``.
    Никогда не бросает исключений в вызывающий код — аудит не
    должен ломать пользовательские сценарии, но «тихой» потери
    нет: pending-файл — это след.
    """
    event = {
        "event_id": uuid.uuid4().hex,
        "guild_id": int(guild_id),
        "channel_id": int(channel_id),
        "tournament_id": str(tournament_id) if tournament_id else None,
        "match_id": str(match_id) if match_id else None,
        "actor_user_id": int(actor_user_id) if actor_user_id else None,
        "target_user_id": int(target_user_id) if target_user_id else None,
        "action": action,
        "source": source,
        "before_data": _sanitize(before) if isinstance(before, (dict, list)) else (
            {"value": _sanitize(before)} if before is not None else None),
        "after_data": _sanitize(after) if isinstance(after, (dict, list)) else (
            {"value": _sanitize(after)} if after is not None else None),
        "status": status,
        "interaction_id": str(interaction_id) if interaction_id else None,
        "dedup_key": dedup_key,
        "created_at": time.time(),
    }

    try:
        inserted = await audit_store.add_event(**{
            k: v for k, v in event.items()
            if k not in ("created_at",)
        })
    except Exception as exc:
        logger.error(
            "audit: PG недоступна, событие %s в pending-файл: %s",
            action, exc,
        )
        _append_pending(event)
        event["persisted"] = "pending"
        return event

    if not inserted:
        # Дедупликация — событие уже записано ранее.
        logger.debug("audit: дубликат события %s (%s)", action, dedup_key)
        return None

    event["persisted"] = "pg"
    # Публикация — best-effort, не блокирует сценарий.
    asyncio.create_task(_safe_publish(event))
    return event


async def _safe_publish(ev: dict[str, Any]) -> None:
    try:
        await _publish(ev)
    except Exception as exc:
        logger.warning("audit: publish failed: %s", exc)


# ---------------------------------------------------------------------------
# Восстановление pending после рестарта/сбоя БД
# ---------------------------------------------------------------------------


async def flush_pending(limit: int = 200) -> int:
    """Повторить запись событий из pending-файла в PostgreSQL.

    Вызывается при старте бота и периодически. События со статусом
    pending вставляются как есть (операция уже выполнена до сбоя
    БД — статус отражает исход действия, а не факт записи).
    """
    items = _load_pending()
    if not items:
        return 0
    flushed = 0
    remaining: list[dict[str, Any]] = []
    for ev in items[:limit]:
        try:
            await audit_store.add_event(
                event_id=ev["event_id"],
                guild_id=ev["guild_id"],
                channel_id=ev["channel_id"],
                tournament_id=ev.get("tournament_id"),
                match_id=ev.get("match_id"),
                actor_user_id=ev.get("actor_user_id"),
                target_user_id=ev.get("target_user_id"),
                action=ev["action"],
                source=ev.get("source", "tournament"),
                before_data=ev.get("before_data"),
                after_data=ev.get("after_data"),
                status=ev.get("status", "pending"),
                interaction_id=ev.get("interaction_id"),
                dedup_key=ev.get("dedup_key"),
            )
            flushed += 1
            asyncio.create_task(_safe_publish(ev))
        except Exception:
            remaining.append(ev)
    leftover = items[limit:] + remaining
    if leftover:
        _save_pending(leftover)
    else:
        _save_pending([])
    if flushed:
        logger.info("audit: flush_pending записал %d событий", flushed)
    return flushed


# ---------------------------------------------------------------------------
# Удобные обёртки для Interaction
# ---------------------------------------------------------------------------


def interaction_ids(interaction: discord.Interaction) -> tuple[str | None, int | None]:
    """(interaction_id, user_id) из Interaction для аудита."""
    try:
        return str(interaction.id), interaction.user.id
    except Exception:
        return None, None


async def record_interaction(
    interaction: discord.Interaction,
    *,
    channel_id: int | None = None,
    tournament_id: str | None = None,
    match_id: str | None = None,
    target_user_id: int | None = None,
    action: str,
    source: str = "tournament",
    before: Any = None,
    after: Any = None,
    status: str = "success",
    dedup_key: str | None = None,
) -> dict[str, Any] | None:
    """record() с автоматическим извлечением guild/channel/actor."""
    iid, uid = interaction_ids(interaction)
    return await record(
        guild_id=interaction.guild_id or 0,
        channel_id=channel_id or interaction.channel_id or 0,
        tournament_id=tournament_id,
        match_id=match_id,
        actor_user_id=uid,
        target_user_id=target_user_id,
        action=action,
        source=source,
        before=before,
        after=after,
        status=status,
        interaction_id=iid,
        dedup_key=dedup_key or (f"{iid}:{action}" if iid else None),
    )
