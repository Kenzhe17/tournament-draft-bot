"""Welcome-карточка Components V2 для новых участников.

Компактное приветствие и ссылки на старт/правила. Без старых баннеров.

send_welcome_message() — полная логика отправки: проверка настройки,
поиск канала WELCOME_CHANNEL_ID (env). Используется и on_member_join,
и /welcometest.
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)

from config import INFO_CHANNEL_ID, WELCOME_CHANNEL_ID


def build_welcome_view(
    member_number: int,
    member: discord.Member,
    info_channel_id: int | None = None,
    start_channel_id: int | None = None,
) -> discord.ui.LayoutView:
    """Compact welcome; existing call signature stays compatible."""
    from utils.design.layouts import DesignView
    view = DesignView(title="Добро пожаловать в Discord Server r!z3", timeout=None)
    view.text(f"{member.mention}, начни с главного меню.")
    buttons = []
    for label, cid in (("Открыть старт", start_channel_id), ("Правила", info_channel_id)):
        if cid:
            buttons.append(discord.ui.Button(label=label, style=discord.ButtonStyle.link,
                url=f"https://discord.com/channels/{member.guild.id}/{cid}"))
    if buttons:
        view.row(*buttons)
    else:
        view.text("Открой /hub, чтобы перейти к основным разделам.")
    return view


async def _find_channel(client, member):
    from services.discord_cfg import channel_id
    from storage.server_layout_store import server_layout_store
    cid = server_layout_store.binding(member.guild.id, "welcome") or channel_id(
        member.guild.id, "discord.welcome_channel_id", WELCOME_CHANNEL_ID)
    if not cid:
        return None, None
    channel = member.guild.get_channel(cid)
    if channel is None:
        try:
            channel = await member.guild.fetch_channel(cid)
        except discord.HTTPException:
            return None, None
    # Never send another guild's welcome to this guild's configured channel.
    if getattr(getattr(channel, "guild", None), "id", None) != member.guild.id:
        return None, None
    return channel, member.guild


async def send_welcome_message(
    client: discord.Client, member: discord.Member
) -> bool:
    """Отправить welcome-карточку. True — сообщение отправлено."""
    if member.bot:
        return False

    # Control Center (PG) → legacy settings.json fallback в peek.
    from services.settings_service import settings_service
    if not settings_service.peek(member.guild.id, "feature.welcome_enabled"):
        return False

    channel, channel_guild = await _find_channel(client, member)
    if channel is None:
        logger.error(
            f"Welcome channel {WELCOME_CHANNEL_ID} not found "
            f"(member guild {member.guild.id}, bot guilds: "
            f"{[g.id for g in client.guilds]})")
        return False

    from storage.info_store import info_store
    from storage.server_layout_store import server_layout_store
    info_channel_id = server_layout_store.binding(member.guild.id, "info") or await info_store.get_channel_id(member.guild.id) or INFO_CHANNEL_ID or None
    start_channel_id = server_layout_store.binding(member.guild.id, "start") or None
    mentions = discord.AllowedMentions(users=[member], roles=False, everyone=False, replied_user=False)

    # Control Center template override: admin_templates['welcome.card']
    # (guild → global). Если шаблон есть и валиден — используем его
    # вместо стандартной карточки; при ошибке — fallback на дефолт.
    custom_view = None
    try:
        from services.cv2_templates import render_template
        from storage import admin_config_store as _cfg_store
        tpl = await _cfg_store.get_template(member.guild.id, "welcome.card") \
            or await _cfg_store.get_template(0, "welcome.card")
        if tpl:
            custom_view = render_template(tpl["payload"])
    except Exception as exc:  # noqa: BLE001 — шаблон не должен ломать welcome
        logger.warning(f"welcome.card template failed, using default: {exc}")
        custom_view = None

    try:
        if custom_view is not None:
            await channel.send(view=custom_view, allowed_mentions=mentions)
        else:
            view = build_welcome_view(0, member, info_channel_id, start_channel_id)
            await channel.send(view=view, allowed_mentions=mentions)
        logger.info(
            f"Welcome sent to {channel.id} in guild "
            f"{channel_guild.id if channel_guild else '?'} "
            f"for member {member.id}")
        return True
    except Exception:
        logger.exception(
            "Failed to send welcome message to channel %s",
            WELCOME_CHANNEL_ID)
        return False
