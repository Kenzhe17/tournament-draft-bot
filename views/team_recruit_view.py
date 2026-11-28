"""Рекрутинговая карточка команды — публичная публикация набора.

Одна актуальная карточка на команду (channel_id/message_id в teams).
Кнопки — persistent DynamicItem ``squad:rec:*``.
"""

from __future__ import annotations

import logging
import re

import discord

from models.team import TEAM_MAX_MEMBERS
from services import squad_service as svc
from storage import squad_store
from storage.team_store import team_store, TeamStoreError
from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()

T_JOIN = re.compile(r"^squad:rec:join:(?P<tid>[\w-]+)$")
T_VIEW = re.compile(r"^squad:rec:view:(?P<tid>[\w-]+)$")


async def _team_or_none(team_id: str):
    return await team_store.refresh_from_db(team_id)


def _member_label(guild: discord.Guild | None, uid: int,
                  captain_id: int) -> str:
    name = f"<@{uid}>"
    if guild is not None:
        m = guild.get_member(uid)
        if m is not None:
            name = m.display_name
    role = "Капитан" if uid == captain_id else "Игрок"
    return name, role


async def render_recruit_card(guild: discord.Guild | None, team,
                              stats: dict) -> discord.ui.LayoutView:
    """Собрать LayoutView карточки набора."""
    closed = team.is_full() or not team.recruiting
    lines = [
        f"## {team.team_name} | {'НАБОР ЗАКРЫТ' if closed else 'НАБОР В КОМАНДУ'}",
        "",
        f"**Состав:** {len(team.members)}/{TEAM_MAX_MEMBERS}  ",
        f"**Свободных мест:** {team.free_slots()}  ",
        f"**Средний SR:** {svc.fmt_sr(stats.get('avg_sr'))}  ",
        f"**Цель:** {team.purpose_label}",
    ]
    req = team.requirements or {}
    req_bits = []
    if req.get("min_sr"):
        req_bits.append(f"SR от {req['min_sr']}")
    if req.get("device"):
        from models.team import DEVICE_LABELS
        req_bits.append(
            f"Устройство: {DEVICE_LABELS.get(req['device'], req['device'])}")
    if req.get("role"):
        req_bits.append(f"Роль: {req['role']}")
    if req_bits:
        lines.append("**Требования:** " + " · ".join(req_bits))
    if team.description:
        lines += ["", team.description[:300]]
    lines += ["", "**Участники**"]
    for idx in range(TEAM_MAX_MEMBERS):
        if idx < len(team.members):
            uid = team.members[idx]
            name, role = _member_label(guild, uid, team.captain_id)
            lines.append(f"{idx + 1:02d}. {name} — {role}")
        else:
            lines.append(f"{idx + 1:02d}. Свободное место")

    view = DesignView(title=f"{team.team_name} — набор",
                      timeout=None)
    view.text("\n".join(lines)[:3900])
    row = discord.ui.ActionRow()
    if closed:
        row.add_item(discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Набор закрыт", custom_id=f"squad:rec:closed:{team.team_id}",
            disabled=True))
    else:
        row.add_item(discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Подать заявку / Вступить",
            custom_id=f"squad:rec:join:{team.team_id}"))
    row.add_item(discord.ui.Button(
        style=discord.ButtonStyle.secondary,
        label="Посмотреть команду",
        custom_id=f"squad:rec:view:{team.team_id}"))
    view.row(*row.children)
    return view


# ----------------------------------------------------------------------
# Persistent DynamicItem-кнопки
# ----------------------------------------------------------------------


class RecJoinBtn(discord.ui.DynamicItem[discord.ui.Button],
                 template=T_JOIN):
    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Подать заявку / Вступить",
            custom_id=custom_id))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(str(interaction.data.get("custom_id")
                       or item.custom_id))

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_recruit_join(interaction)


class RecViewBtn(discord.ui.DynamicItem[discord.ui.Button],
                 template=T_VIEW):
    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Посмотреть команду",
            custom_id=custom_id))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(str(interaction.data.get("custom_id")
                       or item.custom_id))

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_recruit_view(interaction)


class RecruitmentCardView(discord.ui.View):
    """Регистрируется один раз — dispatch по шаблонам squad:rec:*."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(RecJoinBtn("squad:rec:join:0"))
        self.add_item(RecViewBtn("squad:rec:view:0"))


# ----------------------------------------------------------------------
# Обработчики
# ----------------------------------------------------------------------


def _tid_from(interaction: discord.Interaction) -> str | None:
    cid = str(interaction.data.get("custom_id", ""))
    m = T_JOIN.match(cid) or T_VIEW.match(cid)
    return m.group("tid") if m else None


async def handle_recruit_join(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return
    tid = _tid_from(interaction)
    team = await _team_or_none(tid) if tid else None
    if team is None or team.guild_id != guild.id:
        await interaction.response.send_message(
            "Команда больше не существует.", ephemeral=True)
        return
    from views.squad_panels import (
        build_team_card_view, join_team_action, open_apply_modal,
        open_password_modal,
    )
    uid = interaction.user.id
    if team.is_member(uid):
        v = await build_team_card_view(guild, team, uid)
        await interaction.response.send_message(
            view=v, ephemeral=True, allowed_mentions=_NO_MENTIONS)
        return
    if svc.can_join_open(team):
        await join_team_action(interaction, team.team_id, via="open")
        return
    if svc.can_apply(team):
        await open_apply_modal(interaction, team.team_id)
        return
    if svc.needs_password(team):
        await open_password_modal(interaction, team.team_id)
        return
    v = await build_team_card_view(guild, team, uid)
    await interaction.response.send_message(
        view=v, ephemeral=True, allowed_mentions=_NO_MENTIONS)


async def handle_recruit_view(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return
    tid = _tid_from(interaction)
    team = await _team_or_none(tid) if tid else None
    if team is None or team.guild_id != guild.id:
        await interaction.response.send_message(
            "Команда больше не существует.", ephemeral=True)
        return
    from views.squad_panels import build_team_card_view
    v = await build_team_card_view(
        guild, team, interaction.user.id, source="recruit")
    await interaction.response.send_message(
        view=v, ephemeral=True, allowed_mentions=_NO_MENTIONS)


# ----------------------------------------------------------------------
# Карточка приглашения (DM + fallback)
# ----------------------------------------------------------------------


async def build_invite_dm_view(guild: discord.Guild | None, team,
                               invite: squad_store.SquadInvite,
                               avg_sr: float | None) -> discord.ui.LayoutView:
    """Эфемерная карточка приглашения для ЛС (кнопки живут в рамках
    сессии; после рестарта приглашение доступно через Squad Hub)."""
    lines = [
        f"## {team.team_name} — приглашение",
        "",
        f"**Капитан:** <@{team.captain_id}>  ",
        f"**Состав:** {len(team.members)}/{TEAM_MAX_MEMBERS}  ",
        f"**Средний SR:** {svc.fmt_sr(avg_sr)}  ",
        f"**Цель:** {team.purpose_label}  ",
        f"**Вступление:** {team.join_mode_label}",
    ]
    if team.description:
        lines += ["", team.description[:300]]
    lines += ["", "-# Приглашение действует 24 часа."]

    view = DesignView(title=f"Приглашение в {team.team_name}",
                      timeout=None)
    view.text("\n".join(lines)[:3900])
    row = discord.ui.ActionRow()

    async def _accept(i):
        from views.squad_panels import accept_invite
        await accept_invite(i, invite.invite_id)

    async def _decline(i):
        from views.squad_panels import decline_invite
        await decline_invite(i, invite.invite_id)

    async def _card(i):
        from views.squad_panels import show_invite_card
        await show_invite_card(i, invite.invite_id)

    def _mkbtn(style, label, cid, cb):
        b = discord.ui.Button(style=style, label=label, custom_id=cid)
        b.callback = cb  # type: ignore[assignment]
        return b

    row.add_item(_mkbtn(discord.ButtonStyle.success, "Принять",
                        f"squad:invdm:a:{invite.invite_id}", _accept))
    row.add_item(_mkbtn(discord.ButtonStyle.secondary, "Отклонить",
                        f"squad:invdm:d:{invite.invite_id}", _decline))
    row.add_item(_mkbtn(discord.ButtonStyle.secondary,
                        "Посмотреть команду",
                        f"squad:invdm:v:{invite.invite_id}", _card))
    view.row(*row.children)
    return view


async def invite_card_view(guild: discord.Guild | None, team,
                            invite: squad_store.SquadInvite
                            ) -> discord.ui.LayoutView:
    """Карточка приглашения в ephemeral («Посмотреть команду»)."""
    avg = await svc.team_avg_sr(guild.id, team.members, guild) \
        if guild else None
    return await build_invite_dm_view(guild, team, invite, avg)
