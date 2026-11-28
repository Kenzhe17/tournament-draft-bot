"""Views for Battle Royale tournament (Solo / Duo / Squad)."""

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from utils.design import compose, text_card, smart_view

from discord.ui import Modal, TextInput, View, Button, Select

from config import (ADMIN_USER_IDS, BOT_OWNER_ID, BR_ROOM_CHANNEL_ID,
                    NOTIFICATION_CHANNEL_ID,
                    replace_emojis, get_emoji)
from models.br_tournament import (BRFormat, BRTournamentPhase, MAP_NAMES_RU,
                                  br_format_rules)
from storage.br_store import br_store

if TYPE_CHECKING:
    from models.br_tournament import BRTournament

logger = logging.getLogger(__name__)


# ---------- locks & helpers ----------

_BR_LOCKS: dict[str, asyncio.Lock] = {}


def get_br_lock(tournament: "BRTournament") -> asyncio.Lock:
    key = tournament.id or f"{tournament.guild_id}:{tournament.channel_id}"
    if key not in _BR_LOCKS:
        _BR_LOCKS[key] = asyncio.Lock()
    return _BR_LOCKS[key]


def get_tournament(guild_id: int, channel_id: int) -> "BRTournament | None":
    return br_store.get_any(guild_id, channel_id)


def is_org(user_id: int) -> bool:
    return user_id == BOT_OWNER_ID or user_id in ADMIN_USER_IDS


BR_NUM_EMOJIS = [
    "num_1", "num_2", "num_3", "num_4", "num_5", "num_6",
    "num_7", "num_8", "num_9", "num_ten", "num_eleven", "num_twelve",
]

def _num(i: int) -> str:
    if i < len(BR_NUM_EMOJIS):
        return get_emoji(BR_NUM_EMOJIS[i])
    return f"{i + 1}."


def clean_user_name(user) -> str:
    from utils.cosmetics import clean_tournament_nickname
    return (
        clean_tournament_nickname(user.display_name)
        or clean_tournament_nickname(user.name)
        or f"Игрок-{user.id % 10000:04d}"
    )


def slot_members(tournament: "BRTournament", slot: dict) -> list[int]:
    return [int(i) for i in (slot.get("member_ids") or [])]


def slot_member_line(tournament: "BRTournament", slot: dict) -> str:
    """Участники слота через упоминания — только для BR-анонса комнаты."""
    ids = slot_members(tournament, slot)
    if not ids:
        # fallback для legacy-слотов без member_ids
        id_map = {p.get("name"): p.get("id") for p in tournament.players}
        names = [slot.get("captain", "")] + list(slot.get("players", []))
        parts = []
        for nm in names:
            uid = id_map.get(nm)
            parts.append(f"<@{uid}>" if uid and uid > 0 else nm)
        return " ".join(dict.fromkeys(parts)) or "*"
    return " ".join(f"<@{uid}>" for uid in ids)


def _player_name(tournament: "BRTournament", uid: int) -> str:
    dn = None
    for p in tournament.players:
        if p.get("id") == uid:
            dn = p.get("name")
            break
    return dn or f"User_{uid}"


def slot_member_names(tournament: "BRTournament", slot: dict) -> str:
    """Имена участников без упоминаний — для публичных карточек."""
    ids = slot_members(tournament, slot)
    dn = slot.get("display_names") or {}
    if ids:
        parts = [dn.get(str(i)) or _player_name(tournament, i) for i in ids]
    else:
        parts = [slot.get("captain", "")] + [
            n for n in slot.get("players", []) if n]
    return ", ".join(dict.fromkeys(p for p in parts if p)) or "*"


def slot_label(slot: dict, index: int) -> str:
    return slot.get("name") or f"Слот {index + 1}"


def format_mode_label(tournament: "BRTournament") -> str:
    fmt = br_format_rules(tournament.format)["label"]
    if tournament.mode == "team":
        return f"{fmt} · Команды"
    if tournament.mode == "random":
        return f"{fmt} · Жеребьёвка"
    return f"{fmt} · Индивидуально"


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
        f"{join}"
    )


def _clip(text: str, limit: int = 3400) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…"


def slots_public_lines(tournament: "BRTournament") -> list[str]:
    lines = []
    for i, slot in enumerate(tournament.filled_slots()):
        pending = " ⏳" if slot.get("pending_invitee_id") else ""
        lines.append(f"{_num(i)} **{slot_label(slot, i)}**{pending}\n"
                     f"┗ {slot_member_names(tournament, slot)}")
    return lines


def public_embed(tournament: "BRTournament") -> discord.Embed:
    """Публичная карточка BR по текущей фазе."""
    phase = tournament.phase
    title_fmt = format_mode_label(tournament)

    if phase == BRTournamentPhase.REGISTRATION:
        rules = registration_rules_text(tournament)
        if tournament.mode == "random" or tournament.is_solo:
            count = (f"Участников: {len(tournament.filled_slots())}/{tournament.max_slots}"
                     if tournament.is_solo else
                     f"Игроков в пуле: {len(tournament.players)}/{tournament.max_players}")
            body = _clip("\n".join(slots_public_lines(tournament))) or " *"
        else:
            n = len(tournament.filled_slots())
            count = f"Составов: {n}/{tournament.max_slots}"
            body = _clip("\n".join(slots_public_lines(tournament))) or " *"
        return discord.Embed(
            title=f" {replace_emojis('winner')} BATTLE ROYALE CUP | {title_fmt} | Регистрация",
            description=(
                f" **Организатор:** <@{tournament.organizer_id}>\n\n"
                f" **Информация:**\n{rules}\n\n"
                f" **{count}:**\n{body}\n\n"
                f"Используйте кнопки ниже."
            ),
            color=discord.Color.from_rgb(69, 69, 69),
        )

    if phase == BRTournamentPhase.DRAFT:
        body = _clip("\n".join(slots_public_lines(tournament))) or " *"
        return discord.Embed(
            title=f" {replace_emojis('winner')} BATTLE ROYALE CUP | {title_fmt} | Составы",
            description=(
                f" **Организатор:** <@{tournament.organizer_id}>\n\n"
                f" **Фаза:** Составы определены\n"
                f" Карт: {tournament.map_count}\n\n"
                f" **Составы:**\n{body}\n\n"
                f"Капитан может один раз назвать свой состав."
            ),
            color=discord.Color.from_rgb(69, 69, 69),
        )

    if phase == BRTournamentPhase.PLAYING:
        map_ru = MAP_NAMES_RU.get(tournament.current_map_name(), tournament.current_map_name())
        played = sum(1 for f in tournament.map_played if f)
        body = _clip("\n".join(slots_public_lines(tournament)))
        return discord.Embed(
            title=f" {replace_emojis('winner')} BATTLE ROYALE CUP | {title_fmt}",
            description=(
                f" **Карта {tournament.current_map_index + 1} из {tournament.map_count} · {map_ru}**\n\n"
                f" **Статус:** Идёт игра\n"
                f" Сыграно карт: {played}/{tournament.map_count}\n\n"
                f" **Составы:**\n{body or ' *'}"
            ),
            color=discord.Color.from_rgb(69, 69, 69),
        )

    if phase == BRTournamentPhase.COLLECT:
        return discord.Embed(
            title=f" {replace_emojis('winner')} BATTLE ROYALE CUP | {title_fmt}",
            description=(
                " **Результаты проверяются**\n\n"
                f"Все {tournament.map_count} карт сыграны. "
                "Ведущий собирает результаты — промежуточные данные скрыты."
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


def _slot_display_names(tournament: "BRTournament", slot: dict) -> str:
    return slot_member_names(tournament, slot)


def final_embed(tournament: "BRTournament", page: int | None = None) -> discord.Embed:
    """Опубликованный итог: победитель, топ-3, таблица, «По картам»."""
    results = tournament.calculate_final_results()
    if not results:
        return discord.Embed(title="BR Cup — результаты", description="Нет данных.")

    winner = results[0]
    wslot = tournament.teams[winner["slot_index"]]
    lines = [
        f" **Победитель:** {winner['name']}",
        f" {', '.join(_player_name(tournament, i) for i in winner['member_ids']) or _slot_display_names(tournament, wslot)}",
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

    # страница по картам: page = индекс карты
    map_idx = page
    map_ru = MAP_NAMES_RU.get(tournament.get_map_name_for_index(map_idx), "")
    key = tournament.get_stats_key(map_idx)
    rows = []
    for slot in tournament.teams:
        e = (slot.get("stats") or {}).get(key)
        from models.br_tournament import normalize_entry
        ne = normalize_entry(e)
        if ne is None:
            rows.append((999, f"— {slot_label(slot, slot.get('index', 0))} — нет данных"))
        elif ne["dns"]:
            rows.append((500, f"DNS {slot_label(slot, slot.get('index', 0))} (0:{ne['kills']})"))
        else:
            rows.append((ne["place"], f"{ne['place']}. {slot_label(slot, slot.get('index', 0))} — {ne['kills']} киллов"))
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


async def refresh_main_card(interaction: discord.Interaction, t: "BRTournament") -> None:
    """Перерисовать основную публичную карточку турнира."""
    try:
        if not t.message_id:
            return
        channel = interaction.guild.get_channel(t.channel_id)
        if not channel:
            return
        message = await channel.fetch_message(t.message_id)
        embed = public_embed(t)
        await message.edit(view=compose(embed, BRCardView(t.guild_id, t.channel_id, t)))
    except Exception as exc:
        logger.warning("BR refresh_main_card failed: %s", exc)


async def sync_public_card(tournament: "BRTournament", client=None) -> None:
    """Перерисовать публичную карточку турнира (после рестарта и т.п.)."""
    if not tournament.message_id:
        return
    try:
        if client is None:
            return
        guild = client.get_guild(tournament.guild_id)
        channel = guild.get_channel(tournament.channel_id) if guild else None
        if not channel:
            return
        message = await channel.fetch_message(tournament.message_id)
        embed, view = await render_public(tournament)
        await message.edit(view=compose(embed, view))
    except Exception as exc:
        logger.warning("BR sync_public_card failed: %s", exc)


async def strip_tournament_roles(guild, tournament: "BRTournament") -> None:
    """Снять BR-роль у участников, если она не нужна другому активному BR."""
    role = guild.get_role(tournament.role_id)
    if not role:
        return
    busy: set[int] = set()
    for other in br_store.get_by_guild(tournament.guild_id):
        if other.id == tournament.id:
            continue
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
    """Публичная карточка BR турнира (persistent)."""

    def __init__(self, guild_id: int, channel_id: int,
                 tournament: "BRTournament" = None):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        gid, cid = guild_id, channel_id

        phase = tournament.phase if tournament else BRTournamentPhase.REGISTRATION
        mode = tournament.mode if tournament else "random"
        fmt = tournament.format if tournament else BRFormat.SQUAD

        if phase == BRTournamentPhase.REGISTRATION:
            if mode == "individual":
                self.add_item(JoinLeaveButton(gid, cid, "join"))
            elif mode == "random":
                self.add_item(JoinLeaveButton(gid, cid, "join"))
            elif fmt == BRFormat.DUO:
                self.add_item(DuoInviteButton(gid, cid))
                self.add_item(MyInvitesButton(gid, cid))
            elif fmt == BRFormat.SQUAD:
                self.add_item(SquadRegisterButton(gid, cid))
            self.add_item(JoinLeaveButton(gid, cid, "leave"))
        elif phase == BRTournamentPhase.DRAFT and mode == "random":
            self.add_item(RenameSlotButton(gid, cid))
        elif phase in (BRTournamentPhase.PLAYING, BRTournamentPhase.COLLECT,
                       BRTournamentPhase.DRAFT, BRTournamentPhase.REGISTRATION):
            self.add_item(MySlotButton(gid, cid))
        elif phase in (BRTournamentPhase.RESULTS, BRTournamentPhase.COMPLETE):
            self.add_item(ResultsPageButton(gid, cid, -1))
            self.add_item(MySlotButton(gid, cid))
            self.add_item(ResultsPageButton(gid, cid, 1))

        if phase not in (BRTournamentPhase.RESULTS, BRTournamentPhase.COMPLETE,
                         BRTournamentPhase.CANCELLED):
            self.add_item(OrgMenuButton(gid, cid))


class JoinLeaveButton(Button):
    def __init__(self, guild_id: int, channel_id: int, kind: str):
        super().__init__(
            style=discord.ButtonStyle.primary if kind == "join" else discord.ButtonStyle.secondary,
            label="Участвовать" if kind == "join" else "Покинуть",
            custom_id=f"br_{kind}:{guild_id}:{channel_id}",
        )
        self.kind = kind
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        t = get_tournament(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message("Регистрация закрыта.", ephemeral=True)
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
        await interaction.response.send_message(
            vac_service.vac_blocked_text(ts, reason=vac.get("reason"), level=vac.get("level")),
            ephemeral=True)
        return
    name = clean_user_name(interaction.user)
    ok, err = t.register_player(name, interaction.user.id)
    if not ok:
        await interaction.response.send_message(err or "Не удалось зарегистрироваться.", ephemeral=True)
        return
    br_store.set(t)
    embed = public_embed(t)
    await interaction.response.edit_message(view=compose(embed, BRCardView(t.guild_id, t.channel_id, t)))
    await interaction.followup.send("Вы зарегистрированы.", ephemeral=True)


async def _do_leave(interaction: discord.Interaction, t: "BRTournament") -> None:
    if not t.remove_player(interaction.user.id):
        await interaction.response.send_message("Вы не зарегистрированы.", ephemeral=True)
        return
    br_store.set(t)
    embed = public_embed(t)
    await interaction.response.edit_message(view=compose(embed, BRCardView(t.guild_id, t.channel_id, t)))
    await interaction.followup.send("Вы вышли из турнира.", ephemeral=True)


class DuoInviteButton(Button):
    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(style=discord.ButtonStyle.primary, label="Созвать пару",
                         custom_id=f"br_duo_invite:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        t = get_tournament(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message("Регистрация закрыта.", ephemeral=True)
            return
        await interaction.response.send_modal(DuoInviteModal(self.guild_id, self.channel_id))


class DuoInviteModal(Modal):
    invitee = TextInput(label="Напарник (упоминание или ID)", placeholder="@user или 1234567890",
                        required=True, max_length=40)

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(title="Приглашение в пару")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        t = get_tournament(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message("Регистрация закрыта.", ephemeral=True)
            return
        raw = self.invitee.value.strip().strip("<@!>")
        try:
            invitee_id = int(raw)
        except ValueError:
            await interaction.response.send_message("Укажите ID или упоминание игрока.", ephemeral=True)
            return
        member = interaction.guild.get_member(invitee_id)
        if member is None:
            await interaction.response.send_message("Игрок не найден на сервере.", ephemeral=True)
            return
        async with get_br_lock(t):
            ok, err, idx = t.create_duo_invite(
                interaction.user.id, clean_user_name(interaction.user), invitee_id)
            if not ok:
                await interaction.response.send_message(err, ephemeral=True)
                return
            br_store.set(t)
        slot = t.teams[idx]
        mate_name = member.display_name if member else str(invitee_id)
        await interaction.response.send_message(
            f"Приглашение отправлено {mate_name}. Пара «{slot_label(slot, idx)}» "
            "занимает место после подтверждения напарником (кнопка «Мои заявки»).",
            ephemeral=True)
        try:
            embed = public_embed(t)
            await interaction.message.edit(view=compose(embed, BRCardView(t.guild_id, t.channel_id, t)))
        except Exception:
            pass


class MyInvitesButton(Button):
    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="Мои заявки",
                         custom_id=f"br_my_invites:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        t = get_tournament(self.guild_id, self.channel_id)
        if not t:
            await interaction.response.send_message("Турнир не найден.", ephemeral=True)
            return
        uid = interaction.user.id
        invites = [(i, s) for i, s in enumerate(t.teams)
                   if s.get("pending_invitee_id") == uid]
        if not invites:
            await interaction.response.send_message("Нет входящих заявок.", ephemeral=True)
            return
        view = View(timeout=120)
        for i, s in invites:
            cap_name = (s.get("display_names") or {}).get(str(s.get("captain_id")), "")
            cap_txt = f" · {cap_name}" if cap_name else ""
            view.add_item(InviteRespondButton(self.guild_id, self.channel_id,
                                              s.get("uid", str(i)), True,
                                              f"Принять: {slot_label(s, i)}{cap_txt}"[:80]))
            view.add_item(InviteRespondButton(self.guild_id, self.channel_id,
                                              s.get("uid", str(i)), False,
                                              f"Отклонить: {slot_label(s, i)}"[:80],
                                              style=discord.ButtonStyle.danger))
        await interaction.response.send_message("Входящие заявки в пары:", view=view, ephemeral=True)


class InviteRespondButton(Button):
    def __init__(self, guild_id, channel_id, slot_uid, accept, label, style=None):
        super().__init__(style=style or discord.ButtonStyle.success, label=label[:80],
                         custom_id=f"br_invite_{'ok' if accept else 'no'}:{guild_id}:{channel_id}:{slot_uid}")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.slot_uid = slot_uid
        self.accept = accept

    async def callback(self, interaction: discord.Interaction) -> None:
        t = get_tournament(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message("Регистрация закрыта.", ephemeral=True)
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
            await interaction.response.send_message("Заявка не найдена или устарела.", ephemeral=True)
            return
        idx, slot = found
        async with get_br_lock(t):
            ok, msg = t.respond_duo_invite(
                idx, interaction.user.id, clean_user_name(interaction.user), self.accept)
            if t.phase == BRTournamentPhase.REGISTRATION:
                br_store.set(t)
        if not ok:
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await interaction.response.send_message(msg, ephemeral=True)
        await refresh_main_card(interaction, t)


class SquadRegisterButton(Button):
    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(style=discord.ButtonStyle.primary, label="Зарегистрировать отряд",
                         custom_id=f"br_squad_reg:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.team_store import team_store
        from config import BR_TEAM_CHANNEL_ID

        t = get_tournament(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message("Регистрация закрыта.", ephemeral=True)
            return
        if t.format != BRFormat.SQUAD or t.mode != "team":
            await interaction.response.send_message("Недоступно для этого формата.", ephemeral=True)
            return

        team = team_store.get_by_captain(interaction.user.id)
        if not team:
            await interaction.response.send_message(
                f"Вы не капитан команды! Создайте отряд из 4 через `/team create` в <#{BR_TEAM_CHANNEL_ID}>.",
                ephemeral=True)
            return
        if len(team.members) != t.slot_size:
            await interaction.response.send_message(
                f"В отряде {len(team.members)}/{t.slot_size} игроков.", ephemeral=True)
            return

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
            await interaction.response.send_message(
                "Регистрация невозможна: активен VAC у " + ", ".join(names) + ".",
                ephemeral=True)
            return

        display_names = {}
        for uid in team.members:
            m = interaction.guild.get_member(uid)
            display_names[uid] = clean_user_name(m) if m else f"User_{uid}"

        async with get_br_lock(t):
            ok, err = t.register_team_slot(
                team.team_id, team.team_name, team.captain_id,
                list(team.members), display_names)
            if not ok:
                await interaction.response.send_message(err, ephemeral=True)
                return
            br_store.set(t)

        await interaction.response.send_message(
            f"Отряд «{team.team_name}» зарегистрирован.", ephemeral=True)
        try:
            embed = public_embed(t)
            await interaction.message.edit(view=compose(embed, BRCardView(t.guild_id, t.channel_id, t)))
        except Exception:
            pass


class MySlotButton(Button):
    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="Мой слот",
                         custom_id=f"br_my_slot:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        t = get_tournament(self.guild_id, self.channel_id)
        if not t:
            await interaction.response.send_message("Турнир не найден.", ephemeral=True)
            return
        found = t.find_slot_by_user(interaction.user.id)
        if not found and t.phase == BRTournamentPhase.REGISTRATION:
            in_pool = any(p.get("id") == interaction.user.id for p in t.players)
            if in_pool:
                await interaction.response.send_message(
                    "Вы в пуле жеребьёвки — состав определится после старта.", ephemeral=True)
                return
        if not found:
            await interaction.response.send_message("Вы не зарегистрированы в турнире.", ephemeral=True)
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
                    mark = "DNS" if e["dns"] else f"место {e['place']}, {e['kills']} киллов"
                    lines.append(f"Карта {m + 1} ({MAP_NAMES_RU.get(t.get_map_name_for_index(m), '')}): {mark}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class RenameSlotButton(Button):
    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="Назвать состав",
                         custom_id=f"br_rename:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        t = get_tournament(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.DRAFT:
            await interaction.response.send_message("Переименование доступно только на этапе составов.", ephemeral=True)
            return
        found = t.find_slot_by_user(interaction.user.id)
        if not found:
            await interaction.response.send_message("Вы не в составе.", ephemeral=True)
            return
        idx, slot = found
        if slot.get("captain_id") != interaction.user.id:
            await interaction.response.send_message("Только капитан может назвать состав.", ephemeral=True)
            return
        if not t.team_rename_remaining.get(idx, False):
            await interaction.response.send_message("Название уже использовано.", ephemeral=True)
            return
        await interaction.response.send_modal(RenameSlotModal(self.guild_id, self.channel_id))


class RenameSlotModal(Modal):
    name = TextInput(label="Название состава", required=True, max_length=32)

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(title="Название состава")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        t = get_tournament(self.guild_id, self.channel_id)
        if not t:
            await interaction.response.send_message("Турнир не найден.", ephemeral=True)
            return
        found = t.find_slot_by_user(interaction.user.id)
        if not found:
            await interaction.response.send_message("Вы не в составе.", ephemeral=True)
            return
        idx, slot = found
        if t.rename_slot(idx, self.name.value.strip(), interaction.user.id):
            br_store.set(t)
            await interaction.response.send_message(f"Состав переименован в «{self.name.value.strip()}».", ephemeral=True)
            await refresh_main_card(interaction, t)
        else:
            await interaction.response.send_message("Не удалось переименовать (уже использовано или нет прав).", ephemeral=True)


class ResultsPageButton(Button):
    def __init__(self, guild_id: int, channel_id: int, delta: int):
        super().__init__(style=discord.ButtonStyle.secondary,
                         label="◀" if delta < 0 else "▶",
                         custom_id=f"br_rpage:{guild_id}:{channel_id}:{delta}")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        t = get_tournament(self.guild_id, self.channel_id)
        if not t or t.phase not in (BRTournamentPhase.RESULTS, BRTournamentPhase.COMPLETE):
            await interaction.response.send_message("Результаты недоступны.", ephemeral=True)
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
    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="Управление",
                         custom_id=f"br_org_menu:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await interaction.response.send_message(
                "Только владелец/админ может управлять BR.", ephemeral=True)
            return
        t = get_tournament(self.guild_id, self.channel_id)
        if not t:
            await interaction.response.send_message("Турнир не найден.", ephemeral=True)
            return
        view = BRManagementMenuView(self.guild_id, self.channel_id)
        await interaction.response.send_message(
            view=compose(text="Орг-меню BR:", view=view), ephemeral=True)


class BRManagementMenuView(View):
    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        t = br_store.get(guild_id, channel_id) or br_store.get_any(guild_id, channel_id)
        if not t:
            return
        gid, cid = guild_id, channel_id
        if t.phase == BRTournamentPhase.REGISTRATION:
            self.add_item(KickSlotButton(gid, cid))
            self.add_item(DrawButton(gid, cid))
            self.add_item(CancelTournamentButton(gid, cid))
        elif t.phase == BRTournamentPhase.DRAFT:
            self.add_item(BeginGamesButton(gid, cid))
            self.add_item(CancelTournamentButton(gid, cid))
        elif t.phase == BRTournamentPhase.PLAYING:
            self.add_item(RoomButton(gid, cid))
            last = t.current_map_index >= t.map_count - 1
            self.add_item(MapPlayedButton(gid, cid, last=last))
            self.add_item(CancelTournamentButton(gid, cid))
        elif t.phase == BRTournamentPhase.COLLECT:
            self.add_item(EntryPanelButton(gid, cid))
            self.add_item(PreviewResultsButton(gid, cid))
            self.add_item(PublishResultsButton(gid, cid))
            self.add_item(CancelTournamentButton(gid, cid))
        self.add_item(CloseMenuButton(gid, cid))


class CloseMenuButton(Button):
    def __init__(self, guild_id, channel_id):
        super().__init__(style=discord.ButtonStyle.secondary, label="Закрыть",
                         custom_id=f"br_close_menu:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=smart_view(interaction.message, None))


class DrawButton(Button):
    """Жеребьёвка/старт: Solo → сразу к карте 1, остальные → составы."""

    def __init__(self, guild_id, channel_id):
        super().__init__(style=discord.ButtonStyle.success, label="Жеребьёвка / Старт",
                         custom_id=f"br_draw:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await interaction.response.send_message("Только владелец/админ.", ephemeral=True)
            return
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message("Регистрация закрыта.", ephemeral=True)
            return
        async with get_br_lock(t):
            # Проверки перед стартом: лимиты, гильдия, VAC — блокируют старт,
            # регистрация остаётся открытой, показаны конкретные участники.
            problems = await pre_start_problems(interaction.guild, t)
            if problems:
                await interaction.response.send_message(
                    "Старт заблокирован:\n" + "\n".join(problems[:15]) +
                    "\nРегистрация остаётся открытой — исправьте и повторите.",
                    ephemeral=True)
                return
            ok, err = t.draw()
            if not ok:
                await interaction.response.send_message(err, ephemeral=True)
                return
            await grant_tournament_roles(interaction.guild, t)
            br_store.set(t)
        embed = public_embed(t)
        try:
            message = await interaction.channel.fetch_message(t.message_id) if t.message_id else None
            if message:
                await message.edit(view=compose(embed, BRCardView(t.guild_id, t.channel_id, t)))
        except Exception:
            pass
        await interaction.response.send_message(
            "Жеребьёвка выполнена — составы опубликованы." if t.phase == BRTournamentPhase.DRAFT
            else "Турнир начался — карта 1.", ephemeral=True)


class BeginGamesButton(Button):
    def __init__(self, guild_id, channel_id):
        super().__init__(style=discord.ButtonStyle.success, label="Начать игры",
                         custom_id=f"br_begin:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await interaction.response.send_message("Только владелец/админ.", ephemeral=True)
            return
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.DRAFT:
            await interaction.response.send_message("Нет этапа составов.", ephemeral=True)
            return
        async with get_br_lock(t):
            t.begin_games()
            br_store.set(t)
        embed = public_embed(t)
        try:
            if t.message_id:
                message = await interaction.channel.fetch_message(t.message_id)
                await message.edit(view=compose(embed, BRCardView(t.guild_id, t.channel_id, t)))
        except Exception:
            pass
        await interaction.response.send_message("Игры начались — карта 1.", ephemeral=True)


class KickSlotButton(Button):
    def __init__(self, guild_id, channel_id):
        super().__init__(style=discord.ButtonStyle.danger, label="Снять участника/слот",
                         custom_id=f"br_kick:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await interaction.response.send_message("Только владелец/админ.", ephemeral=True)
            return
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message("Регистрация закрыта.", ephemeral=True)
            return
        slots = t.teams
        if not slots:
            await interaction.response.send_message("Нет слотов для снятия.", ephemeral=True)
            return
        view = KickSlotSelectView(self.guild_id, self.channel_id, slots, page=0)
        await interaction.response.send_message("Выберите слот для снятия:", view=view, ephemeral=True)


class KickSlotSelectView(View):
    PER_PAGE = 25

    def __init__(self, guild_id, channel_id, slots, page):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.channel_id = channel_id
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
            self.add_item(KickSlotSelect(self.guild_id, self.channel_id, options))
        if page > 0:
            self.add_item(KickPageButton(self.guild_id, self.channel_id, self.slots, page - 1, "◀"))
        if (page + 1) * self.PER_PAGE < len(slots):
            self.add_item(KickPageButton(self.guild_id, self.channel_id, self.slots, page + 1, "▶"))


class KickSlotSelect(Select):
    def __init__(self, guild_id, channel_id, options):
        super().__init__(placeholder="Слот…", options=options,
                         custom_id=f"br_kick_sel:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await interaction.response.send_message("Только владелец/админ.", ephemeral=True)
            return
        t = br_store.get(self.guild_id, self.channel_id)
        if not t:
            await interaction.response.send_message("Турнир не найден.", ephemeral=True)
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
            await interaction.response.send_message("Слот не найден.", ephemeral=True)
            return
        idx, slot = found
        async with get_br_lock(t):
            ok = t.kick_slot(idx, reason=f"снято <@{interaction.user.id}>")
            if ok:
                br_store.set(t)
        if ok:
            await interaction.response.send_message(
                f"Слот «{slot_label(slot, idx)}» снят.", ephemeral=True)
            await refresh_main_card(interaction, t)
        else:
            await interaction.response.send_message("Не удалось снять слот.", ephemeral=True)


class KickPageButton(Button):
    def __init__(self, guild_id, channel_id, slots, page, label):
        super().__init__(style=discord.ButtonStyle.secondary, label=label,
                         custom_id=f"br_kick_pg:{guild_id}:{channel_id}:{page}")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.slots = slots
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        view = KickSlotSelectView(self.guild_id, self.channel_id, self.slots, self.page)
        await interaction.response.edit_message(view=view)


class CancelTournamentButton(Button):
    def __init__(self, guild_id, channel_id):
        super().__init__(style=discord.ButtonStyle.danger, label="Отменить турнир",
                         custom_id=f"br_cancel:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await interaction.response.send_message("Только владелец/админ.", ephemeral=True)
            return
        await interaction.response.send_modal(CancelReasonModal(self.guild_id, self.channel_id))


class CancelReasonModal(Modal):
    reason = TextInput(label="Причина отмены", required=True, max_length=200)

    def __init__(self, guild_id, channel_id):
        super().__init__(title="Отмена BR турнира")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        t = get_tournament(self.guild_id, self.channel_id)
        if not t or not t.is_active:
            await interaction.response.send_message("Турнир не найден или уже завершён.", ephemeral=True)
            return
        view = CancelConfirmView(self.guild_id, self.channel_id, self.reason.value.strip())
        await interaction.response.send_message(
            f"Подтвердите отмену BR турнира.\nПричина: {self.reason.value.strip()}",
            view=view, ephemeral=True)


class CancelConfirmView(View):
    def __init__(self, guild_id, channel_id, reason):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.reason = reason
        self.add_item(CancelConfirmButton(guild_id, channel_id, reason))


class CancelConfirmButton(Button):
    def __init__(self, guild_id, channel_id, reason):
        super().__init__(style=discord.ButtonStyle.danger, label="Подтвердить отмену",
                         custom_id=f"br_cancel_ok:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.reason = reason

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await interaction.response.send_message("Только владелец/админ.", ephemeral=True)
            return
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or not t.is_active:
            await interaction.response.send_message("Турнир уже не активен.", ephemeral=True)
            return
        async with get_br_lock(t):
            t.phase = BRTournamentPhase.CANCELLED
            t.cancel_reason = self.reason
            t.cancelled_by = interaction.user.id
            br_store.set(t)  # архивирует
        try:
            await strip_tournament_roles(interaction.guild, t)
        except Exception as exc:
            logger.warning("BR cancel strip roles: %s", exc)
        embed = public_embed(t)
        try:
            if t.message_id:
                message = await interaction.channel.fetch_message(t.message_id)
                await message.edit(view=compose(embed, BRCardView(t.guild_id, t.channel_id, t)))
        except Exception:
            pass
        await interaction.response.send_message("Турнир отменён и перенесён в историю.", ephemeral=True)


# ---------- комнаты ----------

class RoomButton(Button):
    def __init__(self, guild_id, channel_id):
        super().__init__(style=discord.ButtonStyle.secondary, label="Комната",
                         custom_id=f"br_room:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await interaction.response.send_message("Только владелец/админ.", ephemeral=True)
            return
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.PLAYING:
            await interaction.response.send_message("Комната доступна во время игр.", ephemeral=True)
            return
        await interaction.response.send_modal(RoomModal(self.guild_id, self.channel_id))


class RoomModal(Modal):
    room_id = TextInput(label="ID комнаты", placeholder="123456", required=True, max_length=20)
    room_password = TextInput(label="Пароль", placeholder="123", required=True, max_length=20)

    def __init__(self, guild_id, channel_id):
        super().__init__(title="Комната BR")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.PLAYING:
            await interaction.response.send_message("Турнир не в фазе игр.", ephemeral=True)
            return
        await publish_room(interaction, t, self.room_id.value.strip(), self.room_password.value.strip())


async def publish_room(interaction: discord.Interaction, t: "BRTournament",
                       room_id: str, password: str) -> None:
    """Опубликовать/обновить карточку комнаты для текущей карты."""
    guild = interaction.guild
    map_idx = str(t.current_map_index)
    t.rooms[map_idx] = {"id": room_id, "password": password}
    # legacy-поле тоже обновляем для совместимости
    t.room_data = {"id": room_id, "password": password}

    map_ru = MAP_NAMES_RU.get(t.current_map_name(), t.current_map_name())
    role = guild.get_role(t.role_id)
    role_mention = role.mention if role else ""

    lines = [
        f"{_num(i)} **{slot_label(s, i)}**\n┗ {slot_member_line(t, s)}"
        for i, s in enumerate(t.filled_slots())
    ]
    card_text = (
        f"{role_mention}\n\n**КОМНАТА ОТКРЫТА**\n"
        f"Карта {t.current_map_index + 1}/{t.map_count} · {map_ru}\n\n"
        + "\n".join(lines)
        + f"\n\n**Данные:**\n```\nID: {room_id}\nПароль: {password}\n```"
    )

    data_channel = guild.get_channel(t.room_channel_id or BR_ROOM_CHANNEL_ID)
    if not data_channel:
        await interaction.response.send_message(
            "BR-канал данных не настроен (BR_ROOM_CHANNEL_ID).", ephemeral=True)
        return
    # проверка доступа аудитории заранее
    perms = data_channel.permissions_for(guild.me)
    if not perms.send_messages:
        await interaction.response.send_message(
            "У бота нет права отправки сообщений в BR-канал.", ephemeral=True)
        return

    try:
        mid = t.room_messages.get("card")
        if mid:
            try:
                msg = await data_channel.fetch_message(mid)
                await msg.edit(view=text_card(card_text))
            except discord.NotFound:
                msg = await data_channel.send(view=text_card(card_text))
                t.room_messages["card"] = msg.id
        else:
            msg = await data_channel.send(view=text_card(card_text))
            t.room_messages["card"] = msg.id
    except Exception as exc:
        logger.error("BR room card publish failed: %s", exc)
        await interaction.response.send_message("Не удалось опубликовать карточку комнаты.", ephemeral=True)
        return

    # уведомление без пароля
    notify = guild.get_channel(NOTIFICATION_CHANNEL_ID)
    if notify:
        try:
            nmid = t.room_messages.get("notify")
            ntext = (f"{role_mention}\n\nКомната открыта: <#{data_channel.id}> "
                     f"(карта {t.current_map_index + 1}/{t.map_count} · {map_ru})")
            if nmid:
                try:
                    nmsg = await notify.fetch_message(nmid)
                    await nmsg.edit(view=text_card(ntext))
                except discord.NotFound:
                    nmsg = await notify.send(view=text_card(ntext))
                    t.room_messages["notify"] = nmsg.id
            else:
                nmsg = await notify.send(view=text_card(ntext))
                t.room_messages["notify"] = nmsg.id
        except Exception as exc:
            logger.warning("BR notify failed: %s", exc)

    br_store.set(t)
    await interaction.response.send_message(
        f"Комната карты {t.current_map_index + 1} опубликована в <#{data_channel.id}>.",
        ephemeral=True)


class MapPlayedButton(Button):
    def __init__(self, guild_id, channel_id, last: bool = False):
        super().__init__(style=discord.ButtonStyle.primary,
                         label="Последняя карта сыграна" if last else "Карта сыграна",
                         custom_id=f"br_map_done:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await interaction.response.send_message("Только владелец/админ.", ephemeral=True)
            return
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.PLAYING:
            await interaction.response.send_message("Турнир не в фазе игр.", ephemeral=True)
            return
        async with get_br_lock(t):
            last = t.current_map_index >= t.map_count - 1
            t.mark_map_played()
            br_store.set(t)
        embed = public_embed(t)
        try:
            if t.message_id:
                message = await interaction.channel.fetch_message(t.message_id)
                await message.edit(view=compose(embed, BRCardView(t.guild_id, t.channel_id, t)))
        except Exception:
            pass
        if t.phase == BRTournamentPhase.COLLECT:
            await interaction.response.send_message(
                "Последняя карта сыграна. Результаты проверяются — "
                "откройте орг-меню → «Ввод результатов».", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"Карта отмечена. Следующая: {t.current_map_index + 1}/{t.map_count} "
                f"({MAP_NAMES_RU.get(t.current_map_name(), '')}). "
                "Опубликуйте новые данные комнаты через «Комната».", ephemeral=True)


# ---------- ввод результатов ----------

class EntryPanelButton(Button):
    def __init__(self, guild_id, channel_id):
        super().__init__(style=discord.ButtonStyle.primary, label="Ввод результатов",
                         custom_id=f"br_entry:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await interaction.response.send_message("Только владелец/админ.", ephemeral=True)
            return
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.COLLECT:
            await interaction.response.send_message(
                "Ввод доступен после последней карты.", ephemeral=True)
            return
        filled, expected = t.results_status()
        view = BREntryPanelView(self.guild_id, self.channel_id, map_index=0, page=0)
        await interaction.response.send_message(
            view=compose(
                text=f"Панель ввода результатов (приватно). Заполнено: {filled}/{expected}.",
                view=view), ephemeral=True)


class BREntryPanelView(View):
    PER_PAGE = 25

    def __init__(self, guild_id, channel_id, map_index, page):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.map_index = map_index
        self.page = page
        t = br_store.get(guild_id, channel_id)
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
                mark = " — DNS"
            else:
                mark = f" — {e['place']} место, {e['kills']}🔥"
            options.append(discord.SelectOption(
                label=f"{i + 1}. {slot_label(s, i)}{mark}"[:100],
                value=str(i)))
        if options:
            self.add_item(EntrySlotSelect(guild_id, channel_id, map_index, options))
        # навигация по картам
        self.add_item(EntryMapButton(guild_id, channel_id, map_index, -1, "◀ Карта"))
        self.add_item(EntryMapButton(guild_id, channel_id, map_index, +1, "Карта ▶"))
        # пакетный ввод по карте
        self.add_item(EntryBatchButton(guild_id, channel_id, map_index))
        # пагинация слотов
        if page > 0:
            self.add_item(EntryPageButton(guild_id, channel_id, map_index, page - 1, "◀"))
        if (page + 1) * self.PER_PAGE < n_slots:
            self.add_item(EntryPageButton(guild_id, channel_id, map_index, page + 1, "▶"))


class EntryMapButton(Button):
    def __init__(self, guild_id, channel_id, map_index, delta, label):
        super().__init__(style=discord.ButtonStyle.secondary, label=label,
                         custom_id=f"br_entry_map:{guild_id}:{channel_id}:{map_index}:{delta}")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.map_index = map_index
        self.delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        t = br_store.get(self.guild_id, self.channel_id)
        if not t:
            await interaction.response.send_message("Турнир не найден.", ephemeral=True)
            return
        new_map = self.map_index + self.delta
        if not (0 <= new_map < t.map_count):
            await interaction.response.send_message("Нет такой карты.", ephemeral=True)
            return
        view = BREntryPanelView(self.guild_id, self.channel_id, new_map, 0)
        await interaction.response.edit_message(view=view)


class EntryPageButton(Button):
    def __init__(self, guild_id, channel_id, map_index, page, label):
        super().__init__(style=discord.ButtonStyle.secondary, label=label,
                         custom_id=f"br_entry_pg:{guild_id}:{channel_id}:{map_index}:{page}")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.map_index = map_index
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        view = BREntryPanelView(self.guild_id, self.channel_id, self.map_index, self.page)
        await interaction.response.edit_message(view=view)


class EntrySlotSelect(Select):
    def __init__(self, guild_id, channel_id, map_index, options):
        super().__init__(placeholder="Слот для ввода…", options=options,
                         custom_id=f"br_entry_slot:{guild_id}:{channel_id}:{map_index}")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.COLLECT:
            await interaction.response.send_message("Ввод недоступен.", ephemeral=True)
            return
        slot_idx = int(self.values[0])
        slot = t.teams[slot_idx]
        await interaction.response.send_modal(
            EntryResultModal(self.guild_id, self.channel_id, self.map_index, slot_idx,
                             slot_label(slot, slot_idx)))


class EntryResultModal(Modal):
    place = TextInput(label="Место (1–48)", placeholder="например 3", required=False, max_length=4)
    kills = TextInput(label="Киллы (0–50)", placeholder="например 7", required=False, max_length=3)
    dns = TextInput(label="DNS? (да / пусто)", placeholder="нет", required=False, max_length=10)

    def __init__(self, guild_id, channel_id, map_index, slot_index, slot_name):
        super().__init__(title=f"Результат: {slot_name}"[:45])
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.map_index = map_index
        self.slot_index = slot_index

    async def on_submit(self, interaction: discord.Interaction) -> None:
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.COLLECT:
            await interaction.response.send_message("Ввод недоступен.", ephemeral=True)
            return
        dns_raw = self.dns.value.strip().lower()
        is_dns = dns_raw in ("да", "dns", "yes", "d")
        is_clear = dns_raw in ("пусто", "очистить", "clear", "-")
        place = None
        if not is_dns and not is_clear:
            try:
                place = int(self.place.value.strip())
            except (ValueError, AttributeError):
                await interaction.response.send_message("Укажите место, DNS или «пусто».", ephemeral=True)
                return
        try:
            kills = int(self.kills.value.strip() or "0")
        except ValueError:
            kills = 0
        async with get_br_lock(t):
            ok, err = t.set_result_entry(
                self.map_index, self.slot_index, place=place, kills=kills,
                dns=is_dns, clear=is_clear, by=interaction.user.id)
            if ok:
                br_store.set(t)
        if ok:
            await interaction.response.send_message(
                f"Карта {self.map_index + 1}: результат слота записан.", ephemeral=True)
        else:
            await interaction.response.send_message(err, ephemeral=True)


class EntryBatchButton(Button):
    def __init__(self, guild_id, channel_id, map_index):
        super().__init__(style=discord.ButtonStyle.secondary, label="Пакет по карте",
                         custom_id=f"br_entry_batch:{guild_id}:{channel_id}:{map_index}")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.map_index = map_index

    async def callback(self, interaction: discord.Interaction) -> None:
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.COLLECT:
            await interaction.response.send_message("Ввод недоступен.", ephemeral=True)
            return
        await interaction.response.send_modal(BatchEntryModal(self.guild_id, self.channel_id, self.map_index))


class BatchEntryModal(Modal):
    data = TextInput(
        label="Строки: №слота место киллы | №слот DNS",
        placeholder="1 1 12\n2 2 8\n3 DNS",
        required=True, max_length=1800,
        style=discord.TextStyle.paragraph)

    def __init__(self, guild_id, channel_id, map_index):
        super().__init__(title="Пакетный ввод по карте")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.map_index = map_index

    async def on_submit(self, interaction: discord.Interaction) -> None:
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.COLLECT:
            await interaction.response.send_message("Ввод недоступен.", ephemeral=True)
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
            if len(parts) >= 2 and parts[1].upper() in ("DNS", "-", "ПУСТО", "ОЧИСТИТЬ", "CLEAR"):
                if parts[1] == "-":
                    entries.append({"slot": slot_no - 1, "clear": True})
                elif parts[1].upper() == "DNS":
                    entries.append({"slot": slot_no - 1, "dns": True, "kills": 0})
                else:
                    entries.append({"slot": slot_no - 1, "clear": True})
                continue
            if len(parts) < 3:
                parse_errors.append(f"строка {ln}: «{line}» — нужно «слот место киллы», «слот DNS» или «слот -»")
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
            await interaction.response.send_message(
                "Ошибки пакета (ничего не сохранено):\n" + "\n".join(parse_errors[:15]),
                ephemeral=True)
            return
        async with get_br_lock(t):
            ok, errors = t.set_results_batch(self.map_index, entries, by=interaction.user.id)
            if ok:
                br_store.set(t)
        if ok:
            await interaction.response.send_message(
                f"Карта {self.map_index + 1}: пакет из {len(entries)} строк сохранён.",
                ephemeral=True)
        else:
            await interaction.response.send_message(
                "Ошибки пакета (ничего не сохранено):\n" + "\n".join(errors[:15]),
                ephemeral=True)


class PreviewResultsButton(Button):
    def __init__(self, guild_id, channel_id):
        super().__init__(style=discord.ButtonStyle.secondary, label="Предпросмотр итогов",
                         custom_id=f"br_preview:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await interaction.response.send_message("Только владелец/админ.", ephemeral=True)
            return
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.COLLECT:
            await interaction.response.send_message("Нет данных для предпросмотра.", ephemeral=True)
            return
        filled, expected = t.results_status()
        results = t.calculate_final_results()
        lines = [f"Заполнено: {filled}/{expected}"]
        for r in results[:15]:
            lines.append(f"{r['rank']}. {r['name']} — {r['total_points']} очк.")
        if len(results) > 15:
            lines.append(f"… и ещё {len(results) - 15}")
        view = PublishResultsView(self.guild_id, self.channel_id)
        await interaction.response.send_message(
            "**Приватный предпросмотр итогов:**\n" + "\n".join(lines),
            view=view, ephemeral=True)


class PublishResultsView(View):
    def __init__(self, guild_id, channel_id):
        super().__init__(timeout=300)
        self.add_item(PublishResultsButton(guild_id, channel_id))


class PublishResultsButton(Button):
    def __init__(self, guild_id, channel_id):
        super().__init__(style=discord.ButtonStyle.success, label="Опубликовать итоги",
                         custom_id=f"br_publish:{guild_id}:{channel_id}")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org(interaction.user.id):
            await interaction.response.send_message("Только владелец/админ.", ephemeral=True)
            return
        t = br_store.get(self.guild_id, self.channel_id)
        if not t or t.phase != BRTournamentPhase.COLLECT:
            await interaction.response.send_message(
                "Итоги уже опубликованы или турнир не готов.", ephemeral=True)
            return
        if not t.results_complete():
            filled, expected = t.results_status()
            await interaction.response.send_message(
                f"Не все записи заполнены: {filled}/{expected}. "
                "Пустые слоты должны быть помечены DNS.", ephemeral=True)
            return
        async with get_br_lock(t):
            if t.results_published:
                await interaction.response.send_message("Итоги уже опубликованы.", ephemeral=True)
                return
            t.phase = BRTournamentPhase.RESULTS
            t.results_published = True
            t.published_at = datetime.now().isoformat()
            t.view_page = -1
            br_store.set(t)  # архивирует в историю
        try:
            await strip_tournament_roles(interaction.guild, t)
        except Exception as exc:
            logger.warning("BR publish strip roles: %s", exc)
        embed = final_embed(t)
        try:
            if t.message_id:
                message = await interaction.channel.fetch_message(t.message_id)
                await message.edit(view=compose(embed, BRCardView(t.guild_id, t.channel_id, t)))
        except Exception:
            pass
        await interaction.response.send_message("Итоги опубликованы.", ephemeral=True)


# ---------- совместимость ----------

# Старые имена, на которые могут ссылаться внешние вызовы.
BRRegistrationView = BRCardView
