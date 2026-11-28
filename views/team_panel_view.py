"""Приватная панель команды (Squad Hub 2.0).

Одна ephemeral CV2-карточка, внутри которой навигация через
edit_message: панель → управление → подменю → назад. Права и
состав каждый раз перечитываются из team_store (актуальное
состояние PostgreSQL, а не объект на момент создания View).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import discord

from models.team import (
    DEVICE_LABELS, DEVICES, JOIN_APPLICATIONS, JOIN_CLOSED, JOIN_MODE_LABELS,
    JOIN_MODES, JOIN_OPEN, JOIN_PASSWORD, PLAY_ROLES, PURPOSE_LABELS,
    PURPOSES, TEAM_MAX_MEMBERS, validate_description, validate_team_name,
)
from services import squad_service as svc
from storage import squad_store
from storage.team_store import team_store, TeamStoreError
from utils.design import colors
from utils.design.components import btn, option, select
from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()

# Cooldown повторной публикации рекрутинговой карточки
_PUBLISH_CD: dict[str, float] = {}
_PUBLISH_CD_SECONDS = 300


def _err(exc: Exception) -> str:
    return getattr(exc, "message", "") or str(exc) or "Ошибка"


def _b(label: str, style: str, cid: str, cb) -> discord.ui.Button:
    b = btn(label, style=style, custom_id=cid)
    b.callback = cb  # type: ignore[assignment]
    return b


def _s(options: list, cid: str, placeholder: str, cb) -> discord.ui.Select:
    s = select(options, custom_id=cid, placeholder=placeholder)
    s.callback = cb  # type: ignore[assignment]
    return s


# ----------------------------------------------------------------------
# Точка входа
# ----------------------------------------------------------------------


async def open_team_panel(interaction: discord.Interaction,
                          team_id: str,
                          notice: str = "") -> None:
    """Открыть/перерисовать панель команды (из кнопки или команды)."""
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Команды работают только на сервере.", ephemeral=True)
        return
    team = await team_store.refresh_from_db(team_id)
    if team is None or team.guild_id != guild.id:
        await interaction.response.send_message(
            "Команда не найдена или расформирована.", ephemeral=True)
        return
    view = await render_team_panel(guild, team, interaction.user.id,
                                   notice=notice)
    itype = getattr(interaction, "type", None)
    try:
        if itype == discord.InteractionType.MESSAGE_COMPONENT:
            await interaction.response.edit_message(view=view)
        elif itype == discord.InteractionType.APPLICATION_COMMAND:
            # slash-команда: ответ уже отложен defer'ом
            await interaction.followup.send(
                view=view, ephemeral=True,
                allowed_mentions=_NO_MENTIONS)
        else:
            await interaction.response.send_message(
                view=view, ephemeral=True,
                allowed_mentions=_NO_MENTIONS)
    except (discord.NotFound, discord.HTTPException):
        try:
            await interaction.followup.send(
                view=view, ephemeral=True,
                allowed_mentions=_NO_MENTIONS)
        except Exception:
            pass


async def send_team_panel(interaction: discord.Interaction,
                          team_id: str, notice: str = "") -> None:
    """Отправить панель новым ephemeral-сообщением (после вступления)."""
    team = await team_store.refresh_from_db(team_id)
    if team is None:
        return
    guild = interaction.guild or interaction.client.get_guild(
        team.guild_id)
    if guild is None:
        await interaction.response.send_message(
            notice or "Готово.", allowed_mentions=_NO_MENTIONS)
        return
    view = await render_team_panel(guild, team, interaction.user.id,
                                   notice=notice)
    kwargs = {"ephemeral": True} if interaction.guild is not None else {}
    await interaction.response.send_message(
        view=view, allowed_mentions=_NO_MENTIONS, **kwargs)


# ----------------------------------------------------------------------
# Главная панель
# ----------------------------------------------------------------------


async def render_team_panel(guild: discord.Guild, team,
                            user_id: int, *,
                            notice: str = "") -> discord.ui.LayoutView:
    members = team.members
    avg = await svc.team_avg_sr(guild.id, members, guild)
    is_captain = team.is_captain(user_id)
    is_member = team.is_member(user_id)

    pending_transfer = svc.pending_transfer_for(guild.id, team.team_id,
                                                user_id)

    lines = [
        f"## {team.team_name}",
        "",
        f"**Статус:** {'Набор открыт' if team.recruiting else 'Набор закрыт'}  ",
        f"**Состав:** {len(members)}/{TEAM_MAX_MEMBERS}  ",
        f"**Средний SR:** {svc.fmt_sr(avg)}  ",
        f"**Вступление:** {team.join_mode_label}  ",
        f"**Цель:** {team.purpose_label}",
    ]
    role = ("Капитан" if is_captain else
            "Участник" if is_member else "Наблюдатель")
    lines.append(f"**Вы:** {role}")
    if team.description:
        lines += ["", team.description[:300]]
    lines += ["", "**Состав команды**"]
    for idx in range(TEAM_MAX_MEMBERS):
        if idx < len(members):
            lines.append(await svc.member_line(guild, team, idx))
        else:
            lines.append(f"{idx + 1:02d}. Свободное место")
    apps = await squad_store.list_pending_applications_for_team(
        team.team_id) if is_captain else []
    if is_captain and apps:
        lines.append(f"\n**Заявок:** {len(apps)}")
    if notice:
        lines += ["", f"-# {notice}"]

    view = DesignView(title=team.team_name[:80], accent=colors.BRAND)
    view.text("\n".join(lines)[:3900])

    tid = team.team_id
    uid = user_id

    async def _refresh(i):
        await open_team_panel(i, tid)

    async def _invite(i):
        await _render_invite_select(i, tid)

    async def _apps(i):
        await _render_applications(i, tid)

    async def _find_players(i):
        from views.squad_panels import open_player_search
        await open_player_search(i, tid)

    async def _publish(i):
        await _do_publish(i, tid)

    async def _manage(i):
        await _render_manage(i, tid)

    async def _my_invites(i):
        from views.squad_panels import open_invites_panel
        await open_invites_panel(i)

    async def _share(i):
        await _render_share(i, tid)

    async def _leave(i):
        await _render_leave_confirm(i, tid)

    async def _accept_captain(i):
        await _do_confirm_captain(i, tid)

    async def _decline_captain(i):
        svc.cancel_transfer_request(guild.id, tid)
        await i.response.send_message(
            "Передача капитанства отклонена.", ephemeral=True)

    async def _join_action(i):
        await _panel_join_action(i, team)

    row_items = []
    if is_captain:
        row_items += [
            _b("Пригласить", "primary", "tm:invite", _invite),
            _b("Заявки", "primary", "tm:apps", _apps),
            _b("Поиск игроков", "secondary", "tm:players", _find_players),
            _b("Опубликовать набор", "secondary", "tm:publish", _publish),
            _b("Управление", "secondary", "tm:manage", _manage),
        ]
    elif is_member:
        row_items += [
            _b("Приглашения", "secondary", "tm:invites", _my_invites),
            _b("Поделиться", "secondary", "tm:share", _share),
            _b("Обновить", "secondary", "tm:refresh", _refresh),
        ]
        if pending_transfer:
            row_items.insert(0, _b(
                "Принять капитанство", "success", "tm:capyes", _accept_captain))
            row_items.insert(1, _b(
                "Отклонить", "secondary", "tm:capno", _decline_captain))
        else:
            row_items.append(_b("Покинуть команду", "danger",
                                "tm:leave", _leave))
            row_items.append(_b("Обновить", "secondary", "tm:refresh2",
                                _refresh))
    else:
        # Посторонний — просмотр чужой команды
        if svc.can_join_open(team):
            row_items.append(_b("Вступить", "success", "tm:join",
                                _join_action))
        if svc.can_apply(team):
            from views.squad_panels import open_apply_modal

            async def _apply(i, _t=team):
                await open_apply_modal(i, _t.team_id)
            row_items.append(_b("Подать заявку", "primary", "tm:apply",
                                _apply))
        if svc.needs_password(team):
            from views.squad_panels import open_password_modal

            async def _pw(i, _t=team):
                await open_password_modal(i, _t.team_id)
            row_items.append(_b("Ввести пароль", "secondary", "tm:pw",
                                _pw))
        row_items.append(_b("Обновить", "secondary", "tm:refresh3",
                            _refresh))

    # раскладываем по 5 кнопок в ряд
    for i in range(0, len(row_items), 5):
        view.row(*row_items[i:i + 5])
    return view


async def _panel_join_action(interaction: discord.Interaction,
                             team) -> None:
    from views.squad_panels import join_team_action
    await join_team_action(interaction, team.team_id, via="panel")


async def _render_share(interaction: discord.Interaction,
                        team_id: str) -> None:
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None:
        return
    view = DesignView(title="Поделиться командой", accent=colors.BRAND)
    view.text(
        f"Команда **{team.team_name}** ищет игроков.\n"
        f"-# Набор: {'открыт' if team.recruiting else 'закрыт'} · "
        f"{team.join_mode_label} · Цель: {team.purpose_label}\n"
        "-# Откройте /team status и введите название команды "
        "или найдите её в Squad Hub → «Найти команду».")
    view.row(_b("Назад", "secondary", "tm:back:share",
                lambda i: open_team_panel(i, team_id)))
    await interaction.response.edit_message(view=view)


# ----------------------------------------------------------------------
# Приглашения (UserSelect)
# ----------------------------------------------------------------------


async def _render_invite_select(interaction: discord.Interaction,
                                team_id: str) -> None:
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_captain(interaction.user.id):
        await interaction.response.send_message(
            "Только капитан может приглашать.", ephemeral=True)
        return
    if team.is_full():
        await interaction.response.send_message(
            "Состав заполнен — приглашать некуда.", ephemeral=True)
        return
    view = DesignView(title="Пригласить игроков", accent=colors.BRAND)
    view.text("Выберите игроков для приглашения (до 10). "
              "Приглашение действует 24 часа.")
    us = discord.ui.UserSelect(
        placeholder="Выберите игроков…",
        custom_id="tm:invite:users",
        min_values=1, max_values=10)

    async def _cb(i):
        users = i.data.get("users") or []
        ids = [int(u["id"]) for u in users]
        await _send_invites_bulk(i, team_id, ids)

    us.callback = _cb  # type: ignore[assignment]
    view.row(us)
    view.row(_b("Назад", "secondary", "tm:back:inv",
                lambda i: open_team_panel(i, team_id)))
    await interaction.response.edit_message(view=view)


async def _send_invites_bulk(interaction: discord.Interaction,
                             team_id: str, user_ids: list[int]) -> None:
    from views.squad_panels import send_invite
    guild = interaction.guild
    sent, failed = 0, []
    for uid in user_ids:
        try:
            # send_invite отвечает ephemeral — для bulk используем
            # внутреннюю логику без ответа
            team = await team_store.refresh_from_db(team_id)
            if team is None or not team.is_captain(
                    interaction.user.id):
                break
            target = guild.get_member(uid) if guild else None
            if target is None or target.bot or team.is_member(uid) \
                    or uid == interaction.user.id:
                failed.append(str(uid))
                continue
            if team.is_full():
                failed.append(str(uid))
                continue
            inv = await squad_store.create_invite(
                guild.id, team.team_id, interaction.user.id, uid)
            if inv is None:
                failed.append(str(uid))
                continue
            try:
                dm = await target.create_dm()
                from views.team_recruit_view import build_invite_dm_view
                avg = await svc.team_avg_sr(guild.id, team.members,
                                            guild)
                msg = await dm.send(
                    view=await build_invite_dm_view(guild, team, inv, avg),
                    allowed_mentions=_NO_MENTIONS)
                await squad_store.set_invite_dm(inv.invite_id, dm.id,
                                                msg.id)
            except (discord.Forbidden, discord.HTTPException):
                pass
            await squad_store.audit(
                guild.id, team.team_id, interaction.user.id,
                "invite_sent", target_id=uid,
                metadata={"invite_id": inv.invite_id})
            sent += 1
        except Exception as exc:
            logger.warning("bulk invite failed for %s: %s", uid, exc)
            failed.append(str(uid))
    view = DesignView(title="Приглашения", accent=colors.BRAND)
    view.text(f"Отправлено: **{sent}**" +
              (f" · пропущено: {len(failed)}" if failed else "") +
              "\n-# Не удалось доставить в ЛС — приглашение доступно "
              "через Squad Hub → Приглашения.")
    view.row(_b("К команде", "primary", "tm:back:inv2",
                lambda i: open_team_panel(i, team_id)))
    await interaction.response.edit_message(view=view)


# ----------------------------------------------------------------------
# Заявки капитана
# ----------------------------------------------------------------------


async def _render_applications(interaction: discord.Interaction,
                               team_id: str) -> None:
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_captain(interaction.user.id):
        await interaction.response.send_message(
            "Только капитан видит заявки.", ephemeral=True)
        return
    apps = await squad_store.list_pending_applications_for_team(team_id)
    view = DesignView(title="Заявки на вступление",
                      accent=colors.BRAND)
    if not apps:
        view.text("Активных заявок нет.")
        view.row(_b("Назад", "secondary", "tm:back:apps",
                    lambda i: open_team_panel(i, team_id)))
        await interaction.response.edit_message(view=view)
        return
    lines = [f"**Заявок:** {len(apps)} — выберите для просмотра."]
    view.text("\n".join(lines))
    options = []
    briefs = {}
    for app in apps[:25]:
        brief = await svc.player_brief(guild.id, app.user_id, guild)
        lfg = await squad_store.get_lfg(guild.id, app.user_id)
        desc = ""
        if lfg and lfg.device:
            desc = DEVICE_LABELS.get(lfg.device, lfg.device)
        if lfg and lfg.play_role:
            desc += (" · " if desc else "") + lfg.play_role
        options.append(option(
            label=brief["name"][:100], value=str(app.application_id),
            description=(
                f"SR {svc.fmt_sr(brief['sr'])} · ELO {svc.fmt_elo(brief['elo'])}"
                + (f" · {desc}" if desc else ""))[:100]))
        briefs[app.application_id] = (app, brief)

    async def _pick(i):
        vals = i.data.get("values") or []
        if vals:
            await _render_application_card(
                i, int(vals[0]), briefs.get(int(vals[0])))

    view.row(_s(options, "tm:apps:pick", "Выберите заявку", _pick))
    view.row(_b("Назад", "secondary", "tm:back:apps2",
                lambda i: open_team_panel(i, team_id)))
    await interaction.response.edit_message(view=view)


async def _render_application_card(interaction: discord.Interaction,
                                   app_id: int,
                                   cached=None) -> None:
    guild = interaction.guild
    team = team_store.get_by_captain(interaction.user.id)
    app = await squad_store.get_application(app_id)
    if app is None or not app.is_pending or app.is_expired:
        await interaction.response.send_message(
            "Заявка недоступна или истекла.", ephemeral=True)
        return
    if team is None or team.team_id != app.team_id \
            or not team.is_captain(interaction.user.id):
        await interaction.response.send_message(
            "Только капитан команды может рассматривать заявки.",
            ephemeral=True)
        return
    if cached is None:
        brief = await svc.player_brief(guild.id, app.user_id, guild)
    else:
        _, brief = cached
    lfg = await squad_store.get_lfg(guild.id, app.user_id)

    lines = [
        f"## Заявка — {brief['name']}",
        "",
        f"**SR:** {svc.fmt_sr(brief['sr'])}  ",
        f"**ELO:** {svc.fmt_elo(brief['elo'])}  ",
        f"**Устройство:** "
        f"{DEVICE_LABELS.get(lfg.device if lfg else '', (lfg.device if lfg else '') or '—')}  ",
        f"**Роль:** {(lfg.play_role if lfg else '') or '—'}",
    ]
    if lfg and lfg.purpose:
        lines.append(f"**Цель:** {PURPOSE_LABELS.get(lfg.purpose, lfg.purpose)}")
    if app.message:
        lines += ["", f"«{app.message[:300]}»"]
    lines += ["", "-# Срок заявки: "
              f"{discord.utils.format_dt(app.expires_at, style='R') if app.expires_at else '—'}"]

    view = DesignView(title="Заявка на вступление", accent=colors.BRAND)
    view.text("\n".join(lines)[:3900])
    tid = team.team_id

    async def _accept(i):
        await _do_accept_application(i, app_id, tid)

    async def _decline(i):
        ok = await squad_store.resolve_application(app_id, "declined")
        await i.response.send_message(
            "Заявка отклонена." if ok else "Заявка уже обработана.",
            ephemeral=True)

    async def _profile(i):
        # Профиль игрока — краткая сводка (SR/ELO/статистика)
        await i.response.send_message(
            view=await _player_profile_view(guild, app.user_id),
            ephemeral=True, allowed_mentions=_NO_MENTIONS)

    row = discord.ui.ActionRow()
    row.add_item(_b("Принять", "success", f"tm:appok:{app_id}", _accept))
    row.add_item(_b("Отклонить", "secondary", f"tm:appno:{app_id}",
                    _decline))
    row.add_item(_b("Профиль", "secondary", f"tm:appprof:{app_id}",
                    _profile))
    row.add_item(_b("Назад", "secondary", f"tm:appback:{app_id}",
                    lambda i: _render_applications(i, tid)))
    view.row(*row.children)
    await interaction.response.edit_message(view=view)


async def _player_profile_view(guild, user_id) -> discord.ui.LayoutView:
    brief = await svc.player_brief(guild.id, user_id, guild)
    lfg = await squad_store.get_lfg(guild.id, user_id)
    view = DesignView(title=brief["name"], accent=colors.BRAND)
    lines = [
        f"**SR:** {svc.fmt_sr(brief['sr'])}  ",
        f"**ELO:** {svc.fmt_elo(brief['elo'])}  ",
    ]
    if lfg and lfg.is_active:
        lines += [
            f"**Устройство:** {DEVICE_LABELS.get(lfg.device, lfg.device or '—')}  ",
            f"**Роль:** {lfg.play_role or '—'}  ",
            f"**Цель:** {PURPOSE_LABELS.get(lfg.purpose, lfg.purpose or '—')}",
        ]
        if lfg.description:
            lines += ["", lfg.description[:300]]
    else:
        lines.append("**LFG:** не активен")
    view.text("\n".join(lines))
    return view


async def _do_accept_application(interaction: discord.Interaction,
                                 app_id: int, team_id: str) -> None:
    """Капитан принимает заявку: атомарное вступление + закрытие заявок."""
    guild = interaction.guild
    app = await squad_store.get_application(app_id)
    if app is None or not app.is_pending:
        await interaction.response.send_message(
            "Заявка уже обработана.", ephemeral=True)
        return
    if app.team_id != team_id:
        await interaction.response.send_message(
            "Заявка не этой команды.", ephemeral=True)
        return
    try:
        team = await team_store.join_team(team_id, app.user_id,
                                          guild.id)
    except TeamStoreError as exc:
        await interaction.response.send_message(
            f"Не удалось принять: {_err(exc)}", ephemeral=True)
        return
    except Exception as exc:
        logger.warning("accept application join failed: %s", exc)
        await interaction.response.send_message(
            "Ошибка базы данных. Попробуйте позже.", ephemeral=True)
        return
    await squad_store.resolve_application(app_id, "accepted")
    # Остальные заявки/приглашения игрока закрываем
    await squad_store.cancel_applications_for_user(
        guild.id, app.user_id, "joined_team")
    await squad_store.cancel_invites_for_user(
        guild.id, app.user_id, "joined_team")
    await squad_store.deactivate_lfg_for_user(guild.id, app.user_id)
    # Заявки в заполненную команду закрываем
    if team.is_full():
        await squad_store.cancel_applications_for_team(
            team_id, status="team_full")
    svc.schedule_hub_refresh(guild)
    await svc.sync_recruit_card(interaction.client, guild, team_id)
    await squad_store.audit(guild.id, team_id, interaction.user.id,
                            "application_accept", target_id=app.user_id,
                            metadata={"application_id": app_id})
    await interaction.response.send_message(
        f"Заявка принята — игрок вступил в **{team.team_name}**.",
        ephemeral=True)
    # Уведомить игрока в ЛС (best effort)
    try:
        target = guild.get_member(app.user_id)
        if target is not None:
            dm = await target.create_dm()
            await dm.send(
                f"Ваша заявка в команду **{team.team_name}** принята — "
                "добро пожаловать!",
                allowed_mentions=_NO_MENTIONS)
    except (discord.Forbidden, discord.HTTPException):
        pass
    # Обновить панель капитана
    try:
        fresh = await team_store.refresh_from_db(team_id)
        if fresh is not None:
            view = await render_team_panel(guild, fresh,
                                           interaction.user.id,
                                           notice="Заявка принята.")
            await interaction.edit_original_response(view=view)
    except Exception:
        pass


# ----------------------------------------------------------------------
# Управление командой (меню капитана)
# ----------------------------------------------------------------------


async def _render_manage(interaction: discord.Interaction,
                         team_id: str) -> None:
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_captain(interaction.user.id):
        await interaction.response.send_message(
            "Только капитан управляет командой.", ephemeral=True)
        return
    req = team.requirements or {}
    req_bits = []
    if req.get("min_sr"):
        req_bits.append(f"SR от {req['min_sr']}")
    if req.get("device"):
        req_bits.append(f"Устройство: {DEVICE_LABELS.get(req['device'], req['device'])}")
    if req.get("role"):
        req_bits.append(f"Роль: {req['role']}")
    if req.get("ff_uid_required"):
        req_bits.append("FF UID обязателен")
    view = DesignView(title=f"Управление — {team.team_name}",
                      accent=colors.BRAND)
    view.text(
        f"**Название:** {team.team_name}\n"
        f"**Описание:** {team.description or '—'}\n"
        f"**Цель:** {team.purpose_label}\n"
        f"**Вступление:** {team.join_mode_label}\n"
        f"**Набор:** {'открыт' if team.recruiting else 'закрыт'}\n"
        f"**Требования:** {' · '.join(req_bits) if req_bits else '—'}")

    async def _name(i):
        await i.response.send_modal(TeamNameModal(team_id))

    async def _desc(i):
        await i.response.send_modal(TeamDescModal(team_id))

    async def _purpose(i):
        await _render_purpose_select(i, team_id)

    async def _joinmode(i):
        await _render_joinmode_select(i, team_id)

    async def _req(i):
        await _render_requirements(i, team_id)

    async def _toggle(i):
        await _do_toggle_recruiting(i, team_id)

    async def _members(i):
        await _render_members(i, team_id)

    async def _transfer(i):
        await _render_transfer(i, team_id)

    async def _publish(i):
        await _do_publish(i, team_id)

    async def _disband(i):
        await _render_disband_confirm(i, team_id)

    async def _back(i):
        await open_team_panel(i, team_id)

    view.row(
        _b("Название", "secondary", "tm:m:name", _name),
        _b("Описание", "secondary", "tm:m:desc", _desc),
        _b("Цель", "secondary", "tm:m:purpose", _purpose),
        _b("Вступление", "secondary", "tm:m:join", _joinmode),
        _b("Требования", "secondary", "tm:m:req", _req),
    )
    view.row(
        _b("Открыть набор" if not team.recruiting else "Закрыть набор",
           "secondary", "tm:m:toggle", _toggle),
        _b("Участники", "secondary", "tm:m:members", _members),
        _b("Передать капитанство", "secondary", "tm:m:transfer",
           _transfer),
        _b("Опубликовать набор", "secondary", "tm:m:publish", _publish),
        _b("Расформировать", "danger", "tm:m:disband", _disband),
    )
    view.row(_b("Назад", "secondary", "tm:m:back", _back))
    await interaction.response.edit_message(view=view)


# --- модалки настроек ---


class TeamNameModal(discord.ui.Modal, title="Название команды"):
    def __init__(self, team_id: str):
        super().__init__()
        self.team_id = team_id
        self.name = discord.ui.TextInput(
            label="Название", style=discord.TextStyle.short,
            required=True, min_length=2, max_length=24)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        err = validate_team_name(self.name.value)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        team = await team_store.refresh_from_db(self.team_id)
        if team is None or not team.is_captain(interaction.user.id):
            await interaction.response.send_message(
                "Нет доступа.", ephemeral=True)
            return
        team.team_name = self.name.value.strip()
        try:
            await team_store.update_team(team)
        except TeamStoreError as exc:
            await interaction.response.send_message(
                _err(exc), ephemeral=True)
            return
        await squad_store.audit(team.guild_id, team_id,
                                interaction.user.id, "rename",
                                metadata={"name": team.team_name})
        svc.schedule_hub_refresh(interaction.guild)
        await open_team_panel(interaction, team_id,
                              notice="Название изменено.")


class TeamDescModal(discord.ui.Modal, title="Описание команды"):
    def __init__(self, team_id: str):
        super().__init__()
        self.team_id = team_id
        self.desc = discord.ui.TextInput(
            label="Описание", style=discord.TextStyle.paragraph,
            required=False, max_length=150)
        self.add_item(self.desc)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        err = validate_description(self.desc.value or "")
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        team = await team_store.refresh_from_db(self.team_id)
        if team is None or not team.is_captain(interaction.user.id):
            await interaction.response.send_message(
                "Нет доступа.", ephemeral=True)
            return
        team.description = (self.desc.value or "").strip()
        await team_store.update_team(team)
        await squad_store.audit(team.guild_id, team_id,
                                interaction.user.id, "desc_change")
        svc.schedule_hub_refresh(interaction.guild)
        await open_team_panel(interaction, team_id,
                              notice="Описание обновлено.")


async def _render_purpose_select(interaction: discord.Interaction,
                                 team_id: str) -> None:
    view = DesignView(title="Цель команды", accent=colors.BRAND)
    view.text("Выберите цель команды.")

    async def _cb(i):
        val = (i.data.get("values") or [PURPOSES[0]])[0]
        team = await team_store.refresh_from_db(team_id)
        if team is None or not team.is_captain(i.user.id):
            await i.response.send_message("Нет доступа.", ephemeral=True)
            return
        team.purpose = val
        await team_store.update_team(team)
        svc.schedule_hub_refresh(i.guild)
        await open_team_panel(i, team_id, notice="Цель обновлена.")

    view.row(_s([option(label=PURPOSE_LABELS[p], value=p)
                 for p in PURPOSES],
                "tm:m:purpose:sel", "Цель", _cb))
    view.row(_b("Назад", "secondary", "tm:m:back2",
                lambda i: _render_manage(i, team_id)))
    await interaction.response.edit_message(view=view)


async def _render_joinmode_select(interaction: discord.Interaction,
                                  team_id: str) -> None:
    view = DesignView(title="Тип вступления", accent=colors.BRAND)
    view.text("Выберите, как игроки могут вступать в команду.")

    async def _cb(i):
        val = (i.data.get("values") or [JOIN_APPLICATIONS])[0]
        team = await team_store.refresh_from_db(team_id)
        if team is None or not team.is_captain(i.user.id):
            await i.response.send_message("Нет доступа.", ephemeral=True)
            return
        if val == JOIN_PASSWORD:
            await i.response.send_modal(TeamPasswordModal(team_id))
            return
        team.join_mode = val
        if val != JOIN_PASSWORD:
            team.password_hash = ""
            team.password = ""
        await team_store.update_team(team)
        svc.schedule_hub_refresh(i.guild)
        await open_team_panel(i, team_id, notice="Настройки вступления "
                                           "обновлены.")

    view.row(_s([option(label=JOIN_MODE_LABELS[m], value=m)
                 for m in JOIN_MODES],
                "tm:m:join:sel", "Тип вступления", _cb))
    view.row(_b("Назад", "secondary", "tm:m:back3",
                lambda i: _render_manage(i, team_id)))
    await interaction.response.edit_message(view=view)


class TeamPasswordModal(discord.ui.Modal, title="Пароль команды"):
    def __init__(self, team_id: str, *, enabling: bool = True):
        super().__init__()
        self.team_id = team_id
        self.enabling = enabling
        self.pw = discord.ui.TextInput(
            label="Пароль (2–64 символа)", style=discord.TextStyle.short,
            required=True, min_length=2, max_length=64)
        self.add_item(self.pw)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        team = await team_store.refresh_from_db(self.team_id)
        if team is None or not team.is_captain(interaction.user.id):
            await interaction.response.send_message(
                "Нет доступа.", ephemeral=True)
            return
        team.set_password(self.pw.value)
        team.join_mode = JOIN_PASSWORD
        await team_store.update_team(team)
        svc.schedule_hub_refresh(interaction.guild)
        await open_team_panel(interaction, self.team_id,
                              notice="Включён вход по паролю.")


async def _render_requirements(interaction: discord.Interaction,
                               team_id: str) -> None:
    """Подменю требований к игрокам (4 select'а)."""
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_captain(interaction.user.id):
        await interaction.response.send_message(
            "Нет доступа.", ephemeral=True)
        return
    req = dict(team.requirements or {})
    view = DesignView(title="Требования к игрокам",
                      accent=colors.BRAND)
    view.text(
        "Обязательные условия вступления и пожелания капитана.\n"
        "-# Пустое значение — требование не установлено.")

    async def _save(i, **kw):
        req.update(kw)
        # убрать пустые значения
        req2 = {k: v for k, v in req.items() if v not in ("", None, 0)}
        t = await team_store.refresh_from_db(team_id)
        if t is None or not t.is_captain(i.user.id):
            await i.response.send_message("Нет доступа.", ephemeral=True)
            return
        t.requirements = req2
        await team_store.update_team(t)
        svc.schedule_hub_refresh(i.guild)
        await _render_requirements(i, team_id)

    async def _minsr(i):
        val = (i.data.get("values") or ["0"])[0]
        await _save(i, min_sr=float(val) if float(val) > 0 else None)

    async def _dev(i):
        val = (i.data.get("values") or [""])[0]
        await _save(i, device=val or None)

    async def _role(i):
        val = (i.data.get("values") or [""])[0]
        await _save(i, role=val or None)

    async def _ffuid(i):
        val = (i.data.get("values") or ["0"])[0]
        await _save(i, ff_uid_required=(val == "1"))

    cur_min = req.get("min_sr") or 0
    view.row(_s(
        [option(label="Без требований", value="0")] +
        [option(label=f"SR {v:.1f}+", value=str(v))
         for v in (5.0, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0)],
        "tm:req:sr", "Минимальный SR",
        _minsr))
    # Текущий выбранный SR — default
    view.row(_s(
        [option(label="Любое", value="")] +
        [option(label=DEVICE_LABELS[d], value=d) for d in DEVICES],
        "tm:req:dev", "Допустимое устройство", _dev))
    view.row(_s(
        [option(label="Любая", value="")] +
        [option(label=r, value=r) for r in PLAY_ROLES],
        "tm:req:role", "Предпочтительная роль", _role))
    view.row(_s(
        [option(label="Не обязательно", value="0"),
         option(label="Обязателен", value="1")],
        "tm:req:ff", "Подтверждённый Free Fire UID", _ffuid))
    view.row(_b("Назад", "secondary", "tm:req:back",
                lambda i: _render_manage(i, team_id)))
    await interaction.response.edit_message(view=view)


async def _do_toggle_recruiting(interaction: discord.Interaction,
                                team_id: str) -> None:
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_captain(interaction.user.id):
        await interaction.response.send_message(
            "Нет доступа.", ephemeral=True)
        return
    team.recruiting = not team.recruiting
    await team_store.update_team(team)
    svc.schedule_hub_refresh(interaction.guild)
    await svc.sync_recruit_card(interaction.client, interaction.guild,
                                team_id)
    await open_team_panel(interaction, team_id,
                          notice=("Набор открыт." if team.recruiting
                                   else "Набор закрыт."))


# --- участники ---


async def _render_members(interaction: discord.Interaction,
                          team_id: str) -> None:
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_captain(interaction.user.id):
        await interaction.response.send_message(
            "Нет доступа.", ephemeral=True)
        return
    view = DesignView(title="Участники команды",
                      accent=colors.BRAND)
    lines = []
    options = []
    for idx, uid in enumerate(team.members):
        brief = await svc.player_brief(guild.id, uid, guild)
        role = "Капитан" if uid == team.captain_id else "Участник"
        lines.append(f"**{brief['name']}** — {role} · "
                     f"SR {svc.fmt_sr(brief['sr'])}")
        if uid != team.captain_id:
            options.append(option(
                label=brief["name"][:100], value=str(uid),
                description=f"SR {svc.fmt_sr(brief['sr'])}"[:100]))
    view.text("\n".join(lines) or "Нет участников")

    if options:
        async def _pick(i):
            vals = i.data.get("values") or []
            if vals:
                await _render_member_card(i, team_id, int(vals[0]))
        view.row(_s(options, "tm:mem:pick", "Управление участником",
                    _pick))
    view.row(_b("Назад", "secondary", "tm:mem:back",
                lambda i: _render_manage(i, team_id)))
    await interaction.response.edit_message(view=view)


async def _render_member_card(interaction: discord.Interaction,
                              team_id: str, target_id: int) -> None:
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_captain(interaction.user.id):
        await interaction.response.send_message(
            "Нет доступа.", ephemeral=True)
        return
    if target_id not in team.members:
        await interaction.response.send_message(
            "Игрок не в команде.", ephemeral=True)
        return
    brief = await svc.player_brief(guild.id, target_id, guild)
    is_captain_target = target_id == team.captain_id
    view = DesignView(title=brief["name"], accent=colors.BRAND)
    view.text(
        f"**SR:** {svc.fmt_sr(brief['sr'])}  \n"
        f"**ELO:** {svc.fmt_elo(brief['elo'])}  \n"
        f"**Роль:** {'Капитан' if is_captain_target else 'Участник'}")

    row = discord.ui.ActionRow()

    async def _kick(i):
        await _render_kick_confirm(i, team_id, target_id)

    async def _transfer(i):
        await _do_request_transfer(i, team_id, target_id)

    if not is_captain_target:
        row.add_item(_b("Исключить", "danger", "tm:mem:kick", _kick))
        row.add_item(_b("Передать капитанство", "primary",
                        "tm:mem:transfer", _transfer))
    row.add_item(_b("Назад", "secondary", "tm:mem:back2",
                    lambda i: _render_members(i, team_id)))
    view.row(*row.children)
    await interaction.response.edit_message(view=view)


async def _render_kick_confirm(interaction: discord.Interaction,
                               team_id: str, target_id: int) -> None:
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_captain(interaction.user.id):
        return
    brief = await svc.player_brief(guild.id, target_id, guild)
    view = DesignView(title="Исключение участника",
                      accent=colors.BRAND)
    reason = await svc.active_tournament_reason(guild.id, team_id)
    if reason:
        view.text(f"{brief['name']} не может быть исключён.\n{reason}")
        view.row(_b("Назад", "secondary", "tm:kick:back",
                    lambda i: _render_member_card(i, team_id, target_id)))
        await interaction.response.edit_message(view=view)
        return
    view.text(f"Исключить **{brief['name']}** из команды "
              f"**{team.team_name}**?")

    async def _yes(i):
        try:
            await team_store.remove_member(team_id, target_id,
                                           actor_id=i.user.id,
                                           guild_id=guild.id)
        except TeamStoreError as exc:
            await i.response.send_message(_err(exc), ephemeral=True)
            return
        await squad_store.audit(guild.id, team_id, i.user.id,
                                "member_kick", target_id=target_id)
        svc.schedule_hub_refresh(guild)
        await svc.sync_recruit_card(i.client, guild, team_id)
        await open_team_panel(i, team_id,
                              notice=f"{brief['name']} исключён.")

    async def _no(i):
        await _render_member_card(i, team_id, target_id)

    view.row(
        _b("Исключить", "danger", "tm:kick:yes", _yes),
        _b("Отмена", "secondary", "tm:kick:no", _no))
    await interaction.response.edit_message(view=view)


# --- передача капитанства ---


async def _render_transfer(interaction: discord.Interaction,
                           team_id: str) -> None:
    """Список кандидатов на капитанство (из меню управления)."""
    await _render_members_transfer(interaction, team_id)


async def _render_members_transfer(interaction: discord.Interaction,
                                   team_id: str) -> None:
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_captain(interaction.user.id):
        return
    options = []
    for uid in team.members:
        if uid == team.captain_id:
            continue
        brief = await svc.player_brief(guild.id, uid, guild)
        options.append(option(label=brief["name"][:100],
                              value=str(uid)))
    view = DesignView(title="Передать капитанство",
                      accent=colors.BRAND)
    if not options:
        view.text("В команде нет других участников.")
    else:
        view.text("Выберите нового капитана — он должен подтвердить "
                  "передачу.")

        async def _pick(i):
            vals = i.data.get("values") or []
            if vals:
                await _do_request_transfer(i, team_id, int(vals[0]))
        view.row(_s(options, "tm:tr:pick", "Новый капитан", _pick))
    view.row(_b("Назад", "secondary", "tm:tr:back",
                lambda i: _render_manage(i, team_id)))
    await interaction.response.edit_message(view=view)


async def _do_request_transfer(interaction: discord.Interaction,
                               team_id: str, target_id: int) -> None:
    """Капитан запрашивает передачу — новый капитан подтверждает."""
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_captain(interaction.user.id):
        await interaction.response.send_message(
            "Нет доступа.", ephemeral=True)
        return
    if target_id == team.captain_id or target_id not in team.members:
        await interaction.response.send_message(
            "Игрок не может стать капитаном.", ephemeral=True)
        return
    reason = await svc.active_tournament_reason(guild.id, team_id)
    if reason:
        await interaction.response.send_message(reason, ephemeral=True)
        return
    svc.request_captain_transfer(guild.id, team_id, target_id)
    await squad_store.audit(guild.id, team_id, interaction.user.id,
                            "captain_transfer_request",
                            target_id=target_id)
    # DM новому капитану (best effort)
    try:
        target = guild.get_member(target_id)
        if target is not None:
            dm = await target.create_dm()
            view = DesignView(title="Передача капитанства",
                              timeout=None)
            view.text(
                f"**{interaction.user.display_name}** предлагает вам "
                f"стать капитаном команды **{team.team_name}**.\n"
                "-# Подтвердите в течение 5 минут.")
            async def _yes(i, _tid=team_id, _uid=target_id):
                await _do_confirm_captain(i, _tid, via_dm=True)
            async def _no(i, _tid=team_id):
                svc.cancel_transfer_request(guild.id, _tid)
                await i.response.send_message(
                    "Передача отклонена.", ephemeral=True)
            view.row(
                _b("Принять", "success", "tm:capdm:yes", _yes),
                _b("Отклонить", "secondary", "tm:capdm:no", _no))
            await dm.send(view=view, allowed_mentions=_NO_MENTIONS)
    except (discord.Forbidden, discord.HTTPException):
        pass
    await interaction.response.send_message(
        f"Запрос на передачу отправлен. Ожидает подтверждения "
        f"<@{target_id}>.", ephemeral=True)


async def _do_confirm_captain(interaction: discord.Interaction,
                              team_id: str, *, via_dm: bool = False) -> None:
    """Новый капитан подтверждает передачу."""
    guild = interaction.guild
    uid = interaction.user.id
    team = await team_store.refresh_from_db(team_id)
    if team is None:
        await interaction.response.send_message(
            "Команда не найдена.", ephemeral=True)
        return
    if not svc.pending_transfer_for(guild.id, team_id, uid):
        await interaction.response.send_message(
            "Нет активного запроса на передачу капитанства.",
            ephemeral=True)
        return
    if uid not in team.members or uid == team.captain_id:
        await interaction.response.send_message(
            "Вы не можете принять капитанство.", ephemeral=True)
        return
    try:
        await team_store.confirm_captain_transfer(team_id, uid)
    except TeamStoreError as exc:
        await interaction.response.send_message(_err(exc), ephemeral=True)
        return
    svc.consume_transfer_request(guild.id, team_id, uid)
    await squad_store.audit(guild.id, team_id, team.captain_id,
                            "captain_transfer", target_id=uid)
    svc.schedule_hub_refresh(guild)
    fresh = await team_store.refresh_from_db(team_id)
    notice = "Вы стали капитаном команды!"
    if via_dm:
        await interaction.response.send_message(notice, ephemeral=True)
    else:
        await open_team_panel(interaction, team_id, notice=notice)
    # уведомить старого капитана
    try:
        old = guild.get_member(team.captain_id)
        if old is not None:
            dm = await old.create_dm()
            await dm.send(
                f"Капитанство команды **{team.team_name}** передано "
                f"<@{uid}>.", allowed_mentions=_NO_MENTIONS)
    except (discord.Forbidden, discord.HTTPException):
        pass


# --- выход / расформирование ---


async def _render_leave_confirm(interaction: discord.Interaction,
                                team_id: str) -> None:
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_member(interaction.user.id):
        await interaction.response.send_message(
            "Вы не в этой команде.", ephemeral=True)
        return
    if team.is_captain(interaction.user.id):
        await interaction.response.send_message(
            "Капитан не может покинуть команду — передайте "
            "капитанство или расформируйте её.", ephemeral=True)
        return
    view = DesignView(title="Покинуть команду", accent=colors.BRAND)
    view.text(f"Выйти из команды **{team.team_name}**?")

    async def _yes(i):
        try:
            await team_store.leave_team(team_id, i.user.id)
        except TeamStoreError as exc:
            await i.response.send_message(_err(exc), ephemeral=True)
            return
        await squad_store.audit(guild.id, team_id, i.user.id,
                                "member_leave")
        svc.schedule_hub_refresh(guild)
        await svc.sync_recruit_card(i.client, guild, team_id)
        await i.response.send_message(
            f"Вы покинули команду **{team.team_name}**.", ephemeral=True)

    async def _no(i):
        await open_team_panel(i, team_id)

    view.row(
        _b("Выйти", "danger", "tm:leave:yes", _yes),
        _b("Отмена", "secondary", "tm:leave:no", _no))
    await interaction.response.edit_message(view=view)


def build_disband_view(guild: discord.Guild, team) -> discord.ui.LayoutView:
    """Карточка подтверждения расформирования (для /team delete и
    меню управления)."""
    view = DesignView(title="Расформирование команды",
                      accent=colors.BRAND)
    view.text(
        f"Вы собираетесь расформировать команду **{team.team_name}**.\n"
        f"Участников: {len(team.members)}/{TEAM_MAX_MEMBERS}.\n"
        "Все игроки покинут состав. История турниров и "
        "индивидуальная статистика сохранятся.")

    async def _yes(i):
        await _do_disband(i, team.team_id)

    async def _no(i):
        await open_team_panel(i, team.team_id)

    view.row(
        _b("Расформировать", "danger", "tm:dis:yes", _yes),
        _b("Отмена", "secondary", "tm:dis:no", _no))
    return view


async def open_disband_command(interaction: discord.Interaction,
                               team_id: str) -> None:
    """/team delete — отправить подтверждение новым ephemeral."""
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None:
        await interaction.followup.send(
            "Команда не найдена.", ephemeral=True)
        return
    reason = await svc.active_tournament_reason(guild.id, team_id)
    if reason:
        await interaction.followup.send(reason, ephemeral=True)
        return
    await interaction.followup.send(
        view=build_disband_view(guild, team), ephemeral=True,
        allowed_mentions=_NO_MENTIONS)


async def _render_disband_confirm(interaction: discord.Interaction,
                                  team_id: str) -> None:
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_captain(interaction.user.id):
        await interaction.response.send_message(
            "Только капитан может расформировать команду.", ephemeral=True)
        return
    reason = await svc.active_tournament_reason(guild.id, team_id)
    if reason:
        view = DesignView(title="Расформирование команды",
                          accent=colors.BRAND)
        view.text(f"Команда **{team.team_name}** не может быть "
                  f"расформирована.\n{reason}")
        view.row(_b("Назад", "secondary", "tm:dis:back",
                    lambda i: _render_manage(i, team_id)))
        await interaction.response.edit_message(view=view)
        return
    await interaction.response.edit_message(
        view=build_disband_view(guild, team))


async def _do_disband(interaction: discord.Interaction,
                      team_id: str) -> None:
    """Финальное расформирование: перепроверки + атомарная операция."""
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None:
        await interaction.response.send_message(
            "Команда не найдена.", ephemeral=True)
        return
    # Повторная проверка капитанства и актуальности
    if team.captain_id != interaction.user.id:
        await interaction.response.send_message(
            "Только капитан может расформировать команду.",
            ephemeral=True)
        return
    reason = await svc.active_tournament_reason(guild.id, team_id)
    if reason:
        await interaction.response.send_message(reason, ephemeral=True)
        return
    # Архивируем рекрутинговую карточку визуально до удаления
    if team.recruitment_message_id and team.recruitment_channel_id:
        try:
            ch = guild.get_channel(team.recruitment_channel_id)
            if ch is not None:
                msg = await ch.fetch_message(team.recruitment_message_id)
                view = DesignView(title=f"{team.team_name} — расформирована",
                                  timeout=None)
                view.text(
                    f"## {team.team_name}\n\n"
                    "Команда расформирована.")
                await msg.edit(view=view,
                               allowed_mentions=_NO_MENTIONS)
        except (discord.NotFound, discord.Forbidden):
            pass
    try:
        name = await team_store.disband(team_id, interaction.user.id)
    except TeamStoreError as exc:
        await interaction.response.send_message(_err(exc), ephemeral=True)
        return
    # Закрыть заявки/приглашения/LFG участников
    for uid in team.members:
        await squad_store.cancel_applications_for_user(
            guild.id, uid, "team_disbanded")
        await squad_store.cancel_invites_for_user(
            guild.id, uid, "team_disbanded")
        await squad_store.deactivate_lfg_for_user(guild.id, uid)
    await squad_store.audit(guild.id, None, interaction.user.id,
                            "team_disband", target_id=team.captain_id,
                            metadata={"team_name": name})
    svc.schedule_hub_refresh(guild)
    await interaction.response.send_message(
        f"Команда **{name}** расформирована.", ephemeral=True)


# --- публикация рекрутинговой карточки ---


async def _do_publish(interaction: discord.Interaction,
                      team_id: str) -> None:
    guild = interaction.guild
    bot = interaction.client
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_captain(interaction.user.id):
        await interaction.response.send_message(
            "Только капитан публикует набор.", ephemeral=True)
        return
    now = time.time()
    last = _PUBLISH_CD.get(team_id, 0)
    if now - last < _PUBLISH_CD_SECONDS:
        wait = int(_PUBLISH_CD_SECONDS - (now - last))
        await interaction.response.send_message(
            f"Повторная публикация доступна через {wait} сек.",
            ephemeral=True)
        return
    if not svc.recruitment_channel_id(guild.id):
        await interaction.response.send_message(
            "Канал рекрутинга не настроен — обратитесь к "
            "администратору.", ephemeral=True)
        return
    try:
        msg = await svc.publish_recruitment_card(bot, guild, team)
    except Exception as exc:
        logger.warning("publish recruitment failed: %s", exc)
        await interaction.response.send_message(
            "Не удалось опубликовать карточку.", ephemeral=True)
        return
    if msg is None:
        await interaction.response.send_message(
            "Канал рекрутинга недоступен.", ephemeral=True)
        return
    _PUBLISH_CD[team_id] = now
    await squad_store.audit(guild.id, team_id, interaction.user.id,
                            "recruit_publish",
                            metadata={"message_id": msg.id})
    await interaction.response.send_message(
        "Карточка набора опубликована.", ephemeral=True)
