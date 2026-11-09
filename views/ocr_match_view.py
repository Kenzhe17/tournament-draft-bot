"""OCR-флоу результатов матчей: скриншот → ручное сопоставление игроков → ELO.

- MatchScreenshotButton — кнопка «📸 Скриншот» на каждый матч (вместо
  ручного ввода K/D капитанами).
- Капитан отправляет скриншот в канал результатов OCR_STATS_CHANNEL_ID
  (cogs/ocr_upload.py ловит аттачмент), бот публикует там сообщение
  с embed'ом (скриншот внутри), пингом обоих капитанов и кнопками.
- Каждый капитан сопоставляет ТОЛЬКО свою команду со своей панелью OCR
  своей кнопкой: выбирает панель (левая/правая), затем для каждого
  игрового ника — Discord-игрока из своего состава. Ник и статистику
  можно править.
- После сопоставления обеих команд результат подтверждается
  АВТОМАТИЧЕСКИ (победитель — по OCR-подсветке; если не определён,
  капитанам/организатору предлагается выбрать вручную).
- «Неправильный скриншот» и карандаши правки статистики — только
  администратор сервера (или владелец бота).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import discord
from utils.design import compose, smart_view, text_card
from discord.ui import Button, Modal, Select, TextInput, View

try:
    # FileUpload в модалках — discord.py >= 2.7 (новый UI Kit)
    from discord.ui import FileUpload
except ImportError:  # pragma: no cover
    FileUpload = None

from config import BOT_OWNER_ID, replace_emojis
from models.tournament import Tournament
from storage.json_store import store
from utils.cosmetics import clean_nickname
from utils.permissions import is_org_check

logger = logging.getLogger(__name__)

PENDING_UPLOAD_TTL = 300  # секунд на отправку скриншота после нажатия кнопки
NONE_VALUE = "__none__"

# Канал, куда приходят скриншоты и публикуются результаты OCR
OCR_STATS_CHANNEL_ID = 1568608461250367569

# Доп. статистика детального экрана отряда ( squad-режим )
EXTRA_STAT_KEYS = (
    "fact_dmg", "knockdowns", "healing", "revives", "revive_x",
    "headshots",
)

# Ожидающие загрузки скриншотов: (guild_id, channel_id, user_id) -> meta
PENDING_UPLOADS: dict[tuple[int, int, int], dict[str, Any]] = {}


def register_pending_upload(
    guild_id: int,
    channel_id: int,
    user_id: int,
    match_type: str,
    match_index: int,
    tchannel_id: int = 0,
    reupload: bool = False,
    squad_team: int | None = None,
    squad_uid: str | None = None,
) -> None:
    # Одна активная заявка на матч: новая регистрация вытесняет
    # старые pending того же пользователя по этому матчу — иначе
    # pop_pending_upload_any может вернуть устаревшую заявку
    # (например, обычную загрузку вместо «Неправильный скриншот»).
    for stale in [
        k for k, m in PENDING_UPLOADS.items()
        if k[0] == guild_id and k[2] == user_id
        and m.get("match_type") == match_type
        and m.get("match_index") == match_index
    ]:
        PENDING_UPLOADS.pop(stale, None)
    PENDING_UPLOADS[(guild_id, channel_id, user_id)] = {
        "match_type": match_type,
        "match_index": match_index,
        "tchannel": tchannel_id or channel_id,
        "reupload": reupload,
        "squad_team": squad_team,
        "squad_uid": squad_uid,
        "expires": time.time() + PENDING_UPLOAD_TTL,
    }


def pop_pending_upload(
    guild_id: int, channel_id: int, user_id: int
) -> dict[str, Any] | None:
    """Точное совпадение (канал + пользователь)."""
    key = (guild_id, channel_id, user_id)
    meta = PENDING_UPLOADS.pop(key, None)
    if meta and meta.get("expires", 0) < time.time():
        return None
    return meta


def pop_pending_upload_any(
    guild_id: int, user_id: int, channel_id: int | None = None
) -> dict[str, Any] | None:
    """Найти pending-загрузку пользователя в сервере.

    Скриншот принимается только в канале, где нажали кнопку, в канале
    турнира или в канале результатов — посторонние каналы не считаются,
    pending при этом сохраняется.
    """
    now = time.time()
    fallback: tuple[Any, dict[str, Any]] | None = None
    for key in list(PENDING_UPLOADS.keys()):
        g, c, u = key
        if g != guild_id or u != user_id:
            continue
        meta = PENDING_UPLOADS.get(key)
        if meta is None or meta.get("expires", 0) < now:
            PENDING_UPLOADS.pop(key, None)
            continue
        if channel_id is not None:
            allowed = {
                c,
                int(meta.get("tchannel") or c),
                OCR_STATS_CHANNEL_ID,
            }
            if channel_id not in allowed:
                continue
            # Приоритет — заявка, зарегистрированная именно в этом
            # канале (свежий контекст нажатия кнопки)
            if c == channel_id:
                PENDING_UPLOADS.pop(key)
                return meta
            if fallback is None:
                fallback = (key, meta)
    if fallback is not None:
        PENDING_UPLOADS.pop(fallback[0])
        return fallback[1]
    return None


def _session_key(match_type: str, match_index: int) -> str:
    return f"{match_type}_{match_index}"


def _match_tuple(tournament: Tournament, match_type: str, match_index: int) -> tuple[int, int]:
    if match_type == "qualifier":
        return tournament.qualifier_matches[match_index]
    if match_type == "semifinal":
        return tournament.semifinal_matches[match_index]
    return (tournament.final_teams[0], tournament.final_teams[1])


def team_roster(tournament: Tournament, team_index: int) -> list[str]:
    """Состав команды: players[] (random-auto/draft) или circle1-4."""
    team = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
    players = [p for p in team.get("players", []) if p]
    if players:
        return players
    return [
        team.get(f"circle{c}") for c in range(1, 5) if team.get(f"circle{c}")
    ]


def _team_display_name(tournament: Tournament, team_index: int) -> str:
    team = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
    captain = team.get("captain", f"Team {team_index}")
    return tournament.team_names.get(team_index, captain)


def _is_match_captain(user: discord.Member, tournament: Tournament, team_index: int) -> bool:
    team = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
    return team.get("captain") == clean_nickname(user.display_name)


def _uploaded_by_is_captain_of(
    tournament: Tournament, team_index: int, user_id: int | None
) -> bool:
    """Discord-ID загрузившего скриншот — капитан команды team_index.

    Сравнение по player_user_ids (имя капитана → discord id), т.к.
    в сессии хранится только user_id, а не Member.
    """
    if not user_id:
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    team = (
        tournament.teams[team_index]
        if 0 <= team_index < len(tournament.teams)
        else {}
    )
    captain = team.get("captain", "")
    if not captain:
        return False
    mapped = tournament.player_user_ids.get(captain)
    try:
        return bool(mapped) and int(mapped) == uid
    except (TypeError, ValueError):
        return False


async def _uploader_is_privileged(guild: discord.Guild, user_id) -> bool:
    """Загрузивший — админ сервера / владелец бота / организатор.

    Таким пользователям разрешено присылать скриншот за любую
    команду — проверка «капитан победившей» к ним не применяется.
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except Exception:
            return False
    if member is None:
        return False
    if member.id == BOT_OWNER_ID:
        return True
    if getattr(member.guild_permissions, "administrator", False):
        return True
    try:
        return bool(is_org_check(member, guild))
    except Exception:
        return False


def _can_use_ocr(user: discord.Member, tournament: Tournament, team_indexes: list[int]) -> bool:
    """Организатор или капитан любой из команд матча."""
    if is_org_check(user, user.guild):
        return True
    return any(_is_match_captain(user, tournament, i) for i in team_indexes)


def _is_server_admin(user: discord.Member) -> bool:
    """Discord-администратор сервера или владелец бота."""
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and perms.administrator) or user.id == BOT_OWNER_ID


def _match_label(match_type: str, match_index: int) -> str:
    if match_type == "final":
        return "Финал"
    if match_type == "semifinal":
        return f"Полуфинал #{match_index + 1}"
    return f"Отборочный #{match_index + 1}"


def _captain_mentions(tournament: Tournament, team_indexes: list[int]) -> str:
    """Пинг капитанов команд матча (или имена, если user_id неизвестен)."""
    parts = []
    for idx in team_indexes:
        team = tournament.teams[idx] if idx < len(tournament.teams) else {}
        captain = team.get("captain", "")
        uid = tournament.player_user_ids.get(captain)
        name = _team_display_name(tournament, idx)
        parts.append(f"<@{uid}> (**{name}**)" if uid else f"**{name}**")
    return "  ".join(parts)


def _team_of_user(
    tournament: Tournament, team_indexes, user_id
) -> int | None:
    """Команда матча, в составе которой состоит Discord-пользователь."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    for idx in team_indexes:
        for name in team_roster(tournament, idx):
            mapped = tournament.player_user_ids.get(name)
            try:
                if mapped and int(mapped) == uid:
                    return idx
            except (TypeError, ValueError):
                continue
    return None


def _auto_panel_for_team(
    tournament: Tournament, session: dict, team_index: int
) -> str | None:
    """Авто-сторона команды без ручного выбора панели.

    Победитель на скриншоте — всегда Левая Панель. Скриншот по правилам
    загружает участник одной из команд: баннер ПОБЕДА/ПОРАЖЕНИЕ на
    скриншоте говорит, выиграла ли его команда. Если загрузивший не
    опознан (админ и т.п.) или режим squad — возвращаем None, и
    капитанам показывается обычный выбор стороны.
    """
    if session.get("mode") == "squad":
        return None
    try:
        t1, t2 = _match_tuple(
            tournament,
            session.get("match_type"),
            session.get("match_index"),
        )
    except Exception:
        return None
    uploader_team = _team_of_user(
        tournament, (t1, t2), session.get("uploaded_by")
    )
    if uploader_team is None:
        return None
    if session.get("banner") == "lose":
        return "right" if team_index == uploader_team else "left"
    # banner "win" или не распознан: загрузившая команда считается
    # победившей — левая панель (правило загрузки результата).
    return "left" if team_index == uploader_team else "right"


# ---------------------------------------------------------------------------
# Гибридное сопоставление: self-service + капитан
# ---------------------------------------------------------------------------

def _row_bound(row: dict) -> bool:
    """Строка OCR уже привязана к Discord-игроку."""
    return bool(row.get("discord_id") or row.get("discord_name"))


def _match_maps(session: dict) -> dict:
    m = session.get("maps")
    if m is None:
        m = {}
        session["maps"] = m
    return m


def _team_map(session: dict, team_index: int) -> dict | None:
    return _match_maps(session).get(str(team_index))


def _mapping_progress(session: dict, team_index: int) -> tuple[int, int]:
    """(сопоставлено, всего) строк OCR команды."""
    tm = _team_map(session, team_index) or {}
    rows = tm.get("rows") or []
    if not rows:
        return 0, 0
    done = sum(1 for r in rows if _row_bound(r))
    return done, len(rows)


def _team_mapping_complete(session: dict, team_index: int) -> bool:
    done, total = _mapping_progress(session, team_index)
    return total > 0 and done == total


def _classic_mapping_complete(session: dict, t1: int, t2: int) -> bool:
    """Все строки OCR обеих команд привязаны, discord_id уникальны."""
    seen: set[int] = set()
    for t in (t1, t2):
        tm = _team_map(session, t)
        if not tm:
            return False
        rows = tm.get("rows") or []
        if not rows:
            return False
        for r in rows:
            if not _row_bound(r):
                return False
            did = r.get("discord_id")
            if did:
                try:
                    did = int(did)
                except (TypeError, ValueError):
                    continue
                if did in seen:
                    return False
                seen.add(did)
    return True


def _uid_bound_in_match(session: dict, user_id, exclude: tuple[int, int] | None = None) -> int | None:
    """team_index, где discord_id уже привязан (кроме exclude=(team,row))."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    for tkey, tm in _match_maps(session).items():
        try:
            tidx = int(tkey)
        except (TypeError, ValueError):
            continue
        for i, r in enumerate(tm.get("rows") or []):
            if exclude and (tidx, i) == exclude:
                continue
            did = r.get("discord_id")
            try:
                if did and int(did) == uid:
                    return tidx
            except (TypeError, ValueError):
                continue
    return None


def _roster_name_for_uid(tournament: Tournament, team_index: int, user_id) -> str | None:
    """Имя в составе команды, за которым закреплён Discord user_id."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    for name in team_roster(tournament, team_index):
        mapped = tournament.player_user_ids.get(name)
        try:
            if mapped and int(mapped) == uid:
                return name
        except (TypeError, ValueError):
            continue
    return None


def _panel_rows(session: dict, panel: str) -> list[dict]:
    return (session.get("panels", {}).get(panel) or {}).get("rows", [])


def _ensure_team_panel(
    tournament: Tournament, session: dict, team_index: int
) -> str | None:
    """Гарантировать maps[team] = {panel, rows-skeleton}.

    Панель берётся из maps/panel_picks/авто-определения. Возвращает
    panel или None, если сторону определить нельзя (ручной выбор).
    """
    tm = _team_map(session, team_index)
    if tm and tm.get("panel"):
        return tm["panel"]
    panel = (session.get("panel_picks") or {}).get(str(team_index))
    if panel not in ("left", "right"):
        panel = _auto_panel_for_team(tournament, session, team_index)
    if panel not in ("left", "right"):
        return None
    session.setdefault("panel_picks", {})[str(team_index)] = panel
    # Skeleton rows — снимок OCR-строк панели с полями привязки.
    rows = []
    for r in _panel_rows(session, panel):
        row = {
            "ocr_name": r.get("name", "?"),
            "kills": int(r.get("kills", 0) or 0),
            "deaths": int(r.get("deaths", 0) or 0),
            "assists": int(r.get("assists", 0) or 0),
            "dmg": int(r.get("dmg", 0) or 0),
            "discord_name": None,
            "discord_id": None,
            "source": None,
        }
        for k in EXTRA_STAT_KEYS:
            if r.get(k) is not None:
                row[k] = r[k]
        rows.append(row)
    _match_maps(session)[str(team_index)] = {"panel": panel, "rows": rows}
    return panel


async def _notify_mapping_conflict(
    guild: discord.Guild, session: dict, text: str
) -> None:
    """Спорная привязка — уведомить организатора в канал статистики."""
    try:
        ch = guild.get_channel(OCR_STATS_CHANNEL_ID) or await guild.fetch_channel(
            OCR_STATS_CHANNEL_ID
        )
        await ch.send(replace_emojis(text))
    except Exception as e:
        logger.warning("mapping conflict notify failed: %s", e)


# Мьютексы на матч для атомарных изменений привязок (self/captain).
_MAPPING_LOCKS: dict[str, asyncio.Lock] = {}
_MAPPING_LOCKS_MAX = 200


def _mapping_lock(match_key: str) -> asyncio.Lock:
    lock = _MAPPING_LOCKS.get(match_key)
    if lock is None:
        lock = asyncio.Lock()
        _MAPPING_LOCKS[match_key] = lock
        if len(_MAPPING_LOCKS) > _MAPPING_LOCKS_MAX:
            _MAPPING_LOCKS.pop(next(iter(_MAPPING_LOCKS)))
    return lock


async def _open_panel_mapping(
    interaction: discord.Interaction,
    tournament: Tournament,
    match_type: str,
    match_index: int,
    team_index: int,
    panel: str,
    auto: bool = False,
) -> None:
    """Зарезервировать панель и открыть маппинг игроков команды."""
    session = tournament.ocr_sessions.get(
        _session_key(match_type, match_index)
    )
    if not session:
        await interaction.response.send_message(
            replace_emojis("❌ OCR-сессия не найдена."), ephemeral=True
        )
        return
    # Панель не должна быть занята другой командой — ни завершённым
    # маппингом, ни резервом другой команды
    picks = session.get("panel_picks", {})
    own_key = str(team_index)
    blocker = None
    for other_key, other_map in session.get("maps", {}).items():
        if str(other_key) != own_key and isinstance(other_map, dict) \
                and other_map.get("panel") == panel:
            blocker = f"maps[{other_key}]"
            break
    if blocker is None:
        for other_key, picked in picks.items():
            if str(other_key) != own_key and picked == panel:
                blocker = f"panel_picks[{other_key}]={picked!r}"
                break
    if blocker is not None:
        logger.warning(
            "OCR panel %s blocked for team %s by %s "
            "(match %s/%s, session maps=%s picks=%s)",
            panel, own_key, blocker,
            match_type, match_index,
            session.get("maps"), picks,
        )
        await interaction.response.send_message(
            replace_emojis(
                "❌ Эта панель уже закреплена за другой командой."
            ),
            ephemeral=True,
        )
        return

    # Резервируем панель сразу — пока идёт маппинг, вторая команда
    # не сможет выбрать ту же сторону
    picks[own_key] = panel
    session["panel_picks"] = picks
    store.set(tournament)

    # Skeleton maps[team] — прогресс и привязки игроков по строкам
    _ensure_team_panel(tournament, session, team_index)
    store.set(tournament)

    name = _team_display_name(tournament, team_index)
    tm = _team_map(session, team_index) or {}
    mapped_rows = tm.get("rows") or []
    side_label = "Левая" if panel == "left" else "Правая"
    done_n, total_n = _mapping_progress(session, team_index)
    embed = discord.Embed(
        title=f"🧩 {name} — сопоставление игроков",
        description=(
            f"Ваша команда — **{side_label} панель**"
            + (" (определено автоматически)" if auto else "")
            + f".\nСопоставлено: **{done_n}/{total_n}**\n"
            "Капитан назначает ники участникам состава — может "
            "переопределить и выбор игроков (override логируется). "
            "Игроки также могут закрепить свой ник сами кнопкой "
            "«Это мой ник»."
        ),
        color=discord.Color.blurple(),
    )
    shown = 0
    for i, row in enumerate(mapped_rows):
        mark = ""
        if _row_bound(row):
            src = "self" if row.get("source") == "self" else "капитан"
            mark = f" ✅ {row.get('discord_name', '?')} ({src})"
        embed.add_field(
            name=f"{i + 1}. {row.get('ocr_name', '?')}{mark}",
            value=(
                f"K/D/A: {row.get('kills', 0)}/{row.get('deaths', 0)}/"
                f"{row.get('assists', 0)} • DMG {row.get('dmg')}"
            ),
            inline=False,
        )
        shown += 1
    if not shown:
        embed.description += "\n\n⚠️ На этой панели не найдено игроков."

    view = PanelMappingView(
        interaction.guild.id, interaction.channel.id, tournament,
        match_type, match_index, team_index, panel,
    )
    await interaction.response.send_message( view=compose(embed, view), ephemeral=True
    )


def _ocr_winning_team_index(session: dict[str, Any]) -> int | None:
    """Команда-победитель: по подсветке панели (2-панельный экран)
    или по баннеру отряда (детальный экран одного отряда)."""
    if session.get("mode") == "squad":
        winners = [
            int(t)
            for t, s in session.get("squads", {}).items()
            if s.get("banner") == "win"
        ]
        return winners[0] if len(winners) == 1 else None
    side = session.get("winner_side")
    if side not in ("left", "right"):
        return None
    for key, mapping in session.get("maps", {}).items():
        if mapping.get("panel") == side:
            try:
                return int(key)
            except (TypeError, ValueError):
                return None
    return None


def _stats_complete(tournament: Tournament, match_type: str, match_index: int) -> bool:
    """Все игроки обеих команд уже имеют статистику в temp_match_stats."""
    match_id = f"{tournament.id}_{_session_key(match_type, match_index)}"
    stats = tournament.temp_match_stats.get(match_id, {})
    if not stats:
        return False
    try:
        t1, t2 = _match_tuple(tournament, match_type, match_index)
    except Exception:
        return False
    for idx in (t1, t2):
        if any(name not in stats for name in team_roster(tournament, idx)):
            return False
    return True


def match_is_finished(tournament: Tournament, match_type: str, match_index: int) -> bool:
    """Матч полностью обработан (ELO начислено)."""
    match_id = f"{tournament.id}_{_session_key(match_type, match_index)}"
    return match_id in tournament.processed_elo_matches


# ---------------------------------------------------------------------------
# Кнопка «Отправить скриншот» на матч
# ---------------------------------------------------------------------------

class MatchScreenshotButton(Button):
    """Кнопка загрузки скриншота результата матча (капитан/организатор).

    Если задан team_index — это кнопка конкретного капитана: скриншот
    детального экрана отряда автоматически назначается этой команде.
    """

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
        label: str,
        team_index: int | None = None,
    ):
        suffix = f":{team_index}" if team_index is not None else ""
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=(
                f"ocr_shot:{guild_id}:{channel_id}:{match_type}:"
                f"{match_index}{suffix}"
            ),
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return

        # Матч должен быть в активной фазе
        phase = tournament.phase.value
        active = (
            (self.match_type == "qualifier" and phase == "qualifiers")
            or (self.match_type == "semifinal" and phase == "semifinals")
            or (self.match_type == "final" and phase == "final")
        )
        if not active:
            await interaction.response.send_message(
                replace_emojis("❌ Матч ещё не активен."), ephemeral=True
            )
            return

        if match_is_finished(tournament, self.match_type, self.match_index):
            await interaction.response.send_message(
                replace_emojis("❌ Результат этого матча уже зафиксирован."),
                ephemeral=True,
            )
            return

        t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
        if self.team_index is not None:
            # Кнопка конкретного капитана — только он или организатор
            allowed = is_org_check(interaction.user, interaction.guild) or (
                self.team_index in (t1, t2)
                and _is_match_captain(
                    interaction.user, tournament, self.team_index
                )
            )
        else:
            allowed = _can_use_ocr(interaction.user, tournament, [t1, t2])
        if not allowed:
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Скриншот может отправить капитан команды "
                    "или организатор."
                ),
                ephemeral=True,
            )
            return

        # Уже есть активная OCR-сессия?
        # В squad-режиме (скриншот одного отряда) второй капитан тоже
        # должен иметь возможность загрузить свой отряд — пропускаем.
        session = tournament.ocr_sessions.get(
            _session_key(self.match_type, self.match_index)
        )
        squad_active = bool(
            session
            and session.get("mode") == "squad"
            and session.get("status") not in ("completed", "cancelled")
        )
        if (
            session
            and session.get("status") not in ("completed", "cancelled")
            and not squad_active
        ):
            await interaction.response.send_message(
                replace_emojis(
                    "⚠️ Для этого матча уже загружен скриншот — "
                    "завершите сопоставление или попросите организатора отменить его."
                ),
                ephemeral=True,
            )
            return

        # Канал для скриншотов — канал результатов; если его нет,
        # просим прислать сюда
        stats_channel = interaction.guild.get_channel(OCR_STATS_CHANNEL_ID)
        if stats_channel is None:
            try:
                stats_channel = await interaction.guild.fetch_channel(
                    OCR_STATS_CHANNEL_ID
                )
            except Exception:
                stats_channel = None
        upload_channel_id = (
            stats_channel.id if stats_channel else self.channel_id
        )

        if FileUpload is not None:
            # Основной путь: модалка с загрузкой файла
            modal = ScreenshotUploadModal(
                self.guild_id, self.channel_id,
                self.match_type, self.match_index,
                squad_team=self.team_index,
            )
            await interaction.response.send_modal(modal)
            return

        register_pending_upload(
            self.guild_id, upload_channel_id, interaction.user.id,
            self.match_type, self.match_index,
            tchannel_id=self.channel_id,
            squad_team=self.team_index,
        )
        where = (
            f"<#{upload_channel_id}>" if stats_channel else "этот канал"
        )
        hint = (
            "Скриншот экрана статистики вашего отряда "
            "(K/D/A, урон, нокдауны, поднятия)"
            if self.team_index is not None
            else "скриншот экрана результатов"
        )
        await interaction.response.send_message(
            replace_emojis(
                f"📸 **{_match_label(self.match_type, self.match_index)}**\n"
                f"Отправьте **{hint}** в {where} "
                f"(в течение {PENDING_UPLOAD_TTL // 60} мин).\n"
                "Там появятся результаты — капитаны сопоставят игроков кнопками."
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Публичное сообщение с результатами OCR + кнопки сопоставления
# ---------------------------------------------------------------------------

async def _register_reupload(
    interaction: discord.Interaction,
    match_type: str,
    match_index: int,
    channel_id: int,
    squad_team: int | None = None,
    squad_uid: str | None = None,
) -> str:
    """Зарегистрировать повторную загрузку скриншота.

    Возвращает строку «куда слать скриншот» для ответа пользователю.
    """
    stats_channel = interaction.guild.get_channel(OCR_STATS_CHANNEL_ID)
    if stats_channel is None:
        try:
            stats_channel = await interaction.guild.fetch_channel(
                OCR_STATS_CHANNEL_ID
            )
        except Exception:
            stats_channel = None
    if stats_channel is not None:
        register_pending_upload(
            interaction.guild.id, stats_channel.id, interaction.user.id,
            match_type, match_index,
            tchannel_id=channel_id, reupload=True,
            squad_team=squad_team, squad_uid=squad_uid,
        )
        return f"<#{stats_channel.id}>"
    register_pending_upload(
        interaction.guild.id, channel_id, interaction.user.id,
        match_type, match_index,
        tchannel_id=channel_id, reupload=True,
        squad_team=squad_team, squad_uid=squad_uid,
    )
    return "этот канал"


# ---------------------------------------------------------------------------
# Модалка загрузки скриншота (FileUpload)
# ---------------------------------------------------------------------------

class ScreenshotUploadModal(Modal):
    """Окно загрузки скриншота результата матча.

    После выбора файла pending-загрузка регистрируется и файл
    прогоняется через тот же on_message-пайплайн cogs/ocr_upload.py
    (шим Message), поэтому squad- и классический режимы работают
    без дублирования логики.
    """

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        match_type: str,
        match_index: int,
        reupload: bool = False,
        squad_team: int | None = None,
        squad_uid: str | None = None,
    ):
        title = (
            "Неправильный скриншот" if reupload else "Скриншот результатов"
        )
        super().__init__(title=title[:45])
        self.guild_id = guild_id
        self.channel_id = channel_id  # канал турнира (не канал результатов)
        self.match_type = match_type
        self.match_index = match_index
        self.reupload = reupload
        self.squad_team = squad_team
        self.squad_uid = squad_uid

        # FileUpload — компонент нового UI Kit (discord.py >= 2.7),
        # label задаётся через ui.Label-обёртку
        self.file_label = discord.ui.Label(
            text="Скриншот результатов 16:9 (PNG/JPG)",
            component=FileUpload(required=True, max_values=1),
        )
        self.add_item(self.file_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        values = getattr(self.file_label.component, "values", None) or []
        attachment = values[0] if values else None
        if attachment is None:
            await interaction.followup.send(
                replace_emojis("❌ Прикрепите скриншот."), ephemeral=True
            )
            return

        channel = interaction.channel
        if channel is None:
            await interaction.followup.send(
                replace_emojis("❌ Канал недоступен."), ephemeral=True
            )
            return
        register_pending_upload(
            interaction.guild.id, channel.id, interaction.user.id,
            self.match_type, self.match_index,
            tchannel_id=self.channel_id,
            reupload=self.reupload,
            squad_team=self.squad_team,
            squad_uid=self.squad_uid,
        )

        async def reply(text: str) -> None:
            await interaction.followup.send(text, ephemeral=True)

        try:
            from cogs.ocr_upload import ModalUploadShim

            cog = interaction.client.get_cog("OCRUploadCog")
            if cog is None:
                await interaction.followup.send(
                    replace_emojis("❌ OCR-модуль недоступен."),
                    ephemeral=True,
                )
                return
            shim = ModalUploadShim(
                interaction.guild, channel, interaction.user,
                attachment, reply,
            )
            await cog.on_message(shim)
        except Exception:
            logger.exception("OCR modal upload failed")
            await interaction.followup.send(
                replace_emojis("❌ Ошибка при обработке скриншота."),
                ephemeral=True,
            )


ADMIN_HELP_ROLE_ID = 1563168378196197397


class AdminCallConfirmView(View):
    """Подтверждение вызова админа (ephemeral, 60 сек)."""

    def __init__(self, guild_id, channel_id, tournament, match_type, match_index):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index

        yes = Button(label="✅ Вызвать", style=discord.ButtonStyle.danger)
        yes.callback = self._confirm
        self.add_item(yes)
        no = Button(label="❌ Отмена", style=discord.ButtonStyle.secondary)
        no.callback = self._cancel
        self.add_item(no)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="Отменено.", view=None
        )

    async def _confirm(self, interaction: discord.Interaction) -> None:
        session = self.tournament.ocr_sessions.get(
            _session_key(self.match_type, self.match_index)
        ) or {}
        session["admin_call_at"] = time.time()
        self.tournament.ocr_sessions[
            _session_key(self.match_type, self.match_index)
        ] = session
        store.set(self.tournament)

        name = _team_display_name(
            self.tournament,
            _match_tuple(self.tournament, self.match_type, self.match_index)[0],
        )
        await interaction.response.edit_message(
            content="✅ Администратор вызван.", view=None
        )
        try:
            await interaction.channel.send(
                f"📣 {interaction.user.mention} вызывает администратора "
                f"по матчу ({name}).\n<@&{ADMIN_HELP_ROLE_ID}>",
                allowed_mentions=discord.AllowedMentions(
                    roles=[discord.Object(id=ADMIN_HELP_ROLE_ID)],
                    users=True,
                ),
            )
        except Exception as exc:
            logger.warning("admin call ping failed: %s", exc)


class OCRPublicView(View):
    """Кнопки на публичном сообщении с распознанными результатами."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
    ):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index

        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        maps = session.get("maps", {})
        t1, t2 = _match_tuple(tournament, match_type, match_index)
        active = session.get("status") not in ("completed", "cancelled")

        for team_index in (t1, t2):
            name = _team_display_name(tournament, team_index)
            done_n, total_n = _mapping_progress(session, team_index)
            complete = _team_mapping_complete(session, team_index)
            progress = f" {done_n}/{total_n}" if total_n else ""
            btn = Button(
                label=f"🧩 {name}{progress}"[:80],
                style=discord.ButtonStyle.primary,
                custom_id=(
                    f"ocr_map:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{team_index}"
                ),
                disabled=not active or complete,
            )
            btn.callback = self._map_callback(team_index)
            self.add_item(btn)

        # Self-service: любой участник матча привязывает свой ник
        if active and session.get("mode") != "squad":
            sm = Button(
                label="🎯 Это мой ник",
                style=discord.ButtonStyle.success,
                custom_id=(
                    f"ocr_selfmap:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}"
                ),
            )
            sm.callback = self._self_map_callback()
            self.add_item(sm)

        if active:
            wrong = Button(
                label="❌ Неправильный скриншот",
                style=discord.ButtonStyle.danger,
                custom_id=(
                    f"ocr_wrong:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}"
                ),
            )
            wrong.callback = self._wrong_shot_callback()
            self.add_item(wrong)

            call = Button(
                label="📣 Вызвать админа",
                style=discord.ButtonStyle.secondary,
                custom_id=(
                    f"ocr_calladmin:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}"
                ),
            )
            call.callback = self._call_admin_callback()
            self.add_item(call)

    def _call_admin_callback(self):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
            # Только капитаны команд матча
            if not any(
                _is_match_captain(interaction.user, tournament, i)
                for i in (t1, t2)
            ):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Вызвать админа могут только капитаны команд."
                    ),
                    ephemeral=True,
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            ) or {}
            # Cooldown 5 минут на матч — от спама вызовами
            last = session.get("admin_call_at", 0)
            if time.time() - float(last) < 300:
                await interaction.response.send_message(
                    replace_emojis(
                        "⏳ Админ уже был вызван недавно — подождите."
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "📣 Вызвать администратора по этому матчу?",
                view=AdminCallConfirmView(
                    self.guild_id, self.channel_id, tournament,
                    self.match_type, self.match_index,
                ),
                ephemeral=True,
            )
        return callback

    def _map_callback(self, team_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            # Кнопка команды — её капитан, организатор или админ
            if not (
                is_org_check(interaction.user, interaction.guild)
                or _is_server_admin(interaction.user)
                or _is_match_captain(interaction.user, tournament, team_index)
            ):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Сопоставить свою команду может только её капитан "
                        "или организатор."
                    ),
                    ephemeral=True,
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            if not session or session.get("status") == "completed":
                await interaction.response.send_message(
                    replace_emojis("❌ OCR-сессия не найдена или завершена."),
                    ephemeral=True,
                )
                return
            # maps[team] может существовать как skeleton (авто-панель /
            # self-bind) — «сопоставлена» только когда все строки привязаны
            if _team_mapping_complete(session, team_index):
                await interaction.response.send_message(
                    replace_emojis("✅ Эта команда уже сопоставлена."),
                    ephemeral=True,
                )
                return

            # Авто-сторона: загрузивший скриншот участник команды +
            # баннер win/lose определяют панель без ручного выбора.
            # Не получилось (админ/посторонний) — обычный выбор стороны.
            auto_panel = _auto_panel_for_team(
                tournament, session, team_index
            )
            if auto_panel is not None:
                await _open_panel_mapping(
                    interaction, tournament,
                    self.match_type, self.match_index,
                    team_index, auto_panel, auto=True,
                )
                return

            embed = discord.Embed(
                title="🧩 Сопоставление команды",
                description=(
                    f"Команда **{_team_display_name(tournament, team_index)}**\n"
                    "Выберите, какая панель результатов принадлежит вашей команде:"
                ),
                color=discord.Color.blurple(),
            )
            view = PanelPickView(
                self.guild_id, self.channel_id, tournament,
                self.match_type, self.match_index, team_index,
            )
            await interaction.response.send_message( view=compose(embed, view), ephemeral=True
            )
        return callback

    def _self_map_callback(self):
        """Кнопка «Это мой ник» — self-service привязка игрока."""
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            if not session or session.get("status") == "completed":
                await interaction.response.send_message(
                    replace_emojis("❌ Сопоставление уже завершено."),
                    ephemeral=True,
                )
                return
            if session.get("mode") == "squad":
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ В squad-режиме используйте кнопки отрядов."
                    ),
                    ephemeral=True,
                )
                return
            t1, t2 = _match_tuple(
                tournament, self.match_type, self.match_index
            )
            team = _team_of_user(tournament, (t1, t2), interaction.user.id)
            if team is None:
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Вы не участник этого матча."
                    ),
                    ephemeral=True,
                )
                return
            roster_name = _roster_name_for_uid(
                tournament, team, interaction.user.id
            ) or interaction.user.display_name
            panel = _ensure_team_panel(tournament, session, team)
            if panel is None:
                await interaction.response.send_message(
                    replace_emojis(
                        "⏳ Сторона вашей команды ещё не определена — "
                        "дождитесь соперника или организатора."
                    ),
                    ephemeral=True,
                )
                return
            store.set(tournament)

            done_n, total_n = _mapping_progress(session, team)
            embed = discord.Embed(
                title="🎯 Это мой ник",
                description=(
                    f"Команда **{_team_display_name(tournament, team)}** "
                    f"({done_n}/{total_n} сопоставлено).\n"
                    "Выберите свой игровой ник из списка — он будет "
                    "закреплён за вашим Discord-аккаунтом."
                ),
                color=discord.Color.green(),
            )
            view = SelfMapView(
                self.guild_id, self.channel_id, tournament,
                self.match_type, self.match_index, team,
                interaction.user.id, roster_name,
            )
            await interaction.response.send_message(
                view=compose(embed, view), ephemeral=True
            )
        return callback

    def _wrong_shot_callback(self):
        """Кнопка «Неправильный скриншот» — запросить повторную загрузку."""
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
            if not _is_server_admin(interaction.user):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Доступно только администратору сервера."
                    ),
                    ephemeral=True,
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            if not session or session.get("status") == "completed":
                await interaction.response.send_message(
                    replace_emojis("❌ OCR-сессия не найдена или завершена."),
                    ephemeral=True,
                )
                return

            if FileUpload is not None:
                modal = ScreenshotUploadModal(
                    self.guild_id, self.channel_id,
                    self.match_type, self.match_index,
                    reupload=True,
                )
                await interaction.response.send_modal(modal)
                return

            where = await _register_reupload(
                interaction, self.match_type, self.match_index,
                self.channel_id,
            )
            await interaction.response.send_message(
                replace_emojis(
                    f"📸 Пришлите **правильный скриншот** в {where} "
                    f"(в течение {PENDING_UPLOAD_TTL // 60} мин) — "
                    "это сообщение с результатами обновится."
                ),
                ephemeral=True,
            )
        return callback


# ---------------------------------------------------------------------------
# Squad-режим: скриншот детального экрана ОДНОГО отряда
# (K/D/A, DMG, фактический урон, нокдауны, исцеление, поднятия, хэдшоты)
# ---------------------------------------------------------------------------


def _squad_panel_key(team_index: int) -> str:
    return f"squad_{team_index}"


class SquadClaimView(View):
    """Кнопки на сообщении с OCR одного отряда до назначения команде.

    Две кнопки — по одной на команду матча: капитан соответствующей
    команды (или организатор) подтверждает, что этот отряд — его.
    """

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
        uid: str,
    ):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.uid = uid

        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        claimed = {int(k) for k in session.get("squads", {})}
        try:
            t1, t2 = _match_tuple(tournament, match_type, match_index)
        except Exception:
            t1, t2 = session.get("teams", [0, 1])[:2]

        for team_index in (t1, t2):
            name = _team_display_name(tournament, team_index)
            btn = Button(
                label=f"🧩 Это {name}"[:80],
                style=discord.ButtonStyle.primary,
                custom_id=(
                    f"ocr_squad_claim:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{team_index}:{uid}"
                ),
                disabled=team_index in claimed,
            )
            btn.callback = self._claim_callback(team_index)
            self.add_item(btn)

        t1v, t2v = t1, t2

        wrong = Button(
            label="❌ Неправильный скриншот",
            style=discord.ButtonStyle.danger,
            custom_id=(
                f"ocr_squad_wrong:{guild_id}:{channel_id}:{match_type}:"
                f"{match_index}:{uid}"
            ),
        )
        wrong.callback = self._wrong_callback(uid, t1v, t2v)
        self.add_item(wrong)

    def _claim_callback(self, team_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            if not (
                is_org_check(interaction.user, interaction.guild)
                or _is_match_captain(interaction.user, tournament, team_index)
            ):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Назначить отряд может только капитан "
                        "этой команды или организатор."
                    ),
                    ephemeral=True,
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            if not session or session.get("status") == "completed":
                await interaction.response.send_message(
                    replace_emojis("❌ OCR-сессия не найдена или завершена."),
                    ephemeral=True,
                )
                return
            session.setdefault("squads", {})
            session.setdefault("squad_pool", [])
            session.setdefault("panels", {})
            pool = session.get("squad_pool", [])
            entry = next((e for e in pool if e.get("uid") == self.uid), None)
            if entry is None:
                await interaction.response.send_message(
                    replace_emojis("❌ Этот скриншот уже назначен команде."),
                    ephemeral=True,
                )
                return
            if str(team_index) in session.get("squads", {}):
                await interaction.response.send_message(
                    replace_emojis("❌ У этой команды уже есть свой отряд."),
                    ephemeral=True,
                )
                return

            session["squads"][str(team_index)] = {
                k: v for k, v in entry.items() if k != "uid"
            }
            session["squad_pool"] = [
                e for e in pool if e.get("uid") != self.uid
            ]
            session.setdefault("panels", {})[
                _squad_panel_key(team_index)
            ] = {
                "name": _team_display_name(tournament, team_index),
                "rows": entry.get("rows", []),
            }
            if len(session["squads"]) >= 2:
                session["status"] = "mapping"
            store.set(tournament)

            name = _team_display_name(tournament, team_index)
            try:
                await interaction.message.edit(
                    view=smart_view(interaction.message, SquadAssignedView(
                        self.guild_id, self.channel_id, tournament,
                        self.match_type, self.match_index, team_index,
                    ))
                )
            except Exception as e:
                logger.warning("Squad claim view swap failed: %s", e)
            await interaction.response.send_message(
                replace_emojis(
                    f"✅ Отряд назначен команде **{name}**.\n"
                    "Нажмите **🧩 Сопоставить** ниже, чтобы привязать "
                    "игровые ники к участникам Discord."
                ),
                ephemeral=True,
            )
        return callback

    def _wrong_callback(self, uid: str, t1: int, t2: int):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            if not _is_server_admin(interaction.user):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Доступно только администратору сервера."
                    ),
                    ephemeral=True,
                )
                return
            if FileUpload is not None:
                modal = ScreenshotUploadModal(
                    self.guild_id, self.channel_id,
                    self.match_type, self.match_index,
                    reupload=True, squad_uid=uid,
                )
                await interaction.response.send_modal(modal)
                return
            where = await _register_reupload(
                interaction, self.match_type, self.match_index,
                self.channel_id, squad_uid=uid,
            )
            await interaction.response.send_message(
                replace_emojis(
                    f"📸 Пришлите **правильный скриншот отряда** в {where} "
                    f"(в течение {PENDING_UPLOAD_TTL // 60} мин) — "
                    "это сообщение обновится."
                ),
                ephemeral=True,
            )
        return callback


class SquadAssignedView(View):
    """Кнопки на сообщении с OCR отряда ПОСЛЕ назначения команде."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
        team_index: int,
    ):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index

        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        active = session.get("status") not in ("completed", "cancelled")
        mapped = str(team_index) in session.get("maps", {})
        name = _team_display_name(tournament, team_index)

        map_btn = Button(
            label=f"🧩 Сопоставить {name}"[:80],
            style=discord.ButtonStyle.primary,
            custom_id=(
                f"ocr_squad_map:{guild_id}:{channel_id}:{match_type}:"
                f"{match_index}:{team_index}"
            ),
            disabled=not active or mapped,
        )
        map_btn.callback = self._map_callback()
        self.add_item(map_btn)

        if active:
            wrong = Button(
                label="❌ Неправильный скриншот",
                style=discord.ButtonStyle.danger,
                custom_id=(
                    f"ocr_squad_wrong2:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{team_index}"
                ),
            )
            wrong.callback = self._wrong_callback()
            self.add_item(wrong)

    def _map_callback(self):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            if not (
                is_org_check(interaction.user, interaction.guild)
                or _is_match_captain(
                    interaction.user, tournament, self.team_index
                )
            ):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Сопоставить свою команду может только её "
                        "капитан или организатор."
                    ),
                    ephemeral=True,
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            if not session or session.get("status") == "completed":
                await interaction.response.send_message(
                    replace_emojis("❌ OCR-сессия не найдена или завершена."),
                    ephemeral=True,
                )
                return
            if str(self.team_index) in session.get("maps", {}):
                await interaction.response.send_message(
                    replace_emojis("✅ Эта команда уже сопоставлена."),
                    ephemeral=True,
                )
                return

            name = _team_display_name(tournament, self.team_index)
            panel = _squad_panel_key(self.team_index)
            rows = (
                session.get("panels", {}).get(panel, {}).get("rows", [])
            )
            embed = discord.Embed(
                title=f"🧩 {name} — сопоставление игроков",
                description=(
                    "Для каждого игрового ника выберите Discord-игрока "
                    "из вашего состава.\n"
                    "Кнопки ✏️ — ник и K/D/A/DMG, кнопки 📊 — "
                    "фактический урон, нокдауны, исцеление, поднятия, "
                    "хэдшоты."
                ),
                color=discord.Color.blurple(),
            )
            for i, row in enumerate(rows):
                embed.add_field(
                    name=f"{i + 1}. {row.get('name', '?')}",
                    value=(
                        f"K/D/A: {row.get('kills', 0)}/"
                        f"{row.get('deaths', 0)}/{row.get('assists', 0)}"
                        f" • DMG {row.get('dmg')}"
                    ),
                    inline=False,
                )
            view = PanelMappingView(
                self.guild_id, self.channel_id, tournament,
                self.match_type, self.match_index, self.team_index, panel,
            )
            await interaction.response.send_message( view=compose(embed, view), ephemeral=True
            )
        return callback

    def _wrong_callback(self):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            try:
                t1, t2 = _match_tuple(
                    tournament, self.match_type, self.match_index
                )
            except Exception:
                t1, t2 = self.team_index, self.team_index
            if not _is_server_admin(interaction.user):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Доступно только администратору сервера."
                    ),
                    ephemeral=True,
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            if not session or session.get("status") == "completed":
                await interaction.response.send_message(
                    replace_emojis("❌ OCR-сессия не найдена или завершена."),
                    ephemeral=True,
                )
                return
            if FileUpload is not None:
                modal = ScreenshotUploadModal(
                    self.guild_id, self.channel_id,
                    self.match_type, self.match_index,
                    reupload=True, squad_team=self.team_index,
                )
                await interaction.response.send_modal(modal)
                return
            where = await _register_reupload(
                interaction, self.match_type, self.match_index,
                self.channel_id, squad_team=self.team_index,
            )
            await interaction.response.send_message(
                replace_emojis(
                    f"📸 Пришлите **новый скриншот отряда** в {where} "
                    f"(в течение {PENDING_UPLOAD_TTL // 60} мин) — "
                    "это сообщение обновится, сопоставление сбросится."
                ),
                ephemeral=True,
            )
        return callback


def _confirm_embed(
    tournament: Tournament, session: dict, match_type: str, match_index: int
) -> discord.Embed:
    """Сводка сопоставления для финального подтверждения."""
    t1, t2 = _match_tuple(tournament, match_type, match_index)
    embed = discord.Embed(
        title=replace_emojis(f"📊 {_match_label(match_type, match_index)} — подтверждение"),
        color=discord.Color.gold(),
    )
    for team_index in (t1, t2):
        mapping = session.get("maps", {}).get(str(team_index))
        name = _team_display_name(tournament, team_index)
        if not mapping:
            embed.add_field(name=name, value="*не сопоставлена*", inline=False)
            continue
        lines = []
        for row in mapping["rows"]:
            lines.append(
                f"**{row['discord_name']}** — {row['kills']}/{row['deaths']}/"
                f"{row['assists']} • DMG {row['dmg']}  *(OCR: {row['ocr_name']})*"
            )
        embed.add_field(name=name, value="\n".join(lines) or "—", inline=False)
    side = session.get("winner_side")
    if side:
        panel_team = next(
            (int(k) for k, m in session.get("maps", {}).items()
             if m.get("panel") == side),
            None,
        )
        if panel_team is not None:
            wname = _team_display_name(tournament, panel_team)
            embed.description = f"🏆 По OCR победитель: **{wname}**\nВыберите победителя ниже."
    return embed


# ---------------------------------------------------------------------------
# Выбор панели команды (шаг 1 маппинга)
# ---------------------------------------------------------------------------

class PanelPickView(View):
    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
        team_index: int,
    ):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index

        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        panels = session.get("panels", {})
        options = []
        for side, label in (("left", "Левая панель"), ("right", "Правая панель")):
            pname = (panels.get(side) or {}).get("name")
            options.append(
                discord.SelectOption(
                    label=f"{label}: {pname}"[:100] if pname else label,
                    value=side,
                    description=(pname or "")[:100],
                )
            )
        self.select = Select(
            placeholder="Ваша команда — на какой панели?",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        values = getattr(self.select, "values", None) or interaction.data.get(
            "values", []
        )
        if not values:
            await interaction.response.defer()
            return
        panel = values[0]
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        await _open_panel_mapping(
            interaction, tournament,
            self.match_type, self.match_index, self.team_index, panel,
        )


# ---------------------------------------------------------------------------
# Маппинг игроков одной команды (шаг 2)
# ---------------------------------------------------------------------------

class PanelMappingView(View):
    """4 select'а (OCR-ник → Discord-игрок) + кнопки правки + сохранение."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
        team_index: int,
        panel: str,
    ):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index
        self.panel = panel
        # row_idx -> discord_name
        self.assign: dict[int, str] = {}

        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        rows = (session.get("panels", {}).get(panel) or {}).get("rows", [])
        roster = team_roster(tournament, team_index)

        # Гибридный режим (classic): капитан видит ВСЕ строки — он может
        # переопределить и self-привязки игроков (override логируется
        # организатору). Squad — legacy: все строки сразу.
        self.hybrid = session.get("mode") != "squad"
        tm = _team_map(session, team_index) or {}
        bound_idx: set[int] = set()
        for i, r in enumerate(tm.get("rows") or []):
            if _row_bound(r):
                bound_idx.add(i)
        # Discord-ники → где привязаны: игрока, закреплённого за ДРУГОЙ
        # строкой, нельзя назначить повторно (один ID — одна строка)
        name_locs: dict[str, list] = {}
        for tk, tm2 in _match_maps(session).items():
            for j, r2 in enumerate(tm2.get("rows") or []):
                dn = r2.get("discord_name")
                if dn:
                    name_locs.setdefault(dn, []).append((tk, j))

        eligible = list(range(min(len(rows), 4)))
        self.eligible_rows = eligible

        for i in eligible:
            row = rows[i]
            forbidden = {
                dn for dn, locs in name_locs.items()
                if (str(self.team_index), i) not in locs
            }
            options = [
                discord.SelectOption(
                    label="— не назначен —",
                    value=NONE_VALUE,
                    description="Пропустить этого игрока",
                )
            ] + [
                discord.SelectOption(label=n[:100], value=n[:100], description="Игрок команды")
                for n in roster
                if (not self.hybrid or n not in forbidden)
            ]
            bound_self = i in bound_idx and (
                (tm.get("rows") or [{}])[i].get("source") == "self"
            )
            placeholder = f"{row.get('name', '?')[:70]} → игрок"
            if bound_self:
                placeholder += " [self]"
            sel = Select(
                placeholder=placeholder[:150],
                options=options[:25],
                min_values=1,
                max_values=1,
            )
            sel.callback = self._make_select_callback(sel, i)
            self.add_item(sel)

        # Кнопки правки по одной на строку + сохранение (макс. 5 в ряду)
        for i in eligible:
            btn = Button(
                label=f"✏️ {i + 1}",
                style=discord.ButtonStyle.secondary,
                custom_id=(
                    f"ocr_edit:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{panel}:{i}"
                ),
            )
            btn.callback = self._make_edit_callback(i)
            self.add_item(btn)

        # Для squad-строк — вторая кнопка правки доп. статистики
        if panel.startswith("squad_"):
            for i in range(min(len(rows), 4)):
                btn = Button(
                    label=f"📊 {i + 1}",
                    style=discord.ButtonStyle.secondary,
                    custom_id=(
                        f"ocr_editx:{guild_id}:{channel_id}:{match_type}:"
                        f"{match_index}:{panel}:{i}"
                    ),
                )
                btn.callback = self._make_extra_edit_callback(i)
                self.add_item(btn)

        save = Button(
            label="💾 Сохранить сопоставление",
            style=discord.ButtonStyle.success,
        )
        save.callback = self._save
        self.add_item(save)

    def _make_select_callback(self, sel: Select, row_idx: int):
        async def callback(interaction: discord.Interaction) -> None:
            values = getattr(sel, "values", None) or interaction.data.get(
                "values", []
            )
            if not values:
                await interaction.response.defer()
                return
            value = values[0]
            self.assign[row_idx] = None if value == NONE_VALUE else value
            await interaction.response.defer()
        return callback

    def _make_edit_callback(self, row_idx: int):
        async def callback(interaction: discord.Interaction) -> None:
            if not _is_server_admin(interaction.user):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Исправлять статистику может только "
                        "администратор сервера."
                    ),
                    ephemeral=True,
                )
                return
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            row = (
                (session or {}).get("panels", {})
                .get(self.panel, {}).get("rows", [])
            )
            if row_idx >= len(row):
                await interaction.response.send_message(
                    replace_emojis("❌ Строка не найдена."), ephemeral=True
                )
                return
            modal = OcrRowEditModal(
                self.guild_id, self.channel_id, self.match_type,
                self.match_index, self.panel, row_idx,
            )
            await interaction.response.send_modal(modal)
        return callback

    def _make_extra_edit_callback(self, row_idx: int):
        async def callback(interaction: discord.Interaction) -> None:
            if not _is_server_admin(interaction.user):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Исправлять статистику может только "
                        "администратор сервера."
                    ),
                    ephemeral=True,
                )
                return
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            row = (
                (session or {}).get("panels", {})
                .get(self.panel, {}).get("rows", [])
            )
            if row_idx >= len(row):
                await interaction.response.send_message(
                    replace_emojis("❌ Строка не найдена."), ephemeral=True
                )
                return
            modal = OcrSquadExtraModal(
                self.guild_id, self.channel_id, self.match_type,
                self.match_index, self.panel, row_idx,
            )
            await interaction.response.send_modal(modal)
        return callback

    async def _save(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        session = tournament.ocr_sessions.get(
            _session_key(self.match_type, self.match_index)
        )
        if not session:
            await interaction.response.send_message(
                replace_emojis("❌ OCR-сессия не найдена."), ephemeral=True
            )
            return

        if self.hybrid:
            await self._save_hybrid(interaction, tournament, session)
            return

        rows = (session.get("panels", {}).get(self.panel) or {}).get("rows", [])
        roster = set(team_roster(tournament, self.team_index))

        # Валидация: все распознанные строки сопоставлены, без дублей,
        # только свой состав
        chosen = [self.assign.get(i) for i in range(len(rows))]
        if not rows or any(not c for c in chosen):
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Сопоставьте всех игроков — каждому игровому "
                    "нику должен соответствовать участник Discord."
                ),
                ephemeral=True,
            )
            return
        if len(set(chosen)) != len(chosen):
            await interaction.response.send_message(
                replace_emojis("❌ Один игрок не может быть назначен дважды."),
                ephemeral=True,
            )
            return
        if any(c not in roster for c in chosen):
            await interaction.response.send_message(
                replace_emojis("❌ Выбранный игрок не входит в вашу команду."),
                ephemeral=True,
            )
            return

        # Панель не должна быть занята другой командой — ни завершённым
        # маппингом, ни резервом другой команды
        picks = session.get("panel_picks", {})
        own_key = str(self.team_index)
        blocker = None
        for other_key, other_map in session.get("maps", {}).items():
            if str(other_key) != own_key and isinstance(other_map, dict) \
                    and other_map.get("panel") == self.panel:
                blocker = f"maps[{other_key}]"
                break
        if blocker is None:
            for other_key, picked in picks.items():
                if str(other_key) != own_key and picked == self.panel:
                    blocker = f"panel_picks[{other_key}]={picked!r}"
                    break
        if blocker is not None:
            logger.warning(
                "OCR panel %s save blocked for team %s by %s "
                "(match %s/%s)",
                self.panel, own_key, blocker,
                self.match_type, self.match_index,
            )
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Эта панель уже закреплена за другой командой."
                ),
                ephemeral=True,
            )
            return

        # Запись маппинга (статистика — актуальная из сессии, после правок)
        mapped_rows = []
        for i, row in enumerate(rows):
            mapped = {
                "discord_name": chosen[i],
                "ocr_name": row.get("name", "?"),
                "kills": int(row.get("kills", 0)),
                "deaths": int(row.get("deaths", 0)),
                "assists": int(row.get("assists", 0)),
                "dmg": int(row.get("dmg", 0) or 0),
            }
            for k in EXTRA_STAT_KEYS:
                if row.get(k) is not None:
                    mapped[k] = row[k]
            mapped_rows.append(mapped)
        session.setdefault("maps", {})[str(self.team_index)] = {
            "panel": self.panel,
            "rows": mapped_rows,
        }
        done = len(session["maps"])
        auto = done >= 2
        session["status"] = "ready" if auto else "mapping"
        store.set(tournament)

        # Обновляем публичное сообщение (кнопки/прогресс)
        await _refresh_public_message(
            interaction, tournament,
            self.match_type, self.match_index,
        )

        name = _team_display_name(tournament, self.team_index)

        if not auto:
            await interaction.response.send_message(
                replace_emojis(
                    f"✅ Команда **{name}** сопоставлена ({done}/2).\n"
                    "Осталась вторая команда — результат зафиксируется "
                    "автоматически."
                ),
                ephemeral=True,
            )
            return

        # Обе команды сопоставлены → автоподтверждение результата
        await _auto_finalize_match(
            interaction, tournament,
            self.match_type, self.match_index, self.team_index,
        )

    async def _save_hybrid(
        self,
        interaction: discord.Interaction,
        tournament: Tournament,
        session: dict,
    ) -> None:
        """Капитан назначает ТОЛЬКО свободные OCR-ники команды.

        Привязки игроков («Это мой ник») не трогаем — приоритет у
        самостоятельного подтверждения; конфликты уходят организатору.
        """
        key = _session_key(self.match_type, self.match_index)
        async with _mapping_lock(key):
            # Свежие объекты под локом — параллельные self/captain
            # привязки не должны перетирать друг друга
            tournament = store.get(self.guild_id, self.channel_id) or tournament
            session = tournament.ocr_sessions.get(key) or session
            if session.get("status") == "completed":
                await interaction.response.send_message(
                    replace_emojis("❌ Сопоставление уже завершено."),
                    ephemeral=True,
                )
                return
            _ensure_team_panel(tournament, session, self.team_index)
            tm = _team_map(session, self.team_index) or {}
            mrows = tm.get("rows") or []
            roster = set(team_roster(tournament, self.team_index))

            chosen = {i: n for i, n in self.assign.items() if n}
            if not chosen:
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Выберите хотя бы один свободный ник."
                    ),
                    ephemeral=True,
                )
                return

            problems: list[str] = []
            new_uids: dict[int, str] = {}
            for i, name in chosen.items():
                if i >= len(mrows):
                    problems.append(f"строка {i + 1} не найдена")
                    continue
                row = mrows[i]
                if name not in roster:
                    problems.append(f"«{name}» не входит в состав")
                    continue
                uid = tournament.player_user_ids.get(name)
                try:
                    uid_i = int(uid) if uid else None
                except (TypeError, ValueError):
                    uid_i = None
                if uid_i is not None:
                    if uid_i in new_uids:
                        problems.append(
                            f"«{name}» и «{new_uids[uid_i]}» — один игрок"
                        )
                    else:
                        new_uids[uid_i] = name
                    # uid не должен быть привязан к строке ВНЕ этого
                    # save — иначе игрок оказался бы на двух никах.
                    # Перенос внутри выбираемых строк разрешён.
                    locs = []
                    for tk, tm2 in _match_maps(session).items():
                        try:
                            tidx = int(tk)
                        except (TypeError, ValueError):
                            continue
                        for j, r2 in enumerate(tm2.get("rows") or []):
                            try:
                                if r2.get("discord_id") and \
                                        int(r2["discord_id"]) == uid_i:
                                    locs.append((tidx, j))
                            except (TypeError, ValueError):
                                continue
                    bad = [
                        l for l in locs
                        if not (l[0] == self.team_index and l[1] in chosen)
                    ]
                    if bad:
                        problems.append(
                            f"«{name}» уже закреплён за другим ником"
                        )
            if problems:
                uniq = list(dict.fromkeys(problems))[:5]
                await interaction.response.send_message(
                    replace_emojis("❌ " + "; ".join(uniq)),
                    ephemeral=True,
                )
                return

            bound_now = 0
            overridden: list[str] = []
            for i, name in chosen.items():
                row = mrows[i]
                prev_name = row.get("discord_name")
                if _row_bound(row) and prev_name == name:
                    continue  # уже назначен этому игроку
                # Капитан может переопределить чужую привязку —
                # фиксируем override для уведомления организатору
                if _row_bound(row):
                    overridden.append(
                        f"{row.get('ocr_name', '?')}: "
                        f"{prev_name} → {name}"
                    )
                uid = tournament.player_user_ids.get(name)
                row["discord_name"] = name
                row["discord_id"] = uid
                row["source"] = "captain"
                bound_now += 1
            store.set(tournament)

            try:
                await _refresh_public_message(
                    interaction, tournament,
                    self.match_type, self.match_index,
                )
            except Exception as e:
                logger.warning("public message refresh failed: %s", e)

            name_team = _team_display_name(tournament, self.team_index)
            done_n, total_n = _mapping_progress(session, self.team_index)

            if overridden:
                await _notify_mapping_conflict(
                    interaction.guild, session,
                    "⚠️ **OCR mapping override** — "
                    f"{_match_label(self.match_type, self.match_index)}: "
                    f"капитан {name_team} переопределил привязки: "
                    f"{'; '.join(overridden)}.",
                )

            t1, t2 = _match_tuple(
                tournament, self.match_type, self.match_index
            )
            if _classic_mapping_complete(session, t1, t2):
                await _auto_finalize_match(
                    interaction, tournament,
                    self.match_type, self.match_index, self.team_index,
                )
                return

            extra = ""
            if overridden:
                extra = (
                    "\n⚠️ Переопределены привязки: "
                    + "; ".join(overridden)
                    + " — организатор уведомлён."
                )
            await interaction.response.send_message(
                replace_emojis(
                    f"✅ Команда **{name_team}**: сопоставлено "
                    f"**{done_n}/{total_n}**.\n"
                    "Осталось сопоставить свободные ники — результат "
                    "зафиксируется автоматически, когда будут "
                    "закреплены все." + extra
                ),
                ephemeral=True,
            )


async def _auto_finalize_match(
    interaction: discord.Interaction,
    tournament: Tournament,
    match_type: str,
    match_index: int,
    team_index: int,
) -> None:
    """Обе команды сопоставлены → автоподтверждение результата.

    Общий финальный шаг для капитанского и self-service маппинга:
    проверка «загрузил капитан победившей» → finalize_ocr_match,
    либо ручной выбор победителя организатором.
    """
    guild_id = tournament.guild_id
    channel_id = tournament.channel_id
    session = tournament.ocr_sessions.get(
        _session_key(match_type, match_index)
    )
    if not session:
        await interaction.response.send_message(
            replace_emojis("❌ OCR-сессия не найдена."), ephemeral=True
        )
        return
    t1, t2 = _match_tuple(tournament, match_type, match_index)
    winner = _ocr_winning_team_index(session)
    name = _team_display_name(tournament, team_index)

    if winner is not None and winner in (t1, t2):
        # Скриншот обязан прислать капитан ПОБЕДИВШЕЙ команды
        # (левая панель / баннер win на скриншоте). Чужой или
        # неизвестный загрузивший → блок автоподтверждения + орг.
        if session.get("mode") == "squad":
            sentry = session.get("squads", {}).get(str(winner)) or {}
            uploader_id = sentry.get("uploaded_by")
        else:
            uploader_id = session.get("uploaded_by")
        uploader_ok = _uploaded_by_is_captain_of(
            tournament, winner, uploader_id
        )
        if not uploader_ok:
            # Админ/организатор может прислать скриншот за любую
            # команду — для них проверка капитана не нужна.
            uploader_ok = await _uploader_is_privileged(
                interaction.guild, uploader_id
            )
        if not uploader_ok:
            await interaction.response.send_message(
                replace_emojis(
                    "⚠️ Скриншот прислан не капитаном победившей "
                    "команды — результат НЕ подтверждён автоматически. "
                    "Организатор проверит вручную."
                ),
                ephemeral=True,
            )
            try:
                ch = interaction.guild.get_channel(
                    OCR_STATS_CHANNEL_ID
                ) or await interaction.guild.fetch_channel(
                    OCR_STATS_CHANNEL_ID
                )
                wname = _team_display_name(tournament, winner)
                await ch.send(
                    replace_emojis(
                        f"⚠️ **OCR: автоподтверждение заблокировано** — "
                        f"{_match_label(match_type, match_index)}\n"
                        f"Победитель по левой панели: **{wname}**, "
                        f"но загрузил <@{uploader_id or 0}> "
                        f"(не его капитан). Проверьте результат вручную."
                    )
                )
            except Exception as e:
                logger.warning("OCR captain-mismatch warn failed: %s", e)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await finalize_ocr_match(
                interaction, tournament, match_type, match_index, winner,
            )
            wname = _team_display_name(tournament, winner)
            await interaction.followup.send(
                replace_emojis(
                    f"✅ Сопоставление завершено — результат "
                    f"**{_match_label(match_type, match_index)}** "
                    f"зафиксирован автоматически. Победитель: **{wname}** "
                    "(по OCR)."
                ),
                ephemeral=True,
            )
        except Exception:
            logger.exception("OCR auto-finalize failed")
            await interaction.followup.send(
                replace_emojis(
                    "❌ Ошибка при автосохранении результата — "
                    "вызовите организатора."
                ),
                ephemeral=True,
            )
        return

    # Победитель по OCR не определён — предлагаем выбрать вручную
    embed = _confirm_embed(tournament, session, match_type, match_index)
    embed.description = (
        (embed.description or "")
        + "\n\n⚠️ OCR не определил победителя — выберите вручную:"
    )
    try:
        channel = interaction.guild.get_channel(OCR_STATS_CHANNEL_ID)
        if channel is None:
            channel = await interaction.guild.fetch_channel(
                OCR_STATS_CHANNEL_ID
            )
    except Exception:
        channel = None
    if channel is not None:
        try:
            await channel.send( view=compose(embed, OCRWinnerPickView(
                    guild_id, channel_id, tournament,
                    match_type, match_index,
                )),
            )
        except Exception as e:
            logger.warning("OCR winner-pick send failed: %s", e)
    await interaction.response.send_message(
        replace_emojis(
            f"✅ Команда **{name}** сопоставлена — сопоставление "
            "завершено. OCR не определил победителя: выбор отправлен "
            "в канал результатов."
        ),
        ephemeral=True,
    )


async def _results_channel(
    interaction: discord.Interaction, session: dict | None = None
) -> discord.TextChannel | None:
    """Канал, где опубликовано OCR-сообщение (канал результатов)."""
    channel_id = (session or {}).get("results_channel_id") or OCR_STATS_CHANNEL_ID
    channel = interaction.guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await interaction.guild.fetch_channel(channel_id)
        except Exception:
            return None
    return channel


async def _refresh_public_message(
    interaction: discord.Interaction,
    tournament: Tournament,
    match_type: str,
    match_index: int,
) -> None:
    """Перерисовать view публичного OCR-сообщения после сохранения маппинга."""
    session = tournament.ocr_sessions.get(_session_key(match_type, match_index))
    if not session:
        return
    channel = await _results_channel(interaction, session)
    if channel is None:
        return

    # Squad-режим: у каждого отряда своё сообщение в канале результатов
    if session.get("mode") == "squad":
        try:
            t1, t2 = _match_tuple(tournament, match_type, match_index)
        except Exception:
            t1, t2 = session.get("teams", [0, 1])[:2]
        for entry in session.get("squad_pool", []):
            mid = entry.get("message_id")
            if not mid:
                continue
            try:
                msg = await channel.fetch_message(mid)
                await msg.edit(
                    view=smart_view(msg, SquadClaimView(
                        tournament.guild_id, tournament.channel_id,
                        tournament, match_type, match_index,
                        entry["uid"],
                    ))
                )
            except Exception as e:
                logger.warning("Squad pool view refresh failed: %s", e)
        for tkey, sentry in session.get("squads", {}).items():
            mid = sentry.get("message_id")
            if not mid:
                continue
            try:
                tidx = int(tkey)
            except (TypeError, ValueError):
                continue
            try:
                msg = await channel.fetch_message(mid)
                await msg.edit(
                    view=smart_view(msg, SquadAssignedView(
                        tournament.guild_id, tournament.channel_id,
                        tournament, match_type, match_index, tidx,
                    ))
                )
            except Exception as e:
                logger.warning("Squad assigned view refresh failed: %s", e)
        return

    if not session.get("message_id"):
        return
    try:
        msg = await channel.fetch_message(session["message_id"])
        await msg.edit(
            view=smart_view(msg, OCRPublicView(
                tournament.guild_id, tournament.channel_id, tournament,
                match_type, match_index,
            ))
        )
    except Exception as e:
        logger.warning("OCR public message refresh failed: %s", e)


# ---------------------------------------------------------------------------
# Self-service: «Это мой ник» — игрок сам привязывает свой OCR-ник
# ---------------------------------------------------------------------------

class SelfMapView(View):
    """Выбор своего игрового ника из распознанных OCR-строк команды."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
        team_index: int,
        user_id: int,
        user_name: str,
    ):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index
        self.user_id = user_id
        self.user_name = user_name

        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        tm = _team_map(session, team_index) or {}
        options = []
        for i, r in enumerate((tm.get("rows") or [])[:4]):
            bound = _row_bound(r)
            own = bound and str(r.get("discord_id")) == str(user_id)
            if bound and not own:
                continue  # чужой или капитанский ник — не показываем
            label = str(r.get("ocr_name") or "?")[:60]
            if own:
                label = f"✓ {label}"[:100]
            desc = (
                f"K/D/A {r.get('kills', 0)}/{r.get('deaths', 0)}/"
                f"{r.get('assists', 0)}"
            )
            if own:
                desc += " — ваш текущий, можно сменить"
            options.append(
                discord.SelectOption(
                    label=label[:100], value=str(i),
                    description=desc[:100],
                )
            )
        self.select = Select(
            placeholder=(
                f"{user_name}: выберите свой игровой ник"
            )[:150],
            options=options[:25]
            or [discord.SelectOption(
                label="— нет доступных ников —", value="none",
            )],
            min_values=1,
            max_values=1,
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        values = getattr(self.select, "values", None) or interaction.data.get(
            "values", []
        )
        if not values or values[0] == "none":
            await interaction.response.defer()
            return
        try:
            row_idx = int(values[0])
        except (TypeError, ValueError):
            await interaction.response.defer()
            return
        await _self_bind_row(
            interaction, self.guild_id, self.channel_id,
            self.match_type, self.match_index, row_idx,
        )


async def _self_bind_row(
    interaction: discord.Interaction,
    guild_id: int,
    channel_id: int,
    match_type: str,
    match_index: int,
    row_idx: int,
) -> None:
    """Игрок подтверждает/меняет свою привязку OCR-ник → Discord.

    Атомарно под per-match локом: одна строка OCR — один Discord ID,
    один Discord ID — одна строка за матч. Свои старые привязки
    освобождаются (перенос на новый ник).
    """
    key = _session_key(match_type, match_index)
    async with _mapping_lock(key):
        tournament = store.get(guild_id, channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        session = tournament.ocr_sessions.get(key)
        if not session or session.get("status") == "completed":
            await interaction.response.send_message(
                replace_emojis("❌ Сопоставление уже завершено."),
                ephemeral=True,
            )
            return
        if session.get("mode") == "squad":
            await interaction.response.send_message(
                replace_emojis(
                    "❌ В squad-режиме сопоставление через кнопки отрядов."
                ),
                ephemeral=True,
            )
            return

        t1, t2 = _match_tuple(tournament, match_type, match_index)
        uid = interaction.user.id
        team = _team_of_user(tournament, (t1, t2), uid)
        if team is None:
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Вы не участник этого матча — сопоставить ник "
                    "может только игрок матча."
                ),
                ephemeral=True,
            )
            return
        roster_name = _roster_name_for_uid(tournament, team, uid)
        if not roster_name:
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Вы не найдены в составе команды этого матча."
                ),
                ephemeral=True,
            )
            return

        panel = _ensure_team_panel(tournament, session, team)
        if panel is None:
            await interaction.response.send_message(
                replace_emojis(
                    "⏳ Сторона вашей команды ещё не определена — "
                    "дождитесь соперника или организатора."
                ),
                ephemeral=True,
            )
            return
        store.set(tournament)

        tm = _team_map(session, team) or {}
        mrows = tm.get("rows") or []
        if row_idx >= len(mrows):
            await interaction.response.send_message(
                replace_emojis("❌ Строка не найдена."), ephemeral=True
            )
            return
        row = mrows[row_idx]

        if _row_bound(row):
            try:
                same = int(row.get("discord_id")) == uid
            except (TypeError, ValueError):
                same = False
            if not same:
                # Конфликт: ник уже закреплён за другим игроком
                await _notify_mapping_conflict(
                    interaction.guild, session,
                    "⚠️ **OCR mapping conflict** — "
                    f"{_match_label(match_type, match_index)}: "
                    f"<@{uid}> пытался занять ник "
                    f"«{row.get('ocr_name', '?')}», уже закреплённый "
                    f"за {row.get('discord_name', '?')} "
                    f"(source={row.get('source')}).",
                )
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Этот ник уже закреплён за другим игроком. "
                        "Если это ошибка — вызовите организатора."
                    ),
                    ephemeral=True,
                )
                return

        # Свои прежние привязки в матче освобождаем — игрок может
        # держать только один ник (перенос на новый).
        for tk, tm2 in _match_maps(session).items():
            try:
                tidx = int(tk)
            except (TypeError, ValueError):
                continue
            for j, r2 in enumerate(tm2.get("rows") or []):
                if (tidx, j) == (team, row_idx):
                    continue
                try:
                    if r2.get("discord_id") and int(r2["discord_id"]) == uid:
                        r2["discord_name"] = None
                        r2["discord_id"] = None
                        r2["source"] = None
                except (TypeError, ValueError):
                    continue

        row["discord_name"] = roster_name
        row["discord_id"] = uid
        row["source"] = "self"
        store.set(tournament)

        try:
            await _refresh_public_message(
                interaction, tournament, match_type, match_index,
            )
        except Exception as e:
            logger.warning("public message refresh failed: %s", e)

        t_name = _team_display_name(tournament, team)
        done_n, total_n = _mapping_progress(session, team)

        if _classic_mapping_complete(session, t1, t2):
            await _auto_finalize_match(
                interaction, tournament, match_type, match_index, team,
            )
            return

        await interaction.response.send_message(
            replace_emojis(
                f"✅ Ваш ник **{row.get('ocr_name', '?')}** закреплён "
                f"за **{roster_name}** ({t_name}: {done_n}/{total_n})."
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Модалка правки строки OCR (ник + K/D/A/DMG)
# ---------------------------------------------------------------------------

class OcrRowEditModal(Modal, title="Исправить строку OCR"):
    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        match_type: str,
        match_index: int,
        panel: str,
        row_idx: int,
    ):
        super().__init__()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.panel = panel
        self.row_idx = row_idx

        tournament = store.get(guild_id, channel_id)
        session = (
            tournament.ocr_sessions.get(_session_key(match_type, match_index))
            if tournament else {}
        ) or {}
        panel_rows = session.get("panels", {}).get(panel, {}).get("rows") or []
        row = panel_rows[row_idx] if row_idx < len(panel_rows) else {}

        self.name_input = TextInput(
            label="Игровой ник (OCR)",
            placeholder="Ник как на экране результатов",
            required=False,
            max_length=64,
            default=str(row.get("name", ""))[:64] or None,
        )
        self.kills_input = TextInput(
            label="Убийства (K)", required=False, max_length=4,
            default=str(row.get("kills", "")) or None,
        )
        self.deaths_input = TextInput(
            label="Смерти (D)", required=False, max_length=4,
            default=str(row.get("deaths", "")) or None,
        )
        self.assists_input = TextInput(
            label="Ассисты (A)", required=False, max_length=4,
            default=str(row.get("assists", "")) or None,
        )
        self.dmg_input = TextInput(
            label="Урон (DMG)", required=False, max_length=8,
            default=str(row.get("dmg", "")) or None,
        )
        for item in (
            self.name_input, self.kills_input, self.deaths_input,
            self.assists_input, self.dmg_input,
        ):
            self.add_item(item)

    @staticmethod
    def _to_int(value: str | None, default: int = 0) -> int | None:
        v = (value or "").strip()
        if not v:
            return default
        try:
            return int(v)
        except ValueError:
            return None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        session = tournament.ocr_sessions.get(
            _session_key(self.match_type, self.match_index)
        )
        rows = (session or {}).get("panels", {}).get(self.panel, {}).get("rows", [])
        if self.row_idx >= len(rows):
            await interaction.response.send_message(
                replace_emojis("❌ Строка не найдена."), ephemeral=True
            )
            return

        kills = self._to_int(self.kills_input.value)
        deaths = self._to_int(self.deaths_input.value)
        assists = self._to_int(self.assists_input.value)
        dmg = self._to_int(self.dmg_input.value)
        if None in (kills, deaths, assists, dmg):
            await interaction.response.send_message(
                replace_emojis("❌ Статистика должна быть целыми числами."),
                ephemeral=True,
            )
            return

        row = rows[self.row_idx]
        new_name = (self.name_input.value or "").strip()
        if new_name:
            row["name"] = new_name[:64]
        row["kills"] = kills
        row["deaths"] = deaths
        row["assists"] = assists
        row["dmg"] = dmg
        # Гибридный маппинг: maps[team].rows — снимок OCR-строк,
        # синхронизируем статистику (привязку discord_* не трогаем).
        for tm in (session.get("maps") or {}).values():
            if tm.get("panel") != self.panel:
                continue
            mrows = tm.get("rows") or []
            if self.row_idx < len(mrows):
                mr = mrows[self.row_idx]
                mr["ocr_name"] = row.get("name", mr.get("ocr_name"))
                mr["kills"] = kills
                mr["deaths"] = deaths
                mr["assists"] = assists
                mr["dmg"] = dmg
        store.set(tournament)

        await interaction.response.send_message(
            replace_emojis("✅ Строка обновлена. Не забудьте нажать «Сохранить»."),
            ephemeral=True,
        )


class OcrSquadExtraModal(Modal, title="Доп. статистика отряда"):
    """Правка расширенной статистики squad-строки (5 полей)."""

    FIELDS = (
        ("fact_dmg", "Фактический урон", 8),
        ("knockdowns", "Нокдауны", 4),
        ("healing", "Исцеление", 8),
        ("revives", "Поднятия", 4),
        ("headshots", "Хэдшоты %", 6),
    )

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        match_type: str,
        match_index: int,
        panel: str,
        row_idx: int,
    ):
        super().__init__()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.panel = panel
        self.row_idx = row_idx

        tournament = store.get(guild_id, channel_id)
        session = (
            tournament.ocr_sessions.get(_session_key(match_type, match_index))
            if tournament else {}
        ) or {}
        panel_rows = session.get("panels", {}).get(panel, {}).get("rows") or []
        row = panel_rows[row_idx] if row_idx < len(panel_rows) else {}

        self.inputs: dict[str, TextInput] = {}
        for key, label, max_len in self.FIELDS:
            val = row.get(key)
            ti = TextInput(
                label=label,
                required=False,
                max_length=max_len,
                default=(
                    str(val)[:max_len] if val is not None else None
                ),
            )
            self.add_item(ti)
            self.inputs[key] = ti

    @staticmethod
    def _to_num(value: str | None):
        v = (value or "").strip().replace(",", ".").rstrip("%")
        if not v:
            return None
        try:
            f = float(v)
        except ValueError:
            return "ERR"
        return int(f) if f == int(f) else round(f, 1)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        session = tournament.ocr_sessions.get(
            _session_key(self.match_type, self.match_index)
        )
        rows = (session or {}).get("panels", {}).get(self.panel, {}).get(
            "rows", []
        )
        if self.row_idx >= len(rows):
            await interaction.response.send_message(
                replace_emojis("❌ Строка не найдена."), ephemeral=True
            )
            return

        row = rows[self.row_idx]
        for key, _label, _ml in self.FIELDS:
            num = self._to_num(self.inputs[key].value)
            if num == "ERR":
                await interaction.response.send_message(
                    replace_emojis("❌ Значения должны быть числами."),
                    ephemeral=True,
                )
                return
            if num is None:
                row.pop(key, None)
            else:
                row[key] = num
        store.set(tournament)

        await interaction.response.send_message(
            replace_emojis(
                "✅ Доп. статистика обновлена. Не забудьте нажать "
                "«Сохранить»."
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Выбор победителя + финализация
# ---------------------------------------------------------------------------

class OCRWinnerPickView(View):
    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
    ):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index

        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        t1, t2 = _match_tuple(tournament, match_type, match_index)
        # Помечаем OCR-победителя (работает и для squad-режима)
        ocr_winner = _ocr_winning_team_index(session)

        for team_index in (t1, t2):
            name = _team_display_name(tournament, team_index)
            label = f"🏆 {name}" if team_index == ocr_winner else name
            btn = Button(
                label=label[:80],
                style=discord.ButtonStyle.success,
                custom_id=(
                    f"ocr_win:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{team_index}"
                ),
            )
            btn.callback = self._make_callback(team_index)
            self.add_item(btn)

    def _make_callback(self, team_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
            if not _can_use_ocr(interaction.user, tournament, [t1, t2]):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Подтверждение доступно капитанам команд "
                        "и организатору."
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True)
            try:
                await finalize_ocr_match(
                    interaction, tournament,
                    self.match_type, self.match_index, team_index,
                )
                await interaction.followup.send(
                    replace_emojis(
                        f"✅ Результат {_match_label(self.match_type, self.match_index)} "
                        "зафиксирован: статистика сохранена, ELO начислено."
                    ),
                    ephemeral=True,
                )
            except Exception:
                logger.exception("OCR finalize failed")
                await interaction.followup.send(
                    replace_emojis("❌ Ошибка при сохранении результата."),
                    ephemeral=True,
                )
        return callback


async def finalize_ocr_match(
    interaction: discord.Interaction,
    tournament: Tournament,
    match_type: str,
    match_index: int,
    winning_team_index: int,
) -> None:
    """Сохранить статистику из OCR-маппинга и провести матч до конца.

    temp_match_stats → process_match_result (ELO/XP/монеты) → история →
    confirm_* победителя → ставки → обновление сообщения турнира.
    """
    guild_id = tournament.guild_id
    channel_id = tournament.channel_id
    key = _session_key(match_type, match_index)
    session = tournament.ocr_sessions.get(key)
    if not session or len(session.get("maps", {})) < 2:
        raise ValueError("OCR-сессия не готова")

    match_id = f"{tournament.id}_{key}"
    t1, t2 = _match_tuple(tournament, match_type, match_index)

    # Статистика: discord_name -> {kills,deaths,assists,damage,ocr_name[,extra]}
    temp_stats: dict[str, dict[str, Any]] = {}
    for mapping in session["maps"].values():
        for i, row in enumerate(mapping["rows"]):
            if not row.get("discord_name"):
                continue  # гибридный маппинг: строка ещё не привязана
            entry = {
                "kills": row["kills"],
                "deaths": row["deaths"],
                "assists": row["assists"],
                "damage": row["dmg"],
                "circle": i + 1,
                "ocr_name": row["ocr_name"],
            }
            for k in EXTRA_STAT_KEYS:
                if row.get(k) is not None:
                    entry[k] = row[k]
            temp_stats[row["discord_name"]] = entry

    already_processed = match_id in tournament.processed_elo_matches

    if not already_processed:
        # 1. ELO/XP/монеты + запись temp_match_stats (process_match_result)
        from views.kd_input_view import process_match_result

        await process_match_result(
            guild_id,
            tournament,
            {
                "match_type": match_type,
                "match_index": match_index,
                "winning_team_index": winning_team_index,
                "team1_index": t1,
                "team2_index": t2,
                "temp_kd_data": temp_stats,
            },
            interaction,
        )

        # Возвращаем ocr_name в сохранённую статистику (исходный ник с экрана)
        stored = tournament.temp_match_stats.get(match_id, {})
        for name, kd in temp_stats.items():
            if name in stored:
                stored[name]["ocr_name"] = kd["ocr_name"]
        store.set(tournament)

        # 2. История матчей для всех игроков
        await _save_match_history(
            tournament, match_type, match_index, winning_team_index,
            t1, t2, temp_stats,
        )

        # 3. Подтверждение победителя (двигает сетку) — только если
        # матч ещё не подтверждён, чтобы не перегенерировать bracket
        tournament = store.get(guild_id, channel_id) or tournament
        phase = tournament.phase.value
        if match_type == "qualifier" and phase == "qualifiers":
            tournament.confirm_qualifier_winner(match_index, winning_team_index)
        elif (
            match_type == "semifinal"
            and match_index < len(tournament.semifinal_winners)
            and tournament.semifinal_winners[match_index] is None
        ):
            tournament.confirm_semifinal_winner(match_index, winning_team_index)
        elif match_type == "final" and tournament.winner_team_index is None:
            await tournament.confirm_final_winner(winning_team_index)
        store.set(tournament)

        # 4. Выплата ставок
        await _resolve_match_bets(tournament, match_type, match_index, t1, t2, winning_team_index)
    else:
        # Матч уже обработан — только обновляем статистику
        if match_id not in tournament.temp_match_stats:
            tournament.temp_match_stats[match_id] = {}
        tournament.temp_match_stats[match_id].update(temp_stats)
        store.set(tournament)

    # 5. Закрываем OCR-сессию и обновляем сообщение турнира
    session["status"] = "completed"
    session["winning_team_index"] = winning_team_index
    store.set(tournament)

    # 5.1. Финальная карточка результата в канал MATCH_RESULTS.
    # Перечитываем tournament/session: store.set выше мог подменить
    # объект — result_message_id обязан попасть в АКТУАЛЬНУЮ сессию,
    # иначе повторный finalize отправит дубликат карточки.
    fresh_t = store.get(guild_id, channel_id)
    if fresh_t is not None:
        tournament = fresh_t
        session = tournament.ocr_sessions.get(key, session)
    try:
        from cogs.ocr_upload import publish_match_result_card

        result_msg = await publish_match_result_card(
            interaction.guild, tournament, match_type, match_index,
            t1, t2, winning_team_index, temp_stats, session,
        )
    except Exception:
        logger.exception("Match result card publish failed")
        result_msg = None

    # 5.2. Карточка матча в cs-4x4-match-N → финальный статус
    try:
        from services.match_announcer import update_match_card

        from services.ocr_service import ordered_round_score

        score = ordered_round_score(session, t1, t2)
        await update_match_card(
            interaction.guild, tournament, match_type, match_index,
            state="done",
            winner_name=_team_display_name(tournament, winning_team_index),
            score=score,
            result_url=result_msg.jump_url if result_msg else "",
        )
    except Exception as exc:
        logger.warning("Match card done update failed: %s", exc)

    # 5.3. Временный канал матча (cs-4x4-match-N) больше не нужен —
    # результат зафиксирован, карточка переведена в «done».
    try:
        from cogs.ocr_upload import _maybe_delete_match_channel

        await _maybe_delete_match_channel(
            interaction.guild, tournament, match_type, match_index
        )
    except Exception as exc:
        logger.warning("Match channel cleanup failed: %s", exc)

    final_text = (
        f"✅ Результат зафиксирован. Победитель: "
        f"**{_team_display_name(tournament, winning_team_index)}**"
    )
    try:
        channel = await _results_channel(interaction, session)
    except Exception:
        channel = None

    if session.get("mode") == "squad":
        # У каждого отряда своё сообщение — гасим кнопки на всех
        mids = [
            s.get("message_id") for s in session.get("squads", {}).values()
        ] + [
            e.get("message_id") for e in session.get("squad_pool", [])
        ]
        if channel is not None:
            for mid in mids:
                if not mid:
                    continue
                try:
                    msg = await channel.fetch_message(mid)
                    await msg.edit( view=compose(text=replace_emojis(final_text), view=None)
                    )
                except Exception as e:
                    logger.warning("Squad message finalize failed: %s", e)
    elif session.get("message_id") and channel is not None:
        try:
            msg = await channel.fetch_message(session["message_id"])
            await msg.edit( view=compose(text=replace_emojis(final_text), view=None),
            )
        except Exception as e:
            logger.warning("OCR message finalize edit failed: %s", e)

    try:
        bot = interaction.client
        await bot.update_tournament_message(interaction.guild, tournament)
    except Exception as e:
        logger.warning("update_tournament_message failed: %s", e)


async def _save_match_history(
    tournament: Tournament,
    match_type: str,
    match_index: int,
    winning_team_index: int,
    t1: int,
    t2: int,
    temp_stats: dict[str, dict[str, Any]],
) -> None:
    """Сохранить MatchHistory для каждого игрока (как в ConfirmFinalWinnerButton)."""
    from storage.match_history_store import match_history_store
    from models.match_history import MatchHistory
    from storage.player_stats_store import player_stats_store

    guild_id = tournament.guild_id
    team_a, team_b = tournament.teams[t1], tournament.teams[t2]
    team_a_name = _team_display_name(tournament, t1)
    team_b_name = _team_display_name(tournament, t2)

    user_ids: dict[str, int] = dict(tournament.player_user_ids)
    for stat in await player_stats_store.get_all(guild_id):
        user_ids[stat.name] = stat.user_id

    def roster_data(team_index: int) -> list[dict]:
        out = []
        for name in team_roster(tournament, team_index):
            kd = temp_stats.get(name, {})
            out.append(
                {
                    "name": name,
                    "kills": kd.get("kills", 0),
                    "deaths": kd.get("deaths", 0),
                    "user_id": user_ids.get(name),
                }
            )
        return out

    players_a = roster_data(t1)
    players_b = roster_data(t2)

    for player_name, kd_data in temp_stats.items():
        is_a = player_name in [p["name"] for p in players_a]
        is_b = player_name in [p["name"] for p in players_b]
        if not (is_a or is_b):
            continue
        user_id = user_ids.get(player_name)
        if not user_id:
            continue

        if is_a:
            my_idx, my_name, mates, opp_idx, opp_name, opps = (
                t1, team_a_name, players_a, t2, team_b_name, players_b
            )
        else:
            my_idx, my_name, mates, opp_idx, opp_name, opps = (
                t2, team_b_name, players_b, t1, team_a_name, players_a
            )

        player_stats = await player_stats_store.get(guild_id, user_id)
        elo_change = (
            player_stats.last_elo_change
            if player_stats and hasattr(player_stats, "last_elo_change")
            else 0
        )

        await match_history_store.add(
            MatchHistory(
                guild_id=guild_id,
                user_id=user_id,
                match_id=f"{tournament.id}_{match_type}_{match_index}",
                tournament_id=tournament.id,
                date=datetime.now(),
                match_type=match_type,
                match_index=match_index,
                team_index=my_idx,
                team_name=my_name,
                teammates=mates,
                opponent_team_index=opp_idx,
                opponent_team_name=opp_name,
                opponents=opps,
                kills=kd_data.get("kills", 0),
                deaths=kd_data.get("deaths", 0),
                assists=kd_data.get("assists", 0),
                damage=kd_data.get("damage", 0),
                result="win" if my_idx == winning_team_index else "loss",
                elo_change=elo_change,
            )
        )


async def _resolve_match_bets(
    tournament: Tournament,
    match_type: str,
    match_index: int,
    t1: int,
    t2: int,
    winning_team_index: int,
) -> None:
    """Выплата ставок по матчу (как в ConfirmFinalWinnerButton)."""
    from storage.bet_store import bet_store
    from storage.user_balance_store import user_balance_store

    match_id = f"{tournament.id}_{_session_key(match_type, match_index)}"
    name_a = _team_display_name(tournament, t1)
    name_b = _team_display_name(tournament, t2)
    winning_name = name_a if winning_team_index == t1 else name_b
    winning_pos = 0 if winning_team_index == t1 else 1

    try:
        payouts, payouts_by_name = await bet_store.resolve_match_bets(
            tournament.guild_id, tournament.id, match_id,
            winning_name, winning_pos,
        )
        for user_id, payout in payouts.items():
            if payout > 0:
                await user_balance_store.add_balance(
                    tournament.guild_id, user_id, payout
                )
        for user_name, payout in payouts_by_name.items():
            if payout > 0:
                tournament.betting_results[user_name] = (
                    tournament.betting_results.get(user_name, 0) + payout
                )
        store.set(tournament)
    except Exception as e:
        logger.error("OCR bet resolution failed: %s", e, exc_info=True)
