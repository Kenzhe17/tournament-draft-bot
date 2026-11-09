"""Публичный журнал репутации — канал «репутация-логи».

- Канал создаётся один раз (или берётся по сохранённому ID из
  bot_settings / REP_LOG_CHANNEL_ID), с read-only правами для @everyone.
- Публикация идёт через rep_log_outbox: события ставятся в очередь в той
  же транзакции, что и начисление REP, а здесь только отправляются
  Components V2-карточки. Ошибка отправки не отменяет REP — событие
  остаётся pending и повторяется на следующем sweep.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import discord

from config import REP_LOG_CHANNEL_ID, REP_LOG_CHANNEL_NAME
from storage.reputation_store import (
    SRC_ADMIN,
    SRC_BOOST,
    SRC_INVITE,
    SRC_MANUAL,
    SRC_SERVER_TAG,
    reputation_store,
)
from utils.design import colors
from utils.design.layouts import DesignView

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

_SETTING_KEY = "rep_log_channel_id"
_MAX_ATTEMPTS_LOG = 50  # после стольких неудач пишем error и оставляем pending


# ---------------------------------------------------------------------------
# Канал
# ---------------------------------------------------------------------------

async def _resolve_channel_id(guild_id: int) -> int:
    """ID канала: bot_settings → env-дефолт."""
    saved = await reputation_store.get_setting(guild_id, _SETTING_KEY)
    if saved:
        try:
            return int(saved)
        except (TypeError, ValueError):
            pass
    return REP_LOG_CHANNEL_ID


async def _save_channel_id(guild_id: int, channel_id: int) -> None:
    try:
        await reputation_store.set_setting(
            guild_id, _SETTING_KEY, str(channel_id)
        )
    except Exception as exc:
        logger.warning("Failed to save rep log channel id: %s", exc)


def _build_overwrites(guild: discord.Guild) -> dict:
    """Права канала: все читают, никто не пишет; бот пишет."""
    me = guild.me
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=False,
            send_messages_in_threads=False,
            create_public_threads=False,
            create_private_threads=False,
            add_reactions=False,
        ),
    }
    if me is not None:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=True,
            manage_messages=True,
        )
    return overwrites


async def _sync_overwrites(channel: discord.TextChannel, guild: discord.Guild) -> None:
    """Выставить read-only для @everyone и write для бота,
    сохранив overwrites остальных ролей."""
    try:
        merged = {t: ow for t, ow in channel.overwrites.items()}
        merged.update(_build_overwrites(guild))
        await channel.edit(overwrites=merged)
    except Exception as exc:
        logger.warning(
            "Failed to sync rep log channel overwrites %s: %s",
            channel.id, exc,
        )


def _find_category(guild: discord.Guild) -> discord.CategoryChannel | None:
    """Подходящая категория: ИНФОРМАЦИЯ / СТАТИСТИКА / первая доступная."""
    categories = list(guild.categories)
    for keyword in ("ИНФОРМАЦИЯ", "СТАТИСТИКА", "INFO", "STAT"):
        for cat in categories:
            if keyword.lower() in cat.name.lower():
                return cat
    return categories[0] if categories else None


async def ensure_log_channel(
    bot: "TournamentBot", guild: discord.Guild
) -> discord.TextChannel | None:
    """Вернуть канал логов репутации, при необходимости создать.

    Не создаёт дубликаты: ID персистится в bot_settings, при удалении
    канала создаётся новый и ID обновляется.
    """
    channel_id = await _resolve_channel_id(guild.id)
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            fetched = await bot.fetch_channel(channel_id)
            if fetched is not None and getattr(fetched, "guild", None) == guild:
                channel = fetched
        except Exception:
            channel = None

    if channel is not None:
        await _sync_overwrites(channel, guild)
        return channel

    # Канал удалён/не настроен — создаём в подходящей категории
    me = guild.me
    perms = getattr(me, "guild_permissions", None)
    if me is None or (perms is not None and not perms.manage_channels):
        logger.warning(
            "Cannot create rep log channel in guild %s: no Manage Channels",
            guild.id,
        )
        return None

    category = _find_category(guild)
    try:
        channel = await guild.create_text_channel(
            REP_LOG_CHANNEL_NAME,
            category=category,
            overwrites=_build_overwrites(guild),
            reason="Reputation System 2.0 — public log channel",
        )
    except Exception as exc:
        logger.error(
            "Failed to create rep log channel in guild %s: %s",
            guild.id, exc, exc_info=True,
        )
        return None

    await _save_channel_id(guild.id, channel.id)
    logger.info(
        "Created rep log channel #%s (%s) in guild %s",
        channel.name, channel.id, guild.id,
    )
    return channel


# ---------------------------------------------------------------------------
# Карточки (Components V2, без эмодзи)
# ---------------------------------------------------------------------------

def _ts(row: dict[str, Any]) -> int:
    ts = row.get("published_at") or row.get("created_at")
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return int(ts.timestamp())
    return 0


def _amount_str(amount: int) -> str:
    return f"{amount:+d} REP"


def build_log_view(row: dict[str, Any], guild_name: str = "") -> DesignView:
    """Components V2 карточка события репутации."""
    source = row["source"]
    user_id = int(row["user_id"])
    actor_id = int(row.get("actor_user_id") or 0)
    subject_id = int(row.get("subject_user_id") or 0)
    amount = int(row["amount"])
    balance = int(row.get("balance_after") or 0)
    ts = _ts(row)
    date_str = f"<t:{ts}:f>" if ts else "—"

    if source == SRC_MANUAL:
        title = "РЕПУТАЦИЯ ПОЛУЧЕНА"
        src_line = "Источник: Выдача от участника"
        people = [
            f"Получатель: <@{user_id}>",
            f"Отправитель: <@{actor_id}>",
        ]
        accent = colors.SUCCESS
    elif source == SRC_INVITE:
        title = "НАГРАДА ЗА ПРИГЛАШЕНИЕ"
        src_line = "Источник: Приглашение участника"
        people = [
            f"Пригласивший: <@{actor_id or user_id}>",
            f"Новый участник: <@{subject_id or user_id}>",
        ]
        accent = colors.SUCCESS
    elif source == SRC_BOOST:
        title = "НАГРАДА ЗА ПОДДЕРЖКУ СЕРВЕРА"
        src_line = "Источник: Буст сервера"
        people = [f"Участник: <@{user_id}>"]
        if guild_name:
            people.append(f"Сервер: {guild_name}")
        accent = colors.SUCCESS
    elif source == SRC_SERVER_TAG:
        title = "НАГРАДА ЗА ТЕГ СЕРВЕРА"
        src_line = "Источник: Установка серверного тега"
        reason = f"Причина: Установка тега {guild_name}" if guild_name else "Причина: Установка серверного тега"
        people = [f"Участник: <@{user_id}>", reason]
        accent = colors.SUCCESS
    elif source == SRC_ADMIN:
        title = "РЕПУТАЦИЯ ИЗМЕНЕНА"
        src_line = "Источник: Корректировка администратора"
        people = [f"Участник: <@{user_id}>"]
        accent = colors.WARNING if amount < 0 else colors.INFO
    else:
        title = "РЕПУТАЦИЯ ОБНОВЛЕНА"
        src_line = f"Источник: {source}"
        people = [f"Участник: <@{user_id}>"]
        accent = colors.NEUTRAL

    # Баннер reputation.png первым элементом карточки
    from services.social.common import BANNER_REPUTATION
    view = DesignView(timeout=None)
    view.component(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(media=f"attachment://{BANNER_REPUTATION}")
        )
    )
    view.sep(large=True)
    view.text(f"> ## {title}")
    view.sep()
    view.text(f"> {src_line}")
    view.sep()
    view.lines(*people)
    view.text(f"Начислено: {_amount_str(amount)}")
    view.sep()
    view.lines(f"Текущая репутация: {balance}", f"Дата: {date_str}")
    # accent используется только информационно; контейнер без цветной полосы
    _ = accent
    return view


# ---------------------------------------------------------------------------
# Паблишер
# ---------------------------------------------------------------------------

async def publish_pending(bot: "TournamentBot", limit: int = 20) -> int:
    """Отправить ожидающие события outbox в канал логов.

    Возвращает число успешно опубликованных. Ошибки отдельного события
    не прерывают остальные и не откатывают REP.
    """
    pending = await reputation_store.get_pending_rep_logs(limit)
    if not pending:
        return 0

    channels: dict[int, discord.TextChannel | None] = {}
    sent = 0
    for row in pending:
        outbox_id = int(row["id"])
        guild_id = int(row["guild_id"])
        try:
            if guild_id not in channels:
                guild = bot.get_guild(guild_id)
                if guild is None:
                    try:
                        guild = await bot.fetch_guild(guild_id)
                    except Exception:
                        guild = None
                channels[guild_id] = (
                    await ensure_log_channel(bot, guild) if guild else None
                )
            channel = channels[guild_id]
            if channel is None:
                await reputation_store.mark_rep_log_failed(
                    outbox_id, "log channel unavailable"
                )
                continue

            view = build_log_view(row, guild_name=channel.guild.name)
            from services.social.common import BANNER_REPUTATION, banner_file
            message = await channel.send(
                view=view,
                file=banner_file(BANNER_REPUTATION),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await reputation_store.mark_rep_log_sent(
                outbox_id, int(message.id)
            )
            sent += 1
        except Exception as exc:
            logger.warning(
                "Rep log publish failed (outbox %s): %s", outbox_id, exc
            )
            await reputation_store.mark_rep_log_failed(outbox_id, str(exc))
            if int(row.get("attempts") or 0) + 1 >= _MAX_ATTEMPTS_LOG:
                logger.error(
                    "Rep log outbox %s exceeded %d attempts, last error: %s",
                    outbox_id, _MAX_ATTEMPTS_LOG, exc,
                )
    return sent
