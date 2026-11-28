"""Views for Battle Royale tournament (Solo / Duo / Squad).

Жизненный цикл — одна публичная карточка (message_id) на весь турнир:
регистрация → составы → «Игры идут» → финал. «Завершить игры» убрано:
результаты вносятся по каждой карте независимо и доступны с фазы
PLAYING (legacy COLLECT продолжает работать). У каждой карты —
своя кнопка и свой приватный ID/пароль комнаты (rooms[map_index],
fallback — legacy глобальный room_data). Все компоненты привязаны
к ID турнира и map_index в custom_id.
"""

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import discord
from utils.design import compose, smart_view

from discord.ui import Modal, TextInput, View, Button, Select

from config import (ADMIN_USER_IDS, BOT_OWNER_ID,
                    replace_emojis, get_emoji)
from models.br_tournament import (BRFormat, BRTournamentPhase, MAP_NAMES_RU,
                                  MAP_STATUS_LABELS, br_format_rules)
from storage.br_store import br_store

if TYPE_CHECKING:
    from models.br_tournament import BRTournament

logger = logging.getLogger(__name__)


# ---------- locks & helpers ----------

_BR_LOCKS: dict[str, asyncio.Lock] = {}


def get_br_lock(tournament: "BRTournament") -> asyncio.Lock:
    return get_br_lock_key(tournament.guild_id, tournament.channel_id, tournament.id)


def get_tournament(guild_id: int, channel_id: int) -> "BRTournament | None":
    return br_store.get_any(guild_id, channel_id)


def is_org(user_id: int) -> bool:
    return user_id == BOT_OWNER_ID or user_id in ADMIN_USER_IDS


def player_allowed(interaction: discord.Interaction, t: "BRTournament") -> bool:
    """Участие в тестовом BR — только владельцу и администраторам.

    Обычные игроки не могут регистрироваться/вступать в пары/отрады,
    принимать приглашения, переименовывать составы и видеть данные
    комнаты тестового турнира — тест не затрагивает реальных users.
    """
    return not getattr(t, "is_test", False) or is_org(interaction.user.id)


def _cid(prefix: str, guild_id: int, channel_id: int, tid: str, *extra) -> str:
    """custom_id компонента, привязанный к конкретному турниру."""
    parts = [prefix, str(guild_id), str(channel_id), str(tid)]
    parts += [str(e) for e in extra]
    return ":".join(parts)


def get_bound(guild_id: int, channel_id: int, tid: str) -> "BRTournament | None":
    """Турнир по (guild, channel, id): активный или архивный.

    None — турнир не найден или custom_id принадлежит другому/старому
    турниру (кнопка устарела).
    """
    t = br_store.get(guild_id, channel_id)
    if t is not None and str(t.id) == str(tid):
        return t
    for h in br_store.history():
        if (h.guild_id == guild_id and h.channel_id == channel_id
                and str(h.id) == str(tid)):
            return h
    return None


def get_br_lock_key(guild_id: int, channel_id: int, tid: str) -> asyncio.Lock:
    return _BR_LOCKS.setdefault(f"{guild_id}:{channel_id}:{tid}", asyncio.Lock())


STALE_MSG = "Кнопка устарела — турнир изменился или завершён. Обновите карточку."


async def _respond(interaction: discord.Interaction, content=None, **kwargs) -> None:
    """Ответ на interaction с учётом возможного defer().

    Если callback успел сделать defer(ephemeral=True) (долгая работа до
    ответа — VAC, запись в store, edit карточки), ответ уходит через
    followup; иначе — обычный response.send_message. Все ответы BR
    ephemeral.
    """
    kwargs.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)


BR_NUM_EMOJIS = [
    "num_1", "num_2", "num_3", "num_4", "num_5", "num_6",
    "num_7", "num_8", "num_9", "num_ten", "num_eleven", "num_twelve",
]


def _num(i: int) -> str:
    if i < len(BR_NUM_EMOJIS):
        return get_emoji(BR_NUM_EMOJIS[i])
    return f"{i + 1}."


def clean_user_name(user) -> str:
    from utils.cosmetics import clean_nickname
    return (
        clean_nickname(user.display_name)
        or clean_nickname(user.name)
        or f"Игрок-{user.id % 10000:04d}"
    )


def slot_members(tournament: "BRTournament", slot: dict) -> list[int]:
    return [int(i) for i in (slot.get("member_ids") or [])]


def slot_member_names(tournament: "BRTournament", slot: dict) -> str:
    """Имена участников без упоминаний — для публичных карточек."""
    return " ・ ".join(slot_member_names_list(tournament, slot)) or "*"


_MENTION_RE = re.compile(r"<[@#][!&]?\d+>|<@&\d+>")
_EVERYONE_RE = re.compile(r"@(everyone|here)\b", re.IGNORECASE)


def _public_name(name: str) -> str:
    """Имя для публичной карточки: без пингов/упоминаний и форматирования.

    Используется существующая очистка никнеймов (clean_nickname) +
    срез <@id>/<@!id>/<@&id>/<#id> и @everyone/@here. Discord ID
    не показываем; пустой результат — нейтральное «Игрок».
    """
    from utils.cosmetics import clean_nickname
    raw = str(name or "")
    # legacy-fallback «User_<id>» — не имя, в публичных карточках
    # показываем нейтральное «Игрок».
    if re.fullmatch(r"(?i)user[_\s-]?\d+", raw.strip()):
        return "Игрок"
    # кастомные эмодзи — до clean_nickname (он срезает '>' раньше,
    # чем его собственный паттерн успевает сработать)
    raw = re.sub(r"<a?:[^>\s]+>", "", raw)
    text = _MENTION_RE.sub("", raw)
    text = _EVERYONE_RE.sub("", text)
    text = clean_nickname(text).strip()
    return text or "Игрок"


def slot_member_names_list(tournament: "BRTournament", slot: dict) -> list[str]:
    """Имена участников слота по порядку.

    Дубликаты исключаются только по Discord ID: разные игроки с
    одинаковыми никнеймами отображаются все.
    """
    ids = slot_members(tournament, slot)
    dn = slot.get("display_names") or {}
    out: list[str] = []
    if ids:
        seen: set[int] = set()
        for i in ids:
            if i in seen:
                continue
            seen.add(i)
            out.append(_public_name(dn.get(str(i)) or _player_name(tournament, i)))
    else:
        # legacy-слот без member_ids — имён с ID нет, показываем все
        # непустые записи: без ID нельзя отличить тёзок.
        for p in [slot.get("captain", "")] + [
                n for n in slot.get("players", []) if n]:
            out.append(_public_name(str(p)))
    return out


def _slot_block_lines(slot: dict, index: int, members: list[str]) -> list[str]:
    """Блок команды/слота: «NN. **Название**» + строка участников."""
    pending = " ⏳" if slot.get("pending_invitee_id") else ""
    title = f"{index + 1:02d}. **{_public_name(slot_label(slot, index))}**{pending}"
    if members:
        return [title, f"┗ {' ・ '.join(members)}"]
    return [title]


def _player_lines(players: list[dict]) -> list[str]:
    """Вертикальный нумерованный список игроков (solo/random-пул)."""
    lines = []
    for i, p in enumerate(players):
        name = _public_name(str(p.get("name") or p.get("display_name") or "?"))
        lines.append(f"{i + 1:02d}. {name}")
    return lines


def registration_list_lines(tournament: "BRTournament") -> list[str]:
    """Список зарегистрированных на карточке регистрации.

    Team-режимы — блоки «NN. **Название**» + «┗ игроки» (незаполненные
    пары с ⏳). Solo — нумерованный список слотов. Random — пул
    игроков до жеребьёвки.
    """
    if tournament.mode == "team":
        lines: list[str] = []
        for i, slot in enumerate(tournament.teams):
            members = slot_member_names_list(tournament, slot)
            lines.extend(_slot_block_lines(slot, i, members))
        return lines
    if tournament.is_solo:
        return _player_lines([
            {"name": slot_label(s, i)} for i, s in
            enumerate(tournament.filled_slots())
        ])
    return _player_lines(tournament.players)


def registration_counter(tournament: "BRTournament") -> str:
    """Счётчик участия для шапки карточки регистрации."""
    if tournament.mode == "team":
        unit = "Пар" if tournament.format == BRFormat.DUO else "Команд"
        return f"{unit}: {len(tournament.filled_slots())}/{tournament.max_slots}"
    if tournament.is_solo:
        return (f"Участников: {len(tournament.filled_slots())}"
                f"/{tournament.max_slots}")
    return (f"Игроков в пуле: {len(tournament.players)}"
            f"/{tournament.max_players}")


def _player_name(tournament: "BRTournament", uid: int) -> str:
    for p in tournament.players:
        if p.get("id") == uid:
            return _public_name(str(p.get("name") or ""))
    # В публичных карточках сырой «User_<id>» не показываем.
    return "Игрок"


def slot_label(slot: dict, index: int) -> str:
    return slot.get("name") or f"Слот {index + 1}"


def format_mode_label(tournament: "BRTournament") -> str:
    fmt = br_format_rules(tournament.format)["label"]
    if tournament.mode == "team":
        return f"{fmt} · Команды"
    if tournament.mode == "random":
        return f"{fmt} · Жеребьёвка"
    return f"{fmt} · Индивидуально"


def my_slot_label(tournament: "BRTournament") -> str:
    if tournament.format == BRFormat.DUO:
        return "Моя пара"
    if tournament.format == BRFormat.SQUAD:
        return "Мой сквад"
    return "Мой слот"


def absent_label(tournament: "BRTournament") -> str:
    """Термин неучастия: Solo — «не участвовал», Duo/Squad — «не участвовала»."""
    return "не участвовал" if tournament.is_solo else "не участвовала"


def registration_rules_text(tournament: "BRTournament") -> str:
    r = tournament.rules
    fmt = tournament.format.value
    if fmt == "solo":
        join = "Нажмите «Участвовать» — каждый играет сам за себя."
    elif fmt == "duo" and tournament.mode == "team":
        join = ("Капитан жмёт «Созвать пару», напарник подтверждает заявку "
                "кнопкой «Мои заявки».")
    elif fmt == "duo":
        join = "Оба игрока пары входят по одному через «Участвовать» — жеребьёвка соберёт пары."
    elif tournament.mode == "team":
        join = ("Капитан регистрирует готовый отряд из 4 через /team "
                "и кнопку «Зарегистрировать отряд».")
    else:
        join = "Игроки входят по одному через «Участвовать» — жеребьёвка соберёт отряды."
    return (
        f"Формат: {format_mode_label(tournament)}\n"
        f"Карт: {tournament.map_count}\n"
        f"Слотов: {tournament.min_slots}–{tournament.max_slots} "
        f"({tournament.min_players}–{tournament.max_players} игроков)\n"
        f"Очки: место 1–12 = 12/9/8/7/6/5/4/3/2/1/0/0 + 1 очко за килл. "
        f"Тай-брейк: Booyah → киллы → место на последней карте → слот.\n"
        f"У каждой карты своя комната — ID и пароль по кнопке карты.\n"
        f"{join}"
    )


def _clip(text: str, limit: int = 3400) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…"


def slots_public_lines(tournament: "BRTournament") -> list[str]:
    if tournament.is_solo:
        # Solo: слот = игрок — плоский нумерованный список без дублей.
        return _player_lines([
            {"name": slot_label(s, i)} for i, s in
            enumerate(tournament.filled_slots())
        ])
    lines = []
    for i, slot in enumerate(tournament.filled_slots()):
        members = slot_member_names_list(tournament, slot)
        lines.extend(_slot_block_lines(slot, i, members))
    return lines


def schedule_block(tournament: "BRTournament") -> str:
    return "\n".join(tournament.schedule_lines())


def schedule_status_lines(tournament: "BRTournament") -> list[str]:
    """Расписание с фактическим статусом каждой карты."""
    lines = []
    for i, m in enumerate(tournament.get_map_sequence()):
        ru = MAP_NAMES_RU.get(m, m)
        status = MAP_STATUS_LABELS.get(
            tournament.map_status(i), tournament.map_status(i))
        lines.append(f"{_num(i)} **{ru}** — {status}")
    return lines


def public_embed(tournament: "BRTournament") -> discord.Embed:
    """Публичная карточка BR по текущей фазе (единая на весь турнир)."""
    phase = tournament.phase
    title_fmt = format_mode_label(tournament)

    if phase == BRTournamentPhase.REGISTRATION:
        count = registration_counter(tournament)
        lines = registration_list_lines(tournament)
        body = "\n".join(lines) if lines else " *"
        return discord.Embed(
            title=f" {replace_emojis('winner')} BATTLE ROYALE CUP | {title_fmt} | Регистрация",
            description=(
                f" **Статус:** Регистрация открыта\n"
                f" Карт: {tournament.map_count} ・ {count}\n\n"
                f"{body}"
            ),
            color=discord.Color.from_rgb(69, 69, 69),
        )

    if phase == BRTournamentPhase.DRAFT:
        body = "\n".join(slots_public_lines(tournament)) or " *"
        return discord.Embed(
            title=f" {replace_emojis('winner')} BATTLE ROYALE CUP | {title_fmt} | Составы",
            description=(
                f" **Организатор:** <@{tournament.organizer_id}>\n\n"
                f" **Фаза:** Составы определены\n"
                f" Карт: {tournament.map_count}\n\n"
                f" **Расписание:**\n{schedule_block(tournament)}\n\n"
                f" **Составы:**\n{body}\n\n"
                f"Капитан может один раз назвать свой состав."
            ),
            color=discord.Color.from_rgb(69, 69, 69),
        )

    if phase in (BRTournamentPhase.PLAYING, BRTournamentPhase.COLLECT):
        # «Турнир идёт»: промежуточные результаты публично не показываем —
        # только факт заполнения по каждой карте.
        filled_maps = sum(
            1 for i in range(tournament.map_count)
            if tournament.map_status(i) == "filled")
        sched = "\n".join(schedule_status_lines(tournament))
        roster = "\n".join(slots_public_lines(tournament)) or " *"
        return discord.Embed(
            title=f" {replace_emojis('winner')} BATTLE ROYALE CUP | {title_fmt}",
            description=(
                f" **Статус:** Турнир идёт\n"
                f" Карт с результатами: {filled_maps}/{tournament.map_count}\n\n"
                f" **Расписание:**\n{sched}\n\n"
                f" **Составы:**\n{roster}"
            ),
            color=discord.Color.from_rgb(69, 69, 69),
        )

    if phase in (BRTournamentPhase.RESULTS, BRTournamentPhase.COMPLETE):
        return final_embed(tournament)

    if phase == BRTournamentPhase.CANCELLED:
        reason = f"\nПричина: {tournament.cancel_reason}" if tournament.cancel_reason else ""
        return discord.Embed(
            title=f" {replace_emojis('winner')} BATTLE ROYALE CUP | Отменён",
            description=f"Турнир отменён.{reason}",
            color=discord.Color.from_rgb(120, 60, 60),
        )

    return discord.Embed(
        title=" BATTLE ROYALE CUP",
        description="Турнир завершён.",
        color=discord.Color.from_rgb(69, 69, 69),
    )


def final_embed(tournament: "BRTournament", page: int | None = None) -> discord.Embed:
    """Опубликованный итог: победитель, топ-3, таблица, «По картам»."""
    results = tournament.calculate_final_results()
    if not results:
        return discord.Embed(title="BR Cup — результаты", description="Нет данных.")

    winner = results[0]
    wslot = tournament.teams[winner["slot_index"]]
    wnames = ", ".join(
        _player_name(tournament, i) for i in winner["member_ids"]
    ) or slot_member_names(tournament, wslot)
    lines = [
        f" **Победитель:** {winner['name']}",
        f" {wnames}",
        "",
    ]
    for r in results[1:4]:
        lines.append(f"{r['rank']}. {r['name']} — {r['total_points']} очков")
    lines.append("")
    lines.append(" **Таблица:**")
    for r in results:
        lines.append(
            f"`{r['rank']:>2}.` {r['name']} — {r['total_points']} очк. "
            f"({r['total_kills']}🔥, Booyah {r['booyahs']})"
        )

    if page is None or page < 0:
        desc = "\n".join(lines)
        if len(desc) > 3900:
            desc = desc[:3900] + "\n…"
        return discord.Embed(
            title=f" {replace_emojis('winner')} BATTLE ROYALE CUP | Итоги",
            description=desc + "\n\nИспользуйте кнопки «По картам» для разбивки.",
            color=discord.Color.gold(),
        )

    # страница по картам: page = индекс карты (номер записи, не фаза)
    map_idx = page
    map_ru = MAP_NAMES_RU.get(tournament.get_map_name_for_index(map_idx), "")
    rows = []
    for i, slot in enumerate(tournament.teams):
        ne = tournament.get_team_stats_for_map(slot, map_idx)
        if ne is None:
            rows.append((999, f"— {slot_label(slot, i)} — нет данных"))
        elif ne["dns"]:
            rows.append((500, f"{absent_label(tournament)} "
                              f"{slot_label(slot, i)} (0:{ne['kills']})"))
        else:
            rows.append((ne["place"], f"{ne['place']}. {slot_label(slot, i)} — {ne['kills']} киллов"))
    rows.sort(key=lambda x: x[0])
    body = "\n".join(r[1] for r in rows)
    if len(body) > 3800:
        body = body[:3800] + "\n…"
    return discord.Embed(
        title=f" {replace_emojis('winner')} BATTLE ROYALE CUP | По картам",
        description=(
            f" **Карта {map_idx + 1} из {tournament.map_count} · {map_ru}**\n\n{body}\n\n"
            "Кнопки ◀ ▶ листают карты."
        ),
        color=discord.Color.from_rgb(69, 69, 69),
    )


async def render_public(tournament: "BRTournament"):
    """(embed, view) для публичной карточки."""
    view = BRCardView(tournament.guild_id, tournament.channel_id, tournament)
    page = getattr(tournament, "view_page", -1)
    if tournament.phase in (BRTournamentPhase.RESULTS, BRTournamentPhase.COMPLETE) and page >= 0:
        return final_embed(tournament, page), view
    return public_embed(tournament), view


async def edit_main_card(tournament: "BRTournament", client=None,
                         channel=None) -> bool:
    """Перерисовать единую публичную карточку. True — карточка обновлена."""
    if not tournament.message_id:
        return False
    try:
        if channel is None:
            if client is None:
                return False
            guild = client.get_guild(tournament.guild_id)
            channel = guild.get_channel(tournament.channel_id) if guild else None
        if not channel:
            return False
        message = await channel.fetch_message(tournament.message_id)
        embed, view = await render_public(tournament)
        await message.edit(view=compose(embed, view))
        return True
    except Exception as exc:
        logger.warning("BR edit_main_card failed: %s", exc)
        return False


async def refresh_main_card(interaction: discord.Interaction, t: "BRTournament") -> None:
    """Перерисовать публичную карточку после действия на ней."""
    await edit_main_card(t, channel=interaction.channel)


async def sync_public_card(tournament: "BRTournament", client=None) -> None:
    """Перерисовать публичную карточку турнира (после рестарта и т.п.)."""
    await edit_main_card(tournament, client=client)


async def strip_tournament_roles(guild, tournament: "BRTournament") -> None:
    """Снять BR-роль у участников, если она не нужна другому активному BR."""
    if getattr(tournament, "is_test", False):
        return  # тестовый турнир не трогает реальные роли
    role = guild.get_role(tournament.role_id)
    if not role:
        return
    busy: set[int] = set()
    for other in br_store.get_by_guild(tournament.guild_id):
        if other.id == tournament.id or getattr(other, "is_test", False):
            continue  # тестовые турниры не «удерживают» реальную роль
        for p in other.players:
            busy.add(p.get("id"))
        for s in other.teams:
            busy.update(slot_members(other, s))
    member_ids = {p.get("id") for p in tournament.players}
    for s in tournament.teams:
        member_ids.update(slot_members(tournament, s))
    for uid in member_ids:
        if not uid or uid < 0 or uid in busy:
            continue
        try:
            member = guild.get_member(uid)
            if member and role in member.roles:
                await member.remove_roles(role)
        except Exception as exc:
            logger.warning("BR strip role %s failed: %s", uid, exc)


async def grant_tournament_roles(guild, tournament: "BRTournament") -> None:
    """Выдать BR-роль по правилам формата."""
    if getattr(tournament, "is_test", False):
        return  # тестовый турнир не меняет роли реальных участников
    role = guild.get_role(tournament.role_id)
    if not role:
        return
    targets: set[int] = set()
    if tournament.mode == "team" and tournament.format == BRFormat.SQUAD:
        # Squad team — только капитаны готовых отрядов
        for s in tournament.filled_slots():
            if s.get("team_id"):
                targets.add(s.get("captain_id") or (slot_members(tournament, s) or [0])[0])
    else:
        # Solo — все; Random — все; Duo team — оба игрока пары
        for s in tournament.filled_slots():
            targets.update(slot_members(tournament, s))
        if not targets:
            targets = {p.get("id") for p in tournament.players}
    for uid in targets:
        if not uid or uid < 0:
            continue
        try:
            member = guild.get_member(uid)
            if member:
                await member.add_roles(role)
        except Exception as exc:
            logger.warning("BR grant role %s failed: %s", uid, exc)


async def pre_start_problems(guild, tournament: "BRTournament") -> list[str]:
    """Проверки перед стартом: гильдия, VAC, лимиты. Пусто — можно."""
    problems: list[str] = []
    err = tournament.draw_error()
    if err:
        problems.append(err)
    member_ids: list[int] = []
    for s in tournament.filled_slots():
        member_ids.extend(slot_members(tournament, s))
    if not member_ids:
        member_ids = [p.get("id") for p in tournament.players]
    vac_ids = set()
    try:
        from services import vac_service
        vac_ids = await vac_service.vac_active_ids(guild.id, member_ids)
    except Exception as exc:
        logger.warning("BR pre-start VAC check failed: %s", exc)
    for uid in member_ids:
        if not uid or uid < 0:
            continue
        m = guild.get_member(uid)
        label = m.display_name if m else f"User_{uid}"
        if uid in vac_ids:
            problems.append(f"{label} — активен VAC")
            continue
        if m is None:
            problems.append(f"{label} — нет на сервере")
    return problems


# ---------- публичная карточка ----------

class BRCardView(View):
    """Публичная карточка BR турнира (persistent, единая на весь турнир)."""

    def __init__(self, guild_id: int, channel_id: int,
                 tournament: "BRTournament" = None,
                 as_phase: "BRTournamentPhase | None" = None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        gid, cid = guild_id, channel_id
        tid = tournament.id if tournament else ""

        # as_phase — переопределение фазы для кнопок (публикация итогов
        # рендерит финальный вид до коммита фазы в хранилище).
        phase = as_phase or (tournament.phase if tournament else BRTournamentPhase.REGISTRATION)
        mode = tournament.mode if tournament else "random"
        fmt = tournament.format if tournament else BRFormat.SQUAD

        if phase == BRTournamentPhase.REGISTRATION:
            if mode in ("individual", "random"):
                self.add_item(JoinLeaveButton(gid, cid, tid, "join"))
            elif fmt == BRFormat.DUO:
                self.add_item(DuoInviteButton(gid, cid, tid))
                self.add_item(MyInvitesButton(gid, cid, tid))
            elif fmt == BRFormat.SQUAD:
                self.add_item(SquadRegisterButton(gid, cid, tid))
            self.add_item(JoinLeaveButton(gid, cid, tid, "leave"))
            self.add_item(MySlotButton(gid, cid, tid,
                                       tournament and my_slot_label(tournament)))
            self.add_item(RulesButton(gid, cid, tid))
        elif phase == BRTournamentPhase.DRAFT:
            if mode == "random" and not (tournament and tournament.is_solo):
                self.add_item(RenameSlotButton(gid, cid, tid))
            self.add_item(MySlotButton(gid, cid, tid,
                                       tournament and my_slot_label(tournament)))
        elif phase in (BRTournamentPhase.PLAYING, BRTournamentPhase.COLLECT):
            # Отдельная кнопка на каждую карту (1..10) — вход в приватную
            # панель карты: комната, статус, ввод результатов (орг).
            for m in range(min(int(tournament.map_count or 0), 10)):
                self.add_item(MapButton(gid, cid, tid, m))
            self.add_item(MySlotButton(gid, cid, tid,
                                       tournament and my_slot_label(tournament)))
        elif phase in (BRTournamentPhase.RESULTS, BRTournamentPhase.COMPLETE):
            self.add_item(ResultsPageButton(gid, cid, tid, -1))
            self.add_item(MySlotButton(gid, cid, tid,
                                       tournament and my_slot_label(tournament)))
            self.add_item(ResultsPageButton(gid, cid, tid, 1))

        if phase not in (BRTournamentPhase.RESULTS, BRTournamentPhase.COMPLETE,
                         BRTournamentPhase.CANCELLED):
            self.add_item(OrgMenuButton(gid, cid, tid))


class JoinLeaveButton(Button):
    def __init__(self, guild_id: int, channel_id: int, tid: str, kind: str):
        super().__init__(
            style=discord.ButtonStyle.primary if kind == "join" else discord.ButtonStyle.secondary,
            label="Участвовать" if kind == "join" else "Покинуть",
            custom_id=_cid(f"br_{kind}", guild_id, channel_id, tid),
        )
        self.kind = kind
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await _respond(interaction,"Регистрация закрыта.", ephemeral=True)
            return
        if not player_allowed(interaction, t):
            await _respond(interaction,
                "Это тестовый турнир — участие только для ведущих.", ephemeral=True)
            return
        async with get_br_lock(t):
            if self.kind == "join":
                await _do_join(interaction, t)
            else:
                await _do_leave(interaction, t)


async def _do_join(interaction: discord.Interaction, t: "BRTournament") -> None:
    from services import vac_service
    vac = await vac_service.check_vac(t.guild_id, interaction.user.id)
    if vac is not None:
        ts = int(vac["expires_at"].timestamp())
        await _respond(interaction,
            vac_service.vac_blocked_text(ts, reason=vac.get("reason"), level=vac.get("level")),
            ephemeral=True)
        return
    name = clean_user_name(interaction.user)
    ok, err = t.register_player(name, interaction.user.id)
    if not ok:
        await _respond(interaction,err or "Не удалось зарегистрироваться.", ephemeral=True)
        return
    br_store.set(t)
    await refresh_main_card(interaction, t)
    await _respond(interaction,"Вы зарегистрированы.", ephemeral=True)


async def _do_leave(interaction: discord.Interaction, t: "BRTournament") -> None:
    if not t.remove_player(interaction.user.id):
        await _respond(interaction,"Вы не зарегистрированы.", ephemeral=True)
        return
    br_store.set(t)
    await refresh_main_card(interaction, t)
    await _respond(interaction,"Вы вышли из турнира.", ephemeral=True)


class DuoInviteButton(Button):
    def __init__(self, guild_id: int, channel_id: int, tid: str):
        super().__init__(style=discord.ButtonStyle.primary, label="Созвать пару",
                         custom_id=_cid("br_duo_invite", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await _respond(interaction,"Регистрация закрыта.", ephemeral=True)
            return
        if not player_allowed(interaction, t):
            await _respond(interaction,
                "Это тестовый турнир — участие только для ведущих.", ephemeral=True)
            return
        await interaction.response.send_modal(DuoInviteModal(self.guild_id, self.channel_id, self.tid))


class DuoInviteModal(Modal):
    invitee = TextInput(label="Напарник (упоминание или ID)", placeholder="@user или 1234567890",
                        required=True, max_length=40)

    def __init__(self, guild_id: int, channel_id: int, tid: str):
        super().__init__(title="Приглашение в пару")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await _respond(interaction,"Регистрация закрыта.", ephemeral=True)
            return
        if not player_allowed(interaction, t):
            await _respond(interaction,
                "Это тестовый турнир — участие только для ведущих.", ephemeral=True)
            return
        raw = self.invitee.value.strip().strip("<@!>")
        try:
            invitee_id = int(raw)
        except ValueError:
            await _respond(interaction,"Укажите ID или упоминание игрока.", ephemeral=True)
            return
        member = interaction.guild.get_member(invitee_id)
        if member is None:
            await _respond(interaction,"Игрок не найден на сервере.", ephemeral=True)
            return
        async with get_br_lock(t):
            ok, err, idx = t.create_duo_invite(
                interaction.user.id, clean_user_name(interaction.user), invitee_id)
            if not ok:
                await _respond(interaction,err, ephemeral=True)
                return
            br_store.set(t)
        slot = t.teams[idx]
        mate_name = member.display_name if member else str(invitee_id)
        await _respond(interaction,
            f"Приглашение отправлено {mate_name}. Пара «{slot_label(slot, idx)}» "
            "занимает место после подтверждения напарником (кнопка «Мои заявки»).",
            ephemeral=True)
        await refresh_main_card(interaction, t)


class MyInvitesButton(Button):
    def __init__(self, guild_id: int, channel_id: int, tid: str):
        super().__init__(style=discord.ButtonStyle.secondary, label="Мои заявки",
                         custom_id=_cid("br_my_invites", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t:
            await _respond(interaction,"Турнир не найден.", ephemeral=True)
            return
        uid = interaction.user.id
        invites = [(i, s) for i, s in enumerate(t.teams)
                   if s.get("pending_invitee_id") == uid]
        if not invites:
            await _respond(interaction,"Нет входящих заявок.", ephemeral=True)
            return
        view = View(timeout=120)
        for i, s in invites:
            cap_name = (s.get("display_names") or {}).get(str(s.get("captain_id")), "")
            cap_txt = f" · {cap_name}" if cap_name else ""
            view.add_item(InviteRespondButton(self.guild_id, self.channel_id, self.tid,
                                              s.get("uid", str(i)), True,
                                              f"Принять: {slot_label(s, i)}{cap_txt}"[:80]))
            view.add_item(InviteRespondButton(self.guild_id, self.channel_id, self.tid,
                                              s.get("uid", str(i)), False,
                                              f"Отклонить: {slot_label(s, i)}"[:80],
                                              style=discord.ButtonStyle.danger))
        await _respond(interaction,"Входящие заявки в пары:", view=view, ephemeral=True)


class InviteRespondButton(Button):
    def __init__(self, guild_id, channel_id, tid, slot_uid, accept, label, style=None):
        super().__init__(style=style or discord.ButtonStyle.success, label=label[:80],
                         custom_id=_cid(f"br_invite_{'ok' if accept else 'no'}",
                                        guild_id, channel_id, tid, slot_uid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.slot_uid = slot_uid
        self.accept = accept

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await _respond(interaction,"Регистрация закрыта.", ephemeral=True)
            return
        if not player_allowed(interaction, t):
            await _respond(interaction,
                "Это тестовый турнир — участие только для ведущих.", ephemeral=True)
            return
        found = t.find_slot_by_uid(self.slot_uid)
        if not found:
            # fallback: uid мог быть числовым индексом legacy-слота
            try:
                idx = int(self.slot_uid)
                found = (idx, t.teams[idx]) if 0 <= idx < len(t.teams) else None
            except ValueError:
                found = None
        if not found:
            await _respond(interaction,"Заявка не найдена или устарела.", ephemeral=True)
            return
        idx, slot = found
        if self.accept:
            # VAC проверяем у ОБОИХ игроков пары при подтверждении.
            vac_bad = await self._vac_members(interaction.guild, t, slot)
            if vac_bad:
                await _respond(interaction,
                    "Подтверждение отклонено — активен VAC у: " + ", ".join(vac_bad) + ".",
                    ephemeral=True)
                return
        async with get_br_lock(t):
            ok, msg = t.respond_duo_invite(
                idx, interaction.user.id, clean_user_name(interaction.user), self.accept)
            if t.phase == BRTournamentPhase.REGISTRATION:
                br_store.set(t)
        if not ok:
            await _respond(interaction,msg, ephemeral=True)
            return
        await _respond(interaction,msg, ephemeral=True)
        await refresh_main_card(interaction, t)

    async def _vac_members(self, guild, t: "BRTournament", slot: dict) -> list[str]:
        """Имена членов слота с активным VAC (капитан + подтверждающий)."""
        try:
            from services import vac_service
        except Exception:
            return []
        bad = []
        ids = list(slot_members(t, slot))
        pending = slot.get("pending_invitee_id")
        if pending:
            ids.append(int(pending))
        for uid in ids:
            if not uid or uid < 0:
                continue
            try:
                vac = await vac_service.check_vac(t.guild_id, uid)
            except Exception:
                continue
            if vac is not None:
                m = guild.get_member(uid)
                bad.append(m.display_name if m else f"User_{uid}")
        return bad


class SquadRegisterButton(Button):
    def __init__(self, guild_id: int, channel_id: int, tid: str):
        super().__init__(style=discord.ButtonStyle.primary, label="Зарегистрировать отряд",
                         custom_id=_cid("br_squad_reg", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        from storage.team_store import team_store, TeamStoreError
        from config import BR_TEAM_CHANNEL_ID

        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await _respond(interaction,"Регистрация закрыта.", ephemeral=True)
            return
        if t.format != BRFormat.SQUAD or t.mode != "team":
            await _respond(interaction,"Недоступно для этого формата.", ephemeral=True)
            return
        if not player_allowed(interaction, t):
            await _respond(interaction,
                "Это тестовый турнир — участие только для ведущих.", ephemeral=True)
            return

        team = team_store.get_by_captain(interaction.user.id)
        if not team or team.guild_id != interaction.guild_id:
            await _respond(interaction,
                f"Вы не капитан команды! Создайте отряд из 4 через `/team create` в <#{BR_TEAM_CHANNEL_ID}>.",
                ephemeral=True)
            return
        if len(team.members) != t.slot_size:
            await _respond(interaction,
                f"В отряде {len(team.members)}/{t.slot_size} игроков.", ephemeral=True)
            return
        # Снимок состава, по которому выполнялась VAC-проверка —
        # под блокировкой ниже он сверяется с актуальным.
        vac_checked_members = set(team.members)

        # VAC — ни один участник
        from services import vac_service
        vac_bad = []
        for uid in team.members:
            vac = await vac_service.check_vac(t.guild_id, uid)
            if vac is not None:
                vac_bad.append(uid)
        if vac_bad:
            names = []
            for u in vac_bad:
                m = interaction.guild.get_member(u)
                names.append(m.display_name if m else f"User_{u}")
            await _respond(interaction,
                "Регистрация невозможна: активен VAC у " + ", ".join(names) + ".",
                ephemeral=True)
            return

        async with get_br_lock(t):
            # Согласованная блокировка (финальный блокер №1):
            # FOR UPDATE на строке teams держится до фиксации
            # регистрации в br_store — join/leave/disband Squad
            # Hub сериализуются через тот же row-lock. Регистрируем
            # ТОЛЬКО актуальный снимок: удалённая команда, смена
            # капитана или изменение состава после VAC-проверки
            # здесь отсекаются.
            try:
                async with team_store.team_registration_lock(
                        team.team_id, team.guild_id,
                        interaction.user.id) as fresh:
                    if set(fresh.members) != vac_checked_members:
                        await _respond(interaction,
                            "Состав команды изменился — "
                            "повторите регистрацию.", ephemeral=True)
                        return
                    display_names = {}
                    for uid in fresh.members:
                        m = interaction.guild.get_member(uid)
                        display_names[uid] = clean_user_name(m) \
                            if m else f"User_{uid}"
                    ok, err = t.register_team_slot(
                        fresh.team_id, fresh.team_name,
                        fresh.captain_id, list(fresh.members),
                        display_names)
                    if not ok:
                        await _respond(interaction, err,
                                       ephemeral=True)
                        return
                    br_store.set(t)
            except TeamStoreError as exc:
                await _respond(interaction,
                    getattr(exc, "message", "") or str(exc),
                    ephemeral=True)
                return

        await _respond(interaction,
            f"Отряд «{team.team_name}» зарегистрирован.", ephemeral=True)
        await refresh_main_card(interaction, t)


class MySlotButton(Button):
    def __init__(self, guild_id: int, channel_id: int, tid: str, label: str = "Мой слот"):
        super().__init__(style=discord.ButtonStyle.secondary, label=label[:80],
                         custom_id=_cid("br_my_slot", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t:
            await _respond(interaction,"Турнир не найден.", ephemeral=True)
            return
        found = t.find_slot_by_user(interaction.user.id)
        if not found and t.phase == BRTournamentPhase.REGISTRATION:
            in_pool = any(p.get("id") == interaction.user.id for p in t.players)
            if in_pool:
                await _respond(interaction,
                    "Вы в пуле жеребьёвки — состав определится после старта.", ephemeral=True)
                return
        if not found:
            await _respond(interaction,"Вы не зарегистрированы в турнире.", ephemeral=True)
            return
        idx, slot = found
        lines = [f"**{slot_label(slot, idx)}**"]
        dn = slot.get("display_names") or {}
        for uid in slot_members(t, slot):
            lines.append(f"• {dn.get(str(uid)) or _player_name(t, uid)}")
        # статистика по картам (только после публикации)
        if t.results_published or t.phase in (BRTournamentPhase.RESULTS, BRTournamentPhase.COMPLETE):
            for m in range(t.map_count):
                e = t.get_team_stats_for_map(slot, m)
                if e:
                    mark = (absent_label(t) if e["dns"]
                            else f"место {e['place']}, {e['kills']} киллов")
                    lines.append(f"Карта {m + 1} ({MAP_NAMES_RU.get(t.get_map_name_for_index(m), '')}): {mark}")
        await _respond(interaction,"\n".join(lines), ephemeral=True)


class RulesButton(Button):
    """Приватная справка: правила начисления, тай-брейки, регистрация."""

    def __init__(self, guild_id: int, channel_id: int, tid: str):
        super().__init__(style=discord.ButtonStyle.secondary, label="Правила",
                         custom_id=_cid("br_rules", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t:
            await _respond(interaction,"Турнир не найден.", ephemeral=True)
            return
        await _respond(interaction, registration_rules_text(t), ephemeral=True)


class MapButton(Button):
    """Публичная кнопка карты: приватная панель с комнатой и статусом."""

    def __init__(self, guild_id: int, channel_id: int, tid: str, map_index: int):
        super().__init__(style=discord.ButtonStyle.secondary,
                         label=f"Карта {map_index + 1}",
                         custom_id=_cid("br_map", guild_id, channel_id, tid, map_index))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase not in (BRTournamentPhase.PLAYING,
                                    BRTournamentPhase.COLLECT):
            await _respond(interaction,"Панель карты недоступна.", ephemeral=True)
            return
        if interaction.guild and t.guild_id != interaction.guild.id:
            await _respond(interaction,"Турнир не найден.", ephemeral=True)
            return
        if not player_allowed(interaction, t):
            await _respond(interaction,
                "Это тестовый турнир — данные только для ведущих.", ephemeral=True)
            return
        can_view = (is_org(interaction.user.id)
                    or t.can_view_room(interaction.user.id))
        view = BRMapPanelView(self.guild_id, self.channel_id, self.tid,
                              self.map_index, can_view_room=can_view)
        await _respond(interaction,
            view=compose(text=map_panel_text(t, self.map_index,
                                             show_room=can_view), view=view),
            ephemeral=True)


def map_panel_text(tournament: "BRTournament", map_index: int,
                   show_room: bool = False) -> str:
    """Текст приватной панели карты.

    ID комнаты показывается только тем, у кого есть право просмотра
    (участники слота / капитаны squad-team / орг); посторонним —
    нейтральная строка без реквизитов.
    """
    ru = MAP_NAMES_RU.get(tournament.get_map_name_for_index(map_index),
                          tournament.get_map_name_for_index(map_index))
    status = MAP_STATUS_LABELS.get(
        tournament.map_status(map_index), tournament.map_status(map_index))
    filled = tournament.map_filled_count(map_index)
    total = len(tournament.teams)
    creds = tournament.room_credentials(map_index)
    if creds and show_room:
        room_line = (f"Комната: ID `{creds['id']}` — пароль по кнопке "
                     "«Данные комнаты»")
    elif creds:
        room_line = "Комната: задана — данные по кнопке «Данные комнаты»"
    else:
        room_line = "Комната: не задана"
    return (
        f"**Карта {map_index + 1} из {tournament.map_count} — {ru}**\n"
        f"Статус: {status}\n"
        f"Результаты: {filled}/{total} слотов\n"
        f"{room_line}"
    )


class BRMapPanelView(View):
    """Публичная панель карты — только просмотр.

    Доступные действия: статус карты, данные комнаты при наличии прав,
    «Назад». Организаторские функции (комната/результаты) живут
    исключительно в приватном меню «Управление».
    """

    def __init__(self, guild_id: int, channel_id: int, tid: str,
                 map_index: int, can_view_room: bool = False):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index
        if can_view_room:
            self.add_item(MapRoomButton(guild_id, channel_id, tid, map_index))
        self.add_item(MapBackButton(guild_id, channel_id, tid, map_index))


class MapRoomButton(Button):
    """Показать ID/пароль комнаты этой карты (только участникам)."""

    def __init__(self, guild_id, channel_id, tid, map_index):
        super().__init__(style=discord.ButtonStyle.primary, label="Данные комнаты",
                         custom_id=_cid("br_map_room", guild_id, channel_id, tid, map_index))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase not in (BRTournamentPhase.PLAYING,
                                    BRTournamentPhase.COLLECT):
            await _respond(interaction,"Комната недоступна.", ephemeral=True)
            return
        if not player_allowed(interaction, t):
            await _respond(interaction,
                "Это тестовый турнир — данные только для ведущих.", ephemeral=True)
            return
        if not (is_org(interaction.user.id)
                or t.can_view_room(interaction.user.id)):
            await _respond(interaction,
                "Данные комнаты видны только участникам турнира.", ephemeral=True)
            return
        creds = t.room_credentials(self.map_index)
        if not creds:
            await _respond(interaction,
                "Ведущий ещё не указал данные комнаты для этой карты.",
                ephemeral=True)
            return
        await _respond(interaction,
            f"**Комната — карта {self.map_index + 1}** "
            f"({MAP_NAMES_RU.get(t.get_map_name_for_index(self.map_index), '')}):\n"
            f"```\nID: {creds['id']}\nПароль: {creds['password']}\n```",
            ephemeral=True)


class MapEntryButton(Button):
    """Орг: открыть панель ввода результатов по этой карте."""

    def __init__(self, guild_id, channel_id, tid, map_index):
        super().__init__(style=discord.ButtonStyle.success, label="Ввести результаты",
                         custom_id=_cid("br_map_entry", guild_id, channel_id, tid, map_index))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        # без defer: edit_message должен идти через response напрямую
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or not t.results_editable:
            await _respond(interaction,"Ввод результатов недоступен.", ephemeral=True)
            return
        filled, expected = t.results_status()
        notes = ""
        if getattr(t, "migration_notes", None):
            notes = ("\n\n**Замечания миграции (сверьте вручную):**\n"
                     + "\n".join(f"• {n}" for n in t.migration_notes[:10]))
        view = BREntryPanelView(self.guild_id, self.channel_id, self.tid,
                                map_index=self.map_index, page=0)
        await interaction.response.edit_message(
            view=compose(
                text=(f"Ввод результатов — карта {self.map_index + 1} "
                      f"({MAP_NAMES_RU.get(t.get_map_name_for_index(self.map_index), '')}). "
                      f"Заполнено всего: {filled}/{expected}." + notes),
                view=view))


class MapBatchButton(Button):
    """Орг: массовый ввод результатов по этой карте."""

    def __init__(self, guild_id, channel_id, tid, map_index):
        super().__init__(style=discord.ButtonStyle.secondary, label="Массовый ввод",
                         custom_id=_cid("br_map_batch", guild_id, channel_id, tid, map_index))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        # без defer: send_modal должен быть первым ответом на interaction
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or not t.results_editable:
            await _respond(interaction,"Ввод результатов недоступен.", ephemeral=True)
            return
        await interaction.response.send_modal(
            BatchEntryModal(self.guild_id, self.channel_id, self.tid,
                            self.map_index))


class MapRoomEditButton(Button):
    """Орг: изменить ID/пароль комнаты этой карты (модалка с текущими значениями)."""

    def __init__(self, guild_id, channel_id, tid, map_index):
        super().__init__(style=discord.ButtonStyle.secondary, label="Изменить комнату",
                         custom_id=_cid("br_map_room_edit", guild_id, channel_id, tid, map_index))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        # без defer: send_modal должен быть первым ответом
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if (not t or not (0 <= self.map_index < t.map_count)
                or t.phase not in (BRTournamentPhase.DRAFT,
                                   BRTournamentPhase.PLAYING,
                                   BRTournamentPhase.COLLECT)):
            await _respond(interaction,"Изменение комнаты недоступно.", ephemeral=True)
            return
        creds = t.room_credentials(self.map_index) or {}
        await interaction.response.send_modal(
            RoomSetModal(self.guild_id, self.channel_id, self.tid,
                         self.map_index, prefill=creds))


def admin_map_panel_text(tournament: "BRTournament", map_index: int) -> str:
    """Текст панели управления картой — только для орга (ephemeral).

    В отличие от публичной панели показывает ID и пароль открытым
    текстом — сообщение приватное, доступ проверен is_org.
    """
    ru = MAP_NAMES_RU.get(tournament.get_map_name_for_index(map_index),
                          tournament.get_map_name_for_index(map_index))
    status = MAP_STATUS_LABELS.get(
        tournament.map_status(map_index), tournament.map_status(map_index))
    filled = tournament.map_filled_count(map_index)
    total = len(tournament.teams)
    creds = tournament.room_credentials(map_index)
    if creds:
        room_line = (f"Комната: ID `{creds['id']}` — пароль `{creds['password']}`")
    else:
        room_line = "Комната: не задана — заполните через «Изменить комнату»."
    return (
        f"**Управление — Карта {map_index + 1} из {tournament.map_count} — {ru}**\n"
        f"Статус: {status}\n"
        f"Результаты: {filled}/{total} слотов\n"
        f"{room_line}"
    )


class BRAdminMapPanelView(View):
    """Приватная панель управления картой (только через «Управление»).

    Комната редактируется в DRAFT/PLAYING/COLLECT; ввод результатов —
    только когда results_editable (PLAYING/COLLECT до публикации).
    """

    def __init__(self, guild_id: int, channel_id: int, tid: str,
                 map_index: int, editable: bool = True):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index
        self.add_item(MapRoomEditButton(guild_id, channel_id, tid, map_index))
        if editable:
            self.add_item(MapEntryButton(guild_id, channel_id, tid, map_index))
            self.add_item(MapBatchButton(guild_id, channel_id, tid, map_index))
        self.add_item(AdminMapRefreshButton(guild_id, channel_id, tid, map_index))
        self.add_item(AdminMapBackButton(guild_id, channel_id, tid, map_index))


class AdminMapButton(Button):
    """Кнопка конкретной карты в орг-меню «Управление»."""

    def __init__(self, guild_id, channel_id, tid, map_index):
        super().__init__(style=discord.ButtonStyle.secondary,
                         label=f"Карта {map_index + 1}",
                         custom_id=_cid("br_adm_map", guild_id, channel_id,
                                        tid, map_index))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,
                           "У вас нет доступа к управлению турниром",
                           ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if (not t or not (0 <= self.map_index < t.map_count)
                or t.phase not in (BRTournamentPhase.DRAFT,
                                   BRTournamentPhase.PLAYING,
                                   BRTournamentPhase.COLLECT)):
            await _respond(interaction,"Управление картой недоступно.",
                           ephemeral=True)
            return
        view = BRAdminMapPanelView(self.guild_id, self.channel_id, self.tid,
                                   self.map_index,
                                   editable=bool(t.results_editable))
        await _respond(interaction,
            view=compose(text=admin_map_panel_text(t, self.map_index),
                         view=view),
            ephemeral=True)


class AdminMapRefreshButton(Button):
    """Обновить панель управления картой актуальными данными."""

    def __init__(self, guild_id, channel_id, tid, map_index):
        super().__init__(style=discord.ButtonStyle.secondary, label="Обновить",
                         custom_id=_cid("br_adm_map_refresh", guild_id,
                                        channel_id, tid, map_index))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await _respond(interaction,
                           "У вас нет доступа к управлению турниром",
                           ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if (not t or not (0 <= self.map_index < t.map_count)
                or t.phase not in (BRTournamentPhase.DRAFT,
                                   BRTournamentPhase.PLAYING,
                                   BRTournamentPhase.COLLECT)):
            await _respond(interaction,"Управление картой недоступно.",
                           ephemeral=True)
            return
        view = BRAdminMapPanelView(self.guild_id, self.channel_id, self.tid,
                                   self.map_index,
                                   editable=bool(t.results_editable))
        await interaction.response.edit_message(
            view=compose(text=admin_map_panel_text(t, self.map_index),
                         view=view))


class AdminMapBackButton(Button):
    """Назад — приватное меню «Управление» турнира."""

    def __init__(self, guild_id, channel_id, tid, map_index):
        super().__init__(style=discord.ButtonStyle.secondary, label="Назад",
                         custom_id=_cid("br_adm_map_back", guild_id,
                                        channel_id, tid, map_index))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await _respond(interaction,
                           "У вас нет доступа к управлению турниром",
                           ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t:
            await _respond(interaction,"Турнир не найден.", ephemeral=True)
            return
        view = BRManagementMenuView(self.guild_id, self.channel_id, t)
        await interaction.response.edit_message(
            view=compose(text="Управление BR:", view=view))


class BRMapsOverviewView(View):
    """Список всех карт с кнопками — возврат из панели/ввода."""

    def __init__(self, guild_id, channel_id, tid):
        super().__init__(timeout=None)
        t = get_bound(guild_id, channel_id, tid)
        for m in range(min(int(getattr(t, "map_count", 0) or 0), 10)):
            self.add_item(MapButton(guild_id, channel_id, tid, m))


class MapBackButton(Button):
    """Назад — список всех карт с кнопками перехода."""

    def __init__(self, guild_id, channel_id, tid, map_index):
        super().__init__(style=discord.ButtonStyle.secondary, label="Назад",
                         custom_id=_cid("br_map_back", guild_id, channel_id, tid, map_index))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        # без defer: edit_message должен идти через response напрямую
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t:
            await _respond(interaction,"Турнир не найден.", ephemeral=True)
            return
        sched = "\n".join(schedule_status_lines(t))
        view = BRMapsOverviewView(self.guild_id, self.channel_id, self.tid)
        await interaction.response.edit_message(
            view=compose(text=f"**Карты турнира:**\n{sched}", view=view))


class RenameSlotButton(Button):
    def __init__(self, guild_id: int, channel_id: int, tid: str):
        super().__init__(style=discord.ButtonStyle.secondary, label="Назвать состав",
                         custom_id=_cid("br_rename", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase != BRTournamentPhase.DRAFT:
            await _respond(interaction,"Переименование доступно только на этапе составов.", ephemeral=True)
            return
        if not player_allowed(interaction, t):
            await _respond(interaction,
                "Это тестовый турнир — участие только для ведущих.", ephemeral=True)
            return
        found = t.find_slot_by_user(interaction.user.id)
        if not found:
            await _respond(interaction,"Вы не в составе.", ephemeral=True)
            return
        idx, slot = found
        if slot.get("captain_id") != interaction.user.id:
            await _respond(interaction,"Только капитан может назвать состав.", ephemeral=True)
            return
        if not t.team_rename_remaining.get(idx, False):
            await _respond(interaction,"Название уже использовано.", ephemeral=True)
            return
        await interaction.response.send_modal(RenameSlotModal(self.guild_id, self.channel_id, self.tid))


class RenameSlotModal(Modal):
    name = TextInput(label="Название состава", required=True, max_length=32)

    def __init__(self, guild_id: int, channel_id: int, tid: str):
        super().__init__(title="Название состава")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase != BRTournamentPhase.DRAFT:
            await _respond(interaction,"Турнир не в фазе составов.", ephemeral=True)
            return
        found = t.find_slot_by_user(interaction.user.id)
        if not found:
            await _respond(interaction,"Вы не в составе.", ephemeral=True)
            return
        idx, slot = found
        if t.rename_slot(idx, self.name.value.strip(), interaction.user.id):
            br_store.set(t)
            await _respond(interaction,f"Состав переименован в «{self.name.value.strip()}».", ephemeral=True)
            await refresh_main_card(interaction, t)
        else:
            await _respond(interaction,"Не удалось переименовать (уже использовано или нет прав).", ephemeral=True)


class ResultsPageButton(Button):
    def __init__(self, guild_id: int, channel_id: int, tid: str, delta: int):
        super().__init__(style=discord.ButtonStyle.secondary,
                         label="◀" if delta < 0 else "▶",
                         custom_id=_cid("br_rpage", guild_id, channel_id, tid, delta))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase not in (BRTournamentPhase.RESULTS, BRTournamentPhase.COMPLETE):
            await _respond(interaction,"Результаты недоступны.", ephemeral=True)
            return
        page = getattr(t, "view_page", -1)
        # -1 = сводка; 0..N-1 = карты; листаем по кругу
        if self.delta > 0:
            page = 0 if page >= t.map_count - 1 else page + 1
        else:
            page = t.map_count - 1 if page <= 0 else page - 1
        t.view_page = page
        br_store.set(t)
        embed = final_embed(t, page)
        await interaction.response.edit_message(view=compose(embed, BRCardView(t.guild_id, t.channel_id, t)))


# ---------- орг-меню ----------

class OrgMenuButton(Button):
    def __init__(self, guild_id: int, channel_id: int, tid: str):
        super().__init__(style=discord.ButtonStyle.secondary, label="Управление",
                         custom_id=_cid("br_org_menu", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,
                "У вас нет доступа к управлению турниром", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t:
            await _respond(interaction,"Турнир не найден.", ephemeral=True)
            return
        view = BRManagementMenuView(self.guild_id, self.channel_id, t)
        await _respond(interaction,
            view=compose(text="Управление BR:", view=view), ephemeral=True)


class BRManagementMenuView(View):
    def __init__(self, guild_id: int, channel_id: int, tournament: "BRTournament"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        t = tournament
        gid, cid, tid = guild_id, channel_id, t.id
        if t.phase == BRTournamentPhase.REGISTRATION:
            self.add_item(KickSlotButton(gid, cid, tid))
            self.add_item(DrawButton(gid, cid, tid))
            self.add_item(CancelTournamentButton(gid, cid, tid))
        elif t.phase == BRTournamentPhase.DRAFT:
            self.add_item(BeginGamesButton(gid, cid, tid))
            # управление комнатами по картам — панель каждой карты
            for m in range(min(int(t.map_count or 0), 10)):
                self.add_item(AdminMapButton(gid, cid, tid, m))
            self.add_item(CancelTournamentButton(gid, cid, tid))
        elif t.phase in (BRTournamentPhase.PLAYING, BRTournamentPhase.COLLECT):
            # отдельная кнопка управления на каждую карту
            for m in range(min(int(t.map_count or 0), 10)):
                self.add_item(AdminMapButton(gid, cid, tid, m))
            self.add_item(PreviewResultsButton(gid, cid, tid))
            self.add_item(PublishResultsButton(gid, cid, tid))
            self.add_item(CancelTournamentButton(gid, cid, tid))
        self.add_item(CloseMenuButton(gid, cid, tid))


class CloseMenuButton(Button):
    def __init__(self, guild_id, channel_id, tid):
        super().__init__(style=discord.ButtonStyle.secondary, label="Закрыть",
                         custom_id=_cid("br_close_menu", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=smart_view(interaction.message, None))


class DrawButton(Button):
    """Жеребьёвка/старт: Solo → сразу «Игры идут», остальные → составы."""

    def __init__(self, guild_id, channel_id, tid):
        super().__init__(style=discord.ButtonStyle.success, label="Жеребьёвка / Старт",
                         custom_id=_cid("br_draw", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await _respond(interaction,"Регистрация закрыта.", ephemeral=True)
            return
        async with get_br_lock(t):
            # Проверки перед стартом: лимиты, гильдия, VAC — блокируют старт,
            # регистрация остаётся открытой, показаны конкретные участники.
            problems = await pre_start_problems(interaction.guild, t)
            if problems:
                await _respond(interaction,
                    "Старт заблокирован:\n" + "\n".join(problems[:15]) +
                    "\nРегистрация остаётся открытой — исправьте и повторите.",
                    ephemeral=True)
                return
            ok, err = t.draw()
            if not ok:
                await _respond(interaction,err, ephemeral=True)
                return
            await grant_tournament_roles(interaction.guild, t)
            br_store.set(t)
        await refresh_main_card(interaction, t)
        await _respond(interaction,
            "Жеребьёвка выполнена — составы опубликованы." if t.phase == BRTournamentPhase.DRAFT
            else "Турнир начался — игры идут.", ephemeral=True)


class BeginGamesButton(Button):
    def __init__(self, guild_id, channel_id, tid):
        super().__init__(style=discord.ButtonStyle.success, label="Начать игры",
                         custom_id=_cid("br_begin", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase != BRTournamentPhase.DRAFT:
            await _respond(interaction,"Нет этапа составов.", ephemeral=True)
            return
        async with get_br_lock(t):
            t.begin_games()
            br_store.set(t)
        await refresh_main_card(interaction, t)
        await _respond(interaction,"Игры начались.", ephemeral=True)


class KickSlotButton(Button):
    def __init__(self, guild_id, channel_id, tid):
        super().__init__(style=discord.ButtonStyle.danger, label="Снять участника/слот",
                         custom_id=_cid("br_kick", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await _respond(interaction,"Регистрация закрыта.", ephemeral=True)
            return
        slots = t.teams
        if not slots:
            await _respond(interaction,"Нет слотов для снятия.", ephemeral=True)
            return
        view = KickSlotSelectView(self.guild_id, self.channel_id, self.tid, slots, page=0)
        await _respond(interaction,"Выберите слот для снятия:", view=view, ephemeral=True)


class KickSlotSelectView(View):
    PER_PAGE = 25

    def __init__(self, guild_id, channel_id, tid, slots, page):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.slots = slots
        self.page = page
        start = page * self.PER_PAGE
        chunk = slots[start:start + self.PER_PAGE]
        options = [discord.SelectOption(
            label=slot_label(s, start + i)[:100],
            value=s.get("uid", str(start + i)),
            description=(" ⏳" if s.get("pending_invitee_id") else "")[:100] or None,
        ) for i, s in enumerate(chunk)]
        if options:
            self.add_item(KickSlotSelect(self.guild_id, self.channel_id, self.tid, options))
        if page > 0:
            self.add_item(KickPageButton(self.guild_id, self.channel_id, self.tid, self.slots, page - 1, "◀"))
        if (page + 1) * self.PER_PAGE < len(slots):
            self.add_item(KickPageButton(self.guild_id, self.channel_id, self.tid, self.slots, page + 1, "▶"))


class KickSlotSelect(Select):
    def __init__(self, guild_id, channel_id, tid, options):
        super().__init__(placeholder="Слот…", options=options,
                         custom_id=_cid("br_kick_sel", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await _respond(interaction,"Регистрация закрыта.", ephemeral=True)
            return
        uid = self.values[0]
        found = t.find_slot_by_uid(uid)
        if not found:
            try:
                idx = int(uid)
                found = (idx, t.teams[idx]) if 0 <= idx < len(t.teams) else None
            except ValueError:
                found = None
        if not found:
            await _respond(interaction,"Слот не найден.", ephemeral=True)
            return
        idx, slot = found
        async with get_br_lock(t):
            ok = t.kick_slot(idx, reason=f"снято <@{interaction.user.id}>")
            if ok:
                br_store.set(t)
        if ok:
            await _respond(interaction,
                f"Слот «{slot_label(slot, idx)}» снят.", ephemeral=True)
            await refresh_main_card(interaction, t)
        else:
            await _respond(interaction,"Не удалось снять слот.", ephemeral=True)


class KickPageButton(Button):
    def __init__(self, guild_id, channel_id, tid, slots, page, label):
        super().__init__(style=discord.ButtonStyle.secondary, label=label,
                         custom_id=_cid("br_kick_pg", guild_id, channel_id, tid, page))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.slots = slots
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await _respond(interaction,
                           "У вас нет доступа к управлению турниром",
                           ephemeral=True)
            return
        view = KickSlotSelectView(self.guild_id, self.channel_id, self.tid, self.slots, self.page)
        await interaction.response.edit_message(view=view)


class CancelTournamentButton(Button):
    def __init__(self, guild_id, channel_id, tid):
        super().__init__(style=discord.ButtonStyle.danger, label="Отменить турнир",
                         custom_id=_cid("br_cancel", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        await interaction.response.send_modal(CancelReasonModal(self.guild_id, self.channel_id, self.tid))


class CancelReasonModal(Modal):
    reason = TextInput(label="Причина отмены", required=True, max_length=200)

    def __init__(self, guild_id, channel_id, tid):
        super().__init__(title="Отмена BR турнира")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,
                           "У вас нет доступа к управлению турниром",
                           ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or not t.is_active:
            await _respond(interaction,"Турнир не найден или уже завершён.", ephemeral=True)
            return
        view = CancelConfirmView(self.guild_id, self.channel_id, self.tid, self.reason.value.strip())
        await _respond(interaction,
            f"Подтвердите отмену BR турнира.\nПричина: {self.reason.value.strip()}",
            view=view, ephemeral=True)


class CancelConfirmView(View):
    def __init__(self, guild_id, channel_id, tid, reason, purge=False):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.reason = reason
        self.add_item(CancelConfirmButton(guild_id, channel_id, tid, reason,
                                          purge=purge))


class CancelConfirmButton(Button):
    def __init__(self, guild_id, channel_id, tid, reason, purge=False):
        super().__init__(style=discord.ButtonStyle.danger,
                         label="Удалить тест" if purge else "Подтвердить отмену",
                         custom_id=_cid("br_cancel_ok", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.reason = reason
        self.purge = purge

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        async with get_br_lock_key(self.guild_id, self.channel_id, self.tid):
            if self.purge:
                await self._purge(interaction)
                return
            t = br_store.get(self.guild_id, self.channel_id)
            if t is None or str(t.id) != str(self.tid):
                await _respond(interaction,STALE_MSG, ephemeral=True)
                return
            if not t.is_active:
                await _respond(interaction,"Турнир уже не активен.", ephemeral=True)
                return
            prev_phase = t.phase
            prev_pending = t.publish_pending
            t.phase = BRTournamentPhase.CANCELLED
            t.cancel_reason = self.reason
            t.cancelled_by = interaction.user.id
            t.publish_pending = None  # отмена снимает замороженный снимок
            try:
                br_store.set(t)  # архивирует
            except Exception:
                # сбой записи: вернуть объект в прежнюю фазу — повтор возможен
                t.phase = prev_phase
                t.cancel_reason = ""
                t.cancelled_by = 0
                t.publish_pending = prev_pending
                await _respond(interaction,
                    "Не удалось сохранить отмену — повторите.", ephemeral=True)
                return
        try:
            await strip_tournament_roles(interaction.guild, t)
        except Exception as exc:
            logger.warning("BR cancel strip roles: %s", exc)
        await refresh_main_card(interaction, t)
        await _respond(interaction,"Турнир отменён и перенесён в историю.", ephemeral=True)

    async def _purge(self, interaction: discord.Interaction) -> None:
        """Полное удаление тестового турнира (активного или архивного).

        Доступно только для is_test — реальные турниры удалять нельзя,
        они хранятся в истории для Hall of Fame.
        """
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if t is None:
            await _respond(interaction, STALE_MSG, ephemeral=True)
            return
        if not t.is_test:
            await _respond(interaction,
                "Удалить можно только тестовый турнир — обычные турниры "
                "остаются в истории.", ephemeral=True)
            return
        br_store.remove(self.guild_id, self.channel_id, self.tid)
        # публичную карточку заменяем заглушкой «удалён»
        try:
            if interaction.guild and t.message_id:
                channel = interaction.guild.get_channel(t.channel_id)
                if channel is not None:
                    from utils.design import text_card
                    msg = await channel.fetch_message(t.message_id)
                    await msg.edit(view=text_card("Тестовый турнир удалён."))
        except Exception as exc:
            logger.warning("BR purge card edit failed: %s", exc)
        await _respond(interaction,
            "Тестовый турнир полностью удалён (активные данные и архив).",
            ephemeral=True)


# ---------- комнаты (ID/пароль на каждую карту) ----------

class RoomPickButton(Button):
    """Орг: выбор карты для задания ID/пароля комнаты."""

    def __init__(self, guild_id, channel_id, tid):
        super().__init__(style=discord.ButtonStyle.secondary, label="Комнаты",
                         custom_id=_cid("br_room_pick", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase not in (BRTournamentPhase.DRAFT,
                                    BRTournamentPhase.PLAYING,
                                    BRTournamentPhase.COLLECT):
            await _respond(interaction,"Комнаты можно задавать после формирования составов.", ephemeral=True)
            return
        options = []
        for i in range(min(t.map_count, 25)):
            ru = MAP_NAMES_RU.get(t.get_map_name_for_index(i), "")
            options.append(discord.SelectOption(
                label=f"Карта {i + 1} — {ru}"[:100], value=str(i)))
        view = View(timeout=300)
        view.add_item(RoomMapSelect(self.guild_id, self.channel_id, self.tid, options))
        await _respond(interaction,
            "Выберите карту для ввода ID и пароля комнаты:",
            view=view, ephemeral=True)


class RoomMapSelect(Select):
    def __init__(self, guild_id, channel_id, tid, options):
        super().__init__(placeholder="Карта…", options=options,
                         custom_id=_cid("br_room_sel", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase not in (BRTournamentPhase.DRAFT,
                                    BRTournamentPhase.PLAYING,
                                    BRTournamentPhase.COLLECT):
            await _respond(interaction,"Турнир не в фазе игр.", ephemeral=True)
            return
        map_index = int(self.values[0])
        await interaction.response.send_modal(
            RoomSetModal(self.guild_id, self.channel_id, self.tid, map_index))


class RoomSetModal(Modal):
    room_id = TextInput(label="ID комнаты", placeholder="123456", required=True, max_length=20)
    room_password = TextInput(label="Пароль", placeholder="123", required=True, max_length=20)

    def __init__(self, guild_id, channel_id, tid, map_index, prefill=None):
        super().__init__(title=f"Комната BR — карта {map_index + 1}")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index
        # Discord не умеет default-значения в модалках — текущие
        # реквизиты показываем в placeholder (модалка только для орга).
        if prefill:
            rid = str(prefill.get("id") or "").strip()
            pwd = str(prefill.get("password") or "").strip()
            if rid:
                self.room_id.placeholder = f"сейчас: {rid}"[:100]
            if pwd:
                self.room_password.placeholder = f"сейчас: {pwd}"[:100]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if (not t or not (0 <= self.map_index < t.map_count)
                or t.phase not in (BRTournamentPhase.DRAFT,
                                   BRTournamentPhase.PLAYING,
                                   BRTournamentPhase.COLLECT)):
            await _respond(interaction,"Турнир не в фазе игр.", ephemeral=True)
            return
        rid = self.room_id.value.strip()
        pwd = self.room_password.value.strip()
        if not rid or not pwd:
            await _respond(interaction,"ID и пароль обязательны.", ephemeral=True)
            return
        async with get_br_lock(t):
            # перепроверяем после блокировки: фаза/карта могли измениться
            if (t.phase not in (BRTournamentPhase.DRAFT,
                                BRTournamentPhase.PLAYING,
                                BRTournamentPhase.COLLECT)
                    or not (0 <= self.map_index < t.map_count)):
                await _respond(interaction,"Турнир не в фазе игр.", ephemeral=True)
                return
            t.set_room(self.map_index, rid, pwd)
            br_store.set(t)
        # публичная карточка и новых сообщений не создаём — данные
        # комнаты видны только приватно через кнопки карт.
        await _respond(interaction,
            f"Данные комнаты для карты {self.map_index + 1} обновлены. "
            "Участники видят их по кнопке карты → «Данные комнаты».",
            ephemeral=True)


# ---------- ввод результатов ----------

class BREntryPanelView(View):
    PER_PAGE = 25

    def __init__(self, guild_id, channel_id, tid, map_index, page):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index
        self.page = page
        t = get_bound(guild_id, channel_id, tid)
        if not t:
            return
        n_slots = len(t.teams)
        start = page * self.PER_PAGE
        chunk = list(enumerate(t.teams))[start:start + self.PER_PAGE]
        options = []
        for i, s in chunk:
            e = t.get_team_stats_for_map(s, map_index)
            mark = ""
            if e is None:
                mark = " — пусто"
            elif e["dns"]:
                mark = f" — {absent_label(t)}"
            else:
                mark = f" — {e['place']} место, {e['kills']}🔥"
            options.append(discord.SelectOption(
                label=f"{i + 1}. {slot_label(s, i)}{mark}"[:100],
                value=str(i)))
        if options:
            self.add_item(EntrySlotSelect(guild_id, channel_id, tid, map_index, options))
        # навигация: назад в панель карты, обновить, листать карты
        self.add_item(EntryBackButton(guild_id, channel_id, tid, map_index))
        self.add_item(EntryRefreshButton(guild_id, channel_id, tid, map_index))
        self.add_item(EntryMapButton(guild_id, channel_id, tid, map_index, -1, "◀ Карта"))
        self.add_item(EntryMapButton(guild_id, channel_id, tid, map_index, +1, "Карта ▶"))
        # пакетный ввод по карте
        self.add_item(EntryBatchButton(guild_id, channel_id, tid, map_index))
        # пагинация слотов
        if page > 0:
            self.add_item(EntryPageButton(guild_id, channel_id, tid, map_index, page - 1, "◀"))
        if (page + 1) * self.PER_PAGE < n_slots:
            self.add_item(EntryPageButton(guild_id, channel_id, tid, map_index, page + 1, "▶"))


class EntryMapButton(Button):
    def __init__(self, guild_id, channel_id, tid, map_index, delta, label):
        super().__init__(style=discord.ButtonStyle.secondary, label=label,
                         custom_id=_cid("br_entry_map", guild_id, channel_id, tid, map_index, delta))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index
        self.delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await _respond(interaction,
                           "У вас нет доступа к управлению турниром",
                           ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t:
            await _respond(interaction,"Турнир не найден.", ephemeral=True)
            return
        new_map = self.map_index + self.delta
        if not (0 <= new_map < t.map_count):
            await _respond(interaction,"Нет такой карты.", ephemeral=True)
            return
        view = BREntryPanelView(self.guild_id, self.channel_id, self.tid, new_map, 0)
        await interaction.response.edit_message(view=view)


class EntryBackButton(Button):
    """Назад — приватная панель управления этой картой."""

    def __init__(self, guild_id, channel_id, tid, map_index):
        super().__init__(style=discord.ButtonStyle.secondary, label="← Карта",
                         custom_id=_cid("br_entry_back", guild_id, channel_id, tid, map_index))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await _respond(interaction,
                           "У вас нет доступа к управлению турниром",
                           ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if (not t or not (0 <= self.map_index < t.map_count)
                or t.phase not in (BRTournamentPhase.DRAFT,
                                   BRTournamentPhase.PLAYING,
                                   BRTournamentPhase.COLLECT)):
            await _respond(interaction,"Управление картой недоступно.",
                           ephemeral=True)
            return
        view = BRAdminMapPanelView(self.guild_id, self.channel_id, self.tid,
                                   self.map_index,
                                   editable=bool(t.results_editable))
        await interaction.response.edit_message(
            view=compose(text=admin_map_panel_text(t, self.map_index),
                         view=view))


class EntryRefreshButton(Button):
    """Обновить панель ввода актуальными данными слотов."""

    def __init__(self, guild_id, channel_id, tid, map_index):
        super().__init__(style=discord.ButtonStyle.secondary, label="Обновить",
                         custom_id=_cid("br_entry_refresh", guild_id, channel_id, tid, map_index))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await _respond(interaction,
                           "У вас нет доступа к управлению турниром",
                           ephemeral=True)
            return
        view = BREntryPanelView(self.guild_id, self.channel_id, self.tid,
                                self.map_index, 0)
        await interaction.response.edit_message(view=view)


class EntryPageButton(Button):
    def __init__(self, guild_id, channel_id, tid, map_index, page, label):
        super().__init__(style=discord.ButtonStyle.secondary, label=label,
                         custom_id=_cid("br_entry_pg", guild_id, channel_id, tid, map_index, page))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await _respond(interaction,
                           "У вас нет доступа к управлению турниром",
                           ephemeral=True)
            return
        view = BREntryPanelView(self.guild_id, self.channel_id, self.tid, self.map_index, self.page)
        await interaction.response.edit_message(view=view)


class EntrySlotSelect(Select):
    def __init__(self, guild_id, channel_id, tid, map_index, options):
        super().__init__(placeholder="Слот для ввода…", options=options,
                         custom_id=_cid("br_entry_slot", guild_id, channel_id, tid, map_index))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or not t.results_editable:
            await _respond(interaction,"Ввод недоступен.", ephemeral=True)
            return
        slot_idx = int(self.values[0])
        slot = t.teams[slot_idx]
        await interaction.response.send_modal(
            EntryResultModal(self.guild_id, self.channel_id, self.tid,
                             self.map_index, slot_idx, slot_label(slot, slot_idx),
                             absent=absent_label(t)))


class EntryResultModal(Modal):
    place = TextInput(label="Место (1–48)", placeholder="например 3", required=False, max_length=4)
    kills = TextInput(label="Киллы (0–50)", placeholder="например 7", required=False, max_length=3)
    dns = TextInput(label="Не участвовал(а)? (да/нет)", placeholder="нет",
                    required=False, max_length=20)

    def __init__(self, guild_id, channel_id, tid, map_index, slot_index,
                 slot_name, absent: str = "не участвовал(а)"):
        super().__init__(title=f"Результат: {slot_name}"[:45])
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index
        self.slot_index = slot_index
        self.absent = absent
        self.dns.label = f"{absent.capitalize()}? (да/нет)"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or not t.results_editable:
            await _respond(interaction,"Ввод недоступен.", ephemeral=True)
            return
        dns_raw = self.dns.value.strip().lower()
        is_dns = dns_raw in ("да", "dns", "yes", "d",
                             "не участвовал", "не участвовала", "ну", "н")
        is_clear = dns_raw in ("пусто", "очистить", "clear", "-")
        place = None
        if not is_dns and not is_clear:
            try:
                place = int(self.place.value.strip())
            except (ValueError, AttributeError):
                await _respond(interaction,"Укажите место, отметку неучастия или «пусто».", ephemeral=True)
                return
        kills_raw = (self.kills.value or "").strip()
        try:
            kills = int(kills_raw) if kills_raw else 0
        except ValueError:
            await _respond(interaction,
                f"Киллы должны быть целым числом 0–50 (получено «{kills_raw}») — запись не сохранена.",
                ephemeral=True)
            return
        ok = False
        err = ""
        async with get_br_lock(t):
            # актуальный объект и повторная проверка под блокировкой:
            # турнир мог быть отменён/удалён/заменён после открытия модалки
            t2 = br_store.get(self.guild_id, self.channel_id)
            if (t2 is None or str(t2.id) != str(self.tid)
                    or not t2.results_editable):
                err = "Ввод недоступен — состояние турнира изменилось."
            else:
                ok, err = t2.set_result_entry(
                    self.map_index, self.slot_index, place=place, kills=kills,
                    dns=is_dns, clear=is_clear, by=interaction.user.id)
                if ok:
                    br_store.set(t2)
                    t = t2
        if ok:
            await refresh_main_card(interaction, t)
            await _respond(interaction,
                f"Карта {self.map_index + 1}: результат слота записан.", ephemeral=True)
        else:
            await _respond(interaction,err, ephemeral=True)


class EntryBatchButton(Button):
    def __init__(self, guild_id, channel_id, tid, map_index):
        super().__init__(style=discord.ButtonStyle.secondary, label="Пакет по карте",
                         custom_id=_cid("br_entry_batch", guild_id, channel_id, tid, map_index))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or not t.results_editable:
            await _respond(interaction,"Ввод недоступен.", ephemeral=True)
            return
        await interaction.response.send_modal(BatchEntryModal(self.guild_id, self.channel_id, self.tid, self.map_index))


class BatchEntryModal(Modal):
    data = TextInput(
        label="Строки: №слота место киллы | №слот НУ",
        placeholder="1 1 12\n2 2 8\n3 НУ",
        required=True, max_length=1800,
        style=discord.TextStyle.paragraph)

    def __init__(self, guild_id, channel_id, tid, map_index):
        super().__init__(title="Пакетный ввод по карте")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid
        self.map_index = map_index

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or not t.results_editable:
            await _respond(interaction,"Ввод недоступен.", ephemeral=True)
            return
        entries = []
        parse_errors = []
        for ln, line in enumerate(self.data.value.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            parts = line.replace("|", " ").split()
            try:
                slot_no = int(parts[0])
            except (ValueError, IndexError):
                parse_errors.append(f"строка {ln}: «{line}» — нет номера слота")
                continue
            absent_tokens = ("DNS", "НУ", "Н", "НЕУЧАСТВОВАЛ", "НЕ УЧАСТВОВАЛ",
                             "НЕ УЧАСТВОВАЛА", "DID NOT PLAY", "DNP")
            if len(parts) >= 2 and (parts[1].upper() in absent_tokens
                                   or parts[1] in ("-", "ПУСТО", "ОЧИСТИТЬ", "CLEAR")):
                if parts[1] == "-" or parts[1].upper() in ("ПУСТО", "ОЧИСТИТЬ", "CLEAR"):
                    entries.append({"slot": slot_no - 1, "clear": True})
                else:
                    entries.append({"slot": slot_no - 1, "dns": True, "kills": 0})
                continue
            if len(parts) < 3:
                parse_errors.append(f"строка {ln}: «{line}» — нужно «слот место киллы», «слот НУ» или «слот -»")
                continue
            try:
                entries.append({
                    "slot": slot_no - 1,
                    "place": int(parts[1]),
                    "kills": int(parts[2]),
                })
            except ValueError:
                parse_errors.append(f"строка {ln}: «{line}» — место/киллы должны быть числами")
        if parse_errors:
            await _respond(interaction,
                "Ошибки пакета (ничего не сохранено):\n" + "\n".join(parse_errors[:15]),
                ephemeral=True)
            return
        ok = False
        errors: list[str] = []
        async with get_br_lock(t):
            # актуальный объект и повторная проверка под блокировкой:
            # турнир мог быть отменён/удалён/заменён после открытия модалки
            t2 = br_store.get(self.guild_id, self.channel_id)
            if (t2 is None or str(t2.id) != str(self.tid)
                    or not t2.results_editable):
                errors = ["Ввод недоступен — состояние турнира изменилось."]
            else:
                ok, errors = t2.set_results_batch(
                    self.map_index, entries, by=interaction.user.id)
                if ok:
                    br_store.set(t2)
                    t = t2
        if ok:
            await refresh_main_card(interaction, t)
            await _respond(interaction,
                f"Карта {self.map_index + 1}: пакет из {len(entries)} строк сохранён.",
                ephemeral=True)
        else:
            await _respond(interaction,
                "Ошибки пакета (ничего не сохранено):\n" + "\n".join(errors[:15]),
                ephemeral=True)


class PreviewResultsButton(Button):
    def __init__(self, guild_id, channel_id, tid):
        super().__init__(style=discord.ButtonStyle.secondary, label="Предпросмотр итогов",
                         custom_id=_cid("br_preview", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t or t.phase not in (BRTournamentPhase.PLAYING,
                                    BRTournamentPhase.COLLECT):
            await _respond(interaction,"Нет данных для предпросмотра.", ephemeral=True)
            return
        filled, expected = t.results_status()
        results = t.calculate_final_results()
        lines = [f"Заполнено: {filled}/{expected}"]
        for r in results[:15]:
            lines.append(f"{r['rank']}. {r['name']} — {r['total_points']} очк.")
        if len(results) > 15:
            lines.append(f"… и ещё {len(results) - 15}")
        if getattr(t, "migration_notes", None):
            lines.append("")
            lines.append("**Замечания миграции (сверьте вручную):**")
            lines.extend(f"• {n}" for n in t.migration_notes[:10])
        view = PublishResultsView(self.guild_id, self.channel_id, self.tid)
        await _respond(interaction,
            "**Приватный предпросмотр итогов:**\n" + "\n".join(lines),
            view=view, ephemeral=True)


class PublishResultsView(View):
    def __init__(self, guild_id, channel_id, tid):
        super().__init__(timeout=300)
        self.add_item(PublishResultsButton(guild_id, channel_id, tid))


class PublishResultsButton(Button):
    """Публикация итогов — восстановимая после сбоя Discord/рестарта.

    Поток: под блокировкой заморозить снимок (publish_pending, правки
    закрываются) → отредактировать карточку финальным embed+кнопками
    → под блокировкой закоммитить снимок в RESULTS/архив. При ошибке
    Discord состояние остаётся COLLECT+pending — повтор использует тот
    же снимок; после рестарта resume_pending_publish доводит публикацию
    до конца. Второго сообщения не создаётся — правится message_id.
    """

    def __init__(self, guild_id, channel_id, tid):
        super().__init__(style=discord.ButtonStyle.success, label="Опубликовать итоги",
                         custom_id=_cid("br_publish", guild_id, channel_id, tid))
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tid = tid

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not is_org(interaction.user.id):
            await _respond(interaction,"Только владелец/админ.", ephemeral=True)
            return
        t = get_bound(self.guild_id, self.channel_id, self.tid)
        if not t:
            await _respond(interaction,STALE_MSG, ephemeral=True)
            return
        if t.phase in (BRTournamentPhase.RESULTS, BRTournamentPhase.COMPLETE) or t.results_published:
            await _respond(interaction,"Итоги уже опубликованы.", ephemeral=True)
            return
        if t.phase not in (BRTournamentPhase.PLAYING, BRTournamentPhase.COLLECT):
            await _respond(interaction,
                "Итоги уже опубликованы или турнир не готов.", ephemeral=True)
            return

        # 1) Под блокировкой: свежий объект, проверка ID/фазы, снимок.
        async with get_br_lock_key(self.guild_id, self.channel_id, self.tid):
            t2 = br_store.get(self.guild_id, self.channel_id)
            if t2 is None or str(t2.id) != str(self.tid):
                await _respond(interaction,STALE_MSG, ephemeral=True)
                return
            if (t2.phase not in (BRTournamentPhase.PLAYING,
                                 BRTournamentPhase.COLLECT)
                    or t2.results_published):
                await _respond(interaction,"Итоги уже опубликованы.", ephemeral=True)
                return
            prev_pending = t2.publish_pending
            prev_pre_phase = t2.pre_publish_phase
            ok, err = t2.freeze_results()
            if not ok:
                await _respond(interaction,err, ephemeral=True)
                return
            try:
                br_store.set(t2)  # сохраняем pending-снимок
            except Exception as exc:
                # откат freeze: все изменённые поля возвращаем — снимок
                # не должен остаться замороженным в памяти, если на
                # диске его нет; повтор начнёт чисто
                t2.publish_pending = prev_pending
                t2.pre_publish_phase = prev_pre_phase
                logger.error("BR publish: save pending failed: %s", exc)
                await _respond(interaction,
                    "Не удалось сохранить снимок итогов — публикация не начата.",
                    ephemeral=True)
                return
            t = t2

        # 1.5) Турнир мог быть отменён/удалён между freeze и правкой
        # карточки — не воскрешаем финальный вид устаревшего состояния.
        t_now = br_store.get(self.guild_id, self.channel_id)
        if (t_now is None or str(t_now.id) != str(self.tid)
                or t_now.phase not in (BRTournamentPhase.PLAYING,
                                       BRTournamentPhase.COLLECT)
                or t_now.publish_pending is None
                or t_now.results_published):
            await _respond(interaction,
                "Публикация прервана: состояние турнира изменилось.",
                ephemeral=True)
            return

        # 2) Discord-карточка с финальным видом (без Комната/Управление).
        #    Сбой → состояние COLLECT+pending, повтор с тем же снимком.
        edited = await _edit_final_card(t, interaction.guild)
        if not edited:
            await _respond(interaction,
                "Не удалось обновить карточку в Discord — итоги НЕ опубликованы. "
                "Снимок сохранён, повторите «Опубликовать итоги» или перезапустите бота.",
                ephemeral=True)
            return

        # 3) Коммит под блокировкой: снимок → RESULTS → архив.
        #    get_bound находит и архивный турнир — если фазу уже сменили
        #    (отмена/параллельный коммит), карточку возвращаем к
        #    актуальному виду, чтобы Discord и архив совпадали.
        try:
            async with get_br_lock_key(self.guild_id, self.channel_id, self.tid):
                t3 = get_bound(self.guild_id, self.channel_id, self.tid)
                if t3 is None:
                    await _respond(interaction,STALE_MSG, ephemeral=True)
                    return
                if (t3.phase in (BRTournamentPhase.PLAYING,
                                 BRTournamentPhase.COLLECT)
                        and t3.publish_pending is not None
                        and not t3.results_published):
                    try:
                        t3.commit_publish()
                        br_store.set(t3)  # архивирует в историю
                    except Exception:
                        # сбой записи: память откачена store'ом, объект — здесь
                        t3.uncommit_publish()
                        raise
                    t = t3
                elif t3.phase in (BRTournamentPhase.RESULTS, BRTournamentPhase.COMPLETE):
                    # параллельный клик уже закоммитил — идемпотентно
                    t = t3
                else:
                    # фазу сменили (например, отмена) — карточка должна
                    # показывать актуальный статус, а не финал
                    await refresh_main_card(interaction, t3)
                    await _respond(interaction,
                        "Публикация прервана: фаза турнира изменилась.", ephemeral=True)
                    return
        except Exception as exc:
            logger.error("BR publish: commit failed: %s", exc)
            await _respond(interaction,
                "Карточка обновлена, но сохранение не удалось — повторите "
                "«Опубликовать итоги» (снимок не изменится).", ephemeral=True)
            return
        try:
            await strip_tournament_roles(interaction.guild, t)
        except Exception as exc:
            logger.warning("BR publish strip roles: %s", exc)
        await _respond(interaction,"Итоги опубликованы.", ephemeral=True)


async def _edit_final_card(t: "BRTournament", guild) -> bool:
    """Отредактировать единую карточку финальным embed и финальными
    кнопками (результаты по картам + «Мой слот», без Комната/Управление).
    True — Discord edit прошёл."""
    try:
        if not t.message_id or guild is None:
            return False
        channel = guild.get_channel(t.channel_id)
        if channel is None:
            return False
        message = await channel.fetch_message(t.message_id)
        embed = final_embed(t)  # calculate_final_results → снимок
        view = BRCardView(t.guild_id, t.channel_id, t,
                          as_phase=BRTournamentPhase.RESULTS)
        await message.edit(view=compose(embed, view))
        return True
    except Exception as exc:
        logger.warning("BR publish: Discord edit failed: %s", exc)
        return False


async def resume_pending_publish(tournament: "BRTournament", client=None) -> bool:
    """Довести отложенную публикацию до конца (после сбоя/рестарта).

    Если турнир в PLAYING/COLLECT с publish_pending — повторяет тот же
    снимок: edit карточки финальным видом, затем commit в архив.
    Идемпотентно.
    """
    if (tournament.phase not in (BRTournamentPhase.PLAYING,
                                 BRTournamentPhase.COLLECT)
            or tournament.publish_pending is None):
        return False
    # Актуальность до правки карточки: переданный объект мог устареть.
    # Турнир удалён (purge) — карточку не воскрешаем. Смена фазы
    # (отмена) обрабатывается после edit: карточка возвращается к
    # актуальному виду в commit-стадии.
    t_check = br_store.get(tournament.guild_id, tournament.channel_id)
    if t_check is None or str(t_check.id) != str(tournament.id):
        return False
    try:
        if client is not None:
            guild = client.get_guild(tournament.guild_id)
        else:
            guild = None
        if guild is None:
            return False
        if not await _edit_final_card(t_check, guild):
            return False
        async with get_br_lock_key(tournament.guild_id, tournament.channel_id, tournament.id):
            t2 = br_store.get(tournament.guild_id, tournament.channel_id)
            can_commit = (t2 is not None and str(t2.id) == str(tournament.id)
                          and t2.phase in (BRTournamentPhase.PLAYING,
                                           BRTournamentPhase.COLLECT)
                          and t2.publish_pending is not None)
            if can_commit:
                try:
                    t2.commit_publish()
                    br_store.set(t2)
                except Exception:
                    # сбой записи: объект откатываем в COLLECT+pending —
                    # следующий запуск/повтор доведёт публикацию до конца
                    t2.uncommit_publish()
                    raise
                try:
                    await strip_tournament_roles(guild, t2)
                except Exception as exc:
                    logger.warning("BR resume strip roles: %s", exc)
                return True
            # Между edit финальной карточки и commit фазу сменили
            # (отмена) или турнир удалили (purge) — возвращаем карточку
            # к актуальному виду, чтобы Discord не показывал итоги
            # отменённого турнира. t2 is None (purge) — карточка уже
            # заменена заглушкой «удалён», устаревший объект не используем.
            if t2 is not None:
                try:
                    await edit_main_card(t2, client=client)
                except Exception as exc:
                    logger.warning("BR resume: restore card failed: %s", exc)
            return False
        return False
    except Exception as exc:
        logger.warning("BR resume_pending_publish failed: %s", exc)
        return False


# ---------- совместимость ----------

# Старые имена, на которые могут ссылаться внешние вызовы.
BRRegistrationView = BRCardView
