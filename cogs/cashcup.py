"""r!z3 CASH CUP: командная регистрация и динамический Double Elimination.

Сервис работает отдельно от Tournament/BR/Ultimatum и от игровой экономики.
Призовые выплаты выполняются вне бота; здесь фиксируется результат и аудит.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.freefire import _has_server_tag
from services.freefire_service import FreeFireError, freefire_service
from copy import deepcopy

from services.cashcup_bracket import (BracketError, create_bracket, ready_matches,
                                      record_result, record_final_game, disqualify)
from services.cashcup_store import CashCupError, cashcup_store
from services import vac_service
from services import moderation_service
from utils.ff_profile_formatter import safe_text
from utils.design import compose

logger = logging.getLogger(__name__)
UID_RE = re.compile(r"^\d{5,15}$")
DEVICE_CHOICES = [
    app_commands.Choice(name="Телефон", value="mobile"),
    app_commands.Choice(name="ПК / эмулятор", value="pc"),
]
REGION_CHOICES = [
    app_commands.Choice(name="RU / CIS", value="ru"),
    app_commands.Choice(name="SG", value="sg"),
    app_commands.Choice(name="IND", value="ind"),
    app_commands.Choice(name="BR", value="br"),
    app_commands.Choice(name="BD", value="bd"),
    app_commands.Choice(name="ID", value="id"),
    app_commands.Choice(name="TH", value="th"),
    app_commands.Choice(name="TW", value="tw"),
    app_commands.Choice(name="US", value="us"),
]


def _is_cashcup_admin(user: discord.Member) -> bool:
    """CASH CUP: только ADMIN (владелец/Administrator/BOT_OWNER_ID/ADMIN_USER_IDS).
    ORG, MANAGER, MODERATOR, HEAD MODERATOR прав не дают (ТЗ §7)."""
    return moderation_service.is_admin(user)


def _team(state: dict, name: str) -> dict:
    team = next((t for t in state["teams"] if t["name"].casefold() == name.strip().casefold()), None)
    if not team:
        raise CashCupError("Команда не найдена")
    return team


def _my_team(state: dict, user_id: int) -> dict:
    team = next((t for t in state["teams"] if t["captain"] == user_id), None)
    if not team:
        raise CashCupError("Вы не капитан зарегистрированной команды")
    return team


def _assert_registration(state: dict) -> None:
    if state["status"] != "registration":
        raise CashCupError("Регистрация уже закрыта")


def _started_markers(bracket: dict) -> set:
    """Матчи, у которых уже есть check-in/комната/игры — «начавшиеся»."""
    return {m["id"] for m in ready_matches(bracket)
            if m.get("checkins") or m.get("room_id") or m.get("games")}


def _push_snapshot(state: dict) -> None:
    """Снимок сетки перед изменением результата — для /cashcup revert.
    Вместе с сеткой сохраняем набор «начавшихся» матчей: если после снимка
    dependent-матч получил check-in/комнату, авто-откат блокируется."""
    if state.get("bracket"):
        state.setdefault("undo", []).append({
            "bracket": deepcopy(state["bracket"]),
            "started": _started_markers(state["bracket"]),
        })
        del state["undo"][:-5]


def _revert_blocked(state: dict) -> set:
    """Матчи, «начавшиеся» после последнего снимка — блокируют авто-откат."""
    if not state.get("undo") or not state.get("bracket"):
        return set()
    return (_started_markers(state["bracket"])
            - state["undo"][-1]["started"])


def _assert_running(state: dict) -> None:
    if state["status"] != "running":
        raise CashCupError("Турнир не идёт")
    if state.get("paused"):
        raise CashCupError("Турнир приостановлен организатором")


def _unique_player(state: dict, user_id: int, ff_uid: str) -> None:
    for team in state["teams"]:
        for player in team["members"] + team["pending"]:
            if player["discord_id"] == user_id:
                raise CashCupError("Этот Discord-аккаунт уже зарегистрирован")
            if player["ff_uid"] == ff_uid:
                raise CashCupError("Этот Free Fire UID уже используется")


def _player_by_id(team: dict, user_id: int) -> dict | None:
    return next((p for p in team["members"] if p["discord_id"] == user_id), None)


def _label(team: dict) -> str:
    return safe_text(team["name"], limit=65)


def _team_name(state: dict, team_id: str) -> str:
    return next((_label(t) for t in state["teams"] if t["id"] == team_id), "Неизвестная команда")


def _panel(state: dict) -> discord.Embed:
    running = state["status"] == "running"
    completed = state["status"] == "completed"
    lines = [f"**Приз:** {safe_text(state['prize'], limit=90)}"]
    if state.get("pool"):
        lines.append(f"**Призовой фонд:** {state['pool']:,} ₸".replace(",", " "))
    if state.get("prizes"):
        pr = state["prizes"]
        pl = pr.get("places", {})
        lines.append(f"**Призы:** 1 — {pl.get('1',0)}, 2 — {pl.get('2',0)}, 3 — {pl.get('3',0)} {pr.get('currency','')}")
    lines += [f"**Команд:** {len(state['teams'])} (без лимита)",
             "**Формат:** Double Elimination, BO1; Grand Final BO3 без reset",
             "**Состав:** 4 основных + 1 запасной; максимум 2 ПК."]
    if state.get("payouts"):
        lines.append(f"**Выплат отмечено:** {len(state['payouts'])}")
    if state["status"] == "registration":
        lines.append("**Регистрация открыта.** Каждый игрок сам вводит Free Fire UID и получает ник из API.")
        lines.append("До старта необходимо подтверждение состава организатором.")
        teams = state.get("teams", [])
        if teams:
            lines.append(f"**Команды ({len(teams)}):** " + ", ".join(
                f"{_label(t)} ({len(t['members'])}/5)" for t in teams[:12]))
            if len(teams) > 12:
                lines.append(f"… ещё {len(teams)-12} — кнопка «Команды»")
    elif running:
        if state.get("paused"):
            lines.append(f"**Турнир приостановлен:** {safe_text(state.get('pause_reason') or 'без указания причины', limit=120)}")
        bracket = state["bracket"]
        active = ready_matches(bracket)
        lines.append(f"**Активные матчи:** {len(active)}")
        for match in active[:6]:
            score = f" · {match['score'][0]}:{match['score'][1]}" if match["bracket"] == "GF" else ""
            lines.append(f"`{match['id']}` {_team_name(state, match['a'])} — {_team_name(state, match['b'])} · BO{match['bo']}{score}")
        if len(active) > 6:
            lines.append(f"… ещё {len(active)-6} матчей (`/cashcup matches`)")
    elif completed:
        lines.append(f"**ЧЕМПИОН:** {_team_name(state, state['bracket']['champion'])}")
    else:
        lines.append("**Турнир отменён.**")
    return discord.Embed(title="r!z3 CASH CUP", description="\n".join(lines),
                         color=0x26262A)


async def _member_check(bot: commands.Bot, guild: discord.Guild, user_id: int) -> str:
    try:
        member = await guild.fetch_member(user_id)
        user = await bot.fetch_user(user_id)
    except discord.HTTPException as exc:
        raise CashCupError(f"<@{user_id}> не найден на сервере / нет доступа Discord") from exc
    if not member or not _has_server_tag(user):
        raise CashCupError(f"<@{user_id}> должен установить тег сервера r!z3")
    if await vac_service.check_vac(guild.id, user_id):
        raise CashCupError(f"<@{user_id}> имеет действующий VAC")
    return member.joined_at.isoformat() if member.joined_at else ""


async def _ff_profile(uid: str, region: str) -> dict:
    uid = uid.strip()
    if not UID_RE.fullmatch(uid):
        raise CashCupError("Free Fire UID должен состоять из 5–15 цифр")
    if not freefire_service.configured:
        raise CashCupError("FREEFIRE_API_KEY не настроен")
    if region not in {c.value for c in REGION_CHOICES}:
        raise CashCupError("Неизвестный регион Free Fire")
    try:
        response = await freefire_service.get_info(region, uid)
    except FreeFireError as exc:
        raise CashCupError(f"Free Fire API: {exc}") from exc
    basic = response.get("basicInfo") or {}
    name = basic.get("nickname")
    actual_uid = str(basic.get("accountId") or uid)
    if not name or actual_uid != uid:
        raise CashCupError("API не подтвердил UID и игровой ник; попробуйте позже")
    return {"ff_uid": uid, "region": region, "nick": str(name)[:80]}


async def _validate_teams(bot: commands.Bot, guild: discord.Guild, state: dict) -> None:
    if len(state["teams"]) < 2:
        raise CashCupError("Необходимо минимум две команды")
    ids = set()
    ff_ids = set()
    for team in state["teams"]:
        if len(team["members"]) not in (4, 5):
            raise CashCupError(f"{_label(team)}: необходимо 4–5 подтверждённых игроков")
        if len(team["lineup"]) != 4:
            raise CashCupError(f"{_label(team)}: основной состав должен содержать 4 игроков")
        if len(set(team["lineup"])) != 4:
            raise CashCupError(f"{_label(team)}: повторяющийся игрок в основе")
        if any(not p["verified"] for p in team["members"]):
            raise CashCupError(f"{_label(team)}: организатор не подтвердил игровые аккаунты")
        if sum(p["device"] == "pc" for p in team["members"]) > 2:
            raise CashCupError(f"{_label(team)}: более двух ПК")
        for p in team["members"]:
            if p["discord_id"] in ids or p["ff_uid"] in ff_ids:
                raise CashCupError("Один аккаунт участвует в нескольких командах")
            ids.add(p["discord_id"])
            ff_ids.add(p["ff_uid"])
    semaphore = asyncio.Semaphore(5)
    async def check(uid: int) -> None:
        async with semaphore:
            await _member_check(bot, guild, uid)
    await asyncio.gather(*(check(uid) for uid in ids))


class CreateTeamModal(discord.ui.Modal, title="Создать команду CASH CUP"):
    name = discord.ui.TextInput(label="Название команды", max_length=40)
    uid = discord.ui.TextInput(label="Ваш Free Fire UID", max_length=15)
    device = discord.ui.TextInput(label="Устройство: mobile или pc", max_length=6)
    region = discord.ui.TextInput(label="Регион (по умолчанию ru)", required=False, max_length=3)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("CashCupCog")
        await cog.register_team(interaction, str(self.name.value), str(self.uid.value),
                                str(self.device.value).strip().lower(),
                                str(self.region.value).strip().lower() or "ru")


class JoinTeamModal(discord.ui.Modal, title="Заявка в команду CASH CUP"):
    name = discord.ui.TextInput(label="Название команды", max_length=40)
    uid = discord.ui.TextInput(label="Ваш Free Fire UID", max_length=15)
    device = discord.ui.TextInput(label="Устройство: mobile или pc", max_length=6)
    region = discord.ui.TextInput(label="Регион (по умолчанию ru)", required=False, max_length=3)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("CashCupCog")
        await cog.join_team(interaction, str(self.name.value), str(self.uid.value),
                            str(self.device.value).strip().lower(),
                            str(self.region.value).strip().lower() or "ru")


class CashCupView(discord.ui.View):
    def __init__(self, registration: bool = True):
        super().__init__(timeout=None)
        if not registration:
            self.create.disabled = True
            self.join.disabled = True

    @discord.ui.button(label="Создать команду", style=discord.ButtonStyle.success,
                       custom_id="cashcup:create_team")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CreateTeamModal())

    @discord.ui.button(label="Вступить в команду", style=discord.ButtonStyle.primary,
                       custom_id="cashcup:join_team")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(JoinTeamModal())

    @discord.ui.button(label="Моя команда", style=discord.ButtonStyle.secondary,
                       custom_id="cashcup:my_team")
    async def roster(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("CashCupCog")
        await cog.show_roster(interaction)

    @discord.ui.button(label="Команды", style=discord.ButtonStyle.secondary,
                       custom_id="cashcup:teams")
    async def teams(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("CashCupCog")
        await cog.show_teams(interaction)

    @discord.ui.button(label="Матчи", style=discord.ButtonStyle.secondary,
                       custom_id="cashcup:matches")
    async def matches(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("CashCupCog")
        await cog.show_active_matches(interaction)

    @discord.ui.button(label="Сетка", style=discord.ButtonStyle.secondary,
                       custom_id="cashcup:bracket")
    async def bracket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("CashCupCog")
        await cog.show_bracket(interaction)


def _match_card_text(state: dict, m: dict) -> str:
    ta = next((t for t in state["teams"] if t["id"] == m["a"]), None)
    tb = next((t for t in state["teams"] if t["id"] == m["b"]), None)
    def roster(t):
        if not t:
            return "—"
        lineup = ", ".join(safe_text(p["nick"], limit=20) for p in t["members"]
                           if p["discord_id"] in t["lineup"])
        return f"{_label(t)} (капитан <@{t['captain']}>)\n  {lineup}"
    stage = {"W": "Winners", "L": "Losers", "GF": "GRAND FINAL"}.get(m["bracket"], m["bracket"])
    lines = [
        f"**{m['id']}** · {stage} · BO{m['bo']} · статус: {m['status']}",
        f"**A:** {roster(ta)}",
        f"**B:** {roster(tb)}",
        f"Check-in: {len(m.get('checkins', []))}/2 · "
        f"Комната: {'готова' if m.get('room_id') else 'ожидается'}",
    ]
    if m["bracket"] == "GF" or m.get("games") or m.get("score", [0, 0]) != [0, 0]:
        lines.append(f"Счёт: **{m['score'][0]}:{m['score'][1]}**")
    if m.get("dispute"):
        lines.append(f"**СПОР:** {safe_text(m['dispute'].get('reason', ''), limit=150)} "
                     f"· доказательств: {len(m['dispute'].get('evidence', []))}")
    return "\n".join(lines)[:1900]


def _board_embed(state: dict) -> discord.Embed:
    b = state.get("bracket") or {}
    active = ready_matches(b) if b else []
    lines = ["**Текущий раунд — выберите матч**"]
    for m in active[:15]:
        score = f" · {m['score'][0]}:{m['score'][1]}" if m["bracket"] == "GF" else ""
        flag = " ⚠️ СПОР" if m["status"] == "disputed" else ""
        lines.append(f"`{m['id']}` {_team_name(state, m['a'])} vs {_team_name(state, m['b'])}"
                     f" · BO{m['bo']}{score}{flag}")
    if not active:
        lines.append("Активных матчей нет" +
                     (f". Чемпион: **{_team_name(state, b['champion'])}**"
                      if b.get("champion") else "."))
    if len(active) > 15:
        lines.append(f"… ещё {len(active)-15} — `/cashcup info matches`")
    return discord.Embed(title="r!z3 CASH CUP · матчи", description="\n".join(lines),
                         color=0x26262A)


class BoardSelectView(discord.ui.View):
    """Persistent Select активных матчей → ephemeral карточка матча."""
    def __init__(self, matches=None):
        super().__init__(timeout=None)
        options = [discord.SelectOption(
            label=m["id"], value=m["id"],
            description=(f"{_short_desc(m)}")[:100]) for m in (matches or [])[:25]]
        if not options:
            options = [discord.SelectOption(label="Матчов нет", value="none")]
        self._sel = discord.ui.Select(
            placeholder="Открыть матч…", custom_id="cashcup:board_select",
            options=options, disabled=not matches)
        self._sel.callback = self._pick
        self.add_item(self._sel)

    async def _pick(self, interaction: discord.Interaction) -> None:
        mid = self._sel.values[0]
        if mid == "none":
            await interaction.response.send_message("Активных матчей нет.", ephemeral=True)
            return
        cog = interaction.client.get_cog("CashCupCog")
        await cog.show_match_card(interaction, mid)


def _short_desc(m: dict) -> str:
    st = {"ready": "ожидание", "disputed": "СПОР"}.get(m["status"], m["status"])
    return f"BO{m['bo']} · {st}"


class CashCupCog(commands.Cog):
    cashcup = app_commands.Group(name="cashcup", description="r!z3 CASH CUP · денежный турнир")
    info = app_commands.Group(name="info", description="CASH CUP · матчи, сетка, история и утилиты")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cashcup.add_command(self.info)

    async def cog_load(self) -> None:
        self.bot.add_view(compose(_panel({"status": "registration", "prize": "", "teams": []}), CashCupView(True)))
        self.bot.add_view(BoardSelectView())

    async def _refresh(self, guild_id: int, state: dict | None = None) -> None:
        if state is None:
            state = await cashcup_store.active(guild_id) or await cashcup_store.latest(guild_id)
        if not state or not state["message_id"]:
            return
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        channel = guild.get_channel(state["channel_id"])
        if not channel:
            return
        try:
            msg = await channel.fetch_message(state["message_id"])
            await msg.edit(view=compose(_panel(state), CashCupView(state["status"] == "registration")),
                           allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.exception("CASH CUP panel refresh failed")
        await self._sync_board(state, channel)

    async def _sync_board(self, state: dict, channel) -> None:
        """Публичная карточка текущего раунда: одно сообщение, обновляется на месте."""
        if state.get("status") != "running" or not state.get("bracket"):
            return
        try:
            active = ready_matches(state["bracket"])
            view = BoardSelectView(active)
            board_id = state.get("board_message_id")
            if board_id:
                try:
                    msg = await channel.fetch_message(board_id)
                    await msg.edit(embed=_board_embed(state), view=view,
                                   allowed_mentions=discord.AllowedMentions.none())
                    return
                except discord.HTTPException:
                    pass  # сообщение удалено — создадим заново
            msg = await channel.send(embed=_board_embed(state), view=view,
                                     allowed_mentions=discord.AllowedMentions.none())
            def change(st):
                st["board_message_id"] = msg.id
            await cashcup_store.mutate(state["guild_id"], 0, "board_sync", change)
        except Exception:
            logger.exception("CASH CUP board sync failed")

    async def show_match_card(self, interaction: discord.Interaction, match_id: str) -> None:
        state = await cashcup_store.active(interaction.guild_id) or await cashcup_store.latest(interaction.guild_id)
        if not state or not state.get("bracket"):
            await self._reply(interaction, "Сетка ещё не сформирована")
            return
        m = next((x for x in state["bracket"]["matches"]
                  if x["id"] == match_id.upper()), None)
        if not m:
            await self._reply(interaction, "Матч не найден")
            return
        await self._reply(interaction, _match_card_text(state, m))

    async def _reply(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(message, ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())

    async def _guard(self, interaction: discord.Interaction, device: str = "mobile") -> str:
        if not interaction.guild or not interaction.guild_id:
            raise CashCupError("Команда доступна только на сервере r!z3")
        if device not in ("pc", "mobile"):
            raise CashCupError("Устройство: mobile или pc")
        return await _member_check(self.bot, interaction.guild, interaction.user.id)

    async def _post(self, interaction: discord.Interaction, action: str,
                    change, details: dict | None = None) -> tuple[dict, Any]:
        result = await cashcup_store.mutate(interaction.guild_id, interaction.user.id,
                                            action, change, details)
        await self._refresh(interaction.guild_id, result[0])
        return result

    @cashcup.command(name="create", description="Создать CASH CUP (организатор)")
    async def create(self, interaction: discord.Interaction, prize: str = "Определяет организатор",
                     pool: app_commands.Range[int, 0, 100_000_000] | None = None) -> None:
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await cashcup_store.create(interaction.guild_id, interaction.channel_id,
                                       interaction.user.id, prize[:90], pool)
            msg = await interaction.channel.send(view=compose(_panel(await cashcup_store.active(interaction.guild_id)), CashCupView(True)),
                                                 allowed_mentions=discord.AllowedMentions.none())
            await cashcup_store.set_message(interaction.guild_id, msg.id)
            await self._reply(interaction, "Регистрация CASH CUP открыта. Создана карточка турнира.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def register_team(self, interaction: discord.Interaction, name: str,
                            uid: str, device: str, region: str = "ru") -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            joined_at = await self._guard(interaction, device)
            name = name.strip()
            if not 2 <= len(name) <= 40:
                raise CashCupError("Название команды: 2–40 символов")
            ff = await _ff_profile(uid, region)
            player = {"discord_id": interaction.user.id, "device": device,
                      "verified": False, "joined_at": joined_at, **ff}
            team_id = uuid.uuid4().hex[:12]
            def change(state):
                _assert_registration(state)
                if any(t["name"].casefold() == name.casefold() for t in state["teams"]):
                    raise CashCupError("Название команды занято")
                _unique_player(state, interaction.user.id, ff["ff_uid"])
                state["teams"].append({"id": team_id, "name": name,
                                       "captain": interaction.user.id, "members": [player],
                                       "pending": [], "lineup": [interaction.user.id]})
            await self._post(interaction, "create_team", change, {"team_id": team_id})
            await self._reply(interaction, f"Команда **{safe_text(name)}** создана. Капитан: {safe_text(ff['nick'])}.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @info.command(name="panel", description="Восстановить карточку CASH CUP (организатор)")
    async def panel(self, interaction: discord.Interaction):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        state = await cashcup_store.active(interaction.guild_id)
        if not state:
            await self._reply(interaction, "Нет активного CASH CUP")
            return
        msg = await interaction.channel.send(
            view=compose(_panel(state), CashCupView(state["status"] == "registration")),
            allowed_mentions=discord.AllowedMentions.none())
        await cashcup_store.set_message(interaction.guild_id, msg.id)
        await self._reply(interaction, "Карточка турнира опубликована повторно")

    @cashcup.command(name="team", description="Создать команду (капитан)")
    @app_commands.choices(device=DEVICE_CHOICES, region=REGION_CHOICES)
    async def team(self, interaction: discord.Interaction, name: str, uid: str,
                   device: app_commands.Choice[str],
                   region: app_commands.Choice[str] | None = None):
        await self.register_team(interaction, name, uid, device.value,
                                 region.value if region else "ru")

    async def join_team(self, interaction: discord.Interaction, name: str,
                        uid: str, device: str, region: str = "ru") -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            joined_at = await self._guard(interaction, device)
            ff = await _ff_profile(uid, region)
            player = {"discord_id": interaction.user.id, "device": device,
                      "verified": False, "joined_at": joined_at, **ff}
            def change(state):
                _assert_registration(state)
                t = _team(state, name)
                _unique_player(state, interaction.user.id, ff["ff_uid"])
                t["pending"].append(player)
                return t["captain"]
            _, captain = await self._post(interaction, "join_request", change,
                                          {"ff_uid": ff["ff_uid"]})
            await self._reply(interaction, f"Заявка в команду отправлена. Игровой ник: **{safe_text(ff['nick'])}**. Капитан <@{captain}> должен принять её через `/cashcup approve`.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="join", description="Отправить заявку в команду")
    @app_commands.choices(device=DEVICE_CHOICES, region=REGION_CHOICES)
    async def join(self, interaction: discord.Interaction, name: str, uid: str,
                   device: app_commands.Choice[str],
                   region: app_commands.Choice[str] | None = None):
        await self.join_team(interaction, name, uid, device.value,
                             region.value if region else "ru")

    @cashcup.command(name="approve", description="Капитан принимает заявку игрока")
    async def approve(self, interaction: discord.Interaction, user: discord.Member) -> None:
        try:
            await _member_check(self.bot, interaction.guild, user.id)
            def change(state):
                _assert_registration(state)
                t = _my_team(state, interaction.user.id)
                if len(t["members"]) >= 5:
                    raise CashCupError("Команда заполнена (5/5)")
                p = next((p for p in t["pending"] if p["discord_id"] == user.id), None)
                if not p:
                    raise CashCupError("У этого игрока нет заявки")
                if p["device"] == "pc" and sum(x["device"] == "pc" for x in t["members"]) >= 2:
                    raise CashCupError("В составе не может быть больше 2 ПК")
                t["pending"].remove(p)
                t["members"].append(p)
                if len(t["lineup"]) < 4:
                    t["lineup"].append(p["discord_id"])
            await self._post(interaction, "approve_player", change, {"user": user.id})
            await self._reply(interaction, f"<@{user.id}> принят в состав.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="leave", description="Отозвать свою заявку или выйти из команды до старта")
    async def leave(self, interaction: discord.Interaction):
        try:
            def change(state):
                _assert_registration(state)
                for t in state["teams"]:
                    if t["captain"] == interaction.user.id:
                        raise CashCupError("Капитан использует /cashcup withdraw")
                    for key in ("pending", "members"):
                        p = next((p for p in t[key] if p["discord_id"] == interaction.user.id), None)
                        if p:
                            t[key].remove(p)
                            if interaction.user.id in t["lineup"]:
                                t["lineup"].remove(interaction.user.id)
                            return t["id"]
                raise CashCupError("Вы не зарегистрированы в CASH CUP")
            await self._post(interaction, "leave_team", change)
            await self._reply(interaction, "Вы покинули заявку/команду")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="kick", description="Капитан удаляет игрока из заявки до старта")
    async def kick(self, interaction: discord.Interaction, user: discord.Member):
        try:
            def change(state):
                _assert_registration(state)
                t = _my_team(state, interaction.user.id)
                if user.id == t["captain"]:
                    raise CashCupError("Капитан не может исключить себя")
                for key in ("pending", "members"):
                    p = next((p for p in t[key] if p["discord_id"] == user.id), None)
                    if p:
                        t[key].remove(p)
                        if user.id in t["lineup"]:
                            t["lineup"].remove(user.id)
                        return
                raise CashCupError("Игрок не состоит в этой команде")
            await self._post(interaction, "kick_player", change, {"user":user.id})
            await self._reply(interaction, "Игрок удалён из команды")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="verify", description="Организатор подтверждает владельцев UID команды")
    async def verify(self, interaction: discord.Interaction, team_name: str) -> None:
        if not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Доступно только организатору")
            return
        try:
            def change(state):
                _assert_registration(state)
                t = _team(state, team_name)
                if len(t["members"]) < 4:
                    raise CashCupError("Сначала соберите минимум 4 игроков")
                for player in t["members"]:
                    player["verified"] = True
                return len(t["members"])
            _, n = await self._post(interaction, "verify_roster", change,
                                     {"team": team_name, "manual": True})
            await self._reply(interaction, f"Подтверждено игроков: {n}. Используйте это только после проверки принадлежности аккаунтов.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def show_roster(self, interaction: discord.Interaction) -> None:
        state = await cashcup_store.active(interaction.guild_id) or await cashcup_store.latest(interaction.guild_id)
        if not state:
            await self._reply(interaction, "CASH CUP не найден")
            return
        t = next((t for t in state["teams"] if any(p["discord_id"] == interaction.user.id for p in t["members"])), None)
        if not t:
            await self._reply(interaction, "Вы не состоите в команде")
            return
        rows = [f"**{_label(t)}** — {len(t['members'])}/5 игроков"]
        for p in t["members"]:
            badge = "✅" if p["verified"] else "⌛"
            lineup = "[основа]" if p["discord_id"] in t["lineup"] else "[запас]"
            rows.append(f"{badge} <@{p['discord_id']}> · {safe_text(p['nick'])} · `{p['ff_uid']}` · {p['device']} {lineup}")
        if t["captain"] == interaction.user.id and t["pending"]:
            rows.append("\n**Заявки:** " + ", ".join(f"<@{p['discord_id']}>" for p in t["pending"][:15]))
        await self._reply(interaction, "\n".join(rows))

    async def show_teams(self, interaction: discord.Interaction) -> None:
        state = await cashcup_store.active(interaction.guild_id) or await cashcup_store.latest(interaction.guild_id)
        if not state or not state.get("teams"):
            await self._reply(interaction, "Команд пока нет")
            return
        lines = [f"**CASH CUP · команды ({len(state['teams'])})**"]
        for t in state["teams"][:25]:
            verified = sum(1 for p in t["members"] if p.get("verified"))
            pcs = sum(1 for p in t["members"] if p.get("device") == "pc")
            lines.append(f"**{_label(t)}** — капитан <@{t['captain']}> · "
                         f"{len(t['members'])}/5 · ПК: {pcs} · verified: {verified}/5")
        if len(state["teams"]) > 25:
            lines.append(f"… ещё {len(state['teams'])-25}")
        await self._reply(interaction, "\n".join(lines)[:1900])

    async def show_active_matches(self, interaction: discord.Interaction) -> None:
        state = await cashcup_store.active(interaction.guild_id) or await cashcup_store.latest(interaction.guild_id)
        if not state or not state.get("bracket"):
            await self._reply(interaction, "Сетка ещё не сформирована")
            return
        items = ready_matches(state["bracket"])
        lines = [f"**CASH CUP · активные матчи**"]
        for m in items[:20] or []:
            score = f" · {m['score'][0]}:{m['score'][1]}" if m["bracket"] == "GF" else ""
            status = "⌛ check-in" if len(m.get("checkins", [])) < 2 else ("🗝️ комната" if m.get("room_id") else "ожидание")
            lines.append(f"`{m['id']}` {_team_name(state,m['a'])} vs {_team_name(state,m['b'])} · BO{m['bo']}{score} · {status}")
        if not items:
            b = state["bracket"]
            lines.append("Нет активных матчей." + (f" Чемпион: **{_team_name(state, b['champion'])}**" if b.get("champion") else ""))
        if len(items) > 20:
            lines.append(f"… ещё {len(items)-20}")
        await self._reply(interaction, "\n".join(lines)[:1900])

    async def show_bracket(self, interaction: discord.Interaction) -> None:
        state = await cashcup_store.active(interaction.guild_id) or await cashcup_store.latest(interaction.guild_id)
        if not state or not state.get("bracket"):
            await self._reply(interaction, "Сетка ещё не сформирована")
            return
        b = state["bracket"]
        lines = ["**CASH CUP · сетка Double Elimination**"]
        for label, key in (("WINNERS", "upper"), ("LOSERS", "lower")):
            entries = b.get(key) or []
            if entries:
                lines.append(f"**{label}:** " + " → ".join(_team_name(state, x) for x in entries[:16]))
                if len(entries) > 16:
                    lines.append(f"… ещё {len(entries)-16} в {label.lower()}")
        dq = b.get("dq") or []
        if dq:
            lines.append("**Дисквалифицированы:** " + ", ".join(_team_name(state, x) for x in dq))
        done = [m for m in b["matches"] if m["status"] in ("completed", "void")]
        lines.append(f"Сыграно серий: {len(done)}/{len(b['matches'])}")
        await self._reply(interaction, "\n".join(lines)[:1900])

    @cashcup.command(name="roster", description="Посмотреть свой состав и заявки")
    async def roster(self, interaction: discord.Interaction) -> None:
        await self.show_roster(interaction)

    @cashcup.command(name="withdraw", description="Капитан снимает команду с регистрации")
    async def withdraw(self, interaction: discord.Interaction):
        try:
            def change(state):
                _assert_registration(state)
                team = _my_team(state, interaction.user.id)
                state["teams"].remove(team)
            await self._post(interaction, "withdraw_team", change)
            await self._reply(interaction, "Команда снята с регистрации")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="remove", description="Организатор удаляет команду до начала турнира")
    async def remove(self, interaction: discord.Interaction, name: str):
        if not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        try:
            def change(state):
                _assert_registration(state)
                state["teams"].remove(_team(state, name))
            await self._post(interaction, "remove_team", change, {"name": name})
            await self._reply(interaction, "Команда удалена")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="start", description="Проверить составы и запустить Double Elimination")
    async def start(self, interaction: discord.Interaction):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        await interaction.response.defer(ephemeral=True)
        try:
            snapshot = await cashcup_store.active(interaction.guild_id)
            if not snapshot or snapshot["status"] != "registration":
                raise CashCupError("Регистрация закрыта")
            await _validate_teams(self.bot, interaction.guild, snapshot)
            signature = json.dumps(snapshot["teams"], sort_keys=True)
            def change(state):
                _assert_registration(state)
                if json.dumps(state["teams"], sort_keys=True) != signature:
                    raise CashCupError("Составы изменились при проверке. Повторите /cashcup start")
                state["bracket"] = create_bracket([t["id"] for t in state["teams"]])
                state["status"] = "running"
                return len(state["teams"])
            _, count = await self._post(interaction, "start", change,
                                        {"teams": len(snapshot["teams"])})
            await self._reply(interaction, f"CASH CUP запущен! Команд: {count}. Сетка Double Elimination сформирована.")
        except (CashCupError, BracketError) as exc:
            await self._reply(interaction, str(exc))

    @info.command(name="matches", description="Активные матчи Winners/Losers")
    async def matches(self, interaction: discord.Interaction):
        state = await cashcup_store.active(interaction.guild_id) or await cashcup_store.latest(interaction.guild_id)
        if not state or not state["bracket"]:
            await self._reply(interaction, "Сетка ещё не сформирована")
            return
        b = state["bracket"]
        items = ready_matches(b)
        if not items:
            await self._reply(interaction, ("Нет активных матчей. Чемпион: " + _team_name(state, b["champion"]))
                              if b["champion"] else "Нет активных матчей")
            return
        lines = [f"**CASH CUP · {len(items)} активных матчей**"]
        for m in items[:24]:
            lines.append(f"`{m['id']}` {_team_name(state,m['a'])} vs {_team_name(state,m['b'])} · BO{m['bo']}")
        if len(items) > 24:
            lines.append(f"… ещё {len(items)-24}; список выводится частично")
        await self._reply(interaction, "\n".join(lines)[:1900])

    @cashcup.command(name="checkin", description="Капитан подтверждает готовность своей команды к матчу")
    async def checkin(self, interaction: discord.Interaction, match_id: str):
        if not interaction.guild:
            await self._reply(interaction, "Нужен сервер r!z3")
            return
        await interaction.response.defer(ephemeral=True)
        try:
            snapshot = await cashcup_store.active(interaction.guild_id)
            if not snapshot or snapshot["status"] != "running":
                raise CashCupError("Нет активного турнира")
            if snapshot.get("paused"):
                raise CashCupError("Турнир приостановлен организатором")
            current = next((m for m in ready_matches(snapshot["bracket"])
                            if m["id"] == match_id.upper()), None)
            if not current:
                raise CashCupError("Матч не найден")
            t = _my_team(snapshot, interaction.user.id)
            if t["id"] not in (current["a"],current["b"]):
                raise CashCupError("Ваша команда не участвует в этом матче")
            await asyncio.gather(*(_member_check(self.bot, interaction.guild, uid)
                                   for uid in t["lineup"]))
            def change(state):
                m = next((m for m in ready_matches(state["bracket"])
                          if m["id"] == match_id.upper()), None)
                if not m or t["id"] not in (m["a"],m["b"]):
                    raise CashCupError("Матч изменился. Повторите check-in")
                fresh_team = _my_team(state, interaction.user.id)
                if fresh_team["lineup"] != t["lineup"]:
                    raise CashCupError("Состав изменился; повторите check-in")
                if interaction.user.id in m["checkins"]:
                    raise CashCupError("Готовность уже подтверждена")
                m["checkins"].append(interaction.user.id)
                return len(m["checkins"])
            _, count = await self._post(interaction, "checkin", change,
                                        {"match": match_id, "captain": interaction.user.id})
            await self._reply(interaction, f"Готовность подтверждена ({count}/2 капитана).")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="result", description="Организатор подтверждает результат BO-серии")
    async def result(self, interaction: discord.Interaction, match_id: str,
                     score_a: int, score_b: int, evidence: str):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        if not evidence.startswith(("https://discord.com/channels/", "https://cdn.discordapp.com/", "https://media.discordapp.net/")):
            await self._reply(interaction, "Нужна ссылка на доказательство результата в Discord")
            return
        try:
            def change(state):
                _assert_running(state)
                previous = next((x for x in ready_matches(state["bracket"])
                                 if x["id"] == match_id.upper()), None)
                if previous and previous["bracket"] == "GF" and previous["games"]:
                    raise CashCupError("Grand Final уже начат: используйте /cashcup game")
                _push_snapshot(state)
                m = record_result(state["bracket"], match_id.upper(), score_a,
                                  score_b, evidence=evidence)
                if state["bracket"]["champion"]:
                    state["status"] = "completed"
                return m["winner"]
            state, winner_id = await self._post(interaction, "result", change,
                                                {"match": match_id, "score": [score_a, score_b],
                                                 "evidence": evidence})
            await self._reply(interaction, f"Результат сохранён. Победитель: **{_team_name(state,winner_id)}**." +
                              (" CASH CUP завершён!" if state["status"] == "completed" else ""))
        except (CashCupError, BracketError) as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="game", description="Записать победу в отдельной игре Grand Final BO3")
    @app_commands.choices(winner=[app_commands.Choice(name="Команда A", value="a"),
                                  app_commands.Choice(name="Команда B", value="b")])
    async def game(self, interaction: discord.Interaction,
                   winner: app_commands.Choice[str], evidence: str):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Доступно только организатору")
            return
        if not evidence.startswith(("https://discord.com/channels/", "https://cdn.discordapp.com/", "https://media.discordapp.net/")):
            await self._reply(interaction, "Нужна ссылка на доказательство игры")
            return
        try:
            def change(state):
                _assert_running(state)
                _push_snapshot(state)
                m = record_final_game(state["bracket"], winner.value, evidence=evidence)
                if state["bracket"]["champion"]:
                    state["status"] = "completed"
                return m["score"]
            state, scores = await self._post(interaction, "final_game", change,
                                              {"winner_side": winner.value, "evidence": evidence})
            await self._reply(interaction, f"Grand Final: **{scores[0]}:{scores[1]}**. " +
                              ("Победитель турнира определён!" if state["status"] == "completed"
                               else "Серия продолжается до двух побед."))
        except (CashCupError, BracketError) as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="room", description="Организатор задаёт ID и пароль комнаты матча")
    async def room(self, interaction: discord.Interaction, match_id: str,
                   room_id: str, password: str):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        try:
            def change(state):
                _assert_running(state)
                m = next((m for m in ready_matches(state["bracket"])
                          if m["id"] == match_id.upper()), None)
                if not m:
                    raise CashCupError("Матч не найден")
                if len(m["checkins"]) != 2:
                    raise CashCupError("Оба капитана должны выполнить /cashcup checkin")
                m["room_id"] = room_id[:50]
                m["room_password"] = password[:50]
                m["room_set_by"] = interaction.user.id
                m["room_set_at"] = datetime.now(timezone.utc).isoformat()
                return m["a"], m["b"]
            state, teams = await self._post(interaction, "set_room", change,
                                           {"match": match_id})
            captains = [t["captain"] for t in state["teams"] if t["id"] in teams]
            failures = []
            delivered = []
            for captain_id in captains:
                try:
                    recipient = await self.bot.fetch_user(captain_id)
                    await recipient.send(f"CASH CUP · Матч `{match_id.upper()}`\n"
                                         f"Комната: `{room_id[:50]}`\nПароль: `{password[:50]}`")
                    delivered.append(captain_id)
                except discord.HTTPException:
                    failures.append(str(captain_id))
            # Аудит доставки: кто получил DM и когда. При неудаче участники
            # всё равно могут запросить комнату через /cashcup roominfo.
            def audit(st):
                mm = next((x for x in st["bracket"]["matches"]
                           if x["id"] == match_id.upper()), None)
                if mm:
                    mm["room_delivered"] = delivered
                    mm["room_delivered_at"] = datetime.now(timezone.utc).isoformat()
            try:
                await cashcup_store.mutate(interaction.guild_id, interaction.user.id,
                                           "room_delivery", audit,
                                           {"match": match_id, "delivered": delivered})
            except Exception:
                logger.exception("CASH CUP room delivery audit failed")
            await self._reply(interaction,
                              "Комната записана. Участники матча и ADMIN могут "
                              "получить данные через /cashcup roominfo "
                              "(работает и при закрытых ЛС)." +
                              (" DM доставлены не всем капитанам: " + ", ".join(failures)
                               if failures else " DM отправлены капитанам."))
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="roominfo", description="Участник матча получает комнату приватно")
    async def roominfo(self, interaction: discord.Interaction, match_id: str):
        state = await cashcup_store.active(interaction.guild_id)
        if not state or state["status"] != "running":
            await self._reply(interaction, "Турнир не запущен")
            return
        m = next((x for x in ready_matches(state["bracket"])
                  if x["id"] == match_id.upper()), None)
        if not m or not m["room_id"]:
            await self._reply(interaction, "Данные комнаты ещё не выданы")
            return
        teams = [t for t in state["teams"] if t["id"] in (m["a"], m["b"])]
        if not _is_cashcup_admin(interaction.user) and not any(
            interaction.user.id in t["lineup"] for t in teams
        ):
            await self._reply(interaction, "Вы не участник этого матча")
            return
        await self._reply(interaction, f"Матч `{m['id']}` · ID `{m['room_id']}` · пароль `{m['room_password']}`")

    @cashcup.command(name="swap", description="Капитан заменяет игрока между матчами")
    async def swap(self, interaction: discord.Interaction, out_player: discord.Member,
                   in_player: discord.Member):
        try:
            await _member_check(self.bot, interaction.guild, in_player.id)
            def change(state):
                _assert_running(state)
                t = _my_team(state, interaction.user.id)
                b = state["bracket"]
                if b["losses"][t["id"]] >= 2:
                    raise CashCupError("Команда выбыла")
                if any((m["room_id"] or t["captain"] in m["checkins"])
                       for m in ready_matches(b) if t["id"] in (m["a"],m["b"])):
                    raise CashCupError("Матч уже получил комнату; состав заблокирован")
                if out_player.id not in t["lineup"]:
                    raise CashCupError("Первый игрок не находится в основе")
                if in_player.id in t["lineup"] or not _player_by_id(t, in_player.id):
                    raise CashCupError("Второй игрок не является зарегистрированным запасным")
                t["lineup"].remove(out_player.id)
                t["lineup"].append(in_player.id)
            await self._post(interaction, "substitute", change,
                             {"out": out_player.id,"in": in_player.id})
            await self._reply(interaction, "Замена зарегистрирована")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @info.command(name="growth", description="Сколько участников пришло после анонса CASH CUP")
    async def growth(self, interaction: discord.Interaction):
        state = await cashcup_store.active(interaction.guild_id) or await cashcup_store.latest(interaction.guild_id)
        if not state:
            await self._reply(interaction, "CASH CUP не найден")
            return
        start = datetime.fromisoformat(state["created_at"])
        members = [p for t in state["teams"] for p in t["members"]]
        newcomers = sum(bool(p.get("joined_at")) and datetime.fromisoformat(p["joined_at"]) >= start
                        for p in members)
        await self._reply(interaction, f"Зарегистрировано игроков: {len(members)}. "
                          f"Вступили на сервер после анонса: {newcomers}. "
                          "Это не доказывает, что они пришли именно из-за турнира.")

    @cashcup.command(name="payout", description="Отметить реальную выплату призов (организатор)")
    async def payout(self, interaction: discord.Interaction, recipient: discord.Member,
                     amount: int, proof: str, tournament_id: str | None = None,
                     place: app_commands.Range[int, 1, 3] | None = None):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Доступно только организатору")
            return
        if amount <= 0 or not proof.startswith(("https://discord.com/channels/", "https://cdn.discordapp.com/")):
            await self._reply(interaction, "Нужна положительная сумма и ссылка на подтверждение")
            return
        try:
            def change(state):
                if state["status"] != "completed":
                    raise CashCupError("Сначала завершите турнир")
                if not any(p["discord_id"] == recipient.id for t in state["teams"] for p in t["members"]):
                    raise CashCupError("Получатель не зарегистрирован в турнире")
                if any(p["recipient"] == recipient.id for p in state["payouts"]):
                    raise CashCupError("Выплата уже отмечена — проверьте журнал")
                state["payouts"].append({"recipient": recipient.id, "amount_kzt": amount,
                                         "place": place, "proof": proof, "actor": interaction.user.id,
                                         "at": datetime.now(timezone.utc).isoformat()})
            state, _ = await cashcup_store.mutate(interaction.guild_id, interaction.user.id, "payout_recorded",
                                       change, {"recipient":recipient.id, "amount_kzt":amount, "proof":proof},
                                       include_finished=True, tournament_id=tournament_id)
            await self._refresh(interaction.guild_id, state)
            await self._reply(interaction, f"Отметка о выплате {amount} ₸ записана в журнал. Денежный перевод бот НЕ выполнял.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @info.command(name="forecast", description="Оценить минимальную нагрузку на игровые комнаты")
    async def forecast(self, interaction: discord.Interaction, rooms: app_commands.Range[int, 1, 64] = 8,
                       minutes_per_game: app_commands.Range[int, 5, 90] = 20):
        state = await cashcup_store.active(interaction.guild_id) or await cashcup_store.latest(interaction.guild_id)
        if not state:
            await self._reply(interaction, "CASH CUP не найден")
            return
        n = len(state["teams"])
        if n < 2:
            await self._reply(interaction, "Недостаточно команд для прогноза")
            return
        from services.cashcup_bracket import schedule_steps
        try:
            steps = schedule_steps([t["id"] for t in state["teams"]])
        except BracketError as exc:
            await self._reply(interaction, f"Не удалось построить симуляцию: {exc}")
            return
        import math
        total_games = sum(steps)
        # Время каждого шага — параллельные матчи, ограниченные числом комнат.
        step_minutes = [math.ceil(cnt / rooms) * minutes_per_game for cnt in steps]
        total = sum(step_minutes)
        load = [round(cnt / rooms, 2) for cnt in steps]
        gf_extra = ""
        if state.get("bracket") and any(m["bracket"] == "GF" for m in state["bracket"]["matches"]):
            gf_extra = ("\nGrand Final BO3 может добавить до 2 лишних игр "
                        f"(~{2 * minutes_per_game} мин) сверх оценки.")
        await self._reply(
            f"Команд: {n}; шагов сетки: {len(steps)}; матчей: {total_games}; комнат: {rooms}.\n"
            f"Оценка занятости: **{total//60} ч {total%60} мин** "
            f"(без check-in, пауз и задержек подтверждений — это НЕ гарантия).\n"
            f"Загрузка по шагам (матчей/комнат): {load}\n"
            f"Пиковая параллельность: {max(steps)} матчей одновременно.{gf_extra}")

    @cashcup.command(name="cancel", description="Отменить CASH CUP (организатор)")
    async def cancel(self, interaction: discord.Interaction, reason: str):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        try:
            def change(state):
                state["status"] = "cancelled"
            await self._post(interaction, "cancel", change, {"reason": reason[:300]})
            await self._reply(interaction, "CASH CUP отменён; запись сохранена в аудите")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))


    @cashcup.command(name="revert", description="Отменить последнее изменение результата (организатор)")
    async def revert(self, interaction: discord.Interaction):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        try:
            def change(state):
                if not state.get("undo"):
                    raise CashCupError("Нет сохранённого состояния для отката")
                if state["status"] not in ("running", "completed"):
                    raise CashCupError("Откат доступен только во время или после турнира")
                new_started = _revert_blocked(state)
                if new_started:
                    raise CashCupError(
                        "Зависимые матчи уже начались (check-in/комната/игры). "
                        "Автоматический откат заблокирован — требуется ручное "
                        "разрешение организатором через /cashcup dq|forfeit|resolve")
                state["bracket"] = state["undo"].pop()["bracket"]
                state.setdefault("reverts", []).append(
                    {"at": datetime.now(timezone.utc).isoformat()})
                state["status"] = ("completed" if state["bracket"].get("champion")
                                   else "running")
            await self._post(interaction, "revert_result", change)
            await self._reply(interaction, "Последнее изменение результата отменено. Сетка восстановлена из снимка.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="forfeit", description="Техническая победа одной из команд матча (организатор)")
    @app_commands.choices(winner=[app_commands.Choice(name="Команда A", value="a"),
                                  app_commands.Choice(name="Команда B", value="b")])
    async def forfeit(self, interaction: discord.Interaction, match_id: str,
                      winner: app_commands.Choice[str], reason: str):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        try:
            def change(state):
                _assert_running(state)
                b = state["bracket"]
                m = next((x for x in ready_matches(b)
                          if x["id"] == match_id.upper() and x["bracket"] != "GF"), None)
                if not m:
                    raise CashCupError("Матч не найден (для Grand Final используйте /cashcup game или /cashcup dq)")
                _push_snapshot(state)
                score = (1, 0) if winner.value == "a" else (0, 1)
                record_result(b, m["id"], *score,
                              evidence=f"technical:{reason[:150]}")
                if b["champion"]:
                    state["status"] = "completed"
                return m["id"]
            await self._post(interaction, "forfeit", change,
                             {"match": match_id, "winner": winner.value, "reason": reason[:200]})
            await self._reply(interaction, "Техническая победа записана.")
        except (CashCupError, BracketError) as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="dq", description="Дисквалифицировать команду: авто-поражения по сетке (организатор)")
    async def dq(self, interaction: discord.Interaction, team_name: str, reason: str):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        try:
            def change(state):
                _assert_running(state)
                t = _team(state, team_name)
                _push_snapshot(state)
                disqualify(state["bracket"], t["id"], evidence=reason[:150])
                if state["bracket"]["champion"]:
                    state["status"] = "completed"
                return t["id"]
            state, tid = await self._post(interaction, "disqualify", change,
                                          {"team": team_name, "reason": reason[:200]})
            b = state["bracket"]
            note = " CASH CUP завершён!" if state["status"] == "completed" else ""
            await self._reply(interaction,
                              f"Команда **{_team_name(state, tid)}** дисквалифицирована.{note}")
        except (CashCupError, BracketError) as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="pause", description="Приостановить турнир (организатор)")
    async def pause(self, interaction: discord.Interaction, reason: str = ""):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        try:
            def change(state):
                _assert_running(state)
                state["paused"] = True
                state["pause_reason"] = reason[:200]
            await self._post(interaction, "pause", change, {"reason": reason[:200]})
            await self._reply(interaction, "CASH CUP приостановлен: результаты, комнаты и замены заблокированы.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="resume", description="Возобновить турнир (организатор)")
    async def resume(self, interaction: discord.Interaction):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        try:
            def change(state):
                _assert_running(state)
                if not state.get("paused"):
                    raise CashCupError("Турнир не приостановлен")
                state["paused"] = False
                state.pop("pause_reason", None)
            await self._post(interaction, "resume", change)
            await self._reply(interaction, "CASH CUP возобновлён.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @info.command(name="history", description="История CASH CUP и список чемпионов")
    async def history(self, interaction: discord.Interaction):
        if not interaction.guild:
            await self._reply(interaction, "Нужен сервер r!z3")
            return
        past = await cashcup_store.list_past(interaction.guild_id, 10)
        if not past:
            await self._reply(interaction, "История CASH CUP пуста")
            return
        lines = ["**CASH CUP · история**"]
        champions = []
        for t in past:
            when = str(t.get("created_at", ""))[:10]
            status = {"registration": "регистрация", "running": "идёт",
                      "completed": "завершён", "cancelled": "отменён"}.get(t["status"], t["status"])
            teams_n = len(t.get("teams", []))
            champ = ""
            b = t.get("bracket")
            if t["status"] == "completed" and b and b.get("champion"):
                champ = f" · чемпион: **{_team_name(t, b['champion'])}**"
                champions.append(f"{_team_name(t, b['champion'])} ({when})")
            lines.append(f"`{t['tournament_id']}` {when} · {status} · команд: {teams_n}{champ}")
        if champions:
            lines.append("\n**Чемпионы:** " + "; ".join(champions[:10]))
        await self._reply(interaction, "\n".join(lines)[:1900])

    @info.command(name="invite", description="Капитан приглашает игрока в свою команду (DM)")
    async def invite(self, interaction: discord.Interaction, user: discord.Member):
        try:
            state = await cashcup_store.active(interaction.guild_id) or await cashcup_store.latest(interaction.guild_id)
            if not state:
                raise CashCupError("CASH CUP не найден")
            _assert_registration(state)
            t = _my_team(state, interaction.user.id)
            if len(t["members"]) + len(t["pending"]) >= 5:
                raise CashCupError("В команде уже 5 человек")
            if any(p["discord_id"] == user.id for p in t["members"] + t["pending"]):
                raise CashCupError("Игрок уже в команде или имеет заявку")
            try:
                member = await interaction.guild.fetch_member(user.id)
            except discord.HTTPException as exc:
                raise CashCupError("Игрок не найден на сервере") from exc
            await member.send(
                f"Вас пригласили в команду **{safe_text(t['name'])}** на r!z3 CASH CUP.\n"
                f"Чтобы принять, выполните `/cashcup join` с названием команды, "
                f"своим Free Fire UID и типом устройства (mobile/pc).")
            await self._reply(interaction, f"Приглашение отправлено <@{user.id}> в ЛС.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))


    @info.command(name="dispute", description="Открыть спор по матчу (капитан участника или ADMIN)")
    async def dispute(self, interaction: discord.Interaction, match_id: str, reason: str):
        if not interaction.guild:
            await self._reply(interaction, "Нужен сервер r!z3")
            return
        await interaction.response.defer(ephemeral=True)
        try:
            state = await cashcup_store.active(interaction.guild_id)
            if not state or state["status"] != "running":
                raise CashCupError("Нет активного турнира")
            b = state["bracket"]
            m = next((x for x in ready_matches(b) if x["id"] == match_id.upper()), None)
            if not m:
                raise CashCupError("Матч не найден или уже завершён")
            if m["bracket"] == "GF":
                raise CashCupError("По Grand Final спор не открывается — используйте /cashcup game или /cashcup dq")
            is_admin = _is_cashcup_admin(interaction.user)
            team_ids = {t["id"] for t in state["teams"]
                        if t["captain"] == interaction.user.id}
            if not is_admin and not (team_ids & {m["a"], m["b"]}):
                raise CashCupError("Спор может открыть капитан команды-участницы или ADMIN")
            def change(st):
                mm = next((x for x in ready_matches(st["bracket"])
                           if x["id"] == m["id"]), None)
                if not mm or mm["status"] != "ready":
                    raise CashCupError("Матч изменился. Повторите")
                mm["status"] = "disputed"
                mm["dispute"] = {"reason": reason[:300], "by": interaction.user.id,
                                 "at": datetime.now(timezone.utc).isoformat(),
                                 "evidence": []}
            await self._post(interaction, "dispute_open", change,
                             {"match": m["id"], "reason": reason[:200]})
            await self._reply(interaction,
                              f"Спор по матчу `{m['id']}` открыт. Продвижение результата заблокировано. "
                              "Участники могут передать доказательства через /cashcup info evidence.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @info.command(name="evidence", description="Передать доказательство по спорному матчу")
    async def evidence(self, interaction: discord.Interaction, match_id: str, url: str):
        if not interaction.guild:
            await self._reply(interaction, "Нужен сервер r!z3")
            return
        if not url.startswith(("https://discord.com/channels/", "https://cdn.discordapp.com/",
                               "https://media.discordapp.net/")):
            await self._reply(interaction, "Нужна ссылка на доказательство в Discord")
            return
        try:
            state = await cashcup_store.active(interaction.guild_id)
            if not state:
                raise CashCupError("Нет активного турнира")
            b = state["bracket"]
            m = next((x for x in b["matches"] if x["id"] == match_id.upper()), None)
            if not m or m.get("status") != "disputed" or not m.get("dispute"):
                raise CashCupError("Спор по этому матчу не открыт")
            team_ids = {t["id"] for t in state["teams"]
                        if any(p["discord_id"] == interaction.user.id for p in t["members"])}
            if not (_is_cashcup_admin(interaction.user) or team_ids & {m["a"], m["b"]}):
                raise CashCupError("Доказательства принимаются только от участников матча и ADMIN")
            def change(st):
                mm = next((x for x in st["bracket"]["matches"] if x["id"] == m["id"]), None)
                if not mm or mm.get("status") != "disputed":
                    raise CashCupError("Спор уже разрешён")
                mm["dispute"]["evidence"].append(
                    {"by": interaction.user.id, "url": url[:300],
                     "at": datetime.now(timezone.utc).isoformat()})
                if len(mm["dispute"]["evidence"]) > 10:
                    raise CashCupError("Лимит доказательств исчерпан")
            await self._post(interaction, "dispute_evidence", change,
                             {"match": m["id"]})
            await self._reply(interaction, "Доказательство добавлено в спор.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @info.command(name="resolve", description="Разрешить спор: победитель матча (ADMIN)")
    @app_commands.choices(winner=[app_commands.Choice(name="Команда A", value="a"),
                                  app_commands.Choice(name="Команда B", value="b")])
    async def resolve(self, interaction: discord.Interaction, match_id: str,
                      winner: app_commands.Choice[str], evidence: str):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Только ADMIN")
            return
        if not evidence.startswith(("https://discord.com/channels/", "https://cdn.discordapp.com/",
                                    "https://media.discordapp.net/")):
            await self._reply(interaction, "Нужна ссылка на подтверждение решения")
            return
        try:
            def change(state):
                _assert_running(state)
                m = next((x for x in state["bracket"]["matches"]
                          if x["id"] == match_id.upper()), None)
                if not m or m.get("status") != "disputed":
                    raise CashCupError("Спор по этому матчу не открыт")
                m["status"] = "ready"
                _push_snapshot(state)
                score = (1, 0) if winner.value == "a" else (0, 1)
                record_result(state["bracket"], m["id"], *score,
                              evidence=f"dispute:{evidence[:150]}")
                if state["bracket"]["champion"]:
                    state["status"] = "completed"
                return m["id"]
            await self._post(interaction, "dispute_resolve", change,
                             {"match": match_id, "winner": winner.value})
            await self._reply(interaction, "Спор разрешён, результат записан.")
        except (CashCupError, BracketError) as exc:
            await self._reply(interaction, str(exc))


def _apply_prizes(state: dict, p1: int, p2: int, p3: int, currency: str) -> dict:
    """Структурированный призовой фонд: 1/2/3 место. Сумма должна
    соответствовать pool, если он задан. Реальные выплаты — вне бота."""
    if min(p1, p2, p3) < 0:
        raise CashCupError("Суммы призовых мест не могут быть отрицательными")
    total = p1 + p2 + p3
    pool = state.get("pool") or 0
    if pool and total != pool:
        raise CashCupError(
            f"Сумма призовых мест ({total}) должна равняться призовому фонду ({pool})")
    state["prizes"] = {"places": {"1": p1, "2": p2, "3": p3},
                       "currency": currency[:10] or "KZT",
                       "total": total}
    return state["prizes"]


    @info.command(name="prizes", description="Призовые места 1/2/3 (ADMIN)")
    async def prizes(self, interaction: discord.Interaction,
                     first: int, second: int, third: int,
                     currency: str = "KZT"):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Только ADMIN")
            return
        try:
            def change(state):
                if state["status"] == "registration" and state.get("teams"):
                    pass
                return _apply_prizes(state, first, second, third, currency)
            _, prizes_data = await self._post(interaction, "set_prizes", change,
                                              {"p1": first, "p2": second, "p3": third})
            await self._reply(interaction,
                              f"Призовые места сохранены: 1 — {prizes_data['places']['1']}, "
                              f"2 — {prizes_data['places']['2']}, 3 — {prizes_data['places']['3']} "
                              f"{prizes_data['currency']}. Выплаты фиксируются через /cashcup payout.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @info.command(name="match", description="Детальная карточка матча")
    async def match_card(self, interaction: discord.Interaction, match_id: str):
        await self.show_match_card(interaction, match_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CashCupCog(bot))
