"""Welcome-карточка Components V2 для новых участников.

Структура: приветствие → Separator → информация → Separator →
финальная строка. Без картинок, кнопок и эмодзи-UI.

send_welcome_message() — полная логика отправки: проверка настройки,
поиск канала WELCOME_CHANNEL_ID (env). Используется и on_member_join,
и /welcometest.
"""

from __future__ import annotations

import logging

import discord

from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)

from config import INFO_CHANNEL_ID, WELCOME_CHANNEL_ID


def build_welcome_view(
    member_number: int,
    member: discord.Member,
    info_channel_id: int | None = None,
) -> discord.ui.LayoutView:
    """Собрать V2-view приветствия с баннером welcome.png."""
    from services.social.common import BANNER_WELCOME, social_view

    info_ref = f"<#{info_channel_id}>" if info_channel_id else "#информация"
    view = social_view(BANNER_WELCOME, timeout=None)
    view.text(
        "**Добро пожаловать на сервер**, "
        f"{member.mention}!\n\n"
        f"Ты стал(а) нашим **{member_number}-м** участником"
    )
    view.sep()
    view.text(
        "Рады видеть тебя среди нас. Ознакомься с информацией и "
        f"правилами в {info_ref}."
    )
    view.sep()
    view.text("୨୧ надеемся, тебе у нас понравится ୨୧")
    return view


async def _find_channel(
    client: discord.Client, member: discord.Member
) -> tuple[discord.abc.Messageable | None, discord.Guild | None]:
    """Канал приветствий: сначала гильдия участника, затем все гильдии бота."""
    channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
    if channel is not None:
        return channel, member.guild
    try:
        channel = await member.guild.fetch_channel(WELCOME_CHANNEL_ID)
        return channel, member.guild
    except discord.HTTPException as e:
        logger.warning(
            f"Welcome channel fetch failed in member guild "
            f"{member.guild.id}: {e}")

    for g in client.guilds:
        if g.id == member.guild.id:
            continue
        ch = g.get_channel(WELCOME_CHANNEL_ID)
        if ch is None:
            try:
                ch = await g.fetch_channel(WELCOME_CHANNEL_ID)
            except discord.HTTPException as e:
                logger.warning(
                    f"Welcome channel fetch failed in guild {g.id}: {e}")
                ch = None
        if ch is not None:
            return ch, g
    return None, None


async def send_welcome_message(
    client: discord.Client, member: discord.Member
) -> bool:
    """Отправить welcome-карточку. True — сообщение отправлено."""
    if member.bot or not WELCOME_CHANNEL_ID:
        return False

    from storage.json_store import store
    settings = store.get_settings()
    if not settings.get("welcome_enabled", True):
        return False

    channel, channel_guild = await _find_channel(client, member)
    if channel is None:
        logger.error(
            f"Welcome channel {WELCOME_CHANNEL_ID} not found "
            f"(member guild {member.guild.id}, bot guilds: "
            f"{[g.id for g in client.guilds]})")
        return False

    # N = текущее количество участников + 1
    number = max(1, (member.guild.member_count or 0) + 1)

    info_channel_id = INFO_CHANNEL_ID or None

    try:
        from services.social.common import BANNER_WELCOME, banner_file
        view = build_welcome_view(number, member, info_channel_id)
        await channel.send(view=view, file=banner_file(BANNER_WELCOME))
        logger.info(
            f"Welcome sent to {channel.id} in guild "
            f"{channel_guild.id if channel_guild else '?'} "
            f"for member {member.id}")
        return True
    except Exception as e:
        logger.exception(
            f"Failed to send welcome message to channel "
            f"{WELCOME_CHANNEL_ID}: {e}")
        return False
