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


async def require_admin(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    member = guild.get_member(interaction.user.id) if guild else None
    if member is None or not (
            member.guild_permissions.administrator
            or member.guild_permissions.manage_guild):
        try:
            await interaction.response.send_message(
                "Требуются права администратора.", ephemeral=True)
        except Exception:
            pass
        return False
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
        await team_store.disband(team_id, team.captain_id)
    except TeamStoreError as exc:
        # disband требует actor==captain; для админа обходим через
        # прямое удаление после повторной проверки
        try:
            await _admin_delete_team(guild.id, team_id)
        except Exception as exc2:
            await interaction.response.send_message(
                _err(exc2), ephemeral=True)
            return
    for uid in members:
        await squad_store.cancel_applications_for_user(
            guild.id, uid, "team_disbanded")
        await squad_store.cancel_invites_for_user(
            guild.id, uid, "team_disbanded")
        await squad_store.deactivate_lfg_for_user(guild.id, uid)
    await squad_store.audit(
        guild.id, None, interaction.user.id, "admin_disband",
        metadata={"team_id": team_id, "team_name": name})
    svc.schedule_hub_refresh(guild)
    view = DesignView(title="Готово", accent=colors.BRAND)
    view.text(f"Команда **{name}** расформирована.")
    view.row(_b("В меню", "secondary", "adm:done",
                lambda i: _back_main(i)))
    await interaction.response.edit_message(view=view)


async def _admin_delete_team(guild_id: int, team_id: str) -> None:
    """Административное удаление команды (минуя проверку капитана)."""
    if team_store._use_postgres:
        from storage.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE team_invites SET status='cancelled', "
                    "resolved_at=now() WHERE team_id=$1 "
                    "AND status='pending'", team_id)
                await conn.execute(
                    "UPDATE team_applications SET status='cancelled', "
                    "resolved_at=now() WHERE team_id=$1 "
                    "AND status='pending'", team_id)
                await conn.execute(
                    "DELETE FROM teams WHERE team_id=$1 AND guild_id=$2",
                    team_id, guild_id)
    team_store._teams.pop(team_id, None)
    team_store._save()


# ----------------------------------------------------------------------
# Сбросы
# ----------------------------------------------------------------------


async def _teams_for_reset(guild_id: int,
                           scope: str) -> tuple[list, str]:
    """Список команд под сброс + описание scope.

    past_cashcup — команды, у которых br_team_id совпадает с
    br_team_id из последнего ЗАВЕРШЁННОГО реального Cash Cup.
    """
    if scope == "all":
        return (team_store.get_by_guild(guild_id),
                "все команды сервера")
    # past_cashcup
    from services.cashcup_store import cashcup_store
    state = await cashcup_store.latest(guild_id)
    if not state or state.get("status") in ("registration", "running"):
        return [], "прошлый Cash Cup не найден"
    br_ids = {t.get("br_team_id") for t in state.get("teams") or []
              if t.get("br_team_id")}
    teams = [t for t in team_store.get_by_guild(guild_id)
             if t.team_id in br_ids]
    return teams, f"команды прошлого Cash Cup ({state.get('name', '?')})"


async def _render_reset_preview(interaction: discord.Interaction,
                                scope: str) -> None:
    guild = interaction.guild
    teams, desc = await _teams_for_reset(guild.id, scope)
    # Исключаем команды в активных турнирах
    free = []
    blocked = 0
    for t in teams:
        if await svc.is_team_in_active_tournament(guild.id, t.team_id):
            blocked += 1
        else:
            free.append(t)
    players = sum(len(t.members) for t in free)
    view = DesignView(title="Предпросмотр сброса", accent=colors.BRAND)
    view.text(
        f"**Scope:** {desc}\n"
        f"**Команд к сбросу:** {len(free)}\n"
        f"**Затронуто игроков:** {players}\n"
        + (f"**Пропущено (активный турнир):** {blocked}\n" if blocked else "")
        + "\n-# Перед сбросом создаётся резервная копия, "
        "действие пишется в аудит.")

    async def _yes(i):
        await _do_reset(i, scope)

    async def _no(i):
        await _back_main(i)

    if free:
        view.row(
            _b("Подтвердить сброс", "danger", "adm:rsyes", _yes),
            _b("Отмена", "secondary", "adm:rsno", _no))
    else:
        view.text("Нет команд для сброса.")
        view.row(_b("Назад", "secondary", "adm:rsback",
                    lambda i: _back_main(i)))
    await interaction.response.edit_message(view=view)


async def _do_reset(interaction: discord.Interaction, scope: str) -> None:
    guild = interaction.guild
    teams, _ = await _teams_for_reset(guild.id, scope)
    # Повторная проверка активных турниров
    targets = [t for t in teams
               if not await svc.is_team_in_active_tournament(
                   guild.id, t.team_id)]
    if not targets:
        await interaction.response.send_message(
            "Нет команд для сброса.", ephemeral=True)
        return
    # Резервная копия
    backup_path = await _backup_teams(guild.id, targets)
    removed = 0
    players = 0
    for t in targets:
        members = list(t.members)
        try:
            await _admin_delete_team(guild.id, t.team_id)
            removed += 1
            players += len(members)
            for uid in members:
                await squad_store.cancel_applications_for_user(
                    guild.id, uid, "team_reset")
                await squad_store.cancel_invites_for_user(
                    guild.id, uid, "team_reset")
                await squad_store.deactivate_lfg_for_user(guild.id, uid)
        except Exception as exc:
            logger.warning("admin reset failed for %s: %s",
                           t.team_id, exc)
    await squad_store.audit(
        guild.id, None, interaction.user.id, "admin_reset",
        metadata={"scope": scope, "teams": removed,
                  "players": players, "backup": str(backup_path)})
    svc.schedule_hub_refresh(guild)
    view = DesignView(title="Сброс выполнен", accent=colors.BRAND)
    view.text(
        f"**Scope:** {scope}\n"
        f"**Удалено команд:** {removed}\n"
        f"**Игроков затронуто:** {players}\n"
        f"**Резервная копия:** {backup_path.name}")
    view.row(_b("В меню", "secondary", "adm:rsdone",
                lambda i: _back_main(i)))
    await interaction.response.edit_message(view=view)


async def _backup_teams(guild_id: int,
                        teams: list) -> Path:
    """JSONL-дамп затрагиваемых команд перед сбросом."""
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"teams_{guild_id}_{ts}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for t in teams:
            f.write(json.dumps(t.to_dict(), ensure_ascii=False) + "\n")
    return path
