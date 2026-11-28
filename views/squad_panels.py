"""Squad Hub 2.0 — приватные панели: каталог команд, LFG,
список приглашений, поиск игроков для капитана, исходящие заявки.

Все панели — ephemeral Components V2 (DesignView). Состояние
(фильтры/страница каталога, черновик LFG) хранится в памяти по
(guild_id, user_id) — панели приватные и пересоздаются на каждый
вызов.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import discord

from models.team import (
    DEVICE_LABELS, DEVICES, JOIN_APPLICATIONS, JOIN_CLOSED, JOIN_MODE_LABELS,
    JOIN_MODES, JOIN_OPEN, JOIN_PASSWORD, PLAY_ROLES, PURPOSE_LABELS,
    PURPOSES, TEAM_MAX_MEMBERS,
)
from services import squad_service as svc
from storage import squad_store
from storage.team_store import team_store, TeamStoreError
from utils.design import colors
from utils.design.components import btn, option, select
from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()

# ----------------------------------------------------------------------
# Эфемерное состояние панелей
# ----------------------------------------------------------------------

_UI_STATE: dict[tuple[int, int], dict[str, Any]] = {}
_STATE_TTL = 900  # 15 минут


def _state(guild_id: int, user_id: int) -> dict[str, Any]:
    key = (int(guild_id), int(user_id))
    st = _UI_STATE.get(key)
    if st is None or time.time() - st.get("_ts", 0) > _STATE_TTL:
        st = {"_ts": time.time()}
        _UI_STATE[key] = st
    st["_ts"] = time.time()
    return st


def _btn(label: str, style: str, cid: str, callback) -> discord.ui.Button:
    b = btn(label, style=style, custom_id=cid)
    b.callback = callback  # type: ignore[assignment]
    return b


# Discord требует value опции длиной 1..100 — пустая строка
# («Любое») отклоняется API с 50035. Пустое значение кодируем
# сентинелом и возвращаем "" в callback.
_ANY_VALUE = "__any__"


def _sel(options: list[discord.SelectOption], cid: str,
         placeholder: str, callback) -> discord.ui.Select:
    fixed = []
    for o in options:
        if not o.value:
            o = discord.SelectOption(
                label=o.label, value=_ANY_VALUE,
                description=o.description, emoji=o.emoji,
                default=o.default)
        fixed.append(o)
    s = select(fixed, custom_id=cid, placeholder=placeholder)

    async def _cb(interaction: discord.Interaction) -> None:
        vals = interaction.data.get("values")
        if vals:
            interaction.data["values"] = [
                "" if v == _ANY_VALUE else v for v in vals]
        await callback(interaction)

    s.callback = _cb  # type: ignore[assignment]
    return s


def _err_text(exc: TeamStoreError | Exception) -> str:
    code = getattr(exc, "code", "")
    msg = getattr(exc, "message", "") or str(exc)
    return msg or code or "Ошибка операции"


# ----------------------------------------------------------------------
# «Моя команда» / «Приглашения» из Squad Hub
# ----------------------------------------------------------------------


async def open_my_team(interaction: discord.Interaction) -> None:
    """Кнопка «Моя команда» публичной карточки."""
    from views.team_panel_view import open_team_panel
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Команды работают только на сервере.", ephemeral=True)
        return
    team = team_store.get_by_member_in_guild(guild.id, interaction.user.id)
    if team is None:
        await interaction.response.send_message(
            view=_no_team_view(guild.id, interaction.user.id),
            ephemeral=True, allowed_mentions=_NO_MENTIONS)
        return
    await open_team_panel(interaction, team.team_id)


def _no_team_view(guild_id: int, user_id: int) -> discord.ui.LayoutView:
    async def _find(i: discord.Interaction):
        await open_catalog(i)

    async def _create(i: discord.Interaction):
        from views.team_create_view import open_create_flow
        await open_create_flow(i)

    view = DesignView(title="У вас нет команды", accent=colors.BRAND)
    view.text(
        "Найдите команду в каталоге, подайте заявку или создайте "
        "собственную.")
    view.row(
        _btn("Найти команду", "primary", "squad:nt:find", _find),
        _btn("Создать команду", "success", "squad:nt:create", _create),
    )
    return view


async def open_invites_panel(interaction: discord.Interaction) -> None:
    """Кнопка «Приглашения» — входящие приглашения пользователя."""
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Приглашения работают только на сервере.", ephemeral=True)
        return
    invites = await squad_store.list_pending_invites_for_user(
        guild.id, interaction.user.id)
    view = DesignView(title="Приглашения в команды",
                      accent=colors.BRAND)
    if not invites:
        view.text("Активных приглашений нет.")
    else:
        lines = []
        rows = []
        for inv in invites[:10]:
            team = team_store.get(inv.team_id)
            tname = team.team_name if team else "—"
            lines.append(
                f"**{tname}** — приглашение от <@{inv.inviter_id}>  \n"
                f"-# до {discord.utils.format_dt(inv.expires_at, style='R')}"
                if inv.expires_at else f"**{tname}**")
            # Кнопки принять/отклонить/карточка — по invite_id
            row = discord.ui.ActionRow()
            async def _accept(i, _inv=inv):
                await accept_invite(i, _inv.invite_id)
            async def _decline(i, _inv=inv):
                await decline_invite(i, _inv.invite_id)
            async def _show(i, _inv=inv):
                await show_invite_card(i, _inv.invite_id)
            row.add_item(_btn("Принять", "success",
                              f"squad:invok:{inv.invite_id}", _accept))
            row.add_item(_btn("Отклонить", "secondary",
                              f"squad:invno:{inv.invite_id}", _decline))
            row.add_item(_btn("Команда", "secondary",
                              f"squad:invw:{inv.invite_id}", _show))
            rows.append(row)
        view.text("\n\n".join(lines))
        for r in rows[:5]:  # лимит action rows в карточке
            view.row(*r.children)
    view.row(_btn("Обновить", "secondary", "squad:inv:refresh",
                  lambda i: open_invites_panel(i)))
    await interaction.response.send_message(
        view=view, ephemeral=True, allowed_mentions=_NO_MENTIONS)


async def accept_invite(interaction: discord.Interaction,
                        invite_id: int) -> None:
    """Принять приглашение: перепроверки + атомарное вступление.

    Работает и из ЛС (guild может быть None — guild_id берём из
    команды)."""
    inv = await squad_store.get_invite(invite_id)
    if inv is None or not inv.is_pending or inv.is_expired:
        await interaction.response.send_message(
            "Приглашение недоступно или истекло.", ephemeral=True)
        return
    if inv.invited_user_id != interaction.user.id:
        await interaction.response.send_message(
            "Это приглашение адресовано другому игроку.", ephemeral=True)
        return
    team = await team_store.refresh_from_db(inv.team_id)
    if team is None:
        await squad_store.resolve_invite(invite_id, "cancelled")
        await interaction.response.send_message(
            "Команда больше не существует.", ephemeral=True)
        return
    gid = int(team.guild_id)
    guild = interaction.guild or interaction.client.get_guild(gid)
    # P0.4: при принятии из ЛС проверяем, что игрок всё ещё
    # участник сервера.
    if guild is None:
        await interaction.response.send_message(
            "Не удалось проверить членство на сервере — "
            "откройте приглашение на сервере.", ephemeral=True)
        return
    try:
        member = guild.get_member(interaction.user.id)
        if member is None:
            member = await guild.fetch_member(interaction.user.id)
    except discord.NotFound:
        member = None
    except Exception:
        member = None
    if member is None:
        await interaction.response.send_message(
            "Вы больше не участник этого сервера — "
            "приглашение недействительно.", ephemeral=True)
        return
    await join_team_action(interaction, team.team_id, via="invite",
                           invite_id=invite_id)


async def decline_invite(interaction: discord.Interaction,
                         invite_id: int) -> None:
    inv = await squad_store.get_invite(invite_id)
    if inv and inv.invited_user_id == interaction.user.id:
        await squad_store.resolve_invite(invite_id, "declined")
    await interaction.response.send_message(
        "Приглашение отклонено.", ephemeral=True)


async def show_invite_card(interaction: discord.Interaction,
                           invite_id: int) -> None:
    """«Посмотреть команду» из приглашения."""
    from views.team_recruit_view import invite_card_view
    inv = await squad_store.get_invite(invite_id)
    if inv is None:
        await interaction.response.send_message(
            "Приглашение не найдено.", ephemeral=True)
        return
    team = await team_store.refresh_from_db(inv.team_id)
    if team is None:
        await interaction.response.send_message(
            "Команда больше не существует.", ephemeral=True)
        return
    view = await invite_card_view(interaction.guild, team, inv)
    await interaction.response.send_message(
        view=view, ephemeral=True, allowed_mentions=_NO_MENTIONS)


# ----------------------------------------------------------------------
# Каталог команд
# ----------------------------------------------------------------------

_SORTS = {
    "new": "Недавно обновлённые",
    "slots": "По свободным местам",
    "sr": "По среднему SR",
    "created": "По времени создания",
}
_PAGE_SIZE = 6


async def open_catalog(interaction: discord.Interaction) -> None:
    """Кнопка «Найти команду» — приватный каталог."""
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Каталог работает только на сервере.", ephemeral=True)
        return
    st = _state(guild.id, interaction.user.id)
    st["cat"] = {"sort": "new", "page": 0,
                 "purpose": "", "join": "", "sr_min": 0.0,
                 "device": ""}
    await _render_catalog(interaction)


async def _catalog_list(guild: discord.Guild,
                        filt: dict[str, Any]) -> list[dict[str, Any]]:
    teams = await squad_store.catalog_teams(guild.id)
    out = []
    for t in teams:
        if filt.get("purpose") and t["purpose"] != filt["purpose"]:
            continue
        if filt.get("join") and t["join_mode"] != filt["join"]:
            continue
        req = t.get("requirements") or {}
        if filt.get("device") and req.get("device") \
                and req["device"] != filt["device"]:
            continue
        out.append(t)
    sort = filt.get("sort", "new")
    if sort == "slots":
        out.sort(key=lambda t: -(TEAM_MAX_MEMBERS - len(t["members"])))
    elif sort == "sr":
        # SR считается асинхронно — сортируем по заполненности как
        # прокси, точная сортировка в _render через avg_sr
        pass
    elif sort == "created":
        out.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    else:
        out.sort(key=lambda t: t.get("updated_at") or "",
                 reverse=True)
    return out


async def _render_catalog(interaction: discord.Interaction,
                          *, edit: bool = False) -> None:
    guild = interaction.guild
    if guild is None:
        return
    uid = interaction.user.id
    st = _state(guild.id, uid)
    cat = st.get("cat") or {"sort": "new", "page": 0}
    st["cat"] = cat
    teams = await _catalog_list(guild, cat)
    # Сортировка по SR — отдельным проходом
    if cat.get("sort") == "sr":
        scored = []
        for t in teams:
            avg = await svc.team_avg_sr(guild.id, t["members"], guild)
            scored.append((avg if avg is not None else -1.0, t))
        scored.sort(key=lambda x: -x[0])
        teams = [t for _, t in scored]
    if cat.get("sr_min"):
        filtered = []
        for t in teams:
            avg = await svc.team_avg_sr(guild.id, t["members"], guild)
            if avg is not None and avg >= float(cat["sr_min"]):
                filtered.append(t)
        teams = filtered

    total_pages = max(1, (len(teams) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(cat.get("page", 0), total_pages - 1))
    cat["page"] = page
    chunk = teams[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]

    view = DesignView(title="Каталог команд", accent=colors.BRAND)
    if not teams:
        view.text("Подходящих команд не найдено.\n"
                  "-# Ослабьте фильтры или создайте свою команду.")
    else:
        lines = [f"**Команд:** {len(teams)} · страница {page + 1}/"
                 f"{total_pages}", ""]
        for i, t in enumerate(chunk):
            idx = page * _PAGE_SIZE + i + 1
            members = t["members"]
            free = TEAM_MAX_MEMBERS - len(members)
            avg = await svc.team_avg_sr(guild.id, members, guild)
            captain = guild.get_member(t["captain_id"])
            cname = captain.display_name if captain else "—"
            lines.append(
                f"**{idx:02d}. {t['team_name']}**  \n"
                f"-# {len(members)}/{TEAM_MAX_MEMBERS} · "
                f"свободно {free} · SR {svc.fmt_sr(avg)} · "
                f"{t['purpose_label'] if 'purpose_label' in t else PURPOSE_LABELS.get(t['purpose'], t['purpose'])} · "
                f"{JOIN_MODE_LABELS.get(t['join_mode'], t['join_mode'])} · "
                f"капитан {cname}")
        view.text("\n".join(lines))

        # Select команд на странице
        options = []
        for i, t in enumerate(chunk):
            idx = page * _PAGE_SIZE + i + 1
            options.append(option(
                label=f"{idx:02d}. {t['team_name']}"[:100],
                value=t["team_id"],
                description=(
                    f"{len(t['members'])}/{TEAM_MAX_MEMBERS} · "
                    f"{JOIN_MODE_LABELS.get(t['join_mode'], '')}")[:100]))
        if options:
            async def _pick(i, _opts=options):
                vals = i.data.get("values") or []
                if vals:
                    await open_team_detail(i, vals[0])
            view.row(_sel(options, "squad:cat:pick",
                                 "Выберите команду", _pick))

    # Фильтры
    frow = discord.ui.ActionRow()

    async def _sort_cb(i):
        val = (i.data.get("values") or ["new"])[0]
        _state(guild.id, i.user.id).setdefault("cat", {})["sort"] = val
        await _render_catalog_edit(i)

    async def _purpose_cb(i):
        val = (i.data.get("values") or [""])[0]
        _state(guild.id, i.user.id).setdefault("cat", {})["purpose"] = val
        await _render_catalog_edit(i)

    async def _join_cb(i):
        val = (i.data.get("values") or [""])[0]
        _state(guild.id, i.user.id).setdefault("cat", {})["join"] = val
        await _render_catalog_edit(i)

    async def _sr_cb(i):
        val = (i.data.get("values") or ["0"])[0]
        _state(guild.id, i.user.id).setdefault("cat", {})["sr_min"] = float(val)
        await _render_catalog_edit(i)

    async def _device_cb(i):
        val = (i.data.get("values") or [""])[0]
        _state(guild.id, i.user.id).setdefault("cat", {})["device"] = val
        await _render_catalog_edit(i)

    view.row(_sel(
        [option(label=v, value=k) for k, v in _SORTS.items()],
        "squad:cat:sort", "Сортировка", _sort_cb))
    view.row(_sel(
        [option(label="Все цели", value="")] +
        [option(label=PURPOSE_LABELS[p], value=p) for p in PURPOSES],
        "squad:cat:purpose", "Цель команды", _purpose_cb))
    view.row(_sel(
        [option(label="Все типы", value="")] +
        [option(label=JOIN_MODE_LABELS[m], value=m) for m in JOIN_MODES
         if m != JOIN_CLOSED],
        "squad:cat:join", "Тип вступления", _join_cb))
    view.row(_sel(
        [option(label="Любой SR", value="0"),
         option(label="SR 6.0+", value="6"),
         option(label="SR 7.0+", value="7"),
         option(label="SR 8.0+", value="8")],
        "squad:cat:sr", "Средний SR", _sr_cb))
    view.row(_sel(
        [option(label="Любое устройство", value="")] +
        [option(label=DEVICE_LABELS[d], value=d) for d in DEVICES],
        "squad:cat:device", "Требование по устройству", _device_cb))

    nav = []
    if page > 0:
        async def _prev(i):
            _state(guild.id, i.user.id).setdefault("cat", {})["page"] = page - 1
            await _render_catalog_edit(i)
        nav.append(_btn("◀ Назад", "secondary", "squad:cat:prev", _prev))
    if page < total_pages - 1:
        async def _next(i):
            _state(guild.id, i.user.id).setdefault("cat", {})["page"] = page + 1
            await _render_catalog_edit(i)
        nav.append(_btn("Вперёд ▶", "secondary", "squad:cat:next", _next))
    async def _reset(i):
        _state(guild.id, i.user.id)["cat"] = {
            "sort": "new", "page": 0, "purpose": "", "join": "",
            "sr_min": 0.0, "device": ""}
        await _render_catalog_edit(i)
    nav.append(_btn("Сбросить фильтры", "secondary", "squad:cat:reset", _reset))
    async def _refresh(i):
        await _render_catalog_edit(i)
    nav.append(_btn("Обновить", "secondary", "squad:cat:refresh", _refresh))
    async def _back(i):
        from views.squad_hub_view import hub_menu_view
        await i.response.edit_message(view=hub_menu_view())
    nav.append(_btn("Назад", "secondary", "squad:cat:back", _back))
    view.row(*nav)

    if edit:
        await interaction.response.edit_message(view=view)
    else:
        await interaction.response.send_message(
            view=view, ephemeral=True, allowed_mentions=_NO_MENTIONS)


async def _render_catalog_edit(interaction: discord.Interaction) -> None:
    """Перерисовать каталог на месте (component callback).

    edit_message — первый ответ на component-interaction;
    defer() здесь нельзя: после него edit_message упал бы с
    InteractionResponded."""
    await _render_catalog(interaction, edit=True)


# ----------------------------------------------------------------------
# Карточка команды из каталога
# ----------------------------------------------------------------------


async def open_team_detail(interaction: discord.Interaction,
                           team_id: str) -> None:
    """Подробная карточка команды (из каталога)."""
    guild = interaction.guild
    if guild is None:
        return
    team = await team_store.refresh_from_db(team_id)
    if team is None or team.guild_id != guild.id:
        await interaction.response.send_message(
            "Команда не найдена.", ephemeral=True)
        return
    view = await build_team_card_view(guild, team, interaction.user.id,
                                      source="catalog")
    await interaction.response.send_message(
        view=view, ephemeral=True, allowed_mentions=_NO_MENTIONS)


async def build_team_card_view(guild: discord.Guild, team,
                               user_id: int,
                               *, source: str = "catalog") -> discord.ui.LayoutView:
    """Публичная (в ephemeral) карточка команды с действиями."""
    members = team.members
    avg = await svc.team_avg_sr(guild.id, members, guild)
    captain = guild.get_member(team.captain_id)
    lines = [
        f"## {team.team_name}",
        "",
        f"**Состав:** {len(members)}/{TEAM_MAX_MEMBERS}  ",
        f"**Свободно:** {team.free_slots()}  ",
        f"**Средний SR:** {svc.fmt_sr(avg)}  ",
        f"**Вступление:** {team.join_mode_label}  ",
        f"**Цель:** {team.purpose_label}  ",
        f"**Капитан:** {captain.display_name if captain else f'<@{team.captain_id}>'}",
        f"**Набор:** {'открыт' if team.recruiting else 'закрыт'}",
    ]
    if team.description:
        lines += ["", team.description[:300]]
    req = team.requirements or {}
    req_bits = []
    if req.get("min_sr"):
        req_bits.append(f"SR от {req['min_sr']}")
    if req.get("device"):
        req_bits.append(f"Устройство: {DEVICE_LABELS.get(req['device'], req['device'])}")
    if req.get("role"):
        req_bits.append(f"Роль: {req['role']}")
    if req_bits:
        lines += ["", "**Требования:** " + " · ".join(req_bits)]
    lines += ["", "**Участники**"]
    for idx in range(TEAM_MAX_MEMBERS):
        if idx < len(members):
            lines.append(await svc.member_line(guild, team, idx))
        else:
            lines.append(f"{idx + 1:02d}. Свободное место")

    view = DesignView(title=team.team_name[:80], accent=colors.BRAND)
    view.text("\n".join(lines)[:3900])

    uid = user_id
    is_member = team.is_member(uid)
    is_captain = team.is_captain(uid)
    row = discord.ui.ActionRow()

    if is_captain:
        async def _open_panel(i):
            from views.team_panel_view import open_team_panel
            await open_team_panel(i, team.team_id)
        row.add_item(_btn("Открыть панель", "primary", "squad:td:panel", _open_panel))
    elif is_member:
        async def _panel(i):
            from views.team_panel_view import open_team_panel
            await open_team_panel(i, team.team_id)
        row.add_item(_btn("Моя команда", "primary", "squad:td:panel2", _panel))
    else:
        if svc.can_join_open(team):
            async def _join(i, _t=team):
                await join_team_action(i, _t.team_id, via="open")
            row.add_item(_btn("Вступить", "success", "squad:td:join", _join))
        if svc.can_apply(team):
            async def _apply(i, _t=team):
                await open_apply_modal(i, _t.team_id)
            row.add_item(_btn("Подать заявку", "primary", "squad:td:apply", _apply))
        if svc.needs_password(team):
            async def _pw(i, _t=team):
                await open_password_modal(i, _t.team_id)
            row.add_item(_btn("Ввести пароль", "secondary", "squad:td:pw", _pw))
        if team.join_mode == JOIN_CLOSED or not team.recruiting or team.is_full():
            b = btn("Вступление недоступно", style="secondary",
                    custom_id="squad:td:none", disabled=True)
            row.add_item(b)

    async def _back(i):
        await open_catalog(i)

    row.add_item(_btn("Назад", "secondary", "squad:td:back", _back))
    view.row(*row.children)
    return view


async def _user_sr(guild_id: int, user_id: int) -> float | None:
    """SR игрока из существующей системы (None — нет данных)."""
    try:
        from storage.player_stats_store import player_stats_store
        stats = await player_stats_store.get(guild_id, user_id)
        if stats is not None and stats.games > 0:
            return float(stats.skill_rating)
    except Exception:
        pass
    return None


async def join_team_action(interaction: discord.Interaction,
                           team_id: str, *, via: str,
                           password: str = "",
                           invite_id: int | None = None,
                           application_id: int | None = None) -> None:
    """Единая точка вступления: все проверки и сайд-эффекты —
    в транзакции team_store.join_team (P0.3)."""
    guild = interaction.guild
    team = team_store.get(team_id)
    gid = int(team.guild_id) if team is not None else (
        guild.id if guild else 0)
    if gid == 0:
        return
    sr = await _user_sr(gid, interaction.user.id)
    try:
        fresh = await team_store.join_team(
            team_id, interaction.user.id, gid, via=via,
            password=password, invite_id=invite_id,
            application_id=application_id, sr=sr)
    except TeamStoreError as exc:
        try:
            await interaction.response.send_message(
                f"Не удалось вступить: {_err_text(exc)}",
                ephemeral=True)
        except Exception:
            pass
        return
    except Exception as exc:
        logger.warning("join_team_action failed: %s", exc)
        try:
            await interaction.response.send_message(
                "Ошибка базы данных. Попробуйте позже.",
                ephemeral=True)
        except Exception:
            pass
        return
    # Пост-коммит сайд-эффекты (карточки/хаб)
    if guild is not None:
        svc.schedule_hub_refresh(guild)
        await svc.sync_recruit_card(interaction.client, guild,
                                    team_id)
        from views.team_panel_view import send_team_panel
        await send_team_panel(interaction, fresh.team_id,
                              f"Вы вступили в команду "
                              f"**{fresh.team_name}**.")
    else:
        try:
            await interaction.response.send_message(
                f"Вы вступили в команду **{fresh.team_name}**.",
                allowed_mentions=_NO_MENTIONS)
        except Exception:
            pass


# ----------------------------------------------------------------------
# Модалки вступления (пароль / заявка)
# ----------------------------------------------------------------------


class PasswordJoinModal(discord.ui.Modal, title="Вступление по паролю"):
    def __init__(self, team_id: str):
        super().__init__()
        self.team_id = team_id
        self.password = discord.ui.TextInput(
            label="Пароль команды", style=discord.TextStyle.short,
            required=True, max_length=64)
        self.add_item(self.password)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        team = await team_store.refresh_from_db(self.team_id)
        if team is None:
            await interaction.response.send_message(
                "Команда не найдена.", ephemeral=True)
            return
        await join_team_action(interaction, team.team_id,
                               via="password",
                               password=self.password.value)


async def open_password_modal(interaction: discord.Interaction,
                              team_id: str) -> None:
    await interaction.response.send_modal(PasswordJoinModal(team_id))


class ApplyModal(discord.ui.Modal, title="Заявка в команду"):
    def __init__(self, team_id: str):
        super().__init__()
        self.team_id = team_id
        self.message = discord.ui.TextInput(
            label="Сообщение капитану", style=discord.TextStyle.paragraph,
            required=False, max_length=300,
            placeholder="Пара слов о себе: роль, SR, опыт…")
        self.add_item(self.message)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        team = await team_store.refresh_from_db(self.team_id)
        if team is None:
            await interaction.response.send_message(
                "Команда не найдена.", ephemeral=True)
            return
        if not svc.can_apply(team):
            await interaction.response.send_message(
                "Команда больше не принимает заявки.",
                ephemeral=True)
            return
        if team_store.get_by_member_in_guild(guild.id,
                                             interaction.user.id):
            await interaction.response.send_message(
                "Вы уже состоите в команде.", ephemeral=True)
            return
        app = await squad_store.create_application(
            guild.id, team.team_id, interaction.user.id,
            self.message.value or "")
        if app is None:
            await interaction.response.send_message(
                "Заявка уже подана или команда недоступна.",
                ephemeral=True)
            return
        await squad_store.audit(guild.id, team.team_id,
                                interaction.user.id, "application_submit",
                                metadata={"application_id": app.application_id})
        await interaction.response.send_message(
            f"Заявка в **{team.team_name}** отправлена. "
            "Капитан уведомлён в своей панели.", ephemeral=True)


async def open_apply_modal(interaction: discord.Interaction,
                           team_id: str) -> None:
    await interaction.response.send_modal(ApplyModal(team_id))


# ----------------------------------------------------------------------
# LFG — «Ищу команду»
# ----------------------------------------------------------------------


async def open_lfg(interaction: discord.Interaction) -> None:
    """Кнопка «Ищу команду» — настройка/управление поиском."""
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Работает только на сервере.", ephemeral=True)
        return
    uid = interaction.user.id
    if team_store.get_by_member_in_guild(guild.id, uid):
        await interaction.response.send_message(
            "Вы уже в команде — поиск команды не нужен.",
            ephemeral=True)
        return
    existing = await squad_store.get_lfg(guild.id, uid)
    await _render_lfg(interaction, existing)


async def _render_lfg(interaction: discord.Interaction,
                      profile: squad_store.LfgProfile | None,
                      *, edit: bool = False) -> None:
    guild = interaction.guild
    uid = interaction.user.id
    view = DesignView(title="Ищу команду", accent=colors.BRAND)
    if profile and profile.is_active:
        exp = profile.expires_at
        view.text(
            "Ваш поиск активен.\n"
            f"-# Устройство: {DEVICE_LABELS.get(profile.device, profile.device or '—')} · "
            f"Роль: {profile.play_role or '—'} · "
            f"Цель: {PURPOSE_LABELS.get(profile.purpose, profile.purpose or '—')}\n"
            f"-# Действует до "
            f"{discord.utils.format_dt(exp, style='R') if exp else '—'}")
    else:
        view.text("Вы не ищете команду. Заполните параметры и "
                  "включите поиск — капитаны смогут найти вас.")

    st = _state(guild.id, uid)
    draft = st.setdefault("lfg", {})
    if profile:
        draft.setdefault("device", profile.device)
        draft.setdefault("role", profile.play_role)
        draft.setdefault("purpose", profile.purpose)

    async def _device_cb(i):
        draft["device"] = (i.data.get("values") or [""])[0]
        await _render_lfg_edit(i)

    async def _role_cb(i):
        draft["role"] = (i.data.get("values") or [""])[0]
        await _render_lfg_edit(i)

    async def _purpose_cb(i):
        draft["purpose"] = (i.data.get("values") or [""])[0]
        await _render_lfg_edit(i)

    view.row(_sel(
        [option(label="Не указано", value="")] +
        [option(label=DEVICE_LABELS[d], value=d) for d in DEVICES],
        "squad:lfg:device", "Устройство", _device_cb))
    view.row(_sel(
        [option(label="Не указано", value="")] +
        [option(label=r, value=r) for r in PLAY_ROLES],
        "squad:lfg:role", "Игровая роль", _role_cb))
    view.row(_sel(
        [option(label="Не указано", value="")] +
        [option(label=PURPOSE_LABELS[p], value=p) for p in PURPOSES],
        "squad:lfg:purpose", "Цель", _purpose_cb))

    async def _desc(i):
        await i.response.send_modal(LfgDescModal())

    async def _activate(i):
        desc = draft.get("desc", "")
        prof = await squad_store.upsert_lfg(
            guild.id, i.user.id, device=draft.get("device", ""),
            play_role=draft.get("role", ""),
            purpose=draft.get("purpose", ""), description=desc)
        if prof is None:
            await i.response.send_message(
                "Не удалось сохранить поиск (нет БД).", ephemeral=True)
            return
        await i.response.send_message(
            "Поиск команды активен на 72 часа.", ephemeral=True)
        svc.schedule_hub_refresh(guild)

    async def _off(i):
        await squad_store.deactivate_lfg(guild.id, i.user.id)
        svc.schedule_hub_refresh(guild)
        await i.response.send_message(
            "Поиск команды отключён.", ephemeral=True)

    row = discord.ui.ActionRow()
    row.add_item(_btn("Описание", "secondary", "squad:lfg:desc", _desc))
    row.add_item(_btn("Включить / Продлить", "success",
                      "squad:lfg:on", _activate))
    if profile and profile.is_active:
        row.add_item(_btn("Отключить", "danger", "squad:lfg:off", _off))
    view.row(*row.children)

    if edit:
        await interaction.response.edit_message(view=view)
    else:
        await interaction.response.send_message(
            view=view, ephemeral=True, allowed_mentions=_NO_MENTIONS)


async def _render_lfg_edit(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    profile = await squad_store.get_lfg(guild.id, interaction.user.id) \
        if guild else None
    await _render_lfg(interaction, profile, edit=True)


class LfgDescModal(discord.ui.Modal, title="Описание для поиска"):
    def __init__(self):
        super().__init__()
        self.desc = discord.ui.TextInput(
            label="Пара слов о себе", style=discord.TextStyle.paragraph,
            required=False, max_length=200)
        self.add_item(self.desc)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        _state(interaction.guild.id, interaction.user.id) \
            .setdefault("lfg", {})["desc"] = self.desc.value or ""
        await interaction.response.send_message(
            "Описание сохранено — нажмите «Включить / Продлить».",
            ephemeral=True)


# ----------------------------------------------------------------------
# Поиск игроков (каталог LFG для капитана)
# ----------------------------------------------------------------------


async def open_player_search(interaction: discord.Interaction,
                             team_id: str | None = None) -> None:
    """Капитан ищет свободных игроков по LFG."""
    guild = interaction.guild
    if guild is None:
        return
    if team_id:
        team = await team_store.refresh_from_db(team_id)
        if team is None or not team.is_captain(interaction.user.id):
            await interaction.response.send_message(
                "Только капитан может искать игроков.", ephemeral=True)
            return
    st = _state(guild.id, interaction.user.id)
    st["ps"] = {"device": "", "role": "", "purpose": "", "page": 0}
    await _render_player_search(interaction)


async def _render_player_search(interaction: discord.Interaction,
                                *, edit: bool = False) -> None:
    guild = interaction.guild
    uid = interaction.user.id
    st = _state(guild.id, uid)
    ps = st.get("ps") or {}
    st["ps"] = ps

    # Исключаем уже состоящих в командах
    member_ids = set()
    for t in team_store.get_by_guild(guild.id):
        member_ids.update(t.members)
    profiles = await squad_store.list_lfg(
        guild.id, exclude_user_ids=member_ids,
        device=ps.get("device", ""), play_role=ps.get("role", ""),
        purpose=ps.get("purpose", ""), limit=50)

    view = DesignView(title="Поиск игроков", accent=colors.BRAND)
    if not profiles:
        view.text("Свободных игроков не найдено.\n"
                  "-# Игроки появляются здесь, когда включают "
                  "«Ищу команду» в Squad Hub.")
    else:
        lines = [f"**Игроков:** {len(profiles)}", ""]
        options = []
        for p in profiles[:25]:
            brief = await svc.player_brief(guild.id, p.user_id, guild)
            lines.append(
                f"**{brief['name']}**  \n"
                f"-# SR {svc.fmt_sr(brief['sr'])} · ELO {svc.fmt_elo(brief['elo'])}"
                f" · {DEVICE_LABELS.get(p.device, p.device or '—')}"
                f" · {p.play_role or '—'}"
                + (f" · {PURPOSE_LABELS.get(p.purpose, p.purpose)}"
                   if p.purpose else ""))
            options.append(option(
                label=brief["name"][:100],
                value=str(p.user_id),
                description=(
                    f"SR {svc.fmt_sr(brief['sr'])} · "
                    f"{DEVICE_LABELS.get(p.device, p.device or '—')} · "
                    f"{p.play_role or '—'}")[:100]))
        view.text("\n".join(lines)[:3900])
        if options:
            async def _pick(i):
                vals = i.data.get("values") or []
                if vals:
                    await _render_player_card(i, int(vals[0]))
            view.row(_sel(options, "squad:ps:pick",
                                 "Выберите игрока", _pick))

    async def _device_cb(i):
        ps["device"] = (i.data.get("values") or [""])[0]
        await _render_ps_edit(i)

    async def _role_cb(i):
        ps["role"] = (i.data.get("values") or [""])[0]
        await _render_ps_edit(i)

    async def _purpose_cb(i):
        ps["purpose"] = (i.data.get("values") or [""])[0]
        await _render_ps_edit(i)

    view.row(_sel(
        [option(label="Любое", value="")] +
        [option(label=DEVICE_LABELS[d], value=d) for d in DEVICES],
        "squad:ps:device", "Устройство", _device_cb))
    view.row(_sel(
        [option(label="Любая", value="")] +
        [option(label=r, value=r) for r in PLAY_ROLES],
        "squad:ps:role", "Роль", _role_cb))
    view.row(_sel(
        [option(label="Любая", value="")] +
        [option(label=PURPOSE_LABELS[p], value=p) for p in PURPOSES],
        "squad:ps:purpose", "Цель", _purpose_cb))

    async def _back(i):
        from views.team_panel_view import open_team_panel
        # Вернуться в панель команды капитана, если она была
        team = team_store.get_by_captain(uid)
        if team is not None:
            await open_team_panel(i, team.team_id)
        else:
            await i.response.send_message(
                "Панель команды не найдена.", ephemeral=True)

    view.row(_btn("Назад", "secondary", "squad:ps:back", _back))

    if edit:
        await interaction.response.edit_message(view=view)
    else:
        await interaction.response.send_message(
            view=view, ephemeral=True, allowed_mentions=_NO_MENTIONS)


async def _render_ps_edit(interaction: discord.Interaction) -> None:
    await _render_player_search(interaction, edit=True)


async def _render_player_card(interaction: discord.Interaction,
                              target_id: int) -> None:
    guild = interaction.guild
    brief = await svc.player_brief(guild.id, target_id, guild)
    lfg = await squad_store.get_lfg(guild.id, target_id)
    view = DesignView(title=brief["name"], accent=colors.BRAND)
    lines = [
        f"**SR:** {svc.fmt_sr(brief['sr'])}  ",
        f"**ELO:** {svc.fmt_elo(brief['elo'])}  ",
        f"**Устройство:** {DEVICE_LABELS.get(lfg.device if lfg else '', (lfg.device if lfg else '') or '—')}  ",
        f"**Роль:** {(lfg.play_role if lfg else '') or '—'}  ",
    ]
    if lfg and lfg.purpose:
        lines.append(f"**Цель:** {PURPOSE_LABELS.get(lfg.purpose, lfg.purpose)}")
    if lfg and lfg.description:
        lines += ["", lfg.description[:300]]
    view.text("\n".join(lines))

    team = team_store.get_by_captain(interaction.user.id)
    row = discord.ui.ActionRow()
    if team is not None:
        async def _inv(i, _t=team, _u=target_id):
            await send_invite(i, _t.team_id, _u)
        row.add_item(_btn("Пригласить", "success", "squad:ps:inv", _inv))

    async def _back(i):
        await _render_player_search(i, edit=True)
    row.add_item(_btn("Назад", "secondary", "squad:ps:back2", _back))
    view.row(*row.children)
    await interaction.response.edit_message(view=view)


# ----------------------------------------------------------------------
# Отправка приглашения (из панели капитана / карточки игрока)
# ----------------------------------------------------------------------


async def send_invite(interaction: discord.Interaction, team_id: str,
                      target_user_id: int) -> None:
    """Капитан приглашает игрока (валидации + DM + fallback)."""
    guild = interaction.guild
    if guild is None:
        return
    team = await team_store.refresh_from_db(team_id)
    if team is None or not team.is_captain(interaction.user.id):
        await interaction.response.send_message(
            "Только капитан может приглашать.", ephemeral=True)
        return
    if target_user_id == interaction.user.id:
        await interaction.response.send_message(
            "Нельзя пригласить самого себя.", ephemeral=True)
        return
    target = guild.get_member(target_user_id)
    if target is None:
        try:
            target = await guild.fetch_member(target_user_id)
        except discord.NotFound:
            target = None
    if target is None:
        await interaction.response.send_message(
            "Игрок не найден на сервере.", ephemeral=True)
        return
    if target.bot:
        await interaction.response.send_message(
            "Нельзя пригласить бота.", ephemeral=True)
        return
    if team.is_member(target_user_id):
        await interaction.response.send_message(
            "Игрок уже в команде.", ephemeral=True)
        return
    other = team_store.get_by_member_in_guild(guild.id,
                                              target_user_id)
    if other is not None and other.team_id != team.team_id:
        await interaction.response.send_message(
            "Игрок уже состоит в другой команде.", ephemeral=True)
        return
    if team.is_full():
        await interaction.response.send_message(
            "Состав заполнен.", ephemeral=True)
        return
    inv = await squad_store.create_invite(
        guild.id, team.team_id, interaction.user.id, target_user_id)
    if inv is None:
        await interaction.response.send_message(
            "Приглашение уже отправлено или ошибка БД.", ephemeral=True)
        return
    # DM — best effort; приглашение доступно и через Squad Hub
    try:
        dm = await target.create_dm()
        from views.team_recruit_view import build_invite_dm_view
        avg = await svc.team_avg_sr(guild.id, team.members, guild)
        msg = await dm.send(
            view=await build_invite_dm_view(guild, team, inv, avg),
            allowed_mentions=_NO_MENTIONS)
        await squad_store.set_invite_dm(inv.invite_id, dm.id, msg.id)
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.info("invite DM failed (hub fallback): %s", exc)
    await squad_store.audit(guild.id, team.team_id, interaction.user.id,
                            "invite_sent", target_id=target_user_id,
                            metadata={"invite_id": inv.invite_id})
    await interaction.response.send_message(
        f"Приглашение отправлено для {target.display_name}. "
        "Если ЛС закрыты — оно доступно в Squad Hub → Приглашения.",
        ephemeral=True)


# ----------------------------------------------------------------------
# «Мои заявки» — исходящие заявки игрока
# ----------------------------------------------------------------------


async def open_my_applications(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return
    apps = await squad_store.list_pending_applications_for_user(
        guild.id, interaction.user.id)
    view = DesignView(title="Мои заявки", accent=colors.BRAND)
    if not apps:
        view.text("Активных заявок нет.")
    else:
        lines = []
        rows = []
        for app in apps[:10]:
            team = team_store.get(app.team_id)
            tname = team.team_name if team else "—"
            lines.append(
                f"**{tname}** — подана "
                f"{discord.utils.format_dt(app.created_at, style='R') if app.created_at else ''}")
            row = discord.ui.ActionRow()

            async def _view(i, _a=app):
                t = team_store.get(_a.team_id)
                if t is None:
                    await i.response.send_message(
                        "Команда не найдена.", ephemeral=True)
                    return
                v = await build_team_card_view(
                    i.guild, t, i.user.id, source="apps")
                await i.response.send_message(
                    view=v, ephemeral=True, allowed_mentions=_NO_MENTIONS)

            async def _withdraw(i, _a=app):
                ok = await squad_store.withdraw_application(
                    _a.application_id, i.user.id)
                await i.response.send_message(
                    "Заявка отозвана." if ok else "Не удалось отозвать.",
                    ephemeral=True)

            row.add_item(_btn("Команда", "secondary",
                              f"squad:appv:{app.application_id}", _view))
            row.add_item(_btn("Отозвать", "danger",
                              f"squad:appw:{app.application_id}", _withdraw))
            rows.append(row)
        view.text("\n".join(lines))
        for r in rows[:5]:
            view.row(*r.children)
    view.row(_btn("Обновить", "secondary", "squad:apps:refresh",
                  lambda i: open_my_applications(i)))
    await interaction.response.send_message(
        view=view, ephemeral=True, allowed_mentions=_NO_MENTIONS)
