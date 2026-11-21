"""r!z3 CASH CUP: командная регистрация и динамический Double Elimination.

Сервис работает отдельно от Tournament/BR/Ultimatum и от игровой экономики.
Призовые выплаты выполняются вне бота; здесь фиксируется результат и аудит.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
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
from storage.team_store import team_store
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

def _find_cc_team(state: dict, user_id: int) -> dict | None:
    """Команда CASH CUP, в которой состоит пользователь (members или pending)."""
    return next((t for t in state.get("teams", [])
                 if any(p["discord_id"] == user_id
                        for p in t["members"] + t.get("pending", []))), None)



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
            # sorted list: state сериализуется json.dumps в PG — set
            # недопустим (TypeError на каждой записи результата).
            "started": sorted(_started_markers(state["bracket"])),
        })
        del state["undo"][:-5]


def _revert_blocked(state: dict) -> set:
    """Матчи, «начавшиеся» после последнего снимка — блокируют авто-откат."""
    if not state.get("undo") or not state.get("bracket"):
        return set()
    return (_started_markers(state["bracket"])
            - set(state["undo"][-1]["started"]))


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
            if ff_uid and player.get("ff_uid") == ff_uid:
                raise CashCupError("Этот Free Fire UID уже используется")


def _player_by_id(team: dict, user_id: int) -> dict | None:
    return next((p for p in team["members"] if p["discord_id"] == user_id), None)


def _label(team: dict) -> str:
    return safe_text(team["name"], limit=65)


def _team_name(state: dict, team_id: str) -> str:
    return next((_label(t) for t in state["teams"] if t["id"] == team_id), "Неизвестная команда")


# ---------- диагностика состава (read-only) ----------

_STATUS_ERROR = "Ошибка состава"
_STATUS_NOT_READY = "Не готова"
_STATUS_WAIT_ADMIN = "Ожидает ADMIN"
_STATUS_READY = "Готова"


def _member_problems(p: dict, *, include_admin: bool = True) -> list[str]:
    """Обязательные данные одного участника. include_admin=False —
    для verify(): отсутствие ADMIN-подтверждения — это и есть
    действие verify, а не ошибка."""
    probs = []
    uid = str(p.get("ff_uid") or "").strip()
    if not (uid.isdigit() and 5 <= len(uid) <= 20):
        probs.append("не заполнен Free Fire UID")
    if not p.get("nick"):
        probs.append("нет игрового ника")
    if not p.get("region"):
        probs.append("не указан регион")
    if p.get("device") not in ("pc", "mobile"):
        probs.append("не указано устройство")
    if not p.get("ff_ok"):
        probs.append("FF API не подтверждён")
    if include_admin and not p.get("verified"):
        probs.append("ожидает подтверждения ADMIN")
    return probs


def _team_diag(state: dict, team: dict, *,
               include_admin: bool = True) -> dict:
    """Полная read-only диагностика команды: ВСЕ проблемы, счётчики
    готовности и статус. Не изменяет состояние."""
    members = team.get("members") or []
    structural: list[str] = []
    if len(members) > 5:
        structural.append("состав превышает максимум 5 игроков")
    dids = [p.get("discord_id") for p in members]
    if len(dids) != len(set(dids)):
        structural.append("дублирующийся Discord ID в составе")
    ff_uids = [str(p.get("ff_uid") or "") for p in members if p.get("ff_uid")]
    if len(ff_uids) != len(set(ff_uids)):
        structural.append("дублирующийся Free Fire UID в составе")
    lineup = team.get("lineup") or []
    member_ids = set(dids)
    if len(lineup) != 4:
        structural.append("основа должна содержать 4 игроков")
    elif len(set(lineup)) != 4:
        structural.append("повторяющийся игрок в основе")
    elif any(x not in member_ids for x in lineup):
        structural.append("в основе игрок вне состава")
    pcs = sum(1 for p in members if p.get("device") == "pc")
    if pcs > 2:
        structural.append("более двух ПК")
    # Дубли между командами (Discord ID / FF UID).
    cross = False
    for other in state.get("teams", []):
        if other is team or other.get("id") == team.get("id"):
            continue
        om = {p.get("discord_id") for p in other.get("members", [])}
        of = {str(p.get("ff_uid") or "") for p in other.get("members", [])
              if p.get("ff_uid")}
        for p in members:
            if p.get("discord_id") in om or \
                    (p.get("ff_uid") and str(p["ff_uid"]) in of):
                cross = True
                break
        if cross:
            break
    if cross:
        structural.append("игрок или Free Fire UID занят в другой команде")

    member_lines: list[str] = []
    has_missing = False
    all_verified = bool(members)
    for p in members:
        probs = _member_problems(p, include_admin=include_admin)
        if probs:
            member_lines.append(
                f"<@{p.get('discord_id')}> — " + "; ".join(probs))
            if any(pr != "ожидает подтверждения ADMIN" for pr in probs):
                has_missing = True
        if include_admin and not p.get("verified"):
            all_verified = False

    ff_api = sum(1 for p in members if p.get("ff_ok"))
    admin_n = sum(1 for p in members if p.get("verified"))
    no_device = sum(1 for p in members
                    if p.get("device") not in ("pc", "mobile"))

    if structural:
        status = _STATUS_ERROR
    elif len(members) < 4 or has_missing:
        status = _STATUS_NOT_READY
    elif not all_verified:
        status = _STATUS_WAIT_ADMIN
    else:
        status = _STATUS_READY
    return {
        "problems": structural + member_lines,
        "structural": structural,
        "status": status,
        "members": len(members),
        "ff_api": ff_api,
        "admin": admin_n,
        "pcs": pcs,
        "no_device": no_device,
    }


def _diag_reply_text(team: dict, diag: dict) -> str:
    """Текст полной проверки команды (read-only отчёт)."""
    lines = ["**CASH CUP · ПРОВЕРКА КОМАНДЫ**", "",
             f"**{_label(team)}**", ""]
    if diag["problems"]:
        lines.append("**Проблемы:**")
        lines += [f"- {p}" for p in diag["problems"][:20]]
        if len(diag["problems"]) > 20:
            lines.append(f"… ещё {len(diag['problems'])-20}")
    else:
        lines.append("Проблем не найдено.")
    lines += ["",
              f"FF API: {diag['ff_api']}/{diag['members']} · "
              f"ADMIN: {diag['admin']}/{diag['members']} · "
              f"ПК: {diag['pcs']} · без устройства: {diag['no_device']}",
              f"**Статус:** {diag['status']}",
              "Итог: команда " + ("допущена." if diag["status"] == _STATUS_READY
                                  else "не допущена.")]
    return "\n".join(lines)[:1900]


def _teams_render(state: dict, page: int) -> tuple[str, int, int]:
    """Страница списка команд. Возвращает (text, page, pages).
    Каждая команда — неделимый блок: страница никогда не
    обрезает команду посередине (фиксировано 5 команд/стр)."""
    teams = state.get("teams") or []
    per_page = 5
    pages = max(1, (len(teams) + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    chunk = teams[page * per_page:(page + 1) * per_page]
    lines = [f"**CASH CUP · команды ({len(teams)})** — "
             f"стр. {page + 1}/{pages}"]
    for t in chunk:
        d = _team_diag(state, t)
        cap = t.get("captain")
        lines += [
            "",
            f"**{_label(t)}**",
            f"- Капитан: <@{cap}>",
            f"- Участники: {d['members']}/5",
            f"- FF API: {d['ff_api']}/{d['members']}",
            f"- ADMIN: {d['admin']}/{d['members']}",
            f"- ПК: {d['pcs']}",
            f"- Устройство не указано: {d['no_device']}",
            f"- Статус: {d['status']}",
        ]
    return "\n".join(lines)[:1950], page, pages


# ---------- /alert: уведомление капитанов о незаполненных данных ----------

_ALERT_COOLDOWN = 15 * 60  # секунд между публичными /alert

# Человекочитаемые причины для публичного отчёта (без FF UID).
_ALERT_PROB_LABELS = {
    "не заполнен Free Fire UID": "не указан Free Fire UID",
    "нет игрового ника": "не указан ник Free Fire",
    "не указан регион": "не указан регион",
    "не указано устройство": "не указано устройство",
    "FF API не подтверждён": "FF API не подтвердил аккаунт",
}

_ALERT_HEADER = (
    "### r!z3 CASH CUP — ПРОВЕРКА СОСТАВОВ\n\n"
    "**Капитаны, внимание!** Для допуска к турниру все участники "
    "команды должны заполнить данные Free Fire и пройти проверку "
    "ADMIN."
)

_ALERT_FOOTER = (
    "### Как пройти проверку\n\n"
    "1. Откройте карточку **CASH CUP**.\n"
    "2. Нажмите **«Мои данные»**.\n"
    "3. Нажмите **«Обновить данные»**.\n"
    "4. Укажите ваш **Free Fire UID**, регион и устройство "
    "(`mobile` или `pc`).\n"
    "5. Отправьте форму и дождитесь проверки Free Fire API.\n"
    "6. После заполнения всех данных дождитесь подтверждения "
    "**ADMIN**.\n\n"
    "**Важно:** каждый участник заполняет данные самостоятельно. "
    "Капитан обязан убедиться, что все игроки команды прошли "
    "процедуру.\n\n"
    "**Команда допускается к турниру только после выполнения "
    "всех обязательных проверок.**"
)


def _alert_member_issue(p: dict) -> tuple[str, bool]:
    """(текст проблемы, нужно ли действие игрока).

    Если заполнены все данные и осталось только ожидание ADMIN —
    показываем отдельно: повторно вводить данные не нужно.
    """
    probs = _member_problems(p)
    fill = [pr for pr in probs if pr != "ожидает подтверждения ADMIN"]
    if not fill:
        if probs:
            return "данные заполнены, ожидает проверки ADMIN", False
        return "", False
    text = ", ".join(dict.fromkeys(
        _ALERT_PROB_LABELS.get(pr, pr) for pr in fill))
    return text, True


def _alert_report(state: dict) -> dict:
    """Read-only отчёт /alert по командам: блоки, капитаны,
    участники, которым нужно действие, счётчик готовых."""
    teams = state.get("teams") or []
    blocks: list[str] = []
    captains: list[int] = []
    actors: list[int] = []
    ready = 0
    for t in teams:
        diag = _team_diag(state, t)
        cap = t.get("captain")
        if cap:
            captains.append(int(cap))
        lines = [f"**{_label(t)}** — капитан <@{cap}>", ""]
        issues: list[str] = []
        for p in t.get("members") or []:
            text, needs = _alert_member_issue(p)
            if text:
                issues.append(f"- <@{p.get('discord_id')}> — {text}")
                if needs:
                    actors.append(int(p["discord_id"]))
        # Структурные проблемы (дубли UID/Discord, состав, >2 ПК,
        # cross-team) — из существующей диагностики, без изменений.
        issues += [f"- {sp}" for sp in diag["structural"]]
        lines += issues or [
            "Все обязательные данные заполнены и проверены."]
        # «Готова» здесь означает только заполненность данных —
        # финальные проверки (_validate_teams: теги, VAC) делаются
        # при /cashcup start, поэтому гарантированный допуск не
        # заявляем.
        status_label = (
            "Данные заполнены" if diag["status"] == _STATUS_READY
            else diag["status"])
        lines += ["",
                  f"Статус: **{status_label} — "
                  f"{diag['admin']}/{diag['members']} подтверждено**"]
        blocks.append("\n".join(lines))
        if diag["status"] == _STATUS_READY:
            ready += 1
    return {
        "blocks": blocks,
        "captains": list(dict.fromkeys(captains)),
        "actors": list(dict.fromkeys(actors)),
        "ready": ready,
        "total": len(teams),
    }


def _alert_roll_lines(title: str, ids: list[int]) -> list[str]:
    """Строки ролл-колла упоминаний (≤15 на строку)."""
    ids = list(dict.fromkeys(int(i) for i in ids if i))
    lines = []
    for i in range(0, len(ids), 15):
        chunk = " ".join(f"<@{u}>" for u in ids[i:i + 15])
        lines.append(f"**{title}:** {chunk}")
    return lines


def _alert_payload(state: dict, guild_id: int
                   ) -> tuple[list[str], list[int], dict]:
    """Строки публичного уведомления, id для allowed_mentions
    и сводка отчёта. Реальные FF UID не выводятся."""
    report = _alert_report(state)
    lines = [_ALERT_HEADER, ""]
    lines += _alert_roll_lines("Капитаны", report["captains"])
    lines += _alert_roll_lines(
        "Требуется заполнить данные", report["actors"])
    lines.append("")
    for block in report["blocks"]:
        lines += block.split("\n") + [""]
    cid, mid = state.get("channel_id"), state.get("message_id")
    if cid and mid:
        lines.append(
            "[Актуальная карточка CASH CUP]"
            f"(https://discord.com/channels/{guild_id}/{cid}/{mid})")
    else:
        lines.append(
            "⚠️ Актуальная карточка турнира не найдена — "
            "обратитесь к ADMIN.")
    lines += ["", _ALERT_FOOTER]
    mention_ids = list(dict.fromkeys(
        report["captains"] + report["actors"]))
    return lines, mention_ids, report


def _pack_lines(lines: list[str], limit: int = 1900) -> list[str]:
    """Упаковка строк в сообщения ≤limit без обрезания блоков:
    при переполнении текст продолжается следующим сообщением."""
    msgs: list[str] = []
    cur = ""
    for ln in lines:
        candidate = (cur + "\n" + ln) if cur else ln
        if len(candidate) > limit and cur:
            msgs.append(cur)
            cur = ln
        else:
            cur = candidate
        while len(cur) > limit:
            msgs.append(cur[:limit])
            cur = cur[limit:]
    if cur:
        msgs.append(cur)
    # Discord: ≤100 уникальных user mentions на сообщение —
    # разбиваем слишком плотные чанки по границам строк.
    out: list[str] = []
    for msg in msgs:
        parts = msg.split("\n")
        buf = ""
        for part in parts:
            cand = (buf + "\n" + part) if buf else part
            if _mention_ids(cand) > 100 and buf:
                out.append(buf)
                buf = part
            else:
                buf = cand
        if buf:
            out.append(buf)
    return out


_MENTION_RE = re.compile(r"<@(!?)(\d+)>")


def _mention_ids(text: str) -> int:
    """Число уникальных user ID в тексте (без @everyone/@here/roles)."""
    return len({m.group(2) for m in _MENTION_RE.finditer(text)})


class TeamsView(discord.ui.View):
    """Пагинация списка команд (эфемерно, правит то же сообщение)."""

    def __init__(self, cog, page: int, pages: int):
        super().__init__(timeout=300)
        if page > 0:
            self.add_item(_ActButton(
                "◀", discord.ButtonStyle.secondary,
                lambda i: cog._teams_goto(i, page - 1)))
        if page < pages - 1:
            self.add_item(_ActButton(
                "▶", discord.ButtonStyle.secondary,
                lambda i: cog._teams_goto(i, page + 1)))


class _MyDataView(discord.ui.View):
    """Эфемерный статус «Мои данные» + кнопка обновления (modal —
    первый ответ этого interaction, defer перед send_modal нет)."""

    def __init__(self, cog, team_id: str | None, tid: str | None):
        super().__init__(timeout=300)
        self.add_item(_ActButton(
            "Обновить данные", discord.ButtonStyle.primary,
            lambda i: i.response.send_modal(
                FFDataModal(team_id=team_id, tid=tid))))


def _panel(state: dict) -> discord.Embed:
    running = state["status"] == "running"
    completed = state["status"] == "completed"
    lines = [f"**Приз:** {safe_text(state['prize'], limit=90)}"]
    if state.get("pool"):
        lines.append(f"**Призовой фонд:** {state['pool']:,} ₸".replace(",", " "))
    if state.get("prizes"):
        pr = state["prizes"]
        lines.append(f"**ПРИЗ ЗА 1 МЕСТО:** {pr['places'].get('1',0):,} {pr.get('currency','')}".replace(","," "))
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
    """Финальный gate перед стартом. Собирает ВСЕ ошибки по всем
    командам (а не только первую) и поднимает сводку разом."""
    errors: list[str] = []
    if len(state["teams"]) < 2:
        raise CashCupError("Необходимо минимум две команды")
    ids = set()
    ff_ids = set()
    for team in state["teams"]:
        if len(team["members"]) not in (4, 5):
            errors.append(f"{_label(team)}: необходимо 4–5 подтверждённых игроков")
            continue
        if len(team["lineup"]) != 4:
            errors.append(f"{_label(team)}: основной состав должен содержать 4 игроков")
        elif len(set(team["lineup"])) != 4:
            errors.append(f"{_label(team)}: повторяющийся игрок в основе")
        for p in team["members"]:
            uid_s = str(p.get("ff_uid") or "")
            if not (uid_s.isdigit() and 5 <= len(uid_s) <= 20):
                errors.append(
                    f"{_label(team)}: у <@{p['discord_id']}> нет корректного Free Fire UID")
            if not p.get("nick"):
                errors.append(
                    f"{_label(team)}: у <@{p['discord_id']}> нет игрового ника")
            if not p.get("region"):
                errors.append(
                    f"{_label(team)}: у <@{p['discord_id']}> не указан регион")
            if p.get("device") not in ("pc", "mobile"):
                errors.append(
                    f"{_label(team)}: у <@{p['discord_id']}> тип устройства mobile/pc")
            if not p.get("ff_ok"):
                errors.append(
                    f"{_label(team)}: Free Fire API не подтвердил аккаунт <@{p['discord_id']}>")
            if not p.get("verified"):
                errors.append(
                    f"{_label(team)}: ADMIN не подтвердил аккаунт <@{p['discord_id']}>")
        if any(not p["verified"] for p in team["members"]):
            errors.append(f"{_label(team)}: организатор не подтвердил игровые аккаунты")
        if sum(p["device"] == "pc" for p in team["members"]) > 2:
            errors.append(f"{_label(team)}: более двух ПК")
        for p in team["members"]:
            if p["discord_id"] in ids or p["ff_uid"] in ff_ids:
                errors.append("Один аккаунт участвует в нескольких командах")
            ids.add(p["discord_id"])
            ff_ids.add(p["ff_uid"])
    semaphore = asyncio.Semaphore(5)
    async def check(uid: int) -> str | None:
        async with semaphore:
            try:
                await _member_check(bot, guild, uid)
            except CashCupError as exc:
                return f"<@{uid}>: {exc}"
            return None
    errors += [e for e in await asyncio.gather(*(check(uid) for uid in ids)) if e]
    if errors:
        raise CashCupError("\n".join(errors)[:1900])


async def _defer(interaction: discord.Interaction, *,
                 ephemeral: bool = True) -> None:
    """Идемпотентный defer: безопасен при повторном вызове из кнопки
    и метода cog, а также если response уже выполнен."""
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral)
    except discord.InteractionResponded:
        pass


async def _send(interaction: discord.Interaction, message: str = "",
                *, view=None, ephemeral: bool = True) -> None:
    """Ответ с учётом уже выполненного defer: followup вместо response."""
    if interaction.response.is_done():
        await interaction.followup.send(
            message, view=view, ephemeral=ephemeral,
            allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(
            message, view=view, ephemeral=ephemeral,
            allowed_mentions=discord.AllowedMentions.none())


class CashCupView(discord.ui.View):
    """Публичная карточка регистрации CASH CUP (persistent view)."""

    def __init__(self, registration: bool = True):
        super().__init__(timeout=None)

    async def _fresh(self, interaction: discord.Interaction) -> bool:
        """Кнопка действует только с актуальной карточкой текущего турнира:
        message_id сообщения обязан совпадать с state['message_id']."""
        cog = interaction.client.get_cog("CashCupCog")
        state = await cog._current_state(interaction.guild_id)
        mid = getattr(getattr(interaction, "message", None), "id", None)
        expected = (state or {}).get("message_id")
        if state and expected and mid and int(mid) != int(expected):
            await _send(interaction,
                        "Эта карточка устарела — используйте актуальную карточку турнира.")
            return False
        return True

    @discord.ui.button(label="Зарегистрировать команду",
                       style=discord.ButtonStyle.success,
                       custom_id="cashcup:register_br")
    async def register_br(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        # Defer ДО любых запросов к PostgreSQL: медленный pool
        # не должен приводить к Discord 10062 Unknown interaction.
        await _defer(interaction)
        if not await self._fresh(interaction):
            return
        cog = interaction.client.get_cog("CashCupCog")
        await cog.register_br_team(interaction)

    @discord.ui.button(label="Мои данные", style=discord.ButtonStyle.primary,
                       custom_id="cashcup:my_data")
    async def my_data(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        # Статус «Мои данные» — ephemeral answer (modal открывается
        # отдельной кнопкой внутри view, т.к. send_modal нельзя
        # после defer/response).
        cog = interaction.client.get_cog("CashCupCog")
        outcome, state = await cog._state_for_card(interaction)
        if outcome == "timeout":
            await interaction.response.send_message(
                "База данных занята — нажмите «Мои данные» ещё раз "
                "через несколько секунд.", ephemeral=True)
            return
        if outcome == "stale":
            await interaction.response.send_message(
                "Эта карточка устарела — используйте актуальную "
                "карточку турнира.", ephemeral=True)
            return
        await cog.show_my_data(interaction, state=state)

    @discord.ui.button(label="Запасной", style=discord.ButtonStyle.primary,
                       custom_id="cashcup:sub_join")
    async def sub_join(self, interaction: discord.Interaction,
                       button: discord.ui.Button):
        cog = interaction.client.get_cog("CashCupCog")
        outcome, state = await cog._state_for_card(interaction)
        if outcome == "timeout":
            await interaction.response.send_message(
                "База данных занята — нажмите «Запасной» ещё раз "
                "через несколько секунд.", ephemeral=True)
            return
        if outcome == "stale":
            await interaction.response.send_message(
                "Эта карточка устарела — используйте актуальную "
                "карточку турнира.", ephemeral=True)
            return
        await cog.sub_offer_teams(interaction, state=state)

    @discord.ui.button(label="Моя команда", style=discord.ButtonStyle.secondary,
                       custom_id="cashcup:my_team")
    async def roster(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        await _defer(interaction)
        if not await self._fresh(interaction):
            return
        cog = interaction.client.get_cog("CashCupCog")
        await cog.show_roster(interaction)

    @discord.ui.button(label="Команды", style=discord.ButtonStyle.secondary,
                       custom_id="cashcup:teams")
    async def teams(self, interaction: discord.Interaction,
                    button: discord.ui.Button):
        await _defer(interaction)
        if not await self._fresh(interaction):
            return
        cog = interaction.client.get_cog("CashCupCog")
        await cog.show_teams(interaction)

    @discord.ui.button(label="Матчи", style=discord.ButtonStyle.secondary,
                       custom_id="cashcup:matches")
    async def matches(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        await _defer(interaction)
        if not await self._fresh(interaction):
            return
        cog = interaction.client.get_cog("CashCupCog")
        await cog.show_active_matches(interaction)

    @discord.ui.button(label="Сетка", style=discord.ButtonStyle.secondary,
                       custom_id="cashcup:bracket")
    async def bracket(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        await _defer(interaction)
        if not await self._fresh(interaction):
            return
        cog = interaction.client.get_cog("CashCupCog")
        await cog.show_bracket(interaction)

    @discord.ui.button(label="Управление", style=discord.ButtonStyle.secondary,
                       custom_id="cashcup:admin")
    async def admin(self, interaction: discord.Interaction,
                    button: discord.ui.Button):
        await _defer(interaction)
        if not await self._fresh(interaction):
            return
        cog = interaction.client.get_cog("CashCupCog")
        await cog.admin_menu(interaction)


def _tid(state) -> str:
    """ID конкретного турнира — привязка ephemeral/persistent компонентов."""
    return str((state or {}).get("tournament_id") or "")


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


def _board_matches(bracket: dict) -> list:
    """Матчи для публичной доски: активные + disputed (видны, но не продвигаются)."""
    active = {m["id"] for m in ready_matches(bracket)}
    return [m for m in bracket["matches"]
            if m["id"] in active or m.get("status") == "disputed"]


BOARD_PER_PAGE = 15  # совпадает с BoardSelectView.PER_PAGE: embed и select
                     # показывают один и тот же срез матчей


def _board_embed(state: dict, *, page: int = 0) -> discord.Embed:
    b = state.get("bracket") or {}
    matches = _board_matches(b) if b else []
    active = ready_matches(b) if b else []
    disputed = [m for m in matches if m.get("status") == "disputed"]
    lines = ["**Текущий раунд — выберите матч**"]
    chunk = matches[page * BOARD_PER_PAGE:(page + 1) * BOARD_PER_PAGE]
    for m in chunk:
        score = f" · {m['score'][0]}:{m['score'][1]}" if m["bracket"] == "GF" else ""
        flag = " ⚠️ СПОР" if m["status"] == "disputed" else ""
        lines.append(f"`{m['id']}` {_team_name(state, m['a'])} vs {_team_name(state, m['b'])}"
                     f" · BO{m['bo']}{score}{flag}")
    if not matches:
        lines.append("Активных матчей нет" +
                     (f". Чемпион: **{_team_name(state, b['champion'])}**"
                      if b.get("champion") else "."))
    if b.get("champion"):
        done = [m for m in b["matches"] if m["status"] == "completed"]
        lines.append(f"\n**🏆 Чемпион: {_team_name(state, b['champion'])}**")
        for m in done[-5:]:
            lines.append(f"`{m['id']}` {_team_name(state, m['a'])} {m['score'][0]}:{m['score'][1]} {_team_name(state, m['b'])}")
    pages = max(1, (len(matches) + BOARD_PER_PAGE - 1) // BOARD_PER_PAGE)
    if pages > 1:
        lines.append(f"\nМатчей: {len(matches)} · страница {page + 1}/{pages} "
                     "(кнопки под списком)")
    return discord.Embed(title="r!z3 CASH CUP · матчи", description="\n".join(lines)[:4000],
                         color=0x26262A)


class BoardSelectView(discord.ui.View):
    """Persistent Select активных матчей → ephemeral карточка матча.
    Пагинация кнопками «Назад/Вперёд» при >25 матчах на доске."""
    PER_PAGE = BOARD_PER_PAGE

    def __init__(self, matches=None, *, page: int = 0, state: dict | None = None):
        super().__init__(timeout=None)
        matches = matches or []
        self._total = len(matches)
        self._page = max(0, min(page, (self._total - 1) // self.PER_PAGE
                                if self._total else 0))
        self._state = state
        chunk = matches[self._page * self.PER_PAGE:
                       (self._page + 1) * self.PER_PAGE]
        options = [discord.SelectOption(
            label=m["id"], value=m["id"],
            description=_short_desc(m)[:100]) for m in chunk]
        if not options:
            options = [discord.SelectOption(label="Матчей нет", value="none")]
        self._sel = discord.ui.Select(
            placeholder="Открыть матч…", custom_id="cashcup:board_select",
            options=options, disabled=not matches)
        self._sel.callback = self._pick
        self.add_item(self._sel)
        # Кнопки пагинации регистрируются всегда (даже disabled): persistent
        # dispatch требует наличия custom_id в зарегистрированном view.
        pages = max(1, (self._total + self.PER_PAGE - 1) // self.PER_PAGE)
        prev_b = discord.ui.Button(label="Назад", style=discord.ButtonStyle.secondary,
                                   custom_id="cashcup:board_prev",
                                   disabled=self._page == 0)
        next_b = discord.ui.Button(label="Вперёд", style=discord.ButtonStyle.secondary,
                                   custom_id="cashcup:board_next",
                                   disabled=self._page >= pages - 1)
        prev_b.callback = lambda i: self._nav(i, -1)
        next_b.callback = lambda i: self._nav(i, +1)
        self.add_item(prev_b)
        self.add_item(next_b)

    async def _fresh(self, interaction: discord.Interaction):
        """None — сообщение не актуальная доска текущего турнира."""
        cog = interaction.client.get_cog("CashCupCog")
        state = await cog._current_state(interaction.guild_id)
        mid = getattr(getattr(interaction, "message", None), "id", None)
        expected = (state or {}).get("board_message_id")
        if state and expected and mid and int(mid) != int(expected):
            await _send(interaction,
                        "Эта доска устарела — используйте актуальную.")
            return None
        return state

    async def _pick(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
        state = await self._fresh(interaction)
        if state is None:
            return
        mid = self._sel.values[0]
        if mid == "none":
            await _send(interaction, "Активных матчей нет.")
            return
        cog = interaction.client.get_cog("CashCupCog")
        await cog.show_match_card(interaction, mid)

    async def _nav(self, interaction: discord.Interaction, delta: int) -> None:
        # Defer до запроса к PostgreSQL — иначе медленный pool даёт 10062.
        await _defer(interaction, ephemeral=False)
        cog = interaction.client.get_cog("CashCupCog")
        state = await cog._current_state(interaction.guild_id)
        mid = getattr(getattr(interaction, "message", None), "id", None)
        expected = (state or {}).get("board_message_id")
        if state and expected and mid and int(mid) != int(expected):
            await _send(interaction,
                        "Эта доска устарела — используйте актуальную.")
            return
        if not state or not state.get("bracket"):
            await _send(interaction, "Состояние турнира не найдено.")
            return
        # delta передаётся внутрь: базовая страница читается и
        # вычисляется уже под per-guild lock вместе со свежим PG-стейтом.
        await cog._sync_board(state, interaction.channel, delta=delta)


def _short_desc(m: dict) -> str:
    st = {"ready": "ожидание", "disputed": "СПОР"}.get(m["status"], m["status"])
    return f"BO{m['bo']} · {st}"


class CashCupCog(commands.Cog):
    cashcup = app_commands.Group(name="cashcup", description="r!z3 CASH CUP · денежный турнир")
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._board_locks: dict[int, asyncio.Lock] = {}
        self._board_pages: dict[int, int] = {}
        self._boards_lock = asyncio.Lock()
        self._boards_ok = False
        # Кэш последнего прочитанного состояния по guild: кнопки,
        # открывающие Modal (send_modal нельзя после defer),
        # проверяют свежесть карточки без ожидания PostgreSQL.
        self._state_cache: dict[int, tuple[float, dict | None]] = {}
        # /alert: per-guild lock + cooldown публичных уведомлений.
        self._alert_locks: dict[int, asyncio.Lock] = {}
        self._alert_last: dict[int, float] = {}
        # (signature отчёта, {sha1 отправленных чанков}) — защита
        # от дублей при частичной публикации; в памяти, при рестарте
        # сбрасывается (cooldown 15 мин ограничивает спам).
        self._alert_sent: dict[int, tuple[str, set]] = {}

    def _board_lock(self, guild_id: int) -> asyncio.Lock:
        return self._board_locks.setdefault(guild_id, asyncio.Lock())

    async def cog_load(self) -> None:
        # Только регистрация persistent components — bot.guilds на этом этапе
        # ещё пуст (Gateway не подключён). Восстановление досок — в on_ready.
        self.bot.add_view(compose(_panel({"status": "registration", "prize": "", "teams": []}), CashCupView(True)))
        self.bot.add_view(BoardSelectView())

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Gateway готов: восстанавливаем доски активных/завершённых турниров.
        Идемпотентно: повторный on_ready (reconnect) не создаёт дублей,
        при полном успехе повторный запуск пропускается."""
        if self._boards_ok:
            return
        async with self._boards_lock:
            if self._boards_ok:
                return
            try:
                failures = await self._restore_boards()
                self._boards_ok = (failures == 0)
            except Exception:
                logger.exception("CASH CUP board restore failed")

    async def _restore_boards(self) -> int:
        """Для каждого guild с активной/завершённой сеткой обновляет доску
        на месте (замена только при подтверждённом NotFound). Возвращает
        число guild, для которых восстановление не удалось."""
        failures = 0
        for guild in getattr(self.bot, "guilds", []) or []:
            try:
                state = await cashcup_store.latest(guild.id)
                if not state:
                    continue
                status = state.get("status")
                if status == "registration":
                    # Регистрация тоже нуждается в восстановлении:
                    # карточка в Discord переживает рестарт, но если
                    # её удалили — без этого кода турнир мёртв.
                    channel = await self._restore_channel(guild, state)
                    if channel is None:
                        failures += 1
                        continue
                    if not await self._restore_registration_card(
                            state, channel):
                        failures += 1
                    continue
                if status not in ("running", "completed") \
                        or not state.get("bracket"):
                    continue
                channel = await self._restore_channel(guild, state)
                if channel is None:
                    failures += 1
                    continue
                # False = edit/send не выполнен (403/429/5xx/нет fresh state):
                # считаем guild неудачным — on_ready повторит при следующем
                # вызове, т.к. _boards_ok останется False.
                if not await self._sync_board(state, channel):
                    failures += 1
            except Exception:
                failures += 1
                logger.exception("CASH CUP board restore failed guild=%s",
                                 getattr(guild, "id", "?"))
        return failures

    async def _restore_channel(self, guild, state: dict):
        """Канал карточки/доски; None при недоступности."""
        channel = guild.get_channel(state.get("channel_id"))
        if channel is None:
            try:
                channel = await guild.fetch_channel(state["channel_id"])
            except Exception:
                return None
        return channel

    async def _restore_registration_card(self, state: dict,
                                         channel) -> bool:
        """Восстановить карточку регистрации после рестарта.

        - message_id жив → идемпотентный edit (обновляет команды,
          НЕ создаёт дублей при каждом on_ready/reconnect);
        - NotFound → ОДНА замена + сохранение нового message_id
          адресной записью (teams/status не трогаем);
        - прочие HTTP-ошибки → False (on_ready повторит позже).
        """
        view = compose(_panel(state), CashCupView(True))
        mid = state.get("message_id")
        if mid:
            try:
                msg = await channel.fetch_message(mid)
            except discord.NotFound:
                msg = None  # удалена вручную — создаём замену
            except discord.HTTPException as exc:
                logger.warning("CASH CUP card fetch failed: %s", exc)
                return False
            if msg is not None:
                try:
                    await msg.edit(view=view,
                                   allowed_mentions=discord.AllowedMentions.none())
                    return True
                except discord.NotFound:
                    pass  # удалили между fetch и edit — заменяем
                except discord.HTTPException as exc:
                    logger.warning("CASH CUP card edit failed: %s", exc)
                    return False
        # Подтверждённое отсутствие сообщения — публикуем замену.
        try:
            msg = await channel.send(view=view,
                                     allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            logger.warning("CASH CUP card resend failed: %s", exc)
            return False
        try:
            # Обновляет ТОЛЬКО колонку message_id активного турнира
            # (unique index гарантирует единственную registration/running
            # строку на guild). teams/status/state не изменяются.
            await cashcup_store.set_message(state["guild_id"], msg.id)
        except Exception:
            # Discord-сообщение создано, но id не сохранён: удаляем
            # сироту, иначе следующий restore создаст дубль.
            try:
                await msg.delete()
            except Exception:
                pass
            logger.exception("CASH CUP card message_id save failed")
            return False
        return True

    async def _refresh(self, guild_id: int, state: dict | None = None) -> None:
        if state is None:
            state = await self._current_state(guild_id)
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

    async def _sync_board(self, state: dict, channel, *,
                          page: int | None = None,
                          delta: int | None = None) -> bool:
        """Публичная карточка текущего раунда: одно сообщение, обновляется
        на месте. Все изменения — под per-guild lock с перечитыванием PG.
        Возвращает True при успешном edit/send, иначе False."""
        if state.get("status") not in ("running", "completed") \
                or not state.get("bracket"):
            return False
        async with self._board_lock(state.get("guild_id") or 0):
            fresh = await cashcup_store.active(state["guild_id"]) \
                or await cashcup_store.latest(state["guild_id"])
            if not fresh:
                return False
            return await self._sync_board_locked(
                fresh, channel, page=page, delta=delta)

    async def _sync_board_locked(self, state: dict, channel, *,
                                 page: int | None = None,
                                 delta: int | None = None) -> bool:
        """Вызывается только под _board_lock. Страница вычисляется здесь:
        delta применяется к последней синхронизированной странице."""
        if state.get("status") not in ("running", "completed") \
                or not state.get("bracket"):
            return False
        try:
            active = _board_matches(state["bracket"])
            pages = max(1, (len(active) + BoardSelectView.PER_PAGE - 1)
                        // BoardSelectView.PER_PAGE)
            base = self._board_pages.get(state["guild_id"], 0)
            if delta is not None:
                page = base + delta
            elif page is None:
                page = base
            page = max(0, min(page, pages - 1))
            view = BoardSelectView(active, page=page, state=state)
            board_id = state.get("board_message_id")
            if board_id:
                try:
                    msg = await channel.fetch_message(board_id)
                    await msg.edit(embed=_board_embed(state, page=page), view=view,
                                   allowed_mentions=discord.AllowedMentions.none())
                    self._board_pages[state["guild_id"]] = page
                    return True
                except discord.NotFound:
                    pass  # сообщение удалено вручную — создаём замену
                except discord.HTTPException as exc:
                    # 429/403/5xx — не создаём дубликат: ретрай позже через _refresh.
                    logger.warning("CASH CUP board edit failed: %s", exc)
                    return False
            msg = await channel.send(embed=_board_embed(state, page=page), view=view,
                                     allowed_mentions=discord.AllowedMentions.none())
            self._board_pages[state["guild_id"]] = page
            def change(st):
                st["board_message_id"] = msg.id
            # tournament_id + include_finished: completed-турнир тоже
            # получает новый board_message_id адресной транзакцией.
            try:
                await cashcup_store.mutate(
                    state["guild_id"], 0, "board_sync", change,
                    include_finished=True,
                    tournament_id=state.get("tournament_id"))
            except Exception:
                # Discord-сообщение создано, но id не сохранён в PG:
                # удаляем сироту, иначе следующий sync создаст дубль.
                try:
                    await msg.delete()
                except Exception:
                    logger.warning("CASH CUP orphan board %s not deleted",
                                   getattr(msg, "id", "?"))
                raise
            return True
        except Exception:
            logger.exception("CASH CUP board sync failed")
            return False

    # ---------- BR-команда → CASH CUP, self-service, ADMIN-меню ----------

    async def register_br_team(self, interaction: discord.Interaction) -> None:
        """Капитан регистрирует свою BR-команду (/team) в CASH CUP."""
        await _defer(interaction)
        try:
            if not interaction.guild or not interaction.guild_id:
                raise CashCupError("Только на сервере r!z3")
            br = team_store.get_by_captain(interaction.user.id)
            if not br or br.guild_id != interaction.guild_id:
                raise CashCupError(
                    "Сначала создайте команду через /team и соберите 4 игроков")
            if len(br.members) < 4:
                raise CashCupError(
                    f"В команде {len(br.members)}/4 — для CASH CUP нужно 4 основных")
            lineup = list(dict.fromkeys(br.members))[:4]

            def change(state):
                _assert_registration(state)
                name = (br.team_name or "CASH CUP")[:40]
                if any(t["name"].casefold() == name.casefold()
                       for t in state["teams"]):
                    raise CashCupError("Команда с таким названием уже в Cash Cup")
                for uid in lineup:
                    _unique_player(state, uid, "")
                now = datetime.now(timezone.utc).isoformat()
                state["teams"].append({
                    "id": uuid.uuid4().hex[:12], "name": name,
                    "captain": br.captain_id,
                    "members": [{"discord_id": u, "device": "", "ff_uid": "",
                                 "nick": "", "region": "", "verified": False,
                                 "joined_at": now, "needs_ff": True}
                                for u in lineup],
                    "pending": [], "lineup": lineup,
                    "sub_invites": [], "br_team_id": br.team_id})

            await self._post(interaction, "register_br", change,
                             {"br_team_id": br.team_id})
            await self._reply(
                interaction,
                f"**{safe_text(br.team_name)}** зарегистрирована в CASH CUP.\n"
                "Каждый игрок подтверждает свой Free Fire UID кнопкой "
                "**«Мои данные»** в карточке турнира. Затем ADMIN проверит "
                "владение аккаунтами и допустит команду.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def confirm_ff(self, interaction: discord.Interaction,
                         team_id: str | None, uid: str, region: str,
                         device: str, *, tid: str | None = None) -> None:
        """Игрок сам подтверждает свой FF UID; запасной присоединяется
        только по приглашению капитана (sub_invites)."""
        await _defer(interaction)
        try:
            if device not in ("pc", "mobile"):
                raise CashCupError("Устройство: mobile или pc")
            joined_at = await self._guard(interaction, device)
            ff = await _ff_profile(uid, region)
            state = (await cashcup_store.active(interaction.guild_id)
                     or await cashcup_store.latest(interaction.guild_id))
            if not state:
                raise CashCupError("CASH CUP не найден")

            def change(state):
                _assert_registration(state)
                t = (next((x for x in state["teams"] if x["id"] == team_id), None)
                     if team_id else _find_cc_team(state, interaction.user.id))
                if not t:
                    raise CashCupError("Команда не найдена — нужен team_id или "
                                       "членство в команде")
                uid_ = interaction.user.id
                player = next((p for p in t["members"] + t.get("pending", [])
                               if p["discord_id"] == uid_), None)
                if player is None:
                    if uid_ not in t.get("sub_invites", []):
                        raise CashCupError("Нет приглашения в запасные этой команды")
                    if len(t["members"]) >= 5:
                        raise CashCupError("Состав заполнен (максимум 5 игроков)")
                    player = {"discord_id": uid_, "device": "", "ff_uid": "",
                              "nick": "", "region": "", "verified": False,
                              "joined_at": joined_at}
                    t["members"].append(player)
                for ot in state["teams"]:
                    for op in ot["members"] + ot.get("pending", []):
                        if op is player:
                            continue
                        if op["discord_id"] == uid_:
                            raise CashCupError("Discord-аккаунт уже в другой команде")
                        if ff["ff_uid"] and op.get("ff_uid") == ff["ff_uid"]:
                            raise CashCupError("Этот Free Fire UID уже используется")
                t["sub_invites"] = [x for x in t.get("sub_invites", [])
                                    if x != uid_]
                # ADMIN-подтверждение сбрасывается только при реальном
                # изменении UID/региона/устройства — повторная отправка
                # тех же данных не снимает verified.
                identity_changed = (
                    str(player.get("ff_uid") or "") != ff["ff_uid"]
                    or player.get("region") != region
                    or player.get("device") != device)
                player.update({"device": device, "region": region,
                               "verified": (False if identity_changed
                                            else bool(player.get("verified"))),
                               "ff_ok": True,
                               "joined_at": joined_at, **ff})
                if sum(1 for p in t["members"]
                       if p.get("device") == "pc") > 2:
                    raise CashCupError("Максимум 2 ПК/эмулятора на весь состав")

            new_state, _ = await self._post(
                interaction, "confirm_ff", change,
                {"team_id": team_id}, tid=tid)
            # Реальный статус ADMIN после операции: verified
            # сохраняется при повторе тех же данных, сбрасывается
            # при смене UID/региона/устройства.
            uid_ = interaction.user.id
            verified_now = any(
                p.get("discord_id") == uid_ and p.get("verified")
                for t in (new_state or {}).get("teams", [])
                for p in t.get("members", []) + t.get("pending", []))
            admin_status = ("ADMIN: подтверждён" if verified_now
                            else "ADMIN: ожидает проверки")
            await self._reply(
                interaction,
                f"Free Fire подтверждён: **{safe_text(ff['nick'])}** "
                f"(`{ff['ff_uid']}`, {device}). {admin_status}.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))
        except FreeFireError as exc:
            await self._reply(interaction, f"Free Fire API: {exc}")

    async def invite_sub(self, interaction: discord.Interaction,
                         raw_id: str, *, tid: str | None = None) -> None:
        """Капитан приглашает запасного (Discord ID) в свою команду."""
        await _defer(interaction)
        try:
            uid = int(str(raw_id).strip().strip("<@!>"))
        except (TypeError, ValueError):
            await self._reply(interaction, "Укажите числовой Discord ID")
            return
        try:
            def change(state):
                _assert_registration(state)
                t = _my_team(state, interaction.user.id)
                if t["captain"] != interaction.user.id:
                    raise CashCupError("Только капитан может приглашать запасных")
                if len(t["members"]) >= 5:
                    raise CashCupError("Запасной слот уже занят")
                if any(p["discord_id"] == uid
                       for p in t["members"] + t.get("pending", [])):
                    raise CashCupError("Игрок уже в этой команде")
                member = interaction.guild.get_member(uid)
                if not member:
                    raise CashCupError("Пользователь не состоит на сервере r!z3")
                inv = t.setdefault("sub_invites", [])
                if uid not in inv:
                    inv.append(uid)
            await self._post(interaction, "invite_sub", change, tid=tid)
            member = interaction.guild.get_member(uid)
            if member:
                try:
                    await member.send(
                        "Вас пригласили в **CASH CUP** как запасного. "
                        "Откройте карточку турнира и нажмите «Запасной».")
                except discord.HTTPException:
                    pass
            await self._reply(interaction,
                              f"Приглашение отправлено <@{uid}>.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def show_my_data(self, interaction: discord.Interaction,
                           *, state: dict | None = None) -> None:
        """«FREE FIRE · МОИ ДАННЫЕ» — статус заполнения профиля
        игрока в текущем турнире + кнопка безопасного обновления.
        Read-only: состояние не меняется."""
        if state is None:
            state = await self._current_state(interaction.guild_id)
        if not state:
            await self._reply(interaction, "CASH CUP не найден")
            return
        uid = interaction.user.id
        team = _find_cc_team(state, uid)
        invite_team = None
        if team is None:
            invite_team = next(
                (x for x in state.get("teams", [])
                 if uid in x.get("sub_invites", [])), None)
        target = team or invite_team
        player = None
        if target is not None:
            player = next(
                (p for p in target.get("members", [])
                 + target.get("pending", [])
                 if p.get("discord_id") == uid), None)
        lines = ["**FREE FIRE · МОИ ДАННЫЕ**"]
        if player is None:
            lines.append("Вы не состоите в команде"
                         + (" — есть приглашение в запасные."
                            if invite_team else "."))
        else:
            ff_uid = str(player.get("ff_uid") or "")
            device = player.get("device")
            lines += [
                f"UID: {'`' + ff_uid + '`' if ff_uid else 'не заполнен'}",
                f"Ник: {safe_text(player.get('nick') or '—', limit=40)}",
                f"Регион: {player.get('region') or 'не выбран'}",
                f"Устройство: {device if device in ('pc', 'mobile') else 'не указано'}",
                f"FF API: {'подтверждён' if player.get('ff_ok') else 'не подтверждён'}",
                f"ADMIN: {'подтверждён' if player.get('verified') else 'ожидает'}",
            ]
        view = (_MyDataView(self, target.get("id") if target else None,
                            _tid(state) or None)
                if target is not None else None)
        # view=None нельзя передавать в discord.py напрямую:
        # send_message проверяет view.is_finished() → AttributeError
        # (None не является MISSING). Передаём только реальный View.
        _view_kw = {"view": view} if view is not None else {}
        if interaction.response.is_done():
            await interaction.followup.send(
                "\n".join(lines)[:1900], ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
                **_view_kw)
        else:
            await interaction.response.send_message(
                "\n".join(lines)[:1900], ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
                **_view_kw)

    async def sub_offer_teams(self, interaction: discord.Interaction,
                              *, state: dict | None = None) -> None:
        """Игрок принимает приглашение в запасные и подтверждает данные.
        state может быть передан кнопкой (уже проверена свежесть
        карточки) — тогда повторное чтение PG не выполняется."""
        if state is None:
            state = await self._current_state(interaction.guild_id)
        offers = [t for t in (state or {}).get("teams", [])
                  if interaction.user.id in t.get("sub_invites", [])
                  and len(t["members"]) < 5]
        if not offers:
            await self._reply(
                interaction,
                "Нет приглашений в запасные. Капитан приглашает кнопкой "
                "«Пригласить запасного» в «Моя команда».")
            return
        tid = _tid(state) or None
        if len(offers) == 1:
            await interaction.response.send_modal(
                FFDataModal(offers[0]["id"], tid=tid))
            return

        async def on_pick(i, v):
            await i.response.send_modal(FFDataModal(v, tid=tid))

        await interaction.response.send_message(
            "**Запасной в CASH CUP** — выберите команду:",
            view=PickView("Команда…",
                          [(_label(t), t["id"], "") for t in offers], on_pick),
            ephemeral=True)

    async def _swap_by_ids(self, interaction: discord.Interaction,
                           out_raw: str, in_raw: str,
                           *, tid: str | None = None) -> None:
        try:
            out_id = int(str(out_raw).strip().strip("<@!>"))
            in_id = int(str(in_raw).strip().strip("<@!>"))
        except (TypeError, ValueError):
            await self._reply(interaction, "Нужны числовые Discord ID")
            return
        out_m = interaction.guild.get_member(out_id)
        in_m = interaction.guild.get_member(in_id)
        if not out_m or not in_m:
            await self._reply(interaction, "Оба игрока должны быть на сервере")
            return
        await self.swap(interaction, out_m, in_m, tid=tid)

    # ---------- ADMIN-меню ----------

    async def admin_menu(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Только ADMIN")
            return
        state = (await cashcup_store.active(interaction.guild_id)
                 or await cashcup_store.latest(interaction.guild_id))
        await _send(interaction,
                    "**r!z3 CASH CUP · управление (ADMIN)**",
                    view=AdminMenuView(state))

    async def _admin_action(self, interaction: discord.Interaction,
                            action: str, *, tid: str | None = None) -> None:
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Только ADMIN")
            return
        if action in ("verify", "remove", "dq", "check"):
            return await self._pick_team(interaction, action, tid=tid)
        if action in ("result", "room", "forfeit", "resolve"):
            return await self._pick_match(interaction, action, tid=tid)
        mutating = {"start", "resume", "revert"}
        direct = {"start": self.start, "resume": self.resume,
                  "revert": self.revert, "matches": self.show_active_matches,
                  "bracket": self.show_bracket, "bracket_final": self.show_bracket,
                  "forecast": self.forecast, "history": self.history,
                  "growth": self.growth}
        if action in direct:
            fn = direct[action]
            if action in mutating:
                return await fn(interaction, tid=tid)
            return await fn(interaction)
        if action == "game":
            return await self._open_game_side(interaction, tid=tid)
        modals = {
            "prize": ("Приз за 1 место",
                      [("Сумма", "например 100000", True, 12),
                       ("Валюта", "KZT", False, 10)],
                      lambda i, v: self._mi_prize(i, v, tid)),
            "payout": ("Отметить выплату (ручную, вне бота)",
                       [("Discord ID игрока чемпиона", "123456789012345678", True, 25),
                        ("Сумма (как в призе)", "100000", True, 12),
                        ("Валюта", "KZT", False, 10),
                        ("Подтверждение ручной выплаты",
                         "https://discord.com/channels/…", True, 200)],
                       lambda i, v: self._mi_payout(i, v, tid)),
            "cancel": ("Отмена CASH CUP",
                       [("Причина", "", True, 200)],
                       lambda i, v: self._mi_cancel(i, v, tid)),
            "pause": ("Пауза турнира",
                      [("Причина (необязательно)", "", False, 200)],
                      lambda i, v: self._mi_pause(i, v, tid)),
        }
        if action in modals:
            title, fields, handler = modals[action]
            await interaction.response.send_modal(
                _CbModal(title, fields, handler))
            return
        await self._reply(interaction, "Неизвестное действие")

    async def _pick_team(self, interaction: discord.Interaction,
                         action: str, *, tid: str | None = None) -> None:
        await _defer(interaction)
        state = (await cashcup_store.active(interaction.guild_id)
                 or await cashcup_store.latest(interaction.guild_id))
        teams = (state or {}).get("teams") or []
        if not teams:
            await self._reply(interaction, "Команд нет")
            return
        titles = {"verify": "Проверить UID команд", "remove": "Удалить команду",
                  "dq": "Дисквалифицировать команду",
                  "check": "Проверить состав"}
        opts = [(_label(t), t["id"], f"капитан <@{t['captain']}>")
                for t in teams]
        await _send(interaction,
                    f"**{titles.get(action, 'Команда')}** — выберите:",
                    view=PickView("Команда…", opts,
                                  lambda i, v: self._team_pick(i, action, v, tid)))

    async def _team_pick(self, interaction: discord.Interaction,
                         action: str, team_id: str,
                         tid: str | None = None) -> None:
        # Select-callback — новое Interaction: PG-чтение до ответа
        # ограничено 2с (modal-ветки не могут defer), иначе 10062.
        try:
            state = await asyncio.wait_for(
                self._current_state(interaction.guild_id), timeout=2.0)
        except Exception:
            await self._reply(
                interaction,
                "База данных занята — выберите команду ещё раз "
                "через несколько секунд.")
            return
        t = next((x for x in (state or {}).get("teams", [])
                  if x["id"] == team_id), None)
        if not t:
            await self._reply(interaction, "Команда не найдена")
            return
        name = t["name"]
        if action == "verify":
            await self.verify(interaction, name, tid=tid)
        elif action == "check":
            await self.check_team(interaction, name, tid=tid)
        elif action == "remove":
            await self.remove(interaction, name, tid=tid)
        elif action == "dq":
            await interaction.response.send_modal(_CbModal(
                "Дисквалификация",
                [("Причина", "", True, 200)],
                lambda i, v: self._mi_dq(i, name, v, tid)))

    async def _pick_match(self, interaction: discord.Interaction,
                          action: str, *, tid: str | None = None) -> None:
        await _defer(interaction)
        state = (await cashcup_store.active(interaction.guild_id)
                 or await cashcup_store.latest(interaction.guild_id))
        b = (state or {}).get("bracket") or {}
        matches = _board_matches(b) if b else []
        if not matches:
            await self._reply(interaction, "Активных матчей нет")
            return
        titles = {"result": "Результат матча", "room": "Комната матча",
                  "forfeit": "Техническая победа", "resolve": "Решить спор"}
        opts = [(f"{m['id']} · {m['score'][0]}:{m['score'][1]}",
                 m["id"],
                 f"{_team_name(state, m['a'])} vs {_team_name(state, m['b'])}")
                for m in matches]
        await _send(interaction,
                    f"**{titles.get(action, 'Матч')}** — выберите:",
                    view=PickView("Матч…", opts,
                                  lambda i, v: self._match_pick(i, action, v, tid)))

    async def _match_pick(self, interaction: discord.Interaction,
                          action: str, match_id: str,
                          tid: str | None = None) -> None:
        mid = match_id.upper()
        try:
            state = await asyncio.wait_for(
                self._current_state(interaction.guild_id), timeout=2.0)
        except Exception:
            await self._reply(
                interaction,
                "База данных занята — выберите матч ещё раз "
                "через несколько секунд.")
            return
        b = (state or {}).get("bracket") or {}
        m = next((x for x in _board_matches(b) if x["id"] == mid), None)
        if not m:
            await self._reply(interaction, "Матч не найден")
            return
        if action == "result":
            await interaction.response.send_modal(_CbModal(
                "Результат матча",
                [("Счёт A", "например 3", True, 5),
                 ("Счёт B", "например 1", True, 5),
                 ("Ссылка на доказательство",
                  "https://discord.com/channels/…", True, 200)],
                lambda i, v: self._mi_result(i, mid, v, tid)))
        elif action == "room":
            await interaction.response.send_modal(_CbModal(
                "Комната матча",
                [("ID комнаты", "", True, 60), ("Пароль", "", True, 60)],
                lambda i, v: self._mi_room(i, mid, v, tid)))
        elif action == "forfeit":
            await interaction.response.send_modal(_CbModal(
                "Техническая победа",
                [("Победитель (a/b)", "a", True, 5),
                 ("Причина", "", True, 200)],
                lambda i, v: self._mi_forfeit(i, mid, v, tid)))
        elif action == "resolve":
            if m.get("status") != "disputed":
                await self._reply(interaction, "Матч не в статусе СПОР")
                return
            await interaction.response.send_modal(_CbModal(
                "Решение спора",
                [("Победитель (a/b)", "a", True, 5),
                 ("Ссылка на подтверждение",
                  "https://discord.com/channels/…", True, 200)],
                lambda i, v: self._mi_resolve(i, mid, v, tid)))

    # ---------- обработчики Modal submit ----------

    @staticmethod
    def _side(v: str) -> str | None:
        s = (v or "").strip().lower()
        return s if s in ("a", "b") else None

    async def _mi_result(self, interaction, match_id, v,
                         tid: str | None = None) -> None:
        await _defer(interaction)
        try:
            sa, sb = int(v[0]), int(v[1])
        except (TypeError, ValueError):
            await self._reply(interaction, "Счёт — два числа")
            return
        state = (await cashcup_store.active(interaction.guild_id)
                 or await cashcup_store.latest(interaction.guild_id))
        m = next((x for x in ((state or {}).get("bracket") or {}).get("matches", [])
                  if x["id"] == match_id.upper()), None)
        if m and m.get("bo") == 1 and sorted((sa, sb)) != [0, 1]:
            await self._reply(interaction,
                              "BO1: допустимы только результаты 1:0 или 0:1")
            return
        await self.result(interaction, match_id, sa, sb,
                          (v[2] or "").strip(), tid=tid)

    async def _mi_room(self, interaction, match_id, v,
                       tid: str | None = None) -> None:
        await self.room(interaction, match_id, (v[0] or "").strip(),
                        (v[1] or "").strip(), tid=tid)

    async def _mi_forfeit(self, interaction, match_id, v,
                          tid: str | None = None) -> None:
        side = self._side(v[0])
        if side is None:
            await self._reply(interaction, "Победитель: только `a` или `b`")
            return
        await self.forfeit(interaction, match_id,
                           app_commands.Choice(name=side.upper(), value=side),
                           (v[1] or "").strip(), tid=tid)

    async def _mi_resolve(self, interaction, match_id, v,
                          tid: str | None = None) -> None:
        side = self._side(v[0])
        if side is None:
            await self._reply(interaction, "Победитель: только `a` или `b`")
            return
        await self.resolve(interaction, match_id,
                           app_commands.Choice(name=side.upper(), value=side),
                           (v[1] or "").strip(), tid=tid)

    async def _open_game_side(self, interaction: discord.Interaction,
                              *, tid: str | None = None) -> None:
        """GF: победитель игры выбирается кнопками, не ручным вводом a/b."""
        await _defer(interaction)
        state = (await cashcup_store.active(interaction.guild_id)
                 or await cashcup_store.latest(interaction.guild_id))
        m = next((x for x in (((state or {}).get("bracket") or {})
                              .get("matches", []))
                  if x.get("bracket") == "GF"
                  and x.get("status") in ("ready", "disputed")), None)
        if not m:
            await self._reply(interaction, "Активного Grand Final нет")
            return
        names = {t["id"]: _label(t)
                 for t in (state or {}).get("teams", [])}
        await _send(interaction,
                    f"**GF · счёт серии {m['score'][0]}:{m['score'][1]}** — "
                    "кто выиграл эту игру?",
                    view=GameSideView(self, m["id"], names.get(m["a"], "A"),
                                      names.get(m["b"], "B"), tid=tid))

    async def _mi_game_side(self, interaction, side: str,
                            evidence: str, tid: str | None = None) -> None:
        await self.game(interaction,
                        app_commands.Choice(name=side.upper(), value=side),
                        evidence, tid=tid)

    async def _mi_dq(self, interaction, team_name, v,
                     tid: str | None = None) -> None:
        await self.dq(interaction, team_name, (v[0] or "").strip(), tid=tid)

    async def _mi_prize(self, interaction, v,
                        tid: str | None = None) -> None:
        try:
            amount = int(v[0])
        except (TypeError, ValueError):
            await self._reply(interaction, "Сумма — число")
            return
        await self.prizes(interaction, amount, (v[1] or "KZT").strip() or "KZT",
                          tid=tid)

    async def _mi_payout(self, interaction, v,
                         tid: str | None = None) -> None:
        try:
            uid = int(str(v[0]).strip().strip("<@!>"))
            amount = int(v[1])
        except (TypeError, ValueError):
            await self._reply(interaction, "Нужны Discord ID и сумма")
            return
        member = interaction.guild.get_member(uid)
        if not member:
            await self._reply(interaction, "Игрок не найден на сервере")
            return
        await self.payout(interaction, member, amount, (v[3] or "").strip(),
                          tournament_id=tid,
                          currency=(v[2] or "").strip() or None)

    async def _mi_cancel(self, interaction, v,
                         tid: str | None = None) -> None:
        await self.cancel(interaction, (v[0] or "").strip() or "отменено ADMIN",
                          tid=tid)

    async def _mi_pause(self, interaction, v,
                        tid: str | None = None) -> None:
        await self.pause(interaction, (v[0] or "").strip(), tid=tid)

    async def show_match_card(self, interaction: discord.Interaction, match_id: str) -> None:
        await _defer(interaction)
        state = await cashcup_store.active(interaction.guild_id) or await cashcup_store.latest(interaction.guild_id)
        if not state or not state.get("bracket"):
            await self._reply(interaction, "Сетка ещё не сформирована")
            return
        m = next((x for x in state["bracket"]["matches"]
                  if x["id"] == match_id.upper()), None)
        if not m:
            await self._reply(interaction, "Матч не найден")
            return
        text = _match_card_text(state, m)
        view = MatchCardView(self, state, m, interaction.user)
        if interaction.response.is_done():
            await interaction.followup.send(text, view=view, ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(text, view=view, ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())

    async def _reply(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(message, ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())

    async def _reply_chunked(self, interaction: discord.Interaction,
                             header: str, blocks: list[str]) -> None:
        """Длинный отчёт несколькими ephemeral-сообщениями.

        Лимит Discord — 2000 символов на сообщение, поэтому отчёт
        упаковывается построчно: блоки и строки проблем никогда не
        обрезаются — при переполнении текст продолжается в
        следующем сообщении. Единственный теоретический fallback —
        принудительный срез ОДНОЙ строки длиннее лимита (практически
        недостижимо: имена и диагностики короче)."""
        limit = 1900
        lines = [header]
        for block in blocks:
            lines.append("")
            lines.extend(block.split("\n"))
        msgs: list[str] = []
        cur = ""
        for ln in lines:
            candidate = (cur + "\n" + ln) if cur else ln
            if len(candidate) > limit and cur:
                msgs.append(cur)
                cur = ln
            else:
                cur = candidate
            while len(cur) > limit:
                msgs.append(cur[:limit])
                cur = cur[limit:]
        if cur:
            msgs.append(cur)
        await self._reply(interaction, msgs[0])
        for extra in msgs[1:]:
            await interaction.followup.send(
                extra, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())

    async def _guard(self, interaction: discord.Interaction, device: str = "mobile") -> str:
        if not interaction.guild or not interaction.guild_id:
            raise CashCupError("Команда доступна только на сервере r!z3")
        if device not in ("pc", "mobile"):
            raise CashCupError("Устройство: mobile или pc")
        return await _member_check(self.bot, interaction.guild, interaction.user.id)

    async def _current_state(self, guild_id: int) -> dict | None:
        state = (await cashcup_store.active(guild_id)
                 or await cashcup_store.latest(guild_id))
        self._state_cache[guild_id] = (time.monotonic(), state)
        return state

    async def _state_for_card(self, interaction: discord.Interaction
                              ) -> tuple[str, dict | None]:
        """Состояние для кнопки, открывающей Modal/Select как ПЕРВЫЙ
        ответ Interaction (defer перед send_modal невозможен).

        Порядок: тёплый кэш (мгновенно) → ограниченное ожидание
        PostgreSQL (~2 c). При недоступной БД возвращаем "timeout" —
        view отвечает ephemeral-сообщением до истечения 3-секундного
        окна Discord, поэтому 10062 Unknown interaction исключён.
        Возвращает ("ok", state) | ("stale", state) | ("timeout", None).
        """
        guild_id = interaction.guild_id
        mid = getattr(getattr(interaction, "message", None), "id", None)
        t0 = time.monotonic()

        def _log(outcome: str) -> None:
            # Метрика времени до первого ответа + причина отказа.
            # Без секретов: только guild/message ids и исход.
            logger.info(
                "cashcup card-check outcome=%s ms=%d guild=%s msg=%s",
                outcome, int((time.monotonic() - t0) * 1000),
                guild_id, mid)

        cached = self._state_cache.get(guild_id)
        if cached and time.monotonic() - cached[0] < 10.0:
            state = cached[1]
            expected = (state or {}).get("message_id")
            if expected and mid and int(expected) == int(mid):
                _log("ok_cache")
                return "ok", state
            # Кэш не подтверждает именно эту карточку (возможна
            # новая карточка после /cashcup panel) — читаем PG.
            _log("cache_miss_fresh_read")
        try:
            state = await asyncio.wait_for(
                self._current_state(guild_id), timeout=2.0)
        except Exception as exc:
            _log(f"timeout_pg_{type(exc).__name__}")
            return "timeout", None
        expected = (state or {}).get("message_id")
        if (state and expected and mid
                and int(mid) != int(expected)):
            _log("stale_card")
            return "stale", state
        _log("ok_pg")
        return "ok", state

    async def _post(self, interaction: discord.Interaction, action: str,
                    change, details: dict | None = None, *,
                    tid: str | None = None) -> tuple[dict, Any]:
        # tid — из ephemeral/persistent view: mutate адресует конкретную
        # строку турнира, а не «последний активный». Без tid — прежнее
        # поведение (slash-команды всегда работают с текущим).
        result = await cashcup_store.mutate(
            interaction.guild_id, interaction.user.id, action, change, details,
            tournament_id=tid)
        self._state_cache[interaction.guild_id] = (
            time.monotonic(), result[0])
        await self._refresh(interaction.guild_id, result[0])
        return result

    @cashcup.command(name="create", description="Создать CASH CUP (организатор)")
    async def create(self, interaction: discord.Interaction, prize: str = "Определяет организатор",
                     amount: app_commands.Range[int, 0, 100_000_000] | None = None,
                     currency: str = "KZT") -> None:
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        await _defer(interaction)
        try:
            await cashcup_store.create(interaction.guild_id, interaction.channel_id,
                                       interaction.user.id, prize[:90],
                                       prize1=amount, currency=currency)
            msg = await interaction.channel.send(view=compose(_panel(await cashcup_store.active(interaction.guild_id)), CashCupView(True)),
                                                 allowed_mentions=discord.AllowedMentions.none())
            await cashcup_store.set_message(interaction.guild_id, msg.id)
            await self._reply(interaction, "Регистрация CASH CUP открыта. Создана карточка турнира.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    @cashcup.command(name="panel", description="Восстановить карточку CASH CUP (ADMIN)")
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

    def _alert_lock(self, guild_id: int) -> asyncio.Lock:
        return self._alert_locks.setdefault(guild_id, asyncio.Lock())

    @app_commands.command(
        name="alert",
        description="Уведомить капитанов о неподтверждённых игроках Cash Cup")
    @app_commands.guild_only()
    @app_commands.describe(
        preview="Показать отчёт только ADMIN, без публикации и пингов")
    async def alert(self, interaction: discord.Interaction,
                    preview: bool = False) -> None:
        """Публичное уведомление капитанов о незаполненных данных.

        Только CASH CUP ADMIN. Публикует отчёт в канал карточки
        регистрации; состояние турнира не изменяется. Cooldown
        15 минут, защита от параллельных запусков per-guild lock.
        """
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        await _defer(interaction)
        guild_id = interaction.guild_id
        try:
            async with self._alert_lock(guild_id):
                state = await cashcup_store.active(guild_id)
                if not state or state.get("status") != "registration":
                    await self._reply(
                        interaction,
                        "Нет активного CASH CUP на регистрации — "
                        "уведомление не публикуется.")
                    return
                lines, mention_ids, report = _alert_payload(
                    state, guild_id)
                chunks = _pack_lines(lines)
                if preview:
                    await self._reply(interaction, chunks[0])
                    for extra in chunks[1:]:
                        await interaction.followup.send(
                            extra, ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none())
                    return
                if report["total"] == 0:
                    await self._reply(
                        interaction,
                        "Нет зарегистрированных команд — "
                        "уведомлять некого.")
                    return
                if report["ready"] == report["total"]:
                    await self._reply(
                        interaction,
                        f"Все команды готовы ({report['ready']}/"
                        f"{report['total']}) — уведомление не требуется.")
                    return
                last = self._alert_last.get(guild_id)
                elapsed = time.monotonic() - last if last else None
                if elapsed is not None and elapsed < _ALERT_COOLDOWN:
                    wait_min = int((_ALERT_COOLDOWN - elapsed + 59) // 60)
                    await self._reply(
                        interaction,
                        f"Уведомление уже отправлялось — повторите "
                        f"через {wait_min} мин.")
                    return
                # Повторная проверка статуса непосредственно перед
                # публикацией: состав/статус могли измениться.
                fresh = await cashcup_store.active(guild_id)
                if not fresh \
                        or fresh.get("status") != "registration" \
                        or fresh.get("tournament_id") \
                        != state.get("tournament_id"):
                    await self._reply(
                        interaction,
                        "Состояние турнира изменилось — "
                        "уведомление отменено.")
                    return
                # Актуальность: отчёт и упоминания пересобираются
                # из СВЕЖЕГО состояния — за время проверок игроки
                # могли заполнить данные или ADMIN подтвердить состав.
                lines, mention_ids, report = _alert_payload(
                    fresh, guild_id)
                chunks = _pack_lines(lines)
                if report["total"] == 0:
                    await self._reply(
                        interaction, "Нет команд — отменено.")
                    return
                if report["ready"] == report["total"]:
                    await self._reply(
                        interaction,
                        f"Все команды готовы ({report['ready']}/"
                        f"{report['total']}) — публикация отменена, "
                        "cooldown не израсходован.")
                    return
                channel = interaction.guild.get_channel(
                    fresh.get("channel_id"))
                if channel is None:
                    try:
                        channel = await interaction.guild.fetch_channel(
                            fresh["channel_id"])
                    except Exception:
                        channel = None
                if channel is None:
                    await self._reply(
                        interaction,
                        "Канал карточки CASH CUP недоступен — "
                        "уведомление не опубликовано.")
                    return
                # Dedup частичной публикации: храним подпись отчёта
                # и хэши уже отправленных чанков — повторный вызов
                # не дублирует доставленные сообщения.
                import hashlib
                sig = hashlib.sha1(
                    "\n".join(lines).encode()).hexdigest()
                sent_map = getattr(self, "_alert_sent", None)
                if sent_map is None:
                    sent_map = self._alert_sent = {}
                dedup = sent_map.get(guild_id)
                # Дедуп действует только для НЕЗАВЕРШЁННОЙ
                # публикации того же отчёта: после полного
                # уведомления или при изменении отчёта — свежая
                # рассылка без пропусков.
                if dedup and (dedup[0] != sig or dedup[2]):
                    dedup = None
                sent_hashes = set(dedup[1]) if dedup else set()
                sent = 0
                failed_at = None
                for idx, chunk in enumerate(chunks):
                    chash = hashlib.sha1(chunk.encode()).hexdigest()
                    if chash in sent_hashes:
                        sent += 1
                        continue
                    # allowed_mentions — ТОЛЬКО реально упомянутые
                    # в этом сообщении пользователи, ≤100, без
                    # roles/@everyone/@here.
                    ids = sorted({int(u) for u in
                                  re.findall(r"<@(\d+)>", chunk)})[:100]
                    mentions = discord.AllowedMentions(
                        users=[discord.Object(id=u) for u in ids])
                    try:
                        await channel.send(
                            chunk, allowed_mentions=mentions)
                        sent_hashes.add(chash)
                        sent += 1
                    except discord.HTTPException as exc:
                        failed_at = idx
                        logger.warning(
                            "cashcup alert send failed guild=%s "
                            "chunk=%s/%s: %s",
                            guild_id, idx + 1, len(chunks), exc)
                        break
                sent_map[guild_id] = (
                    sig, sent_hashes, sent >= len(chunks))
                # Cooldown — только если хоть одно сообщение ушло;
                # полная ошибка не блокирует повторную попытку.
                if sent:
                    self._alert_last[guild_id] = time.monotonic()
                logger.info(
                    "cashcup alert guild=%s admin=%s tournament=%s "
                    "teams=%s ready=%s mentions=%s messages=%s",
                    guild_id, interaction.user.id,
                    state.get("tournament_id"), report["total"],
                    report["ready"], len(mention_ids), sent)
                try:
                    from services import tournament_audit_service as audit_svc
                    await audit_svc.record_interaction(
                        interaction,
                        channel_id=state.get("channel_id"),
                        tournament_id=str(state.get("tournament_id") or ""),
                        action="cashcup_alert",
                        source="cashcup",
                        after={
                            "teams": report["total"],
                            "ready": report["ready"],
                            "mentions": len(mention_ids),
                            "messages": sent,
                        },
                    )
                except Exception as exc:
                    logger.warning("cashcup alert audit failed: %s", exc)
                if sent == 0:
                    await self._reply(
                        interaction,
                        "Не удалось опубликовать уведомление — "
                        "проверьте права бота в канале карточки. "
                        "Cooldown не установлен, можно повторить.")
                    return
                total_chunks = len(chunks)
                if failed_at is not None or sent < total_chunks:
                    await self._reply(
                        interaction,
                        f"Уведомление опубликовано ЧАСТИЧНО: "
                        f"сообщений {sent}/{total_chunks}, "
                        f"команд {report['total']}, "
                        f"готово {report['ready']}. "
                        f"Повторный вызов не продублирует "
                        f"отправленные части (в пределах перезапуска "
                        f"и неизменного отчёта).")
                    return
                await self._reply(
                    interaction,
                    f"Уведомление опубликовано: команд "
                    f"{report['total']}, готово {report['ready']}, "
                    f"упоминаний {len(mention_ids)}, "
                    f"сообщений {sent}.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))
        except Exception:
            logger.exception("cashcup alert failed guild=%s", guild_id)
            await self._reply(
                interaction,
                "Ошибка при формировании уведомления — "
                "повторите позже.")

    async def leave(self, interaction: discord.Interaction, *,
                    tid: str | None = None):
        await _defer(interaction)
        try:
            def change(state):
                _assert_registration(state)
                for t in state["teams"]:
                    if t["captain"] == interaction.user.id:
                        raise CashCupError("Капитан снимает команду кнопкой «Снять команду»")
                    for key in ("pending", "members"):
                        p = next((p for p in t[key] if p["discord_id"] == interaction.user.id), None)
                        if p:
                            t[key].remove(p)
                            if interaction.user.id in t["lineup"]:
                                t["lineup"].remove(interaction.user.id)
                            return t["id"]
                raise CashCupError("Вы не зарегистрированы в CASH CUP")
            await self._post(interaction, "leave_team", change, tid=tid)
            await self._reply(interaction, "Вы покинули заявку/команду")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def verify(self, interaction: discord.Interaction, team_name: str,
                     *, tid: str | None = None) -> None:
        await _defer(interaction)
        if not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Доступно только организатору")
            return
        try:
            def change(state):
                _assert_registration(state)
                t = _team(state, team_name)
                # Полная диагностика ДО подтверждения: показываем ВСЕ
                # проблемы, не только первую. include_admin=False —
                # отсутствие ADMIN-подтверждения и есть предмет verify.
                diag = _team_diag(state, t, include_admin=False)
                if diag["status"] != _STATUS_READY:
                    probs = diag["problems"] or [
                        f"состав {len(t['members'])}/5 — минимум 4 игрока"]
                    raise CashCupError(
                        f"**{_label(t)}** — проверка не пройдена:\n"
                        + "\n".join(f"- {p}" for p in probs[:20])[:1800])
                for player in t["members"]:
                    player["verified"] = True
                return len(t["members"])
            _, n = await self._post(interaction, "verify_roster", change,
                                     {"team": team_name, "manual": True}, tid=tid)
            await self._reply(interaction, f"Подтверждено игроков: {n}. Используйте это только после проверки принадлежности аккаунтов.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def check_team(self, interaction: discord.Interaction,
                         team_name: str, *, tid: str | None = None) -> None:
        """ADMIN read-only полная диагностика состава: ВСЕ проблемы
        за один запрос, состояние турнира не меняется."""
        await _defer(interaction)
        if not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Доступно только организатору")
            return
        try:
            state = (await cashcup_store.active(interaction.guild_id)
                     or await cashcup_store.latest(interaction.guild_id))
            if not state:
                raise CashCupError("CASH CUP не найден")
            t = _team(state, team_name)
            diag = _team_diag(state, t)
            await self._reply(interaction, _diag_reply_text(t, diag))
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def show_roster(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
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
        subs = len(t["members"]) - len(t["lineup"])
        rows.append(f"\n**Итого:** {len(t['members'])}/5 · основа: {len(t['lineup'])} · запас: {subs}")
        text = "\n".join(rows)[:1900]
        view = RosterView(is_captain=(t["captain"] == interaction.user.id),
                          tid=_tid(state))
        if interaction.response.is_done():
            await interaction.followup.send(text, view=view, ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(text, view=view, ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())

    async def show_teams(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
        state = await cashcup_store.active(interaction.guild_id) or await cashcup_store.latest(interaction.guild_id)
        if not state or not state.get("teams"):
            await self._reply(interaction, "Команд пока нет")
            return
        text, page, pages = _teams_render(state, 0)
        if interaction.response.is_done():
            await interaction.followup.send(
                text, view=TeamsView(self, page, pages), ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(
                text, view=TeamsView(self, page, pages), ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())

    async def _teams_goto(self, interaction: discord.Interaction,
                          page: int) -> None:
        """Пагинация списка команд: правит то же ephemeral-сообщение.
        PG-чтение ограничено 2с — component Interaction тоже имеет
        3-секундное окно Discord."""
        try:
            state = await asyncio.wait_for(
                self._current_state(interaction.guild_id), timeout=2.0)
        except Exception:
            await interaction.response.send_message(
                "База данных занята — нажмите «Команды» ещё раз "
                "через несколько секунд.", ephemeral=True)
            return
        if not state or not state.get("teams"):
            await interaction.response.send_message(
                "Команд пока нет.", ephemeral=True)
            return
        text, page, pages = _teams_render(state, page)
        await interaction.response.edit_message(
            content=text, view=TeamsView(self, page, pages))

    async def show_active_matches(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
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
        await _defer(interaction)
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

    async def withdraw(self, interaction: discord.Interaction, *,
                       tid: str | None = None):
        await _defer(interaction)
        try:
            def change(state):
                _assert_registration(state)
                team = _my_team(state, interaction.user.id)
                if team["captain"] != interaction.user.id:
                    raise CashCupError("Снять команду может только капитан")
                state["teams"].remove(team)
            await self._post(interaction, "withdraw_team", change, tid=tid)
            await self._reply(interaction, "Команда снята с регистрации")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def remove(self, interaction: discord.Interaction, name: str,
                     *, tid: str | None = None):
        await _defer(interaction)
        if not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        try:
            def change(state):
                _assert_registration(state)
                state["teams"].remove(_team(state, name))
            await self._post(interaction, "remove_team", change,
                             {"name": name}, tid=tid)
            await self._reply(interaction, "Команда удалена")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def start(self, interaction: discord.Interaction, *,
                    tid: str | None = None):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        await _defer(interaction)
        try:
            snapshot = await cashcup_store.active(interaction.guild_id)
            if not snapshot or snapshot["status"] != "registration":
                raise CashCupError("Регистрация закрыта")
            _check_prize_state(snapshot)
            # Сводка ВСЕХ проблем команд до запуска — read-only,
            # сетка не создаётся, пока проверки не пройдены.
            # Отчёт разбивается на сообщения по ~1900 символов:
            # при 13+ проблемных командах ни одна не обрезается —
            # все проблемы доступны в отдельных ephemeral-ответах.
            bad = []
            for t in snapshot["teams"]:
                d = _team_diag(snapshot, t)
                if d["status"] != _STATUS_READY:
                    bad.append((t, d))
            if bad:
                blocks = []
                for t, d in bad:
                    # ВСЕ проблемы без усечения: построчная упаковка
                    # в _reply_chunked гарантирует доступность каждой.
                    blocks.append(
                        f"**{_label(t)}** — {d['status']}\n"
                        + "\n".join(f"- {p}" for p in d["problems"]))
                await self._reply_chunked(
                    interaction,
                    "**CASH CUP · старт невозможен — проблемы команд:**",
                    blocks)
                return
            await _validate_teams(self.bot, interaction.guild, snapshot)
            signature = json.dumps(snapshot["teams"], sort_keys=True)
            prize_sig = json.dumps(snapshot.get("prizes") or {}, sort_keys=True)
            def change(state):
                _assert_registration(state)
                if json.dumps(state["teams"], sort_keys=True) != signature:
                    raise CashCupError("Составы изменились при проверке. Повторите /cashcup start")
                # Приз перепроверяется внутри транзакции: конкурентное
                # изменение prizes отклоняет старт без частичной сетки.
                if json.dumps(state.get("prizes") or {}, sort_keys=True) != prize_sig:
                    raise CashCupError("Приз изменился при проверке. Повторите /cashcup start")
                _check_prize_state(state)
                state["bracket"] = create_bracket([t["id"] for t in state["teams"]])
                state["status"] = "running"
                return len(state["teams"])
            _, count = await self._post(interaction, "start", change,
                                        {"teams": len(snapshot["teams"])}, tid=tid)
            await self._reply(interaction, f"CASH CUP запущен! Команд: {count}. Сетка Double Elimination сформирована.")
        except (CashCupError, BracketError) as exc:
            await self._reply(interaction, str(exc))

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

    async def checkin(self, interaction: discord.Interaction, match_id: str,
                      *, tid: str | None = None):
        if not interaction.guild:
            await self._reply(interaction, "Нужен сервер r!z3")
            return
        await _defer(interaction)
        try:
            snapshot = await cashcup_store.active(interaction.guild_id)
            if not snapshot or snapshot["status"] != "running":
                raise CashCupError("Нет активного турнира")
            if tid and _tid(snapshot) != tid:
                raise CashCupError("Состояние турнира изменилось — откройте карточку заново")
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
                                        {"match": match_id, "captain": interaction.user.id},
                                        tid=tid)
            await self._reply(interaction, f"Готовность подтверждена ({count}/2 капитана).")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def result(self, interaction: discord.Interaction, match_id: str,
                     score_a: int, score_b: int, evidence: str,
                     *, tid: str | None = None):
        await _defer(interaction)
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
                    raise CashCupError("Grand Final уже начат: используйте игру GF")
                if previous:
                    if len(previous.get("checkins", [])) != 2:
                        raise CashCupError(
                            "Сначала оба капитана подтверждают готовность (check-in)")
                    if not previous.get("room_id"):
                        raise CashCupError(
                            "Сначала ADMIN выдаёт комнату матча")
                _push_snapshot(state)
                m = record_result(state["bracket"], match_id.upper(), score_a,
                                  score_b, evidence=evidence)
                if state["bracket"]["champion"]:
                    state["status"] = "completed"
                return m["winner"]
            state, winner_id = await self._post(interaction, "result", change,
                                                {"match": match_id, "score": [score_a, score_b],
                                                 "evidence": evidence}, tid=tid)
            await self._reply(interaction, f"Результат сохранён. Победитель: **{_team_name(state,winner_id)}**." +
                              (" CASH CUP завершён!" if state["status"] == "completed" else ""))
        except (CashCupError, BracketError) as exc:
            await self._reply(interaction, str(exc))

    async def game(self, interaction: discord.Interaction,
                   winner: app_commands.Choice[str], evidence: str,
                   *, tid: str | None = None):
        await _defer(interaction)
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
                                              {"winner_side": winner.value, "evidence": evidence},
                                              tid=tid)
            await self._reply(interaction, f"Grand Final: **{scores[0]}:{scores[1]}**. " +
                              ("Победитель турнира определён!" if state["status"] == "completed"
                               else "Серия продолжается до двух побед."))
        except (CashCupError, BracketError) as exc:
            await self._reply(interaction, str(exc))

    async def room(self, interaction: discord.Interaction, match_id: str,
                   room_id: str, password: str, *, tid: str | None = None):
        await _defer(interaction)
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
                                           {"match": match_id}, tid=tid)
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
                                           {"match": match_id, "delivered": delivered},
                                           tournament_id=tid)
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

    async def roominfo(self, interaction: discord.Interaction, match_id: str,
                       *, tid: str | None = None):
        await _defer(interaction)
        state = await cashcup_store.active(interaction.guild_id)
        if tid and state and _tid(state) != tid:
            await self._reply(interaction,
                              "Состояние турнира изменилось — откройте карточку заново")
            return
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

    async def swap(self, interaction: discord.Interaction, out_player: discord.Member,
                   in_player: discord.Member, *, tid: str | None = None):
        await _defer(interaction)
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
                             {"out": out_player.id,"in": in_player.id}, tid=tid)
            await self._reply(interaction, "Замена зарегистрирована")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def growth(self, interaction: discord.Interaction):
        await _defer(interaction)
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

    async def payout(self, interaction: discord.Interaction, recipient: discord.Member,
                     amount: int, proof: str, tournament_id: str | None = None,
                     place: int | None = None, currency: str | None = None):
        """Отметка РУЧНОЙ выплаты приза за 1 место. Бот не переводит деньги:
        записывает только факт, инициатора, сумму, валюту и время в аудит."""
        await _defer(interaction)
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Доступно только организатору")
            return
        if amount <= 0 or not proof.startswith(("https://discord.com/channels/", "https://cdn.discordapp.com/")):
            await self._reply(interaction, "Нужна положительная сумма и ссылка на подтверждение")
            return
        try:
            def change(state):
                if state["status"] != "completed":
                    raise CashCupError("Выплата возможна только после завершения турнира")
                b = state.get("bracket") or {}
                champ = next((t for t in state["teams"]
                              if t["id"] == b.get("champion")), None)
                if not champ:
                    raise CashCupError("Чемпион не определён")
                if not any(p["discord_id"] == recipient.id for p in champ["members"]):
                    raise CashCupError("Выплату может получить только игрок команды-чемпиона")
                prizes = state.get("prizes") or {}
                expected = (prizes.get("places") or {}).get("1") or 0
                prize_cur = prizes.get("currency") or "KZT"
                if expected <= 0:
                    raise CashCupError(
                        "Приз за 1 место не установлен — выплата невозможна")
                if currency and currency != prize_cur:
                    raise CashCupError(f"Валюта выплаты должна быть {prize_cur}")
                if amount != expected:
                    raise CashCupError(
                        f"Сумма должна равняться призу за 1 место ({expected} {prize_cur})")
                if state["payouts"]:
                    raise CashCupError("Выплата уже отмечена — повторная отметка запрещена")
                state["payouts"].append({"recipient": recipient.id, "amount": amount,
                                         "currency": prize_cur, "place": 1,
                                         "champion_team": champ["id"],
                                         "proof": proof, "actor": interaction.user.id,
                                         "manual": True,
                                         "at": datetime.now(timezone.utc).isoformat()})
            state, _ = await cashcup_store.mutate(interaction.guild_id, interaction.user.id, "payout_recorded",
                                       change, {"recipient": recipient.id, "amount": amount,
                                                "currency": currency, "proof": proof},
                                       include_finished=True, tournament_id=tournament_id)
            await self._refresh(interaction.guild_id, state)
            prize_cur = (((state.get("prizes") or {}).get("currency")) or "KZT")
            await self._reply(interaction, f"Отметка о выплате {amount} {prize_cur} записана в журнал. Денежный перевод бот НЕ выполнял.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def forecast(self, interaction: discord.Interaction, rooms: app_commands.Range[int, 1, 64] = 8,
                       minutes_per_game: app_commands.Range[int, 5, 90] = 20):
        await _defer(interaction)
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
            interaction,
            f"Команд: {n}; шагов сетки: {len(steps)}; матчей: {total_games}; комнат: {rooms}.\n"
            f"Оценка занятости: **{total//60} ч {total%60} мин** "
            f"(без check-in, пауз и задержек подтверждений — это НЕ гарантия).\n"
            f"Загрузка по шагам (матчей/комнат): {load}\n"
            f"Пиковая параллельность: {max(steps)} матчей одновременно.{gf_extra}")

    async def cancel(self, interaction: discord.Interaction, reason: str,
                     *, tid: str | None = None):
        await _defer(interaction)
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        try:
            def change(state):
                state["status"] = "cancelled"
            await self._post(interaction, "cancel", change,
                             {"reason": reason[:300]}, tid=tid)
            await self._reply(interaction, "CASH CUP отменён; запись сохранена в аудите")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))


    async def revert(self, interaction: discord.Interaction, *,
                     tid: str | None = None):
        await _defer(interaction)
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
            await self._post(interaction, "revert_result", change, tid=tid)
            await self._reply(interaction, "Последнее изменение результата отменено. Сетка восстановлена из снимка.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def forfeit(self, interaction: discord.Interaction, match_id: str,
                      winner: app_commands.Choice[str], reason: str,
                      *, tid: str | None = None):
        await _defer(interaction)
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
                             {"match": match_id, "winner": winner.value, "reason": reason[:200]},
                             tid=tid)
            await self._reply(interaction, "Техническая победа записана.")
        except (CashCupError, BracketError) as exc:
            await self._reply(interaction, str(exc))

    async def dq(self, interaction: discord.Interaction, team_name: str, reason: str,
                 *, tid: str | None = None):
        await _defer(interaction)
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
            state, dq_team = await self._post(interaction, "disqualify", change,
                                          {"team": team_name, "reason": reason[:200]}, tid=tid)
            b = state["bracket"]
            note = " CASH CUP завершён!" if state["status"] == "completed" else ""
            await self._reply(interaction,
                              f"Команда **{_team_name(state, dq_team)}** дисквалифицирована.{note}")
        except (CashCupError, BracketError) as exc:
            await self._reply(interaction, str(exc))

    async def pause(self, interaction: discord.Interaction, reason: str = "",
                    *, tid: str | None = None):
        await _defer(interaction)
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Недостаточно прав")
            return
        try:
            def change(state):
                _assert_running(state)
                state["paused"] = True
                state["pause_reason"] = reason[:200]
            await self._post(interaction, "pause", change,
                             {"reason": reason[:200]}, tid=tid)
            await self._reply(interaction, "CASH CUP приостановлен: результаты, комнаты и замены заблокированы.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def resume(self, interaction: discord.Interaction, *,
                     tid: str | None = None):
        await _defer(interaction)
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
            await self._post(interaction, "resume", change, tid=tid)
            await self._reply(interaction, "CASH CUP возобновлён.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def history(self, interaction: discord.Interaction):
        await _defer(interaction)
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

    async def dispute(self, interaction: discord.Interaction, match_id: str, reason: str,
                      *, tid: str | None = None):
        if not interaction.guild:
            await self._reply(interaction, "Нужен сервер r!z3")
            return
        await _defer(interaction)
        try:
            state = await cashcup_store.active(interaction.guild_id)
            if tid and state and _tid(state) != tid:
                raise CashCupError("Состояние турнира изменилось — откройте карточку заново")
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
                             {"match": m["id"], "reason": reason[:200]}, tid=tid)
            await self._reply(interaction,
                              f"Спор по матчу `{m['id']}` открыт. Продвижение результата заблокировано. "
                              "Участники могут передать доказательства через /cashcup info evidence.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def evidence(self, interaction: discord.Interaction, match_id: str, url: str,
                       *, tid: str | None = None):
        await _defer(interaction)
        if not interaction.guild:
            await self._reply(interaction, "Нужен сервер r!z3")
            return
        if not url.startswith(("https://discord.com/channels/", "https://cdn.discordapp.com/",
                               "https://media.discordapp.net/")):
            await self._reply(interaction, "Нужна ссылка на доказательство в Discord")
            return
        try:
            state = await cashcup_store.active(interaction.guild_id)
            if tid and state and _tid(state) != tid:
                raise CashCupError("Состояние турнира изменилось — откройте карточку заново")
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
                             {"match": m["id"]}, tid=tid)
            await self._reply(interaction, "Доказательство добавлено в спор.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))

    async def resolve(self, interaction: discord.Interaction, match_id: str,
                      winner: app_commands.Choice[str], evidence: str,
                      *, tid: str | None = None):
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Только ADMIN")
            return
        await _defer(interaction)
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
                             {"match": match_id, "winner": winner.value}, tid=tid)
            await self._reply(interaction, "Спор разрешён, результат записан.")
        except (CashCupError, BracketError) as exc:
            await self._reply(interaction, str(exc))


    async def prizes(self, interaction: discord.Interaction,
                     amount: int, currency: str = "KZT",
                     *, tid: str | None = None):
        """Приз за 1 место (вызывается из админ-меню)."""
        await _defer(interaction)
        if not interaction.guild or not _is_cashcup_admin(interaction.user):
            await self._reply(interaction, "Только ADMIN")
            return
        try:
            def change(state):
                _assert_registration(state)
                return _apply_prizes(state, amount, currency)
            _, prizes_data = await self._post(interaction, "set_prize", change,
                                              {"amount": amount}, tid=tid)
            await self._reply(interaction,
                              f"Приз за 1 место: {prizes_data['places']['1']} "
                              f"{prizes_data['currency']}. Выплата ручная — "
                              "/cashcup payout после завершения.")
        except CashCupError as exc:
            await self._reply(interaction, str(exc))


def _check_prize_state(state: dict) -> None:
    """Приз за 1 место обязателен для денежного турнира; 2/3 места — 0."""
    pr = state.get("prizes") or {}
    places = pr.get("places") or {}
    if (places.get("1") or 0) <= 0:
        raise CashCupError(
            "Сначала ADMIN устанавливает приз за 1 место («Управление» → «Приз за 1 место»)")
    if places.get("2") or places.get("3"):
        raise CashCupError("Допускается только приз за 1 место")
    if not str(pr.get("currency") or "").strip():
        raise CashCupError("Не указана валюта приза")


def _apply_prizes(state: dict, amount: int, currency: str) -> dict:
    """Приз только за 1 место. Реальные выплаты — вручную вне бота,
    бот лишь фиксирует факт через /cashcup payout."""
    if amount < 0:
        raise CashCupError("Сумма приза не может быть отрицательной")
    state["prizes"] = {"places": {"1": amount, "2": 0, "3": 0},
                       "currency": currency[:10] or "KZT",
                       "total": amount}
    return state["prizes"]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CashCupCog(bot))

# ===================== Кнопочный UX: BR-команды, self-service, ADMIN =====================

class _ActButton(discord.ui.Button):
    def __init__(self, label, style, callback):
        super().__init__(label=label[:80], style=style)
        self._cb = callback

    async def callback(self, interaction):
        await self._cb(interaction)


class _PickSelect(discord.ui.Select):
    def __init__(self, placeholder, options, on_pick):
        super().__init__(
            placeholder=placeholder[:100],
            options=[discord.SelectOption(label=l[:100], value=v[:100],
                                          description=(d or "")[:100])
                     for l, v, d in options[:25]])
        self._on_pick = on_pick

    async def callback(self, interaction):
        await self._on_pick(interaction, self.values[0])


class PickView(discord.ui.View):
    """Эфемерный select с пагинацией: до 25 опций на страницу."""

    PER_PAGE = 25

    def __init__(self, placeholder, options, on_pick, timeout=300, page=0):
        super().__init__(timeout=timeout)
        self._placeholder = placeholder
        self._options = options
        self._on_pick = on_pick
        self._page = max(0, min(page, self._pages() - 1))
        chunk = options[self._page * self.PER_PAGE:
                        (self._page + 1) * self.PER_PAGE]
        pages = self._pages()
        title = placeholder if pages == 1 else f"{placeholder} · {self._page + 1}/{pages}"
        self.add_item(_PickSelect(title, chunk, on_pick))
        if pages > 1:
            self.add_item(_ActButton("Назад", discord.ButtonStyle.secondary,
                                     lambda i: self._nav(i, -1)))
            self.add_item(_ActButton("Вперёд", discord.ButtonStyle.secondary,
                                     lambda i: self._nav(i, 1)))

    def _pages(self) -> int:
        return max(1, (len(self._options) + self.PER_PAGE - 1) // self.PER_PAGE)

    async def _nav(self, interaction, delta):
        await interaction.response.send_message(
            "…", view=PickView(self._placeholder, self._options,
                               self._on_pick, page=self._page + delta),
            ephemeral=True)


class _CbModal(discord.ui.Modal):
    """Modal с произвольным набором полей; callback(interaction, values)."""

    def __init__(self, title, fields, callback):
        super().__init__(title=title[:45], timeout=600)
        self._callback = callback
        for label, placeholder, required, max_len in fields:
            self.add_item(discord.ui.TextInput(
                label=label[:45], placeholder=(placeholder or "")[:100],
                required=required, max_length=max_len))

    async def on_submit(self, interaction):
        await self._callback(interaction, [c.value for c in self.children])


class FFDataModal(discord.ui.Modal):
    """Игрок сам подтверждает свой Free Fire UID/регион/устройство."""

    def __init__(self, team_id: str | None = None, tid: str | None = None):
        super().__init__(title="Free Fire — мои данные", timeout=600)
        self.team_id = team_id
        self.tid = tid
        self.ff_uid = discord.ui.TextInput(label="Free Fire UID",
                                           placeholder="123456789",
                                           min_length=5, max_length=20)
        self.region = discord.ui.TextInput(label="Регион", default="ru",
                                           max_length=12)
        self.device = discord.ui.TextInput(label="Устройство: mobile или pc",
                                           default="mobile", max_length=10)
        self.add_item(self.ff_uid)
        self.add_item(self.region)
        self.add_item(self.device)

    async def on_submit(self, interaction):
        cog = interaction.client.get_cog("CashCupCog")
        await cog.confirm_ff(
            interaction, self.team_id, self.ff_uid.value.strip(),
            self.region.value.strip().lower() or "ru",
            self.device.value.strip().lower(), tid=self.tid)


class InviteSubModal(_CbModal):
    def __init__(self, cog_getter, tid: str | None = None):
        super().__init__(
            "Пригласить запасного",
            [("Discord ID игрока", "123456789012345678", True, 25)],
            lambda i, v: cog_getter(i).invite_sub(i, v[0], tid=tid))


class SwapModal(_CbModal):
    def __init__(self, cog_getter, tid: str | None = None):
        super().__init__(
            "Замена в составе",
            [("Кого убрать (Discord ID)", "123456789012345678", True, 25),
             ("Кого добавить (Discord ID)", "123456789012345678", True, 25)],
            lambda i, v: cog_getter(i)._swap_by_ids(i, v[0], v[1], tid=tid))


class RosterView(discord.ui.View):
    """Эфемерные действия со своим составом. Привилегированные кнопки
    добавляются только капитану; серверные проверки — в методах cog."""

    def __init__(self, is_captain: bool = False, tid: str | None = None):
        super().__init__(timeout=300)
        self.is_captain = is_captain
        self.tid = tid
        self.add_item(_ActButton("Мои данные", discord.ButtonStyle.primary,
                                 self._my_data))
        if is_captain:
            self.add_item(_ActButton("Пригласить запасного",
                                     discord.ButtonStyle.secondary,
                                     self._invite_sub))
            self.add_item(_ActButton("Заменить", discord.ButtonStyle.secondary,
                                     self._swap))
            self.add_item(_ActButton("Снять команду", discord.ButtonStyle.danger,
                                     self._withdraw))
        self.add_item(_ActButton("Выйти", discord.ButtonStyle.secondary,
                                 self._leave))

    async def _my_data(self, interaction):
        await interaction.response.send_modal(FFDataModal(tid=self.tid))

    async def _invite_sub(self, interaction):
        await interaction.response.send_modal(
            InviteSubModal(lambda i: i.client.get_cog("CashCupCog"),
                           tid=self.tid))

    async def _swap(self, interaction):
        await interaction.response.send_modal(
            SwapModal(lambda i: i.client.get_cog("CashCupCog"), tid=self.tid))

    async def _withdraw(self, interaction):
        cog = interaction.client.get_cog("CashCupCog")
        await cog.withdraw(interaction, tid=self.tid)

    async def _leave(self, interaction):
        cog = interaction.client.get_cog("CashCupCog")
        await cog.leave(interaction, tid=self.tid)


class MatchCardView(discord.ui.View):
    """Эфемерная карточка матча: кнопки игрока-капитана и ADMIN разделены."""

    def __init__(self, cog, state, m, user):
        super().__init__(timeout=300)
        mid = m["id"]
        tid = _tid(state)
        teams = {t["id"]: t for t in state.get("teams", [])}
        participant = any(
            t["id"] in (m.get("a"), m.get("b")) and t.get("captain") == user.id
            for t in teams.values())
        is_admin = _is_cashcup_admin(user)

        def modal_btn(label, style, title, fields, handler):
            return _ActButton(label, style, lambda i: i.response.send_modal(
                _CbModal(title, fields, lambda ii, v: handler(ii, v))))

        if participant:
            self.add_item(_ActButton("Подтвердить готовность",
                                     discord.ButtonStyle.success,
                                     lambda i: cog.checkin(i, mid, tid=tid)))
            self.add_item(_ActButton("Комната",
                                     discord.ButtonStyle.secondary,
                                     lambda i: cog.roominfo(i, mid, tid=tid)))
            self.add_item(modal_btn(
                "Открыть спор", discord.ButtonStyle.danger, "Спор по матчу",
                [("Причина", "", True, 200)],
                lambda i, v: cog.dispute(i, mid, (v[0] or "").strip(), tid=tid)))
            self.add_item(modal_btn(
                "Доказательство", discord.ButtonStyle.secondary,
                "Доказательство",
                [("Ссылка", "https://discord.com/channels/…", True, 200)],
                lambda i, v: cog.evidence(i, mid, (v[0] or "").strip(), tid=tid)))
        if is_admin:
            self.add_item(modal_btn(
                "Выдать комнату", discord.ButtonStyle.primary, "Комната матча",
                [("ID комнаты", "", True, 60), ("Пароль", "", True, 60)],
                lambda i, v: cog._mi_room(i, mid, v, tid)))
            self.add_item(modal_btn(
                "Результат", discord.ButtonStyle.success, "Результат матча",
                [("Счёт A", "например 3", True, 5),
                 ("Счёт B", "например 1", True, 5),
                 ("Ссылка на доказательство", "https://discord.com/channels/…", True, 200)],
                lambda i, v: cog._mi_result(i, mid, v, tid)))
            if m.get("bracket") == "GF":
                self.add_item(_ActButton(
                    "Игра GF", discord.ButtonStyle.primary,
                    lambda i: cog._open_game_side(i, tid=tid)))
            self.add_item(modal_btn(
                "Тех. победа", discord.ButtonStyle.danger, "Техническая победа",
                [("Победитель (a/b)", "a", True, 5),
                 ("Причина", "", True, 200)],
                lambda i, v: cog._mi_forfeit(i, mid, v, tid)))
            if m.get("status") == "disputed":
                self.add_item(modal_btn(
                    "Решить спор", discord.ButtonStyle.success, "Решение спора",
                    [("Победитель (a/b)", "a", True, 5),
                     ("Ссылка на подтверждение", "https://discord.com/channels/…", True, 200)],
                    lambda i, v: cog._mi_resolve(i, mid, v, tid)))


class AdminMenuView(discord.ui.View):
    """Эфемерное ADMIN-меню CASH CUP (кнопки вместо slash-команд)."""

    def __init__(self, state):
        super().__init__(timeout=300)
        self.tid = _tid(state)
        status = (state or {}).get("status")
        bracket = (state or {}).get("bracket") or {}
        disputed = any(mm.get("status") == "disputed"
                       for mm in bracket.get("matches", []))
        opts = [("Матчи", "matches", ""), ("Сетка", "bracket", ""),
                ("Прогноз нагрузки", "forecast", ""),
                ("История", "history", "")]
        if status == "registration":
            opts += [("Проверить команду", "verify", ""),
                     ("Проверить состав", "check", ""),
                     ("Удалить команду", "remove", ""),
                     ("Приз за 1 место", "prize", ""),
                     ("Запустить турнир", "start", ""),
                     ("Отменить турнир", "cancel", "")]
        elif status == "running":
            opts += [("Результат матча", "result", ""),
                     ("Выдать комнату", "room", ""),
                     ("Тех. победа", "forfeit", ""),
                     ("Игра GF", "game", ""),
                     ("Дисквалификация", "dq", ""),
                     ("Откат результата", "revert", ""),
                     ("Пауза", "pause", ""),
                     ("Продолжить", "resume", "")]
            if disputed:
                opts.append(("Решить спор", "resolve", ""))
        elif status == "completed":
            opts += [("Отметить выплату", "payout", ""),
                     ("Сетка", "bracket_final", "")]
        seen = set()
        opts = [o for o in opts if not (o[1] in seen or seen.add(o[1]))]

        async def on_pick(interaction, value):
            cog = interaction.client.get_cog("CashCupCog")
            await cog._admin_action(interaction, value, tid=self.tid)

        self.add_item(_PickSelect("Управление CASH CUP…", opts, on_pick))

class GameSideView(discord.ui.View):
    """Выбор победителя одной игры Grand Final — кнопками, не вводом a/b."""

    def __init__(self, cog, match_id: str, name_a: str, name_b: str,
                 tid: str | None = None):
        super().__init__(timeout=300)
        self.tid = tid
        for side, name in (("a", name_a), ("b", name_b)):
            async def pick(interaction, _side=side):
                await interaction.response.send_modal(_CbModal(
                    "Игра GF — доказательство",
                    [("Ссылка", "https://discord.com/channels/…", True, 200)],
                    lambda i, v: cog._mi_game_side(i, _side, (v[0] or "").strip(),
                                                 tid=self.tid)))
            self.add_item(_ActButton(f"Победа: {name}"[:80],
                                     discord.ButtonStyle.success, pick))
