"""Welcome-карточка Components V2 для новых участников.

Структура: MediaGallery(welcome.png) → приветствие → Separator →
информация → Separator → финальная строка. Без кнопок и эмодзи-UI.

send_welcome_message() — полная логика отправки: проверка настройки,
поиск канала 1569098034325889144, номер участника из PostgreSQL.
Используется и on_member_join, и /welcometest.
"""

from __future__ import annotations

import logging
from pathlib import Path

import discord

from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)

WELCOME_CHANNEL_ID = 1569098034325889144

WELCOME_PNG = (
    Path(__file__).resolve().parents[1]
    / "assets_ds_server" / "welcome" / "welcome.png"
)
WELCOME_FILENAME = "welcome.png"


def build_welcome_view(
    member_number: int,
    member: discord.Member,
    info_channel_id: int | None = None,
) -> discord.ui.LayoutView:
    """Собрать V2-view приветствия. Файл прикрепляется к сообщению."""
    info_ref = f"<#{info_channel_id}>" if info_channel_id else "#информация"
    view = DesignView(timeout=None)
    view.component(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(media=f"attachment://{WELCOME_FILENAME}")
        )
    )
    view.sep(large=True)
    view.text(
        "₊˚⊹♡ **Добро пожаловать на сервер**, "
        f"{member.mention}!\n\n"
        f"Ты стал(а) нашим **{member_number}-м** участником ♡"
    )
    view.sep()
    view.text(
        "Рады видеть тебя среди нас. Ознакомься с информацией и "
        f"правилами в {info_ref}."
    )
    view.sep()
    view.text("୨୧ надеемся, тебе у нас понравится ୨୧")
    return view


def welcome_file() -> discord.File:
    return discord.File(str(WELCOME_PNG), filename=WELCOME_FILENAME)


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
    if member.bot:
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

    try:
        from storage.welcome_store import welcome_store
        number = await welcome_store.member_number(
            member.guild.id, member.id)
    except Exception:
        logger.exception("welcome number failed")
        number = max(1, (member.guild.member_count or 1))

    try:
        from config import INFO_CHANNEL_ID
        info_channel_id = INFO_CHANNEL_ID or None
    except Exception:
        info_channel_id = None

    try:
        view = build_welcome_view(number, member, info_channel_id)
        await channel.send(view=view, file=welcome_file())
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
