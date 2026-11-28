"""Административный интерфейс команд (Squad Hub 2.0).

Приватный Discord-интерфейс для администраторов сервера:
просмотр всех команд, поиск, расформирование, сброс команд
прошлого Cash Cup и полный сброс команд сервера (с предпросмотром,
подтверждением, проверкой активных турниров, резервной копией и
записью в team_audit).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord

from config import DATA_DIR
from models.team import TEAM_MAX_MEMBERS
from services import squad_service as svc
from storage import squad_store
from storage.team_store import team_store, TeamStoreError
from utils.design import colors
from utils.design.components import btn, option, select
from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()


def _b(label: str, style: str, cid: str, cb) -> discord.ui.Button:
    b = btn(label, style=style, custom_id=cid)
    b.callback = cb  # type: ignore[assignment]
    return b


def _s(options: list, cid: str, placeholder: str, cb) -> discord.ui.Select:
    s = select(options, custom_id=cid, placeholder=placeholder)
    s.callback = cb  # type: ignore[assignment]
    return s


def _err(exc: Exception) -> str:
    return getattr(exc, "message", "") or str(exc) or "Ошибка"


async def _is_admin_now(interaction: discord.Interaction) -> bool:
    """Актуальная проверка прав r!z3 на момент callback.

    Использует существующую систему авторизации (administrator /
    владелец бота / ADMIN_USER_IDS / роли org) — Manage Guild
    сам по себе админ-доступ к командам не даёт (P0.4)."""
    guild = interaction.guild
    if guild is None:
        return False
    member = guild.get_member(interaction.user.id)
    if member is None:
        try:
            member = await guild.fetch_member(interaction.user.id)
        except Exception:
            member = None
    if member is None:
        return False
    try:
        from utils.permissions import is_admin_check
        return bool(is_admin_check(member, guild))
    except Exception:
        return False


async def require_admin(interaction: discord.Interaction) -> bool:
    if await _is_admin_now(interaction):
        return True
    try:
        await interaction.response.send_message(
            "Требуются права администратора.", ephemeral=True)
    except Exception:
        pass
    return False


async def _admin_guard(interaction: discord.Interaction) -> bool:
    """True — доступ запрещён (ответ уже отправлен)."""
    if await _is_admin_now(interaction):
        return False
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "Недостаточно прав.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "Недостаточно прав.", ephemeral=True)
    except Exception:
        pass
    return True


# ----------------------------------------------------------------------
# Главное меню
# ----------------------------------------------------------------------


async def open_admin_panel(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Работает только на сервере.", ephemeral=True)
        return
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    view = await _admin_main_view(guild)
    await interaction.followup.send(
        view=view, ephemeral=True, allowed_mentions=_NO_MENTIONS)


async def _admin_main_view(guild: discord.Guild) -> discord.ui.LayoutView:
    teams = team_store.get_by_guild(guild.id)
    recruiting = sum(1 for t in teams if t.recruiting)
    view = DesignView(title="Администрирование команд", accent=colors.BRAND)
    view.text(
        f"**Команд на сервере:** {len(teams)}  \n"
        f"**С набором:** {recruiting}")

    async def _all(i):
        await _render_team_list(i)

    async def _search(i):
        await i.response.send_modal(AdminSearchModal())

    async def _past_cc(i):
        await _render_reset_preview(i, scope="past_cashcup")

    async def _all_reset(i):
        await _render_reset_preview(i, scope="all")

    view.row(
        _b("Все команды", "primary", "adm:all", _all),
        _b("Поиск", "secondary", "adm:search", _search),
    )
    view.row(
        _b("Сброс команд прошлого Cash Cup", "secondary",
           "adm:resetcc", _past_cc),
        _b("Полный сброс команд сервера", "danger",
           "adm:resetall", _all_reset),
    )
    return view


# ----------------------------------------------------------------------
# Список команд / поиск
# ----------------------------------------------------------------------


async def _render_team_list(interaction: discord.Interaction,
                            teams: list | None = None,
                            title: str = "Все команды") -> None:
    guild = interaction.guild
    if teams is None:
        teams = team_store.get_by_guild(guild.id)
    view = DesignView(title=title[:80], accent=colors.BRAND)
    if not teams:
        view.text("Команд не найдено.")
        view.row(_b("Назад", "secondary", "adm:back",
                    lambda i: _back_main(i)))
        await interaction.response.edit_message(view=view)
        return
    lines = [f"**Команд:** {len(teams)} — выберите для управления."]
    view.text("\n".join(lines))
    options = []
    for t in teams[:25]:
        options.append(option(
            label=t.team_name[:100], value=t.team_id,
            description=(
                f"{len(t.members)}/{TEAM_MAX_MEMBERS} · "
                f"{'набор' if t.recruiting else 'закрыт'}")[:100]))
    briefs = {t.team_id: t for t in teams[:25]}

    async def _pick(i):
        if await _admin_guard(i):
            return
        vals = i.data.get("values") or []
        if vals:
            await _render_team_admin(i, vals[0])

    view.row(_s(options, "adm:pick", "Команда", _pick))
    view.row(_b("Назад", "secondary", "adm:back2",
                lambda i: _back_main(i)))
    await interaction.response.edit_message(view=view)


async def _back_main(interaction: discord.Interaction) -> None:
    view = await _admin_main_view(interaction.guild)
    await interaction.response.edit_message(view=view)


class AdminSearchModal(discord.ui.Modal, title="Поиск команды"):
    def __init__(self):
        super().__init__()
        self.q = discord.ui.TextInput(
            label="Название, капитан или ID участника",
            style=discord.TextStyle.short, required=True, max_length=100)
        self.add_item(self.q)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if await _admin_guard(interaction):
            return
        guild = interaction.guild
        q = self.q.value.strip().lower()
        results = []
        for t in team_store.get_by_guild(guild.id):
            if q in t.team_name.lower():
                results.append(t)
                continue
            member = guild.get_member(t.captain_id)
            cname = (member.display_name if member else "").lower()
            if q in str(t.captain_id) or q in cname:
                results.append(t)
                continue
            for uid in t.members:
                m = guild.get_member(uid)
                dname = (m.display_name if m else "").lower()
                if q in str(uid) or q in dname:
                    results.append(t)
                    break
        await _render_team_list(interaction, results,
                                title=f"Поиск: {self.q.value[:60]}")


async def _render_team_admin(interaction: discord.Interaction,
                             team_id: str) -> None:
    if await _admin_guard(interaction):
        return
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None or team.guild_id != guild.id:
        await interaction.response.send_message(
            "Команда не найдена.", ephemeral=True)
        return
    avg = await svc.team_avg_sr(guild.id, team.members, guild)
    in_tournament = await svc.is_team_in_active_tournament(
        guild.id, team_id)
    lines = [
        f"## {team.team_name}",
        "",
        f"**Капитан:** <@{team.captain_id}>  ",
        f"**Состав:** {len(team.members)}/{TEAM_MAX_MEMBERS}  ",
        f"**Средний SR:** {svc.fmt_sr(avg)}  ",
        f"**Набор:** {'открыт' if team.recruiting else 'закрыт'} · "
        f"{team.join_mode_label}  ",
        f"**Цель:** {team.purpose_label}  ",
        f"**Создана:** {team.created_at[:10] if team.created_at else '—'}",
    ]
    if in_tournament:
        lines.append("**Турнир:** активная регистрация — "
                     "управление составом заблокировано")
    view = DesignView(title=team.team_name[:80], accent=colors.BRAND)
    view.text("\n".join(lines)[:3900])

    async def _disband(i):
        await _render_admin_disband(i, team_id)

    view.row(
        _b("Расформировать", "danger", "adm:disband", _disband),
        _b("Назад", "secondary", "adm:back3",
           lambda i: _render_team_list(i)),
    )
    await interaction.response.edit_message(view=view)


async def _render_admin_disband(interaction: discord.Interaction,
                                team_id: str) -> None:
    if await _admin_guard(interaction):
        return
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None:
        return
    reason = await svc.active_tournament_reason(guild.id, team_id)
    view = DesignView(title="Расформирование (админ)",
                      accent=colors.BRAND)
    if reason:
        view.text(f"**{team.team_name}** не может быть "
                  f"расформирована.\n{reason}")
        view.row(_b("Назад", "secondary", "adm:disback",
                    lambda i: _render_team_admin(i, team_id)))
        await interaction.response.edit_message(view=view)
        return
    view.text(
        f"Административное расформирование **{team.team_name}**.\n"
        f"Участников: {len(team.members)}. Действие необратимо.")

    async def _yes(i):
        await _do_admin_disband(i, team_id)

    async def _no(i):
        await _render_team_admin(i, team_id)

    view.row(
        _b("Расформировать", "danger", "adm:disyes", _yes),
        _b("Отмена", "secondary", "adm:disno", _no))
    await interaction.response.edit_message(view=view)


async def _do_admin_disband(interaction: discord.Interaction,
                            team_id: str) -> None:
    if await _admin_guard(interaction):
        return
    guild = interaction.guild
    team = await team_store.refresh_from_db(team_id)
    if team is None:
        await interaction.response.send_message(
            "Команда не найдена.", ephemeral=True)
        return
    reason = await svc.active_tournament_reason(guild.id, team_id)
    if reason:
        await interaction.response.send_message(reason, ephemeral=True)
        return
    name = team.team_name
    members = list(team.members)
    try:
        await team_store.admin_disband(team_id, guild.id,
                                       interaction.user.id)
    except TeamStoreError as exc:
        await interaction.response.send_message(_err(exc),
                                                ephemeral=True)
        return
    for uid in members:
        await squad_store.cancel_applications_for_user(
            guild.id, uid, "team_disbanded")
        await squad_store.cancel_invites_for_user(
            guild.id, uid, "team_disbanded")
        await squad_store.deactivate_lfg_for_user(guild.id, uid)
    svc.schedule_hub_refresh(guild)
    view = DesignView(title="Готово", accent=colors.BRAND)
    view.text(f"Команда **{name}** расформирована.")
    view.row(_b("В меню", "secondary", "adm:done",
                lambda i: _back_main(i)))
    await interaction.response.edit_message(view=view)


# ----------------------------------------------------------------------
# Сбросы (P0.6)
# ----------------------------------------------------------------------

# Подтверждённые снапшоты сброса: (guild_id, user_id) → запись.
# Снапшот фиксирует точный список team_id + версии составов на
# момент предпросмотра; TTL 10 минут.
_PENDING_RESETS: dict[tuple[int, int], dict[str, Any]] = {}
_RESET_TTL = 600.0


async def _teams_for_reset(guild_id: int,
                           scope: str) -> tuple[list, str, dict | None]:
    """Команды под сброс + описание scope + данные последнего
    завершённого Cash Cup (для сравнения составов).

    past_cashcup — команды, у которых team_id совпадает с
    br_team_id из последнего ЗАВЕРШЁННОГО (status='completed')
    реального Cash Cup — не latest(), который может вернуть
    активный или отменённый турнир.
    """
    cup = None
    if scope == "all":
        return (team_store.get_by_guild(guild_id),
                "все команды сервера", None)
    from services.cashcup_store import cashcup_store
    cup = await cashcup_store.last_completed(guild_id)
    if not cup:
        return [], "прошлый завершённый Cash Cup не найден", None
    br_ids = {t.get("br_team_id") for t in cup.get("teams") or []
              if t.get("br_team_id")}
    teams = [t for t in team_store.get_by_guild(guild_id)
             if t.team_id in br_ids]
    return (teams,
            f"команды последнего завершённого Cash Cup "
            f"({cup.get('name') or cup.get('tournament_id', '?')})",
            cup)


def _cup_members_by_team(cup: dict | None) -> dict[str, set[int]]:
    """br_team_id → множество discord_id участников по данным
    завершённого турнира (для пометки «состав изменился»)."""
    out: dict[str, set[int]] = {}
    if not cup:
        return out
    for t in cup.get("teams") or []:
        br = t.get("br_team_id")
        if not br:
            continue
        ids = set()
        for p in (t.get("members") or []) + (t.get("pending") or []):
            did = p.get("discord_id") if isinstance(p, dict) else p
            try:
                ids.add(int(did))
            except (TypeError, ValueError):
                pass
        out[str(br)] = ids
    return out


def _snapshots_for(teams: list) -> list[dict[str, Any]]:
    """Точный снимок списка для подтверждения и tx-сверки."""
    return [{
        "team_id": t.team_id,
        "updated_at": t.updated_at,
        "members": sorted(int(m) for m in t.members),
        "captain_id": int(t.captain_id),
        "team_name": t.team_name,
    } for t in teams]


async def _render_reset_preview(interaction: discord.Interaction,
                                scope: str) -> None:
    if await _admin_guard(interaction):
        return
    guild = interaction.guild
    teams, desc, cup = await _teams_for_reset(guild.id, scope)
    # Команды под защитой активных турниров — не трогаем.
    free: list = []
    blocked: list = []
    for t in teams:
        reason = await svc.active_tournament_reason(guild.id,
                                                    t.team_id)
        (blocked if reason else free).append((t, reason))
    cup_members = _cup_members_by_team(cup)
    view = DesignView(title="Предпросмотр сброса", accent=colors.BRAND)
    lines = [
        f"**Scope:** {desc}",
        f"**Команд к сбросу:** {len(free)}",
        f"**Затронуто игроков:** "
        f"{sum(len(t.members) for t, _ in free)}",
    ]
    if blocked:
        lines.append(f"**Защищено активным турниром:** {len(blocked)}")
    # Полный список удаляемых команд (капитан + состав).
    lines.append("")
    lines.append("**Команды к удалению:**")
    shown = 0
    for t, _ in free[:20]:
        changed = ""
        cup_ids = cup_members.get(t.team_id)
        if cup_ids is not None and cup_ids != set(t.members):
            changed = " ⚠️ состав менялся после турнира"
        lines.append(
            f"• **{t.team_name}** — капитан <@{t.captain_id}> · "
            f"{len(t.members)}/{TEAM_MAX_MEMBERS}{changed}")
        shown += 1
    if len(free) > shown:
        lines.append(f"• …и ещё {len(free) - shown}")
    if blocked:
        lines += ["", "**Пропущены (активный турнир):**"]
        for t, _ in blocked[:10]:
            lines.append(f"• {t.team_name}")
    lines += ["", "-# Сброс выполняется одной транзакцией; "
              "составы сверяются со снимком — при изменении "
              "потребуется повторное подтверждение."]
    view.text("\n".join(lines)[:3900])

    async def _yes(i):
        await _do_reset(i, scope)

    async def _no(i):
        await _back_main(i)

    if free:
        # Фиксируем точный список + версии составов. Предварительно
        # обновляем объекты из БД — updated_at в снапшоте должен
        # совпадать со строкой, которую заблокирует disband_many.
        fresh = []
        for t, _ in free:
            ft = await team_store.refresh_from_db(t.team_id)
            fresh.append((ft or t, _))
        _PENDING_RESETS[(guild.id, interaction.user.id)] = {
            "scope": scope,
            "desc": desc,
            "snapshots": _snapshots_for([t for t, _ in fresh]),
            "created": time.monotonic(),
        }
        view.row(
            _b("Подтвердить сброс", "danger", "adm:rsyes", _yes),
            _b("Отмена", "secondary", "adm:rsno", _no))
    else:
        _PENDING_RESETS.pop((guild.id, interaction.user.id), None)
        view.row(_b("Назад", "secondary", "adm:rsback",
                    lambda i: _back_main(i)))
    await interaction.response.edit_message(view=view)


async def _archive_recruit_cards(guild: discord.Guild | None,
                                 removed: list[dict]) -> None:
    """Пост-коммит: заархивировать карточки набора удалённых
    команд (best effort)."""
    if guild is None:
        return
    for item in removed:
        cid, mid = (item.get("recruitment_channel_id"),
                    item.get("recruitment_message_id"))
        if not cid or not mid:
            continue
        try:
            channel = guild.get_channel(int(cid))
            if channel is None:
                channel = await guild.fetch_channel(int(cid))
            msg = await channel.fetch_message(int(mid))
            view = DesignView(title="Набор закрыт",
                              accent=colors.BRAND)
            view.text(
                f"Команда **{item.get('team_name', '?')}** "
                "расформирована — набор закрыт.")
            await msg.edit(view=view)
        except Exception as exc:
            logger.info("recruit card archive failed: %s", exc)


async def _do_reset(interaction: discord.Interaction,
                    scope: str) -> None:
    if await _admin_guard(interaction):
        return
    guild = interaction.guild
    key = (guild.id, interaction.user.id)
    pending = _PENDING_RESETS.pop(key, None)
    if (pending is None or pending.get("scope") != scope
            or time.monotonic() - pending.get("created", 0)
            > _RESET_TTL):
        await interaction.response.send_message(
            "Предпросмотр устарел — откройте сброс заново.",
            ephemeral=True)
        return
    snapshots = pending["snapshots"]
    if not snapshots:
        await interaction.response.send_message(
            "Нет команд для сброса.", ephemeral=True)
        return
    # Резервная копия: актуальные данные подтверждённого списка
    # (disband_many сверит составы в транзакции — бэкап совпадает
    # с тем, что будет удалено).
    backup_path = await _backup_teams(guild.id, snapshots)
    try:
        removed = await team_store.disband_many(
            guild.id, snapshots, interaction.user.id)
    except TeamStoreError as exc:
        code = getattr(exc, "code", "")
        if code in ("list_changed", "tournament_started"):
            # Список изменился — требуем повторного подтверждения.
            await interaction.response.send_message(
                f"{_err(exc)}\nОткройте предпросмотр заново.",
                ephemeral=True)
        else:
            await interaction.response.send_message(
                f"Сброс отменён: {_err(exc)}. Ничего не удалено.",
                ephemeral=True)
        return
    except Exception as exc:
        logger.warning("admin mass reset failed: %s", exc)
        await interaction.response.send_message(
            "Ошибка базы данных — сброс откачен, команды не "
            "удалены.", ephemeral=True)
        return
    # Пост-коммит сайд-эффекты.
    players = 0
    for item in removed:
        players += len(item.get("members") or [])
        for uid in item.get("members") or []:
            try:
                await squad_store.cancel_applications_for_user(
                    guild.id, int(uid), "team_reset")
                await squad_store.cancel_invites_for_user(
                    guild.id, int(uid), "team_reset")
                await squad_store.deactivate_lfg_for_user(
                    guild.id, int(uid))
            except Exception:
                pass
    await _archive_recruit_cards(guild, removed)
    svc.schedule_hub_refresh(guild)
    view = DesignView(title="Сброс выполнен", accent=colors.BRAND)
    view.text(
        f"**Scope:** {pending.get('desc', scope)}\n"
        f"**Удалено команд:** {len(removed)}\n"
        f"**Игроков затронуто:** {players}\n"
        f"**Резервная копия:** {backup_path.name}")
    view.row(_b("В меню", "secondary", "adm:rsdone",
                lambda i: _back_main(i)))
    await interaction.response.edit_message(view=view)


async def _backup_teams(guild_id: int,
                        snapshots: list[dict]) -> Path:
    """JSONL-дамп подтверждённого списка команд перед сбросом.

    Берём актуальные строки по team_id из снимка — disband_many
    в транзакции гарантирует, что они не изменятся до удаления.
    """
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"teams_{guild_id}_{ts}.jsonl"
    teams = []
    for snap in snapshots:
        t = team_store.get(snap["team_id"])
        if t is not None and int(t.guild_id) == int(guild_id):
            teams.append(t)
    with open(path, "w", encoding="utf-8") as f:
        for t in teams:
            f.write(json.dumps(t.to_dict(), ensure_ascii=False)
                    + "\n")
    return path
