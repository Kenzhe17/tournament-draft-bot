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
import hashlib
import json
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

from config import (BOT_OWNER_ID, OCR_ADMIN_ROLE_ID as _CFG_OCR_ADMIN_ROLE_ID,
                    OCR_STATS_CHANNEL_ID as _CFG_OCR_STATS_CHANNEL_ID,
                    replace_emojis)
from models.tournament import Tournament
from storage.json_store import store
from utils.cosmetics import clean_nickname
from utils.permissions import is_org_check

logger = logging.getLogger(__name__)

PENDING_UPLOAD_TTL = 300  # секунд на отправку скриншота после нажатия кнопки
NONE_VALUE = "__none__"

# Канал, куда приходят скриншоты и публикуются результаты OCR
OCR_STATS_CHANNEL_ID = _CFG_OCR_STATS_CHANNEL_ID

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


def _source_label(source) -> str:
    """Человекочитаемая подпись источника привязки (без 'self')."""
    if source == "self":
        return "Игрок сам выбрал себя"
    if source == "captain":
        return "капитан"
    return str(source or "неизвестно")


def _self_display(row: dict) -> str:
    """Discord-ник игрока, сам выбравшего себя (fallback — discord_name)."""
    return (
        row.get("discord_display")
        or row.get("discord_name")
        or "?"
    )


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


def _map_has_claims(tm: dict | None) -> bool:
    """У команды есть реальная привязка к панели: подтверждённый
    маппинг или хотя бы одна занятая строка."""
    if not isinstance(tm, dict):
        return False
    if tm.get("confirmed"):
        return True
    return any(_row_bound(r) for r in (tm.get("rows") or []))


def _panel_claimed_by_other(
    session: dict, team_index: int, panel: str
) -> bool:
    """Панель уже занята ДРУГОЙ командой (подтверждение или привязки).

    Skeleton без привязок и «резервы» panel_picks claims не считаются —
    панель закрепляется только фактическим сопоставлением.
    """
    for tk, tm2 in _match_maps(session).items():
        if str(tk) == str(team_index):
            continue
        if not isinstance(tm2, dict) or tm2.get("panel") != panel:
            continue
        if _map_has_claims(tm2):
            return True
    return False


def _ensure_team_panel(
    tournament: Tournament, session: dict, team_index: int,
    panel: str | None = None,
) -> str | None:
    """Гарантировать maps[team] = {panel, rows-skeleton}.

    Панель берётся из maps/явного параметра/авто-определения
    (panel_picks — legacy-фолбэк для старых сессий). Возвращает
    panel или None, если сторону определить нельзя.
    """
    tm = _team_map(session, team_index)
    if tm and tm.get("panel"):
        return tm["panel"]
    if panel not in ("left", "right"):
        panel = (session.get("panel_picks") or {}).get(str(team_index))
    if panel not in ("left", "right"):
        panel = _auto_panel_for_team(tournament, session, team_index)
    if panel not in ("left", "right"):
        return None
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
    """Лок мутаций OCR-сессии матча — ТОТ ЖЕ per-match лок, что у
    finalize/cancel/claim (cogs.ocr_upload._room_lock).

    Единая иерархия: все изменения сессии (маппинг, правки статов,
    комнаты) и финализация сериализуются на одном ключе
    _session_key(match_type, match_index) — mapping не может
    перезаписать «finalizing»/«completed», финализация не может
    начаться поверх незавершённой мутации. Порядок захвата всегда
    _mapping_lock → (после освобождения) _finalize_lock → _room_lock;
    обратного порядка нет → deadlock невозможен.
    """
    from cogs.ocr_upload import _room_lock

    return _room_lock(match_key)


def _room_session(session: dict, room_index: int) -> dict | None:
    """Эффективная session-подобная структура для одной комнаты.

    Все helper'ы маппинга работают с плоским dict; для комнаты отдаём
    её собственные panels/maps/panel_picks/banner/score. Ссылки на
    maps/panel_picks — те же объекты, что в room → мутации сохраняются.
    """
    if not session or session.get("mode") != "rooms":
        return None
    for r in session.get("rooms") or []:
        try:
            if int(r.get("room_index", 0)) != int(room_index):
                continue
        except (TypeError, ValueError):
            continue
        return {
            "match_type": session.get("match_type"),
            "match_index": session.get("match_index"),
            "mode": "classic",
            "uploaded_by": session.get("uploaded_by"),
            "banner": r.get("banner"),
            "score": r.get("score"),
            "top5": r.get("top5", False),
            "winner_side": r.get("winner_side"),
            "panels": r.get("panels") or {},
            "maps": r.setdefault("maps", {}),
            "panel_picks": r.setdefault("panel_picks", {}),
            "status": r.get("status"),
        }
    return None


def _rooms_all_mapped(session: dict, t1: int, t2: int) -> bool:
    """Все комнаты имеют полное сопоставление обеих команд."""
    rooms = session.get("rooms") or []
    if not rooms:
        return False
    for r in rooms:
        rs = _room_session(session, r.get("room_index"))
        if rs is None or not _classic_mapping_complete(rs, t1, t2):
            return False
    return True


def _rooms_unmapped_count(session: dict) -> int:
    """Сколько комнат ещё не сопоставлены (все строки привязаны)."""
    n = 0
    for r in session.get("rooms") or []:
        maps = r.get("maps") or {}
        ok = bool(maps) and all(
            (tm.get("rows") or [])
            and all(_row_bound(row) for row in tm["rows"])
            for tm in maps.values()
        )
        if not ok:
            n += 1
    return n


def _rooms_aggregate_stats(
    session: dict, tournament: Tournament, t1: int, t2: int
) -> dict:
    """Суммарная статистика матча по всем комнатам.

    Ключ агрегации — discord_id (НЕ discord_name): один игрок может
    менять Free Fire-ник между комнатами, display_name тоже не
    влияет. Результат ключуется именем ростера (его читает
    process_match_result). Игровые ники всех комнат собираются
    в ocr_name для отображения. Суммируем K/D/A/DMG, не среднее.
    """
    # uid → имя ростера (оба состава матча)
    uid_to_name: dict[int, str] = {}
    for idx in (t1, t2):
        for n in team_roster(tournament, idx):
            uid = tournament.player_user_ids.get(n)
            try:
                if uid:
                    uid_to_name[int(uid)] = n
            except (TypeError, ValueError):
                continue

    agg: dict[int, dict] = {}
    for room in session.get("rooms") or []:
        rs = _room_session(session, room.get("room_index"))
        if rs is None:
            raise ValueError("Комната без данных сопоставления")
        # Один Discord ID — максимум одна строка на комнату
        # (иначе статистика удвоится). Между комнатами повтор — норма.
        seen_dids: set[int] = set()
        for mapping in (rs.get("maps") or {}).values():
            for i, row in enumerate(mapping.get("rows") or []):
                if not row.get("discord_id"):
                    continue
                try:
                    did = int(row["discord_id"])
                except (TypeError, ValueError):
                    continue
                if did in seen_dids:
                    raise ValueError(
                        f"Discord ID {did} привязан к двум строкам "
                        f"комнаты {room.get('room_index')}"
                    )
                seen_dids.add(did)
                if did not in uid_to_name:
                    raise ValueError(
                        f"Discord ID {did} не входит в состав матча"
                    )
                entry = agg.setdefault(did, {
                    "kills": 0, "deaths": 0, "assists": 0,
                    "damage": 0, "circle": i + 1,
                    "ocr_name": row.get("ocr_name") or "?",
                    "_nicks": [],
                })
                entry["kills"] += int(row.get("kills") or 0)
                entry["deaths"] += int(row.get("deaths") or 0)
                entry["assists"] += int(row.get("assists") or 0)
                entry["damage"] += int(row.get("dmg") or 0)
                nick = row.get("ocr_name")
                if nick and nick not in entry["_nicks"]:
                    entry["_nicks"].append(nick)
                for k in EXTRA_STAT_KEYS:
                    v = row.get(k)
                    if v is not None:
                        try:
                            entry[k] = int(entry.get(k) or 0) + int(v)
                        except (TypeError, ValueError):
                            pass
    temp_stats: dict[str, dict] = {}
    for did, entry in agg.items():
        nicks = entry.pop("_nicks", [])
        if nicks:
            entry["ocr_name"] = " / ".join(nicks)[:64]
        temp_stats[uid_to_name[did]] = entry
    return temp_stats


def _rooms_stats_complete(session: dict) -> bool:
    """Все привязанные строки всех комнат имеют K/D/A/DMG.

    None («не определено») блокирует подтверждение — нули не
    подставляются автоматически.
    """
    for room in session.get("rooms") or []:
        maps = room.get("maps") or {}
        if len(maps) < 2:
            return False
        for tm in maps.values():
            rows = tm.get("rows") or []
            if not rows:
                return False
            for row in rows:
                if not _row_bound(row):
                    return False
                for f in ("kills", "deaths", "assists", "dmg"):
                    if row.get(f) is None:
                        return False
    return True


def check_ocr_session_ready(
    tournament: Tournament,
    session: dict | None,
    t1: int,
    t2: int,
    require_winner: bool = True,
) -> tuple[bool, str, str]:
    """Готовность rooms-сессии к финализации (ТЗ Ranked MM).

    True только когда: сессия активна (не completed/cancelled), все
    комнаты присутствуют и полностью сопоставлены, статистика полна
    и агрегируется по всему ростеру без конфликтов Discord ID, а при
    require_winner — зафиксирован winning_team_index команды-
    победителя. OCR-вывод и баннеры на готовность НЕ влияют.
    Возвращает (ready, reason, detail).
    """
    if not session or session.get("mode") != "rooms":
        return False, "no_session", ""
    if session.get("status") in ("completed", "cancelled"):
        return False, "closed", ""
    if not _rooms_all_mapped(session, t1, t2):
        return False, "unmapped", ""
    if not _rooms_stats_complete(session):
        return False, "stats_incomplete", ""
    try:
        agg = _rooms_aggregate_stats(session, tournament, t1, t2)
    except Exception as e:
        return False, "agg_error", str(e)
    missing = [
        n for idx in (t1, t2)
        for n in team_roster(tournament, idx)
        if n not in agg
    ]
    if missing:
        return False, "missing_stats", ", ".join(missing[:8])
    if require_winner:
        wti = session.get("winning_team_index")
        try:
            wi = int(wti) if wti is not None else None
        except (TypeError, ValueError):
            wi = None
        if wi not in (t1, t2):
            return False, "winner_unset", ""
    return True, "ready", ""


def _squad_session_ready(
    tournament: Tournament, session: dict, t1: int, t2: int
) -> tuple[bool, str]:
    """Готовность squad-сессии к финализации (Ranked MM).

    Оба отряда назначены командам матча; у каждой команды все строки
    сопоставлены с Discord ID ИЗ ЕЁ ростера (без повторов между
    командами), заполнены K/D/A/Damage. Ошибки и незавершённые
    сопоставления блокируют начисление ELO. Возвращает (ready,
    reason).
    """
    squads = session.get("squads") or {}
    maps = session.get("maps") or {}
    for t in (t1, t2):
        if str(t) not in squads and str(t) not in maps:
            return False, "squad_missing"
    seen: set[int] = set()
    for t in (t1, t2):
        tm = _team_map(session, t)
        rows = (tm or {}).get("rows") or []
        roster = set(team_roster(tournament, t))
        expected = len(roster)
        if expected and len(rows) != expected:
            return False, "roster_mismatch"
        if not rows:
            return False, "unmapped"
        for r in rows:
            if not _row_bound(r):
                return False, "unmapped"
            # Привязка обязана содержать Discord ID и имя из
            # ростера ЭТОЙ команды — чужой игрок не пройдёт.
            try:
                did = int(r.get("discord_id"))
            except (TypeError, ValueError):
                return False, "no_discord_id"
            if did in seen:
                return False, "duplicate_id"
            seen.add(did)
            dname = r.get("discord_name")
            if roster and dname not in roster:
                return False, "foreign_player"
            for k in ("kills", "deaths", "assists", "dmg"):
                if r.get(k) is None:
                    return False, "missing_stats"
    return True, "ready"


def _rooms_confirmed_winner(
    tournament: Tournament, session: dict, t1: int, t2: int
) -> int | None:
    """Победитель rooms-матча: зафиксированный winning_team_index
    (команда отправителя первого принятого файла), для legacy-сессий
    —fallback на команду uploaded_by. Никогда не выводится из OCR.
    """
    wti = session.get("winning_team_index")
    try:
        winner = int(wti) if wti is not None else None
    except (TypeError, ValueError):
        winner = None
    if winner in (t1, t2):
        return winner
    return _team_of_user(tournament, (t1, t2), session.get("uploaded_by"))


def _session_mm_winner(
    tournament: Tournament, session: dict, t1: int, t2: int
) -> int | None:
    """Победитель Ranked MM для classic/squad-режимов.

    Тот же принцип, что у rooms: команда капитана, отправившего
    первый принятый файл (winning_team_index фиксируется при создании
    сессии). Fallback — команда uploaded_by; None — комплект админа,
    команду выбирает организатор явно. OCR-баннеры/статистика не
    участвуют.
    """
    wti = session.get("winning_team_index")
    try:
        wi = int(wti) if wti is not None else None
    except (TypeError, ValueError):
        wi = None
    if wi in (t1, t2):
        return wi
    return _team_of_user(tournament, (t1, t2), session.get("uploaded_by"))


def _rooms_ready_reason_text(reason: str, detail: str = "") -> str:
    """Пользовательское сообщение о причине неготовности rooms-комплекта."""
    if reason == "unmapped":
        return ("⚠️ Не все комнаты сопоставлены — проверьте "
                "сообщения комнат.")
    if reason == "stats_incomplete":
        return ("⚠️ Есть нераспознанные значения статистики — "
                "исправьте их через «Исправить статистику».")
    if reason == "agg_error":
        return ("⚠️ Агрегация статистики содержит ошибки "
                f"({detail}) — исправьте привязки.")
    if reason == "missing_stats":
        return ("⚠️ Нет статистики для игроков: " + detail +
                ". Заполните вручную через «Исправить статистику».")
    if reason == "winner_unset":
        return ("⚠️ Победитель не определён — администратор выберет "
                "команду при подтверждении.")
    return "⚠️ OCR-комплект ещё не готов к подтверждению."


async def _reply_ephemeral(interaction: discord.Interaction, text: str) -> None:
    """Эфемерный ответ с учётом уже занятого response (defer/followup)."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        pass


async def _maybe_auto_finalize_mm_rooms(
    interaction: discord.Interaction,
    tournament: Tournament,
    match_type: str,
    match_index: int,
) -> tuple[bool, str, str]:
    """Ranked MM: автоматическая финализация rooms-комплекта.

    Вызывается после каждого действия, способного завершить
    подготовку (последнее сопоставление, сохранение исправления
    статистики). Кнопка «Подтвердить результаты» не обязательна:
    при полной готовности finalize запускается сразу. Для обычных
    турниров и не-rooms режимов — no-op (False, "", "").
    """
    if not getattr(tournament, "mm_search_channel_id", None):
        return False, "", ""
    key = _session_key(match_type, match_index)
    session = tournament.ocr_sessions.get(key)
    if not session or session.get("mode") != "rooms":
        return False, "", ""
    if session.get("status") in ("completed", "cancelled", "finalizing"):
        return False, "", ""
    t1, t2 = _match_tuple(tournament, match_type, match_index)
    ready, reason, detail = check_ocr_session_ready(
        tournament, session, t1, t2
    )
    if not ready:
        return False, reason, detail
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    try:
        await finalize_ocr_match(
            interaction, tournament, match_type, match_index,
            _rooms_confirmed_winner(tournament, session, t1, t2) or t1,
        )
        wname = _team_display_name(
            tournament,
            _rooms_confirmed_winner(tournament, session, t1, t2) or t1,
        )
        await interaction.followup.send(
            replace_emojis(
                f"✅ Все комнаты обработаны и сопоставлены — результат "
                f"**{_match_label(match_type, match_index)}** "
                f"зафиксирован автоматически. Победитель: **{wname}**."
            ),
            ephemeral=True,
        )
    except Exception:
        logger.exception("MM rooms auto-finalize failed")
        try:
            await interaction.followup.send(
                replace_emojis(
                    "❌ Автоматическое завершение не удалось — "
                    "вызовите организатора или нажмите "
                    "«Подтвердить результаты»."
                ),
                ephemeral=True,
            )
        except Exception:
            pass
        return False, "error", ""
    return True, "", ""


def _ensure_room_panel_map(
    tournament: Tournament, rs: dict, team_index: int, panel: str
) -> dict | None:
    """Skeleton maps[team] для комнаты БЕЗ потери None-статистики.

    В отличие от _ensure_team_panel не приводит None→0: нераспознанные
    K/D/A/Damage остаются «не определено» до ручного исправления.
    """
    tm = _team_map(rs, team_index)
    if tm and tm.get("panel") == panel:
        return tm
    if tm and _map_has_claims(tm):
        # Уже есть привязки на другой панели — не затираем
        return tm
    prows = (rs.get("panels") or {}).get(panel, {}).get("rows") or []
    rows = []
    for r in prows:
        row = {
            "ocr_name": r.get("name", "?"),
            "kills": r.get("kills"),
            "deaths": r.get("deaths"),
            "assists": r.get("assists"),
            "dmg": r.get("dmg"),
            "discord_name": None,
            "discord_id": None,
            "source": None,
        }
        for k in EXTRA_STAT_KEYS:
            if r.get(k) is not None:
                row[k] = r[k]
        rows.append(row)
    _match_maps(rs)[str(team_index)] = {
        "panel": panel, "rows": rows, "confirmed": False,
    }
    return _team_map(rs, team_index)


def _rs(v) -> str:
    """Статистика для отображения: None → «—» (не определено)."""
    return "—" if v is None else str(v)


def _room_squad_panel_keys(room: dict) -> list[str]:
    """Ключи squad-панелей комнаты: "squad", "squad2", ..."""
    return [
        k for k in (room.get("panels") or {})
        if str(k).startswith("squad")
    ]


def _squad_panel_for_team(room: dict, team_index: int) -> str | None:
    """Свободная squad-панель для команды (или уже закреплённая за ней).

    Два squad-скриншота одной комнаты занимают панели "squad"/"squad2";
    привязка панель→команда — по порядку обращения (кто первый
    открыл маппинг, тот и берёт первую свободную). None — свободных
    панелей нет → команде нужна ручная панель.
    """
    maps = room.get("maps") or {}
    tm = maps.get(str(team_index))
    if tm and str(tm.get("panel") or "").startswith("squad"):
        return tm["panel"]
    claimed = {
        str(m.get("panel"))
        for m in maps.values()
        if str(m.get("panel") or "").startswith("squad")
    }
    for k in _room_squad_panel_keys(room):
        if k not in claimed:
            return k
    return None


def _ensure_manual_map(rs: dict, team_index: int) -> dict:
    """Ручной skeleton для команды без скриншота (squad-комната)."""
    tm = _team_map(rs, team_index)
    if tm:
        return tm
    rows = [{
        "ocr_name": "?", "kills": None, "deaths": None,
        "assists": None, "dmg": None,
        "discord_name": None, "discord_id": None, "source": None,
    } for _ in range(4)]
    _match_maps(rs)[str(team_index)] = {
        "panel": "manual", "rows": rows, "confirmed": False,
    }
    return _team_map(rs, team_index)


# Локи финализации по матчу — защита от двойного ELO при
# одновременных подтверждениях (processed_elo_matches — вторая линия).
_FINALIZE_LOCKS: dict[str, asyncio.Lock] = {}
_FINALIZE_LOCKS_MAX = 200


def _finalize_lock(match_key: str) -> asyncio.Lock:
    lock = _FINALIZE_LOCKS.get(match_key)
    if lock is None:
        lock = asyncio.Lock()
        _FINALIZE_LOCKS[match_key] = lock
        if len(_FINALIZE_LOCKS) > _FINALIZE_LOCKS_MAX:
            _FINALIZE_LOCKS.pop(next(iter(_FINALIZE_LOCKS)))
    return lock


def _finalize_stats_gate(
    fstatus: str | None, json_processed: bool
) -> tuple[bool, bool]:
    """Решение финализации по статусу match_finalizations + JSON-флагу.

    Возвращает (already_processed, stats_done):
    - already_processed=True → вообще ничего не делать (матч завершён);
    - stats_done=True → статистика/ELO уже закоммичены (в т.ч. до
      падения) — process_match_result НЕ вызывать, добить только
      идемпотентные шаги (история/подтверждение/ставки/карточка).

    DB-статус authoritative; fstatus=None (нет DB/записи) — фолбэк
    на processed_elo_matches, как до введения таблицы.
    """
    already_processed = fstatus == "done" or (
        fstatus is None and json_processed
    )
    stats_done = fstatus in ("stats_done", "done") or (
        fstatus is None and json_processed
    )
    return already_processed, stats_done


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
    # Панель блокируем только если её уже заняла ДРУГАЯ команда
    # фактическим сопоставлением. Свой skeleton без привязок можно
    # переоткрыть на другую панель — выбор панели сам по себе
    # ничего не закрепляет.
    if _panel_claimed_by_other(session, team_index, panel):
        logger.warning(
            "OCR panel %s blocked for team %s "
            "(match %s/%s, session maps=%s)",
            panel, team_index, match_type, match_index,
            session.get("maps"),
        )
        await interaction.response.send_message(
            replace_emojis(
                "❌ Эта панель уже закреплена за другой командой."
            ),
            ephemeral=True,
        )
        return

    # Если у команды skeleton на ДРУГОЙ панели и в нём нет привязок —
    # пересоздаём под выбранную (выбор панели не является резервом).
    own_map = _team_map(session, team_index)
    if (
        isinstance(own_map, dict)
        and own_map.get("panel") not in (None, panel)
        and not _map_has_claims(own_map)
    ):
        _match_maps(session).pop(str(team_index), None)

    # Skeleton maps[team] — прогресс и привязки игроков по строкам.
    # Панель НЕ резервируется: закрепление — при первом bind/save.
    # Если у команды уже есть привязки на другой панели — маппинг
    # открываем на фактической панели, а не на повторно выбранной.
    actual_panel = _ensure_team_panel(
        tournament, session, team_index, panel=panel
    ) or panel
    store.set(tournament)

    name = _team_display_name(tournament, team_index)
    tm = _team_map(session, team_index) or {}
    mapped_rows = tm.get("rows") or []
    side_label = "Левая" if actual_panel == "left" else "Правая"
    done_n, total_n = _mapping_progress(session, team_index)
    embed = discord.Embed(
        title=f"🧩 {name} — сопоставление игроков",
        description=(
            f"**{side_label} панель**"
            + (" (авто)" if auto else "")
            + f" · сопоставлено **{done_n}/{total_n}**\n"
            "Назначьте ники участникам. Игроки могут выбрать свой ник "
            "сами — «Это мой ник»."
        ),
        color=discord.Color.blurple(),
    )
    shown = 0
    for i, row in enumerate(mapped_rows):
        mark = ""
        if _row_bound(row):
            if row.get("source") == "self":
                # Self-привязка — показываем просто Discord-ник
                mark = f" ✅ {_self_display(row)}"
            else:
                mark = (
                    f" ✅ {row.get('discord_name', '?')} "
                    f"({_source_label(row.get('source'))})"
                )
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
        # Канал ТУРНИРА, а не канал сообщения — OCR-карточка для
        # /tournament create живёт в канале результатов, и store.get
        # по interaction.channel.id возвращал «Турнир не найден».
        interaction.guild.id, tournament.channel_id, tournament,
        match_type, match_index, team_index, actual_panel,
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
            and session.get("status") not in ("completed", "cancelled", "finalizing")
        )
        # Rooms-сессия создана, но файлы ещё не приняты (пользователь
        # закрыл модалку) — разрешаем продолжить сбор через основную
        # кнопку, не создавая новую сессию и не меняя uploaded_by.
        if (
            self.team_index is None
            and not squad_active
            and session
            and session.get("status") not in ("completed", "cancelled", "finalizing")
        ):
            from cogs.ocr_upload import (
                RoomCollectView,
                _rooms_file_submit_allowed,
                rooms_session_can_continue,
            )

            if rooms_session_can_continue(session):
                # Ranked MM: пустой комплект может продолжить любой
                # капитан/админ — первый файл зафиксирует победителя.
                if not _rooms_file_submit_allowed(
                    tournament, session, interaction.user
                ):
                    await interaction.response.send_message(
                        replace_emojis(
                            "❌ Этот комплект может продолжить только "
                            "его отправитель или администратор."
                        ),
                        ephemeral=True,
                    )
                    return
                await interaction.response.send_message(
                    content=replace_emojis(
                        "▶️ Сессия загрузки уже создана — "
                        "продолжите отправку скриншотов:"
                    ),
                    view=RoomCollectView(
                        self.guild_id, self.channel_id, tournament,
                        self.match_type, self.match_index,
                        int(session.get("gen", 1)),
                    ),
                    ephemeral=True,
                )
                return
        if (
            session
            and session.get("status") not in ("completed", "cancelled", "finalizing")
            and not squad_active
        ):
            await interaction.response.send_message(
                replace_emojis(
                    "⚠️ Результаты этого матча уже загружены и "
                    "обрабатываются — повторная отправка не требуется. "
                    "Если нужен откат, обратитесь к администратору."
                ),
                ephemeral=True,
            )
            return

        # Главная кнопка (без team_index) → мультикомнатный флоу:
        # сначала «СКОЛЬКО КОМНАТ БЫЛО СЫГРАНО?» (1–10), затем
        # загрузка файлов. Кнопки капитанов (team_index) — squad-путь.
        # stats_channel здесь не нужен — fetch_channel это Discord API
        # и его латентность входит в 3-секундное окно interaction.
        if self.team_index is None:
            from cogs.ocr_upload import RoomCountSelectView

            view = RoomCountSelectView(
                self.guild_id, self.channel_id, tournament,
                self.match_type, self.match_index,
            )
            await interaction.response.send_message(
                content=replace_emojis(
                    "**СКОЛЬКО КОМНАТ БЫЛО СЫГРАНО?**\n"
                    "Выберите количество — потребуется столько же "
                    "скриншотов (по одному на комнату)."
                ),
                view=view,
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

async def _respond_cancel_result(
    interaction: discord.Interaction, res: str
) -> None:
    """Единый ephemeral-ответ на результат отмены OCR-комплекта."""
    if res == "forbidden":
        msg = "❌ «Неправильный скриншот» доступен только администратору."
    elif res in ("none", "done"):
        msg = "ℹ️ Активной загрузки нет."
    else:
        msg = "✅ Весь комплект скриншотов отменён — можно загрузить заново."
    await interaction.response.send_message(
        replace_emojis(msg), ephemeral=True
    )


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


ADMIN_HELP_ROLE_ID = _CFG_OCR_ADMIN_ROLE_ID


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
        active = session.get("status") not in ("completed", "cancelled", "finalizing")

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
            if not session or session.get("status") in ("completed", "finalizing"):
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
            if not session or session.get("status") in ("completed", "finalizing"):
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
                    f"**{_team_display_name(tournament, team)}** — "
                    f"сопоставлено {done_n}/{total_n}\n"
                    "Выберите свой ник — он закрепится за вами."
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
            if not session or session.get("status") in ("completed", "finalizing"):
                await interaction.response.send_message(
                    replace_emojis("❌ OCR-сессия не найдена или завершена."),
                    ephemeral=True,
                )
                return

            # Ranked MM: админское «Неправильный скриншот» = полный
            # откат комплекта (не reupload) — победитель и mappings
            # обнуляются, загрузку начинает победивший капитан заново.
            if getattr(tournament, "mm_search_channel_id", None):
                from cogs.ocr_upload import cancel_rooms_session

                res = await cancel_rooms_session(
                    interaction.guild, interaction.user, tournament,
                    self.match_type, self.match_index,
                )
                await _respond_cancel_result(interaction, res)
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
            if not session or session.get("status") in ("completed", "finalizing"):
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
            if getattr(tournament, "mm_search_channel_id", None):
                from cogs.ocr_upload import cancel_rooms_session

                res = await cancel_rooms_session(
                    interaction.guild, interaction.user, tournament,
                    self.match_type, self.match_index,
                )
                await _respond_cancel_result(interaction, res)
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
        active = session.get("status") not in ("completed", "cancelled", "finalizing")
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
            if not session or session.get("status") in ("completed", "finalizing"):
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
            if not session or session.get("status") in ("completed", "finalizing"):
                await interaction.response.send_message(
                    replace_emojis("❌ OCR-сессия не найдена или завершена."),
                    ephemeral=True,
                )
                return
            if getattr(tournament, "mm_search_channel_id", None):
                from cogs.ocr_upload import cancel_rooms_session

                res = await cancel_rooms_session(
                    interaction.guild, interaction.user, tournament,
                    self.match_type, self.match_index,
                )
                await _respond_cancel_result(interaction, res)
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
                mrows = tm.get("rows") or []
                brow = mrows[i] if i < len(mrows) else {}
                placeholder += f" · {_self_display(brow)}"
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

        # Админ может добавить строку, если OCR распознал не всех
        # игроков панели (классический двухпанельный экран, до 4 строк)
        if self.panel in ("left", "right") and len(rows) < 4:
            add = Button(
                label="➕ Строка",
                style=discord.ButtonStyle.secondary,
            )
            add.callback = self._make_add_row_callback()
            self.add_item(add)

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

    def _make_add_row_callback(self):
        async def callback(interaction: discord.Interaction) -> None:
            if not _is_server_admin(interaction.user):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Добавлять строки может только "
                        "администратор сервера."
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(
                OcrRowAddModal(
                    self.guild_id, self.channel_id, self.match_type,
                    self.match_index, self.panel,
                )
            )
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

        key = _session_key(self.match_type, self.match_index)
        async with _mapping_lock(key):
            # Свежие объекты под локом + status-guard: запись
            # маппинга сериализована с finalize/отменой — после
            # «finalizing»/«completed» состав изменить нельзя.
            tournament = store.get(self.guild_id, self.channel_id) or tournament
            session = tournament.ocr_sessions.get(key) or session
            if session.get("status") in ("completed", "finalizing", "cancelled"):
                await interaction.response.send_message(
                    replace_emojis("❌ Сопоставление уже завершено."),
                    ephemeral=True,
                )
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

            # Панель закрепляется подтверждением сопоставления: блокируем
            # только если её фактически заняла другая команда.
            if _panel_claimed_by_other(
                session, self.team_index, self.panel
            ):
                logger.warning(
                    "OCR panel %s save blocked for team %s "
                    "(match %s/%s)",
                    self.panel, self.team_index,
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
                "confirmed": True,
            }
            session.setdefault("panel_picks", {})[str(self.team_index)] = (
                self.panel
            )
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
            if session.get("status") in ("completed", "finalizing"):
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

            # Панель закрепляется подтверждением: если её уже заняла
            # другая команда — сохранение отклоняем.
            own_panel = tm.get("panel") or self.panel
            if own_panel and _panel_claimed_by_other(
                session, self.team_index, own_panel
            ):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Эта панель уже закреплена за другой командой."
                    ),
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
            tm["confirmed"] = True
            session.setdefault("panel_picks", {})[str(self.team_index)] = (
                own_panel
            )
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

    # --- Мультикомнатный режим -------------------------------------
    #Финализация только когда ВСЕ комнаты сопоставлены. Победитель —
    # команда отправителя комплекта (правило r!z3); не-участник
    # (админ) → ручной выбор победителя.
    if session.get("mode") == "rooms":
        if not _rooms_all_mapped(session, t1, t2):
            left = _rooms_unmapped_count(session)
            await interaction.response.send_message(
                replace_emojis(
                    f"✅ Комната сопоставлена. Осталось комнат без "
                    f"полного сопоставления: **{left}**."
                ),
                ephemeral=True,
            )
            return
        # Ranked MM: автоматическая финализация без кнопки
        # «Подтвердить результаты» — при полной готовности.
        if getattr(tournament, "mm_search_channel_id", None):
            done, reason, detail = await _maybe_auto_finalize_mm_rooms(
                interaction, tournament, match_type, match_index
            )
            if done:
                return
            # Не готово — причина + карточка с кнопками правки/админа.
            await _reply_ephemeral(
                interaction, _rooms_ready_reason_text(reason, detail)
            )
            await _rooms_send_confirmation(
                interaction, tournament, match_type, match_index,
            )
            return
        await _rooms_send_confirmation(
            interaction, tournament, match_type, match_index,
        )
        return

    is_mm = bool(getattr(tournament, "mm_search_channel_id", None))
    if is_mm:
        # Ranked MM: победитель — команда отправителя первого
        # принятого файла (зафиксирована в сессии), OCR не участвует.
        winner = _session_mm_winner(tournament, session, t1, t2)
    else:
        winner = _ocr_winning_team_index(session)
    name = _team_display_name(tournament, team_index)

    if winner is not None and winner in (t1, t2):
        # Скриншот обязан прислать капитан ПОБЕДИВШЕЙ команды
        # (левая панель / баннер win на скриншоте). Чужой или
        # неизвестный загрузивший → блок автоподтверждения + орг.
        # Для Ranked MM проверка не нужна: победитель уже равен
        # команде отправителя комплекта по построению.
        if session.get("mode") == "squad":
            sentry = session.get("squads", {}).get(str(winner)) or {}
            uploader_id = sentry.get("uploaded_by")
        else:
            uploader_id = session.get("uploaded_by")
        if is_mm:
            uploader_ok = True
        else:
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
                    + ("(по отправителю скриншота)."
                       if is_mm else "(по OCR).")
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


# Подпись последнего применённого состояния OCR-сообщений:
# session_key -> sha1. Повторный refresh без изменений состояния
# не редактирует сообщение — иначе каждое нажатие перерисовывает
# MediaGallery и картинка «обновляется бесконечно».
_REFRESH_SIGS: dict[str, str] = {}
_REFRESH_SIGS_MAX = 500


def _session_view_sig(session: dict) -> str:
    """Хэш состояния сессии, от которого зависят кнопки/view сообщения."""
    payload = {
        k: session.get(k)
        for k in (
            "status", "mode", "maps", "squads", "squad_pool",
            "message_id",
        )
    }
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def _store_refresh_sig(sig_key: str, sig: str) -> None:
    _REFRESH_SIGS[sig_key] = sig
    if len(_REFRESH_SIGS) > _REFRESH_SIGS_MAX:
        _REFRESH_SIGS.pop(next(iter(_REFRESH_SIGS)))


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
    sig = _session_view_sig(session)
    sig_key = (
        f"{tournament.guild_id}:{tournament.channel_id}:"
        f"{_session_key(match_type, match_index)}"
    )
    if _REFRESH_SIGS.get(sig_key) == sig:
        return  # состояние не менялось — сообщение уже актуально
    channel = await _results_channel(interaction, session)
    if channel is None:
        return

    # Squad-режим: у каждого отряда своё сообщение в канале результатов
    if session.get("mode") == "squad":
        try:
            t1, t2 = _match_tuple(tournament, match_type, match_index)
        except Exception:
            t1, t2 = session.get("teams", [0, 1])[:2]
        squad_ok = 0
        squad_total = 0
        for entry in session.get("squad_pool", []):
            mid = entry.get("message_id")
            if not mid:
                continue
            squad_total += 1
            try:
                msg = await channel.fetch_message(mid)
                await msg.edit(
                    view=smart_view(msg, SquadClaimView(
                        tournament.guild_id, tournament.channel_id,
                        tournament, match_type, match_index,
                        entry["uid"],
                    ))
                )
                squad_ok += 1
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
            squad_total += 1
            try:
                msg = await channel.fetch_message(mid)
                await msg.edit(
                    view=smart_view(msg, SquadAssignedView(
                        tournament.guild_id, tournament.channel_id,
                        tournament, match_type, match_index, tidx,
                    ))
                )
                squad_ok += 1
            except Exception as e:
                logger.warning("Squad assigned view refresh failed: %s", e)
        # Подпись ставим только если хоть одно сообщение обновилось —
        # иначе следующий вызов повторит попытку.
        if squad_total == 0 or squad_ok:
            _store_refresh_sig(sig_key, sig)
        return

    if not session.get("message_id"):
        _store_refresh_sig(sig_key, sig)
        return
    try:
        msg = await channel.fetch_message(session["message_id"])
        await msg.edit(
            view=smart_view(msg, OCRPublicView(
                tournament.guild_id, tournament.channel_id, tournament,
                match_type, match_index,
            ))
        )
        _store_refresh_sig(sig_key, sig)
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
            options.append(
                discord.SelectOption(
                    label=label[:100], value=str(i),
                    description=desc[:100],
                )
            )
        self.select = Select(
            placeholder="Выберите свой игровой ник",
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
        if not session or session.get("status") in ("completed", "finalizing"):
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

        # Панель закрепляется фактической привязкой: если другая
        # команда уже заняла её — self-bind отклоняем.
        own_panel = tm.get("panel")
        if own_panel and _panel_claimed_by_other(
            session, team, own_panel
        ):
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Эта панель уже закреплена за другой командой."
                ),
                ephemeral=True,
            )
            return

        if _row_bound(row):
            try:
                same = int(row.get("discord_id")) == uid
            except (TypeError, ValueError):
                same = False
            if not same:
                # Конфликт: ник уже закреплён за другим игроком
                if row.get("source") == "self":
                    holder = _self_display(row)
                else:
                    holder = (
                        f"{row.get('discord_name', '?')} "
                        f"({_source_label(row.get('source'))})"
                    )
                await _notify_mapping_conflict(
                    interaction.guild, session,
                    "⚠️ **OCR mapping conflict** — "
                    f"{_match_label(match_type, match_index)}: "
                    f"<@{uid}> пытался занять ник "
                    f"«{row.get('ocr_name', '?')}», уже закреплённый "
                    f"за {holder}.",
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
        row["discord_display"] = interaction.user.display_name
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


class OcrRowAddModal(Modal, title="Добавить строку OCR"):
    """Админ вручную добавляет строку игрока, которую OCR не распознал.

    Дописывает строку в конец panels[panel].rows и в снимок
    maps[team].rows — индексы остаются согласованными. После добавления
    нужно переоткрыть окно сопоставления, чтобы появилась новая строка.
    """

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        match_type: str,
        match_index: int,
        panel: str,
    ):
        super().__init__()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.panel = panel

        self.name_input = TextInput(
            label="Игровой ник (OCR)",
            placeholder="Ник как на экране результатов",
            required=True,
            max_length=64,
        )
        self.kills_input = TextInput(
            label="Убийства (K)", required=True, max_length=4,
        )
        self.deaths_input = TextInput(
            label="Смерти (D)", required=True, max_length=4,
        )
        self.assists_input = TextInput(
            label="Ассисты (A)", required=True, max_length=4,
        )
        self.dmg_input = TextInput(
            label="Урон (DMG)", required=True, max_length=8,
        )
        for item in (
            self.name_input, self.kills_input, self.deaths_input,
            self.assists_input, self.dmg_input,
        ):
            self.add_item(item)

    @staticmethod
    def _to_int(value: str) -> int | None:
        try:
            return int((value or "").strip())
        except (TypeError, ValueError):
            return None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_server_admin(interaction.user):
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Добавлять строки может только "
                    "администратор сервера."
                ),
                ephemeral=True,
            )
            return

        key = _session_key(self.match_type, self.match_index)
        async with _mapping_lock(key):
            tournament = store.get(self.guild_id, self.channel_id)
            session = (
                tournament.ocr_sessions.get(key)
                if tournament else None
            )
            if not session:
                await interaction.response.send_message(
                    replace_emojis("❌ OCR-сессия не найдена."),
                    ephemeral=True,
                )
                return
            if session.get("status") in ("completed", "finalizing"):
                await interaction.response.send_message(
                    replace_emojis("❌ Сопоставление уже завершено."),
                    ephemeral=True,
                )
                return

            prows = session.setdefault("panels", {}).setdefault(
                self.panel, {}
            ).setdefault("rows", [])
            if len(prows) >= 4:
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ На панели уже 4 строки — больше не нужно."
                    ),
                    ephemeral=True,
                )
                return

            kills = self._to_int(self.kills_input.value)
            deaths = self._to_int(self.deaths_input.value)
            assists = self._to_int(self.assists_input.value)
            dmg = self._to_int(self.dmg_input.value)
            if None in (kills, deaths, assists, dmg):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Статистика должна быть целыми числами."
                    ),
                    ephemeral=True,
                )
                return

            name = (self.name_input.value or "").strip()[:64]
            if not name:
                await interaction.response.send_message(
                    replace_emojis("❌ Укажите игровой ник."),
                    ephemeral=True,
                )
                return

            prows.append({
                "name": name,
                "kills": kills,
                "deaths": deaths,
                "assists": assists,
                "dmg": dmg,
            })
            # Снимок maps[team] — та же строка в конец, индексы 1:1
            for tm in (session.get("maps") or {}).values():
                if not isinstance(tm, dict) or tm.get("panel") != self.panel:
                    continue
                (tm.setdefault("rows", [])).append({
                    "ocr_name": name,
                    "kills": kills,
                    "deaths": deaths,
                    "assists": assists,
                    "dmg": dmg,
                    "discord_name": None,
                    "discord_id": None,
                    "source": None,
                })
            store.set(tournament)

        await interaction.response.send_message(
            replace_emojis(
                "✅ Строка добавлена. Переоткройте сопоставление "
                "игроков — там появится новый ряд."
            ),
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
    """Обёртка финализации с per-match локом (защита от двойного ELO)."""
    key = _session_key(match_type, match_index)
    lock = _finalize_lock(key)
    async with lock:
        # Тот же per-match лок, что у административной отмены комплекта:
        # финализация и «Неправильный скриншот» не могут идти
        # одновременно — отмена либо увидит финализированную сессию,
        # либо удалит её до старта начисления ELO.
        from cogs.ocr_upload import _room_lock

        async with _room_lock(key):
            await _finalize_ocr_match_impl(
                interaction, tournament, match_type, match_index,
                winning_team_index,
            )


async def _finalize_ocr_match_impl(
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
    is_rooms = bool(session and session.get("mode") == "rooms")
    if not session or (
        not is_rooms and len(session.get("maps", {})) < 2
    ):
        raise ValueError("OCR-сессия не готова")

    match_id = f"{tournament.id}_{key}"
    t1, t2 = _match_tuple(tournament, match_type, match_index)

    # Повторная проверка готовности ПОД _room_lock (мы уже внутри
    # finalize_ocr_match): между ready-check вызывающего и захватом
    # лока другое действие могло изменить сопоставление или
    # статистику. Финализируем только актуальный согласованный
    # снимок. Только Ranked MM — обычные турниры сохраняют прежний
    # ручной контроль (частичный маппинг допустим при confirm).
    if getattr(tournament, "mm_search_channel_id", None):
        if is_rooms:
            ready, reason, _ = check_ocr_session_ready(
                tournament, session, t1, t2, require_winner=False
            )
        elif session.get("mode") == "squad":
            # Squad: оба отряда назначены, 4 игрока на команду,
            # уникальные Discord ID из ростеров, K/D/A/Damage полны.
            ready, reason = _squad_session_ready(
                tournament, session, t1, t2
            )
        else:
            ready = _classic_mapping_complete(session, t1, t2)
            reason = "unmapped"
        if not ready:
            raise ValueError(
                f"OCR-сессия не готова ({reason}) — данные изменились "
                "после проверки, повторите после исправления"
            )

    if is_rooms:
        # Победитель rooms-матча — ТОЛЬКО зафиксированный в сессии
        # (команда отправителя первого принятого файла). Аргумент
        # вызывающего не может его переопределить; None (админ-
        # комплект) → явный выбор организатора, переданный аргументом.
        pinned = _rooms_confirmed_winner(tournament, session, t1, t2)
    elif getattr(tournament, "mm_search_channel_id", None):
        # Ranked MM classic/squad: тот же принцип — победитель
        # зафиксирован отправителем первого принятого файла.
        pinned = _session_mm_winner(tournament, session, t1, t2)
    else:
        pinned = None
    if pinned is not None:
        winning_team_index = pinned

    # Атомарный переход в финализацию: статус фиксируется в store
    # ДО начисления ELO. Отмена и callbacks сериализованы _room_lock
    # обёртки; «finalizing» дополнительно отсекает mapping-операции
    # (их guard пропускает только collecting/awaiting_mapping/mapping).
    # При падении до stats_done повторный finalize идемпотентен
    # (PG-гейт), а админ-отмена недоведённого комплекта допустима.
    if session.get("status") != "finalizing":
        session["status"] = "finalizing"
        store.set(tournament)

    # Статистика: discord_name -> {kills,deaths,assists,damage,ocr_name[,extra]}
    temp_stats: dict[str, dict[str, Any]] = {}
    if is_rooms:
        # Агрегация по всем комнатам: суммы K/D/A/DMG на discord_id
        # (ключ агрегата — roster-имя, см. _rooms_aggregate_stats).
        # Один матч = одна победа/ELO.
        temp_stats = _rooms_aggregate_stats(session, tournament, t1, t2)
    else:
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

    # Restart-safe идемпотентность: match_finalizations в PostgreSQL —
    # authoritative-статус, переживает рестарт и второй процесс.
    # stats_done пишется В ТОЙ ЖЕ транзакции, что и player_stats —
    # значит «статистика закоммичена» и «статус записан» не расходятся.
    from storage.finalize_store import (
        get_status as _fin_get_status,
        mark_done as _fin_mark_done,
    )

    fstatus = await _fin_get_status(guild_id, match_id)
    json_processed = match_id in tournament.processed_elo_matches
    already_processed, stats_done = _finalize_stats_gate(
        fstatus, json_processed
    )

    if not already_processed:
        # 1. ELO/XP/монеты + запись temp_match_stats (process_match_result).
        # Пропускаем, если статистика уже закоммичена до падения —
        # повторный вызов применил бы ELO дважды.
        if not stats_done:
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
                    "finalize_match_id": match_id,
                },
                interaction,
            )

        # Возвращаем ocr_name в сохранённую статистику (исходный ник с экрана)
        stored = tournament.temp_match_stats.get(match_id, {})
        for name, kd in temp_stats.items():
            if name in stored:
                stored[name]["ocr_name"] = kd["ocr_name"]
        store.set(tournament)

        # 2. История матчей для всех игроков (UNIQUE match_id —
        # повторная запись безопасна)
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

        # 5. Финализация завершена — устойчивый маркер в PostgreSQL
        await _fin_mark_done(guild_id, match_id)
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

        # Rooms-режим: итогового счёта раундов нет — карточка матча
        # без score (не подставляем убийства вместо счёта).
        score = None if is_rooms else ordered_round_score(session, t1, t2)
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

    # Публичное OCR-сообщение: гасим кнопки, оставляем скриншот и
    # короткий финальный статус. Итог с именем победителя уходит
    # только инициатору — ephemeral.
    from cogs.ocr_upload import _mapping_layout

    done_caption = "✅ Матч завершён — результат в канале результатов."
    done_view = _mapping_layout(None, done_caption)

    async def _strip_buttons(mid: int) -> None:
        if channel is None or not mid:
            return
        try:
            msg = await channel.fetch_message(mid)
            await msg.edit(view=done_view)
        except Exception as e:
            logger.warning("OCR message finalize edit failed: %s", e)

    if session.get("mode") == "squad":
        # У каждого отряда своё сообщение — гасим кнопки на всех
        mids = [
            s.get("message_id") for s in session.get("squads", {}).values()
        ] + [
            e.get("message_id") for e in session.get("squad_pool", [])
        ]
        for mid in mids:
            await _strip_buttons(mid)
    elif is_rooms:
        # Гасим кнопки на сообщениях всех комнат + прогресс
        mids = []
        for r in session.get("rooms") or []:
            mids.extend(r.get("message_ids") or [])
            if r.get("message_id"):
                mids.append(r["message_id"])
        prog_ch_id = session.get("progress_channel_id")
        prog_id = session.get("progress_message_id")
        for mid in dict.fromkeys(mids):
            await _strip_buttons(mid)
        if prog_ch_id and prog_id and channel is not None:
            try:
                pmsg = await channel.fetch_message(prog_id)
                await pmsg.edit(
                    content=replace_emojis(done_caption), view=None
                )
            except Exception as e:
                logger.warning("rooms progress finalize edit failed: %s", e)
        # Сообщение подтверждения результатов — гасим кнопки
        cmid = session.get("confirm_message_id")
        if cmid and channel is not None:
            try:
                cmsg = await channel.fetch_message(cmid)
                await cmsg.edit(
                    content=replace_emojis(done_caption), view=None
                )
            except Exception as e:
                logger.warning("rooms confirm finalize edit failed: %s", e)
    else:
        await _strip_buttons(session.get("message_id"))

    # Уведомление о победителе — только тому, кто подтвердил результат
    try:
        await interaction.followup.send(
            replace_emojis(final_text), ephemeral=True
        )
    except Exception:
        try:
            await interaction.response.send_message(
                replace_emojis(final_text), ephemeral=True
            )
        except Exception as e:
            logger.warning("final result ephemeral failed: %s", e)

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

    # Длительность матча: от «Начало» в карточке матча до подтверждения
    # результата. Фолбэк — created_at OCR-сессии.
    duration_seconds = 0
    start_ts: float | None = None
    for _m in (tournament.match_channels or {}).values():
        if (
            _m.get("match_type") == match_type
            and _m.get("match_index") == match_index
            and _m.get("started_at")
        ):
            try:
                start_ts = float(_m["started_at"])
            except (TypeError, ValueError):
                start_ts = None
            break
    if start_ts is None:
        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        created_raw = session.get("created_at")
        if created_raw:
            try:
                start_ts = datetime.fromisoformat(str(created_raw)).timestamp()
            except Exception:
                start_ts = None
    if start_ts is not None:
        duration_seconds = max(0, int(time.time() - start_ts))
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
                duration_seconds=duration_seconds,
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

    match_id = f"{tournament.id}_{_session_key(match_type, match_index)}"
    name_a = _team_display_name(tournament, t1)
    name_b = _team_display_name(tournament, t2)
    winning_name = name_a if winning_team_index == t1 else name_b
    winning_pos = 0 if winning_team_index == t1 else 1

    try:
        # Выплата выигрышей атомарна внутри resolve_match_bets.
        _, payouts_by_name = await bet_store.resolve_match_bets(
            tournament.guild_id, tournament.id, match_id,
            winning_name, winning_pos,
        )
        for user_name, payout in payouts_by_name.items():
            if payout > 0:
                tournament.betting_results[user_name] = (
                    tournament.betting_results.get(user_name, 0) + payout
                )
        store.set(tournament)
    except Exception as e:
        logger.error("OCR bet resolution failed: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Мультикомнатный режим (OCR V2): маппинг по комнатам
# ---------------------------------------------------------------------------

def _rooms_session_fresh(guild_id: int, channel_id: int,
                         match_type: str, match_index: int):
    """Свежие tournament+session для rooms-операций."""
    tournament = store.get(guild_id, channel_id)
    if not tournament:
        return None, None
    session = tournament.ocr_sessions.get(
        _session_key(match_type, match_index)
    )
    return tournament, session


def _room_map_team(
    tournament: Tournament, user, team_index: int
) -> bool:
    """Маппить команду может её капитан, участник или организатор."""
    if is_org_check(user, getattr(user, "guild", None)):
        return True
    try:
        uid = int(user.id)
    except (TypeError, ValueError):
        return False
    for name in team_roster(tournament, team_index):
        mapped = tournament.player_user_ids.get(name)
        try:
            if mapped and int(mapped) == uid:
                return True
        except (TypeError, ValueError):
            continue
    return False


async def _rooms_after_map(
    interaction: discord.Interaction,
    tournament: Tournament,
    match_type: str,
    match_index: int,
    room_index: int,
) -> None:
    """После сохранения привязок комнаты: финализация или прогресс."""
    fresh, session = _rooms_session_fresh(
        tournament.guild_id, tournament.channel_id, match_type, match_index
    )
    if not session or session.get("mode") != "rooms":
        return
    t1, t2 = _match_tuple(fresh or tournament, match_type, match_index)
    if _rooms_all_mapped(session, t1, t2):
        # Ranked MM: при полной готовности — автоматическая
        # финализация без кнопки подтверждения; иначе карточка
        # с кнопками правки и конкретной причиной.
        if getattr(fresh or tournament, "mm_search_channel_id", None):
            done, reason, detail = await _maybe_auto_finalize_mm_rooms(
                interaction, fresh or tournament, match_type, match_index
            )
            if done:
                return
            await _reply_ephemeral(
                interaction, _rooms_ready_reason_text(reason, detail)
            )
            await _rooms_send_confirmation(
                interaction, fresh or tournament, match_type, match_index,
            )
            return
        # Обычные турниры: отдельное подтверждение результатов
        # с агрегированной статистикой.
        await _rooms_send_confirmation(
            interaction, fresh or tournament, match_type, match_index,
        )
        return
    left = _rooms_unmapped_count(session)
    await interaction.followup.send(
        replace_emojis(
            f"✅ Комната {room_index} сопоставлена. "
            f"Осталось комнат: **{left}**."
        ),
        ephemeral=True,
    )


def _rooms_guard(
    interaction: discord.Interaction, tournament, session,
    room_index: int,
) -> tuple:
    """Проверки для room-операций. (rsession, error_text)."""
    if not session or session.get("mode") != "rooms":
        return None, "❌ Сессия не найдена или не мультикомнатная."
    if session.get("status") not in (
        "awaiting_mapping", "mapping", "collecting"
    ):
        return None, "⚠️ Сопоставление для этого матча уже завершено."
    rs = _room_session(session, room_index)
    if rs is None:
        return None, "❌ Комната не найдена."
    return rs, None


class RoomView(View):
    """Кнопки под скриншотом комнаты: маппинг команд, self, откат."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
        room_index: int,
        timeout: float | None = None,
    ):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.room_index = room_index
        try:
            t1, t2 = _match_tuple(tournament, match_type, match_index)
        except Exception:
            t1 = t2 = 0
        for team_index in (t1, t2):
            if not team_index:
                continue
            b = Button(
                label=f"🧩 {_team_display_name(tournament, team_index)}",
                style=discord.ButtonStyle.primary,
                custom_id=(
                    f"room_map:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{room_index}:{team_index}"
                ),
                row=0,
            )
            b.callback = self._make_team_cb(team_index)
            self.add_item(b)
        b = Button(
            label="🎯 Это мой ник",
            style=discord.ButtonStyle.success,
            custom_id=(
                f"room_self:{guild_id}:{channel_id}:{match_type}:"
                f"{match_index}:{room_index}"
            ),
            row=1,
        )
        b.callback = self._on_self
        self.add_item(b)
        b = Button(
            label="❌ Неправильный скриншот",
            style=discord.ButtonStyle.danger,
            custom_id=(
                f"room_wrong:{guild_id}:{channel_id}:{match_type}:"
                f"{match_index}:{room_index}"
            ),
            row=1,
        )
        b.callback = self._on_wrong
        self.add_item(b)
        b = Button(
            label="📣 Вызвать админа",
            style=discord.ButtonStyle.secondary,
            custom_id=(
                f"room_admin:{guild_id}:{channel_id}:{match_type}:"
                f"{match_index}:{room_index}"
            ),
            row=1,
        )
        b.callback = self._on_admin
        self.add_item(b)

    def _make_team_cb(self, team_index: int):
        async def _cb(interaction: discord.Interaction) -> None:
            await self._on_team(interaction, team_index)
        return _cb

    async def _on_team(
        self, interaction: discord.Interaction, team_index: int
    ) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        _, session = _rooms_session_fresh(
            self.guild_id, self.channel_id,
            self.match_type, self.match_index,
        )
        rs, err = _rooms_guard(
            interaction, tournament, session, self.room_index
        )
        if err:
            await interaction.response.send_message(
                replace_emojis(err), ephemeral=True
            )
            return
        if not _room_map_team(interaction.user, tournament, team_index):
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Сопоставлять команду может её капитан, "
                    "участник или организатор."
                ),
                ephemeral=True,
            )
            return
        if int(session.get("rooms_received") or 0) < int(
            session.get("rooms_expected") or 0
        ):
            await interaction.response.send_message(
                replace_emojis(
                    "⏳ Дождитесь полного комплекта скриншотов."
                ),
                ephemeral=True,
            )
            return
        room = next(
            (r for r in session.get("rooms") or []
             if int(r.get("room_index", 0)) == int(self.room_index)),
            None,
        )
        if room is not None and room.get("kind") == "squad":
            # Squad-комната: скриншоты только своих отрядов, вторая
            # команда — вручную. Свободная squad-панель достаётся
            # первому открывшему маппинг.
            maps = room.get("maps") or {}
            squad_panel = _squad_panel_for_team(room, team_index)
            if squad_panel and str(team_index) not in maps:
                _ensure_room_panel_map(
                    tournament, rs, team_index, squad_panel
                )
                store.set(tournament)
                await _open_room_team_map(
                    interaction, tournament, self.match_type,
                    self.match_index, self.room_index, team_index,
                    squad_panel,
                )
            else:
                _ensure_manual_map(rs, team_index)
                store.set(tournament)
                await _open_room_team_map(
                    interaction, tournament, self.match_type,
                    self.match_index, self.room_index, team_index, "manual",
                )
            return
        panel = (rs.get("panel_picks") or {}).get(str(team_index))
        if panel not in ("left", "right"):
            panel = _auto_panel_for_team(tournament, rs, team_index)
        if panel not in ("left", "right"):
            await interaction.response.send_message(
                replace_emojis(
                    "Выберите панель вашей команды на этом скриншоте:"
                ),
                view=RoomPanelPickView(
                    self.guild_id, self.channel_id, tournament,
                    self.match_type, self.match_index,
                    self.room_index, team_index,
                ),
                ephemeral=True,
            )
            return
        _ensure_room_panel_map(tournament, rs, team_index, panel)
        store.set(tournament)
        await _open_room_team_map(
            interaction, tournament, self.match_type,
            self.match_index, self.room_index, team_index, panel,
        )

    async def _on_self(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        _, session = _rooms_session_fresh(
            self.guild_id, self.channel_id,
            self.match_type, self.match_index,
        )
        rs, err = _rooms_guard(
            interaction, tournament, session, self.room_index
        )
        if err:
            await interaction.response.send_message(
                replace_emojis(err), ephemeral=True
            )
            return
        if int(session.get("rooms_received") or 0) < int(
            session.get("rooms_expected") or 0
        ):
            await interaction.response.send_message(
                replace_emojis(
                    "⏳ Дождитесь полного комплекта скриншотов."
                ),
                ephemeral=True,
            )
            return
        t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
        team_index = _team_of_user(
            tournament, (t1, t2), interaction.user.id
        )
        if team_index is None:
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Вы не участник этого матча — выбрать свой "
                    "ник могут только игроки команд."
                ),
                ephemeral=True,
            )
            return
        room = next(
            (r for r in session.get("rooms") or []
             if int(r.get("room_index", 0)) == int(self.room_index)),
            None,
        )
        if room is not None and room.get("kind") == "squad":
            maps = room.get("maps") or {}
            tm = maps.get(str(team_index))
            if tm is None:
                squad_panel = _squad_panel_for_team(room, team_index)
                if squad_panel:
                    _ensure_room_panel_map(
                        tournament, rs, team_index, squad_panel
                    )
                else:
                    _ensure_manual_map(rs, team_index)
                store.set(tournament)
            panel = (_team_map(rs, team_index) or {}).get("panel")
        else:
            panel = (rs.get("panel_picks") or {}).get(str(team_index))
            if panel not in ("left", "right"):
                panel = _auto_panel_for_team(tournament, rs, team_index)
            if panel in ("left", "right"):
                _ensure_room_panel_map(tournament, rs, team_index, panel)
                store.set(tournament)
        if panel != "manual" and not (
            panel in ("left", "right") or str(panel).startswith("squad")
        ):
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Сторона вашей команды на этом скриншоте ещё "
                    "не определена — попросите капитана выбрать панель."
                ),
                ephemeral=True,
            )
            return
        name = _roster_name_for_uid(tournament, team_index, interaction.user.id)
        view = RoomSelfMapView(
            self.guild_id, self.channel_id, tournament,
            self.match_type, self.match_index, self.room_index,
            team_index, panel, interaction.user.id,
            interaction.user.display_name, name or "?",
        )
        await interaction.response.send_message(
            content=replace_emojis(
                f"**Комната {self.room_index}** — выберите свой "
                "игровой ник из списка (только не занятые строки):"
            ),
            view=view,
            ephemeral=True,
        )

    async def _on_wrong(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        from cogs.ocr_upload import cancel_rooms_session

        res = await cancel_rooms_session(
            interaction.guild, interaction.user, tournament,
            self.match_type, self.match_index,
        )
        if res == "forbidden":
            await interaction.response.send_message(
                replace_emojis(
                    "❌ «Неправильный скриншот» доступен только "
                    "администратору."
                ),
                ephemeral=True,
            )
        elif res in ("none", "done"):
            await interaction.response.send_message(
                replace_emojis("ℹ️ Активной загрузки нет."),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                replace_emojis(
                    "✅ Весь комплект скриншотов отменён — можно "
                    "загрузить заново."
                ),
                ephemeral=True,
            )

    async def _on_admin(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        try:
            ch = interaction.guild.get_channel(OCR_STATS_CHANNEL_ID)
            if ch is None:
                ch = await interaction.guild.fetch_channel(OCR_STATS_CHANNEL_ID)
            await ch.send(
                replace_emojis(
                    f"📣 **{interaction.user}** вызвал администратора "
                    f"— матч **{_match_label(self.match_type, self.match_index)}**, "
                    f"комната {self.room_index}."
                )
            )
        except Exception as e:
            logger.warning("room call-admin failed: %s", e)
        await interaction.response.send_message(
            replace_emojis("📣 Администратор уведомлён."), ephemeral=True
        )


class RoomPanelPickView(View):
    """Ручной выбор панели для комнаты (когда авто-определение не сработало)."""

    def __init__(
        self, guild_id: int, channel_id: int, tournament,
        match_type: str, match_index: int, room_index: int,
        team_index: int,
    ):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.room_index = room_index
        self.team_index = team_index
        _, session = _rooms_session_fresh(
            guild_id, channel_id, match_type, match_index
        )
        rs = _room_session(session, room_index) if session else None
        panels = (rs or {}).get("panels") or {}
        options = []
        for side, label in (("left", "Левая панель"), ("right", "Правая панель")):
            pname = (panels.get(side) or {}).get("name")
            options.append(discord.SelectOption(
                label=(f"{label}: {pname}" if pname else label)[:100],
                value=side,
                description=(pname or "")[:100],
            ))
        self.select = Select(
            placeholder="Ваша команда — на какой панели?",
            options=options, min_values=1, max_values=1,
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        values = getattr(self.select, "values", None) or []
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
        _, session = _rooms_session_fresh(
            self.guild_id, self.channel_id,
            self.match_type, self.match_index,
        )
        rs = _room_session(session, self.room_index) if session else None
        if rs is None:
            await interaction.response.send_message(
                replace_emojis("❌ Комната не найдена."), ephemeral=True
            )
            return
        rs.setdefault("panel_picks", {})[str(self.team_index)] = panel
        store.set(tournament)
        await _open_room_team_map(
            interaction, tournament, self.match_type,
            self.match_index, self.room_index, self.team_index, panel,
        )


async def _open_room_team_map(
    interaction: discord.Interaction,
    tournament: Tournament,
    match_type: str,
    match_index: int,
    room_index: int,
    team_index: int,
    panel: str,
) -> None:
    """Открыть ephemeral-маппинг команды внутри комнаты."""
    _, session = _rooms_session_fresh(
        tournament.guild_id, tournament.channel_id,
        match_type, match_index,
    )
    rs = _room_session(session, room_index) if session else None
    if rs is None:
        await interaction.response.send_message(
            replace_emojis("❌ Комната не найдена."), ephemeral=True
        )
        return
    if _panel_claimed_by_other(rs, team_index, panel):
        await interaction.response.send_message(
            replace_emojis(
                "❌ Эта панель уже закреплена за другой командой "
                "в этой комнате."
            ),
            ephemeral=True,
        )
        return
    _ensure_room_panel_map(tournament, rs, team_index, panel)
    store.set(tournament)

    name = _team_display_name(tournament, team_index)
    tm = _team_map(rs, team_index) or {}
    rows = tm.get("rows") or []
    if panel == "manual":
        side_label = "Ручной ввод"
    elif str(panel).startswith("squad"):
        side_label = "Отряд"
    else:
        side_label = "Левая" if panel == "left" else "Правая"
    embed = discord.Embed(
        title=f"🧩 {name} — Комната {room_index}",
        description=(
            f"**{side_label} панель** · сопоставьте игроков. "
            "Игроки могут выбрать свой ник сами («Это мой ник»."
        ),
        color=discord.Color.blurple(),
    )
    for i, row in enumerate(rows):
        mark = ""
        if _row_bound(row):
            mark = (
                f" ✅ {_self_display(row)}"
                if row.get("source") == "self"
                else f" ✅ {row.get('discord_name', '?')}"
            )
        embed.add_field(
            name=f"{i + 1}. {row.get('ocr_name', '?')}{mark}",
            value=(
                f"K/D/A: {_rs(row.get('kills'))}/{_rs(row.get('deaths'))}/"
                f"{_rs(row.get('assists'))} • DMG {_rs(row.get('dmg'))}"
            ),
            inline=False,
        )
    view = RoomTeamMapView(
        interaction.guild.id, tournament.channel_id, tournament,
        match_type, match_index, room_index, team_index, panel,
    )
    await interaction.response.send_message(
        view=compose(embed, view), ephemeral=True
    )


class RoomTeamMapView(View):
    """Маппинг 4 строк команды внутри одной комнаты."""

    def __init__(
        self, guild_id: int, channel_id: int, tournament,
        match_type: str, match_index: int, room_index: int,
        team_index: int, panel: str,
    ):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.room_index = room_index
        self.team_index = team_index
        self.panel = panel
        self.selects: dict[int, Select] = {}

        _, session = _rooms_session_fresh(
            guild_id, channel_id, match_type, match_index
        )
        rs = _room_session(session, room_index) if session else None
        tm = (_team_map(rs, self.team_index) if rs else None) or {}
        rows = tm.get("rows") or []
        roster = team_roster(tournament, self.team_index)

        eligible = list(range(min(len(rows), 4)))
        for i in eligible:
            row = rows[i]
            cur = row.get("discord_name")
            forbidden = set()
            for tk, tm2 in _match_maps(rs or {}).items():
                for j, r2 in enumerate(tm2.get("rows") or []):
                    if (str(self.team_index), i) == (tk, j):
                        continue
                    dn = r2.get("discord_name")
                    if dn and dn != cur:
                        forbidden.add(dn)
            options = [
                discord.SelectOption(label="— не назначен —", value=""),
            ]
            for name in roster:
                if name in forbidden:
                    continue
                options.append(discord.SelectOption(
                    label=name[:100], value=name[:100],
                ))
            if cur and cur not in [o.value for o in options]:
                options.append(discord.SelectOption(
                    label=f"{cur} (текущий)", value=cur[:100],
                ))
            sel = Select(
                placeholder=f"Строка {i + 1}: {row.get('ocr_name', '?')}",
                options=options[:25],
                min_values=1, max_values=1,
                row=i,
            )
            sel.callback = self._make_row_cb(i)
            self.selects[i] = sel
            self.add_item(sel)

        save = Button(
            label="✅ Сохранить сопоставление",
            style=discord.ButtonStyle.success,
            custom_id=(
                f"room_save:{guild_id}:{channel_id}:{match_type}:"
                f"{match_index}:{room_index}:{team_index}"
            ),
            row=4,
        )
        save.callback = self._on_save
        self.add_item(save)

    def _make_row_cb(self, i: int):
        async def _cb(interaction: discord.Interaction) -> None:
            values = getattr(self.selects[i], "values", None) or []
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(
                replace_emojis(
                    f"Строка {i + 1} → "
                    f"**{values[0] if values and values[0] else '—'}** "
                    "(сохранится по кнопке ниже)"
                ),
                ephemeral=True,
            )
        return _cb

    async def _on_save(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        _, session = _rooms_session_fresh(
            self.guild_id, self.channel_id,
            self.match_type, self.match_index,
        )
        rs, err = _rooms_guard(
            interaction, tournament, session, self.room_index
        )
        if err:
            await interaction.response.send_message(
                replace_emojis(err), ephemeral=True
            )
            return
        if not _room_map_team(interaction.user, tournament, self.team_index):
            await interaction.response.send_message(
                replace_emojis("❌ Нет прав на сопоставление этой команды."),
                ephemeral=True,
            )
            return

        chosen: dict[int, str] = {}
        for i, sel in self.selects.items():
            vals = getattr(sel, "values", None) or []
            if vals:
                chosen[i] = vals[0]

        async with _mapping_lock(
            _session_key(self.match_type, self.match_index)
        ):
            tournament = store.get(self.guild_id, self.channel_id)
            _, session = _rooms_session_fresh(
                self.guild_id, self.channel_id,
                self.match_type, self.match_index,
            )
            rs = _room_session(session, self.room_index)
            tm = _team_map(rs, self.team_index)
            if tm is None or tm.get("panel") != self.panel:
                _ensure_room_panel_map(
                    tournament, rs, self.team_index, self.panel
                )
                tm = _team_map(rs, self.team_index)
            rows = (tm or {}).get("rows") or []

            used: dict[int, tuple] = {}
            for tk, tm2 in _match_maps(rs).items():
                for j, r in enumerate(tm2.get("rows") or []):
                    try:
                        if r.get("discord_id"):
                            used[int(r["discord_id"])] = (int(tk), j)
                    except (TypeError, ValueError):
                        pass

            uid_by_name = {
                n: tournament.player_user_ids.get(n)
                for n in team_roster(tournament, self.team_index)
            }
            errors = []
            for i, name in chosen.items():
                if not name or i >= len(rows):
                    continue
                row = rows[i]
                old_did = row.get("discord_id")
                uid = uid_by_name.get(name)
                try:
                    uid_i = int(uid) if uid else None
                except (TypeError, ValueError):
                    uid_i = None
                if uid_i is not None:
                    loc = used.get(uid_i)
                    if loc is not None and loc != (self.team_index, i):
                        errors.append(
                            f"Игрок {name} уже привязан к другой строке "
                            "в этой комнате."
                        )
                        continue
                if row.get("source") == "self" and old_did and (
                    str(old_did) != str(uid)
                ):
                    try:
                        await _notify_mapping_conflict(
                            interaction.guild, session,
                            f"⚠️ Капитан изменил self-привязку в комнате "
                            f"{self.room_index}: строка "
                            f"{row.get('ocr_name')} — "
                            f"{row.get('discord_name')} → {name}.",
                        )
                    except Exception:
                        pass
                row["discord_name"] = name
                row["discord_id"] = uid
                row["discord_display"] = name
                row["source"] = (
                    "self"
                    if row.get("source") == "self"
                    and str(old_did) == str(uid)
                    else "captain"
                )
            if errors:
                await interaction.response.send_message(
                    replace_emojis("❌ " + "\n".join(errors[:5])),
                    ephemeral=True,
                )
                return
            tm["confirmed"] = True
            store.set(tournament)

        await interaction.response.send_message(
            replace_emojis(
                f"✅ Команда **{_team_display_name(tournament, self.team_index)}**, "
                f"комната {self.room_index} — сопоставление сохранено."
            ),
            ephemeral=True,
        )
        await _rooms_after_map(
            interaction, tournament, self.match_type,
            self.match_index, self.room_index,
        )


class RoomSelfMapView(View):
    """Self-привязка: игрок выбирает свой ник в строке комнаты."""

    def __init__(
        self, guild_id: int, channel_id: int, tournament,
        match_type: str, match_index: int, room_index: int,
        team_index: int, panel: str, user_id: int,
        display_name: str, roster_name: str,
    ):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.room_index = room_index
        self.team_index = team_index
        self.panel = panel
        self.user_id = user_id
        self.display_name = display_name
        self.roster_name = roster_name

        _, session = _rooms_session_fresh(
            guild_id, channel_id, match_type, match_index
        )
        rs = _room_session(session, room_index) if session else None
        tm = (_team_map(rs, team_index) if rs else None) or {}
        rows = tm.get("rows") or []

        options = []
        for i, row in enumerate(rows[:4]):
            if _row_bound(row):
                continue
            options.append(discord.SelectOption(
                label=f"{i + 1}. {row.get('ocr_name', '?')}",
                value=str(i),
                description=(
                    f"K/D/A {row.get('kills', 0)}/{row.get('deaths', 0)}/"
                    f"{row.get('assists', 0)}"
                )[:100],
            ))
        if not options:
            self.add_item(Button(
                label="Нет свободных строк",
                style=discord.ButtonStyle.secondary,
                disabled=True, custom_id="room_self_none",
            ))
            return
        self.select = Select(
            placeholder="Выберите свой игровой ник",
            options=options[:25], min_values=1, max_values=1,
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)
        confirm = Button(
            label=f"✅ Это я ({display_name})",
            style=discord.ButtonStyle.success,
            custom_id=(
                f"room_self_ok:{guild_id}:{channel_id}:{match_type}:"
                f"{match_index}:{room_index}:{team_index}:{user_id}"
            ),
        )
        confirm.callback = self._on_confirm
        self.add_item(confirm)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        values = getattr(self.select, "values", None) or []
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            replace_emojis(
                f"Выбрана строка {values[0] if values else '?'} — "
                "нажмите «Это я» для подтверждения."
            ),
            ephemeral=True,
        )

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        if int(interaction.user.id) != int(self.user_id):
            await interaction.response.send_message(
                replace_emojis("❌ Подтвердить может только сам игрок."),
                ephemeral=True,
            )
            return
        values = getattr(self.select, "values", None) or []
        if not values:
            await interaction.response.send_message(
                replace_emojis("❌ Сначала выберите строку."), ephemeral=True
            )
            return
        try:
            row_idx = int(values[0])
        except (TypeError, ValueError):
            return
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        async with _mapping_lock(
            _session_key(self.match_type, self.match_index)
        ):
            tournament = store.get(self.guild_id, self.channel_id)
            _, session = _rooms_session_fresh(
                self.guild_id, self.channel_id,
                self.match_type, self.match_index,
            )
            rs, err = _rooms_guard(
                interaction, tournament, session, self.room_index
            )
            if err:
                await interaction.response.send_message(
                    replace_emojis(err), ephemeral=True
                )
                return
            for tk, tm2 in _match_maps(rs).items():
                for j, r in enumerate(tm2.get("rows") or []):
                    if (str(self.team_index), row_idx) == (tk, j):
                        continue
                    try:
                        if r.get("discord_id") and int(r["discord_id"]) == self.user_id:
                            await interaction.response.send_message(
                                replace_emojis(
                                    "❌ Ваш Discord ID уже привязан к "
                                    "другой строке в этой комнате."
                                ), ephemeral=True,
                            )
                            return
                    except (TypeError, ValueError):
                        pass
            tm = _team_map(rs, self.team_index)
            rows = (tm or {}).get("rows") or []
            if row_idx >= len(rows):
                await interaction.response.send_message(
                    replace_emojis("❌ Строка не найдена."), ephemeral=True
                )
                return
            row = rows[row_idx]
            row["discord_name"] = self.roster_name
            row["discord_id"] = self.user_id
            row["discord_display"] = self.display_name
            row["source"] = "self"
            if tm is not None:
                tm["confirmed"] = True
            store.set(tournament)

        await interaction.response.send_message(
            replace_emojis(
                f"✅ Комната {self.room_index}: вы привязаны к "
                f"**{row.get('ocr_name', '?')}**."
            ),
            ephemeral=True,
        )
        await _rooms_after_map(
            interaction, tournament, self.match_type,
            self.match_index, self.room_index,
        )


# ---------------------------------------------------------------------------
# Подтверждение результатов multi-room матча (без автofinalize)
# ---------------------------------------------------------------------------

def _rooms_confirm_channel(guild: discord.Guild, session: dict):
    """Канал подтверждения — тот же, что у сообщений комнат/прогресса."""
    ch_id = session.get("progress_channel_id")
    if ch_id:
        try:
            return guild.get_channel(int(ch_id))
        except Exception:
            pass
    return None


async def _rooms_send_confirmation(
    interaction: discord.Interaction,
    tournament: Tournament,
    match_type: str,
    match_index: int,
) -> None:
    """Все комнаты сопоставлены → embed подтверждения + кнопки."""
    key = _session_key(match_type, match_index)
    session = tournament.ocr_sessions.get(key)
    if not session:
        return
    t1, t2 = _match_tuple(tournament, match_type, match_index)
    winner = _rooms_confirmed_winner(tournament, session, t1, t2)
    wname = (
        _team_display_name(tournament, winner) if winner is not None else "—"
    )

    embed = discord.Embed(
        title="📋 ПОДТВЕРЖДЕНИЕ РЕЗУЛЬТАТОВ",
        description=(
            f"**{_match_label(match_type, match_index)}** · "
            f"Комнат сыграно: **{len(session.get('rooms') or [])}**\n"
            f"**Победитель:** {wname}\n"
            "Проверьте агрегированную статистику перед подтверждением."
        ),
        color=discord.Color.gold(),
    )
    try:
        stats = _rooms_aggregate_stats(session, tournament, t1, t2)
    except Exception as e:
        logger.warning("rooms aggregate for confirm failed: %s", e)
        stats = {}
        embed.description += (
            "\n\n⚠️ Часть привязок содержит ошибки — "
            "исправьте через «Исправить статистику»."
        )
    complete = _rooms_stats_complete(session)
    if not complete:
        embed.description += (
            "\n\n⚠️ Есть нераспознанные значения (K/D/A/DMG) — "
            "подтверждение будет отклонено до их исправления."
        )
    for team_index in (t1, t2):
        tname = _team_display_name(tournament, team_index)
        lines = []
        for name in team_roster(tournament, team_index):
            uid = tournament.player_user_ids.get(name)
            st = stats.get(name)
            if st is None:
                lines.append(f"• {name} — статистика отсутствует")
                continue
            nick_part = f" _{st.get('ocr_name')}" if st.get("ocr_name") else ""
            lines.append(
                f"• <@{uid}>{nick_part} — "
                f"{st.get('kills', 0)} / {st.get('deaths', 0)} / "
                f"{st.get('assists', 0)} — {st.get('damage', 0)} DMG"
            )
        embed.add_field(
            name=f"{'🏆 ' if team_index == winner else ''}{tname}",
            value="\n".join(lines) or "—",
            inline=False,
        )

    channel = _rooms_confirm_channel(interaction.guild, session)
    if channel is None:
        channel = interaction.channel
    view = RoomsConfirmView(
        tournament.guild_id, tournament.channel_id, tournament,
        match_type, match_index,
    )
    msg = await channel.send(
        content=replace_emojis(
            f"**{_match_label(match_type, match_index)}** — "
            "комнаты сопоставлены. Проверьте и подтвердите результат:"
        ),
        embed=embed, view=view,
    )
    session["confirm_message_id"] = msg.id
    store.set(tournament)
    try:
        note = replace_emojis(
            "✅ Все комнаты сопоставлены — отправлено "
            "подтверждение результата."
        )
        if interaction.response.is_done():
            await interaction.followup.send(note, ephemeral=True)
        else:
            await interaction.response.send_message(note, ephemeral=True)
    except Exception:
        pass


class RoomsConfirmView(View):
    """Подтверждение / исправление / вызов админа для multi-room."""

    def __init__(
        self, guild_id: int, channel_id: int, tournament,
        match_type: str, match_index: int,
    ):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        cid = f"{guild_id}:{channel_id}:{match_type}:{match_index}"
        specs = (
            ("✅ Подтвердить результаты", discord.ButtonStyle.success, "ok"),
            ("✏️ Исправить статистику", discord.ButtonStyle.primary, "edit"),
            ("📣 Вызвать администратора", discord.ButtonStyle.secondary, "admin"),
        )
        for label, style, suffix in specs:
            b = Button(
                label=label, style=style,
                custom_id=f"rooms_confirm:{suffix}:{cid}",
            )
            b.callback = self._make_cb(suffix)
            self.add_item(b)

    def _make_cb(self, suffix: str):
        async def _cb(interaction: discord.Interaction) -> None:
            if suffix == "ok":
                await self._on_confirm(interaction)
            elif suffix == "edit":
                await self._on_edit(interaction)
            else:
                await self._on_admin(interaction)
        return _cb

    async def _fresh(self):
        return store.get(self.guild_id, self.channel_id)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        tournament = await self._fresh()
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
        # Права — как в обычном workflow: капитан/организатор
        if not _can_use_ocr(interaction.user, tournament, [t1, t2]):
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Подтвердить результат может капитан команды "
                    "или организатор."
                ),
                ephemeral=True,
            )
            return
        session = tournament.ocr_sessions.get(
            _session_key(self.match_type, self.match_index)
        )
        # «finalizing» пропускаем: это либо активная финализация
        # (повторный вход идемпотентен через PG-гейт), либо восстановление
        # после падения — подтверждение должно иметь возможность retry.
        if not session or session.get("status") == "completed":
            await interaction.response.send_message(
                replace_emojis("ℹ️ Результат уже зафиксирован."),
                ephemeral=True,
            )
            return
        # Единый гейт готовности (ТЗ Ranked MM): комнаты, сопоставление,
        # полнота статистики, агрегация по всему ростеру. Победитель
        # проверяется отдельно ниже (может потребоваться ручной выбор).
        ready, reason, detail = check_ocr_session_ready(
            tournament, session, t1, t2, require_winner=False
        )
        if not ready:
            await interaction.response.send_message(
                replace_emojis(_rooms_ready_reason_text(reason, detail)),
                ephemeral=True,
            )
            return
        # Победитель — зафиксированная команда отправителя первого
        # принятного файла; OCR/баннеры на него не влияют.
        winner = _rooms_confirmed_winner(tournament, session, t1, t2)
        if winner is None:
            # Отправитель — не участник: ручной выбор организатором
            try:
                ch = _rooms_confirm_channel(interaction.guild, session) \
                    or interaction.channel
                await ch.send(
                    embed=_confirm_embed(
                        tournament, session,
                        self.match_type, self.match_index,
                    ),
                    view=OCRWinnerPickView(
                        tournament.guild_id, tournament.channel_id,
                        tournament, self.match_type, self.match_index,
                    ),
                )
            except Exception as e:
                logger.warning("rooms winner pick send failed: %s", e)
            await interaction.response.send_message(
                replace_emojis(
                    "⚠️ Отправитель комплекта не участник команд — "
                    "организатор выберет победителя вручную."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await finalize_ocr_match(
                interaction, tournament,
                self.match_type, self.match_index, winner,
            )
            wname = _team_display_name(tournament, winner)
            await interaction.followup.send(
                replace_emojis(
                    f"✅ Результат **{_match_label(self.match_type, self.match_index)}** "
                    f"зафиксирован. Победитель: **{wname}**."
                ),
                ephemeral=True,
            )
        except Exception:
            logger.exception("rooms confirm finalize failed")
            await interaction.followup.send(
                replace_emojis(
                    "❌ Ошибка финализации — вызовите организатора."
                ),
                ephemeral=True,
            )

    async def _on_edit(self, interaction: discord.Interaction) -> None:
        tournament = await self._fresh()
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
        session = tournament.ocr_sessions.get(
            _session_key(self.match_type, self.match_index)
        )
        # Исправлять может капитан/орг/админ (как и маппинг)
        allowed = session and (
            _can_use_ocr(interaction.user, tournament, [t1, t2])
            or _is_server_admin(interaction.user)
        )
        if not allowed:
            await interaction.response.send_message(
                replace_emojis("❌ Нет прав на исправление."), ephemeral=True
            )
            return
        view = RoomsEditNavView(
            tournament.guild_id, tournament.channel_id, tournament,
            self.match_type, self.match_index,
        )
        await interaction.response.send_message(
            content=replace_emojis(
                "✏️ Выберите комнату для исправления статистики:"
            ),
            view=view, ephemeral=True,
        )

    async def _on_admin(self, interaction: discord.Interaction) -> None:
        tournament = await self._fresh()
        if not tournament:
            return
        try:
            ch = interaction.guild.get_channel(OCR_STATS_CHANNEL_ID) \
                or await interaction.guild.fetch_channel(OCR_STATS_CHANNEL_ID)
            await ch.send(
                replace_emojis(
                    f"📣 **{interaction.user}** вызвал администратора — "
                    f"**{_match_label(self.match_type, self.match_index)}**."
                )
            )
        except Exception as e:
            logger.warning("rooms confirm call-admin failed: %s", e)
        await interaction.response.send_message(
            replace_emojis("📣 Администратор уведомлён."), ephemeral=True
        )


class RoomsEditNavView(View):
    """Выбор комнаты → команды → строки для исправления статистики."""

    def __init__(self, guild_id, channel_id, tournament, match_type, match_index):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        options = []
        for r in session.get("rooms") or []:
            ridx = r.get("room_index")
            kind = "отряд" if r.get("kind") == "squad" else "2 команды"
            options.append(discord.SelectOption(
                label=f"Комната {ridx} ({kind})"[:100], value=str(ridx),
            ))
        if not options:
            self.add_item(Button(
                label="Нет комнат", style=discord.ButtonStyle.secondary,
                disabled=True, custom_id="rooms_edit_none",
            ))
            return
        self.select = Select(
            placeholder="Комната", options=options[:25],
            min_values=1, max_values=1,
        )
        self.select.callback = self._on_room
        self.add_item(self.select)

    async def _on_room(self, interaction: discord.Interaction) -> None:
        values = getattr(self.select, "values", None) or []
        if not values:
            await interaction.response.defer()
            return
        room_index = int(values[0])
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
        view = RoomsEditTeamView(
            self.guild_id, self.channel_id, tournament,
            self.match_type, self.match_index, room_index, [t1, t2],
        )
        await interaction.response.send_message(
            content=replace_emojis(f"Комната {room_index} — выберите команду:"),
            view=view, ephemeral=True,
        )


class RoomsEditTeamView(View):
    def __init__(self, guild_id, channel_id, tournament, match_type,
                 match_index, room_index, team_indexes):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.room_index = room_index
        options = []
        for ti in team_indexes:
            options.append(discord.SelectOption(
                label=_team_display_name(tournament, ti)[:100],
                value=str(ti),
            ))
        self.select = Select(
            placeholder="Команда", options=options,
            min_values=1, max_values=1,
        )
        self.select.callback = self._on_team
        self.add_item(self.select)

    async def _on_team(self, interaction: discord.Interaction) -> None:
        values = getattr(self.select, "values", None) or []
        if not values:
            await interaction.response.defer()
            return
        team_index = int(values[0])
        tournament = store.get(self.guild_id, self.channel_id)
        view = RoomsEditRowView(
            self.guild_id, self.channel_id, tournament,
            self.match_type, self.match_index, self.room_index, team_index,
        )
        await interaction.response.send_message(
            content=replace_emojis(
                f"Комната {self.room_index} — выберите строку игрока:"
            ),
            view=view, ephemeral=True,
        )


class RoomsEditRowView(View):
    def __init__(self, guild_id, channel_id, tournament, match_type,
                 match_index, room_index, team_index):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.room_index = room_index
        self.team_index = team_index
        _, session = _rooms_session_fresh(
            guild_id, channel_id, match_type, match_index
        )
        rs = _room_session(session, room_index) if session else None
        tm = (_team_map(rs, team_index) if rs else None) or {}
        rows = tm.get("rows") or []
        options = []
        for i, row in enumerate(rows[:8]):
            mark = " ✅" if _row_bound(row) else ""
            options.append(discord.SelectOption(
                label=f"{i + 1}. {row.get('ocr_name', '?')}{mark}"[:100],
                value=str(i),
                description=(
                    f"{_rs(row.get('kills'))}/{_rs(row.get('deaths'))}/"
                    f"{_rs(row.get('assists'))} · {_rs(row.get('dmg'))} DMG"
                )[:100],
            ))
        if not options:
            self.add_item(Button(
                label="Нет строк — сначала сопоставьте команду",
                style=discord.ButtonStyle.secondary,
                disabled=True, custom_id="rooms_edit_rows_none",
            ))
            return
        self.select = Select(
            placeholder="Строка игрока", options=options[:25],
            min_values=1, max_values=1,
        )
        self.select.callback = self._on_row
        self.add_item(self.select)
        # Досрочный выход: на squad-скриншоте может быть <4 строк —
        # добавляем пустую строку для ручного ввода статистики.
        if len(rows) < 4:
            add = Button(
                label="➕ Добавить строку (игрок вышел раньше)",
                style=discord.ButtonStyle.secondary,
                custom_id=(
                    f"rooms_edit_add:{self.guild_id}:{self.channel_id}:"
                    f"{self.match_type}:{self.match_index}:"
                    f"{self.room_index}:{self.team_index}"
                ),
            )
            add.callback = self._on_add
            self.add_item(add)

    async def _on_add(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        _, session = _rooms_session_fresh(
            self.guild_id, self.channel_id,
            self.match_type, self.match_index,
        )
        rs = _room_session(session, self.room_index) if session else None
        tm = _team_map(rs, self.team_index) if rs else None
        rows = (tm or {}).get("rows")
        if tm is None or rows is None or len(rows) >= 4:
            await interaction.response.send_message(
                replace_emojis("❌ Нечего добавлять."), ephemeral=True
            )
            return
        rows.append({
            "ocr_name": "?", "kills": None, "deaths": None,
            "assists": None, "dmg": None,
            "discord_name": None, "discord_id": None, "source": None,
        })
        store.set(tournament)
        modal = RoomRowEditModal(
            self.guild_id, self.channel_id, tournament,
            self.match_type, self.match_index, self.room_index,
            self.team_index, len(rows) - 1,
        )
        await interaction.response.send_modal(modal)

    async def _on_row(self, interaction: discord.Interaction) -> None:
        values = getattr(self.select, "values", None) or []
        if not values:
            await interaction.response.defer()
            return
        row_idx = int(values[0])
        tournament = store.get(self.guild_id, self.channel_id)
        modal = RoomRowEditModal(
            self.guild_id, self.channel_id, tournament,
            self.match_type, self.match_index, self.room_index,
            self.team_index, row_idx,
        )
        await interaction.response.send_modal(modal)


class RoomRowEditModal(Modal):
    """Исправление одной строки статистики внутри комнаты."""

    def __init__(self, guild_id, channel_id, tournament, match_type,
                 match_index, room_index, team_index, row_idx):
        super().__init__(title=f"Исправить — комн. {room_index}", timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.room_index = room_index
        self.team_index = team_index
        self.row_idx = row_idx

        _, session = _rooms_session_fresh(
            guild_id, channel_id, match_type, match_index
        )
        rs = _room_session(session, room_index) if session else None
        tm = (_team_map(rs, team_index) if rs else None) or {}
        rows = tm.get("rows") or []
        row = rows[row_idx] if row_idx < len(rows) else {}

        self.nick = TextInput(
            label="Игровой ник", required=False, max_length=32,
            default=(row.get("ocr_name") or "")[:32],
        )
        self.kills = TextInput(
            label="Kills (пусто = не определено)", required=False,
            max_length=6,
            default="" if row.get("kills") is None else str(row.get("kills")),
        )
        self.deaths = TextInput(
            label="Deaths", required=False, max_length=6,
            default="" if row.get("deaths") is None else str(row.get("deaths")),
        )
        self.assists = TextInput(
            label="Assists", required=False, max_length=6,
            default="" if row.get("assists") is None else str(row.get("assists")),
        )
        self.dmg = TextInput(
            label="Damage", required=False, max_length=8,
            default="" if row.get("dmg") is None else str(row.get("dmg")),
        )
        for item in (self.nick, self.kills, self.deaths,
                     self.assists, self.dmg):
            self.add_item(item)

    @staticmethod
    def _num(text: str):
        t = (text or "").strip()
        if not t:
            return None
        if not t.isdigit():
            raise ValueError("только целые неотрицательные числа")
        return int(t)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            kills = self._num(self.kills.value)
            deaths = self._num(self.deaths.value)
            assists = self._num(self.assists.value)
            dmg = self._num(self.dmg.value)
        except ValueError as e:
            await interaction.response.send_message(
                replace_emojis(f"❌ Некорректное значение: {e}"),
                ephemeral=True,
            )
            return
        nick = (self.nick.value or "").strip()[:32] or None

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        async with _mapping_lock(
            _session_key(self.match_type, self.match_index)
        ):
            tournament = store.get(self.guild_id, self.channel_id)
            _, session = _rooms_session_fresh(
                self.guild_id, self.channel_id,
                self.match_type, self.match_index,
            )
            if not session or session.get("status") in (
                "completed", "cancelled", "finalizing"
            ):
                await interaction.response.send_message(
                    replace_emojis(
                        "⚠️ Сопоставление для этого матча уже завершено."
                    ),
                    ephemeral=True,
                )
                return
            rs = _room_session(session, self.room_index)
            if rs is None:
                await interaction.response.send_message(
                    replace_emojis("❌ Комната не найдена."), ephemeral=True
                )
                return
            tm = _team_map(rs, self.team_index)
            rows = (tm or {}).get("rows") or []
            if self.row_idx >= len(rows):
                await interaction.response.send_message(
                    replace_emojis("❌ Строка не найдена."), ephemeral=True
                )
                return
            row = rows[self.row_idx]
            if nick:
                row["ocr_name"] = nick
            row["kills"] = kills
            row["deaths"] = deaths
            row["assists"] = assists
            row["dmg"] = dmg
            # Зеркалим в panels (исходные OCR-данные комнаты)
            panel = (tm or {}).get("panel")
            prows = ((rs.get("panels") or {}).get(panel) or {}).get("rows")
            if prows and self.row_idx < len(prows):
                pr = prows[self.row_idx]
                if nick:
                    pr["name"] = nick
                pr["kills"] = kills
                pr["deaths"] = deaths
                pr["assists"] = assists
                pr["dmg"] = dmg
            store.set(tournament)

        # Ranked MM: сохранённая правка может стать последним шагом
        # готовности — пробуем автоматическую финализацию (без кнопки
        # «Подтвердить результаты»). Если финализировано — ответ уже
        # отправлен хелпером, дальше не идём.
        tournament = store.get(self.guild_id, self.channel_id)
        if tournament is not None:
            done, _, _ = await _maybe_auto_finalize_mm_rooms(
                interaction, tournament, self.match_type, self.match_index
            )
            if done:
                return

        # Если строка не привязана к Discord-игроку — сразу предлагаем
        # выбор игрока (единый процесс: строка → статы → привязка).
        _, session = _rooms_session_fresh(
            self.guild_id, self.channel_id,
            self.match_type, self.match_index,
        )
        rs = _room_session(session, self.room_index) if session else None
        tm = _team_map(rs, self.team_index) if rs else None
        rows = (tm or {}).get("rows") or []
        row_now = rows[self.row_idx] if self.row_idx < len(rows) else {}
        if not _row_bound(row_now):
            tournament = store.get(self.guild_id, self.channel_id)
            await interaction.response.send_message(
                content=replace_emojis(
                    f"✅ Комната {self.room_index}, строка "
                    f"{self.row_idx + 1} — статистика сохранена. "
                    "Теперь выберите Discord-игрока:"
                ),
                view=RoomRowBindView(
                    self.guild_id, self.channel_id, tournament,
                    self.match_type, self.match_index, self.room_index,
                    self.team_index, self.row_idx,
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            replace_emojis(
                f"✅ Комната {self.room_index}, строка "
                f"{self.row_idx + 1} — статистика обновлена."
            ),
            ephemeral=True,
        )


class RoomRowBindView(View):
    """Привязка добавленной/непривязанной строки комнаты к Discord-игроку."""

    def __init__(
        self, guild_id, channel_id, tournament, match_type,
        match_index, room_index, team_index, row_idx,
    ):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.room_index = room_index
        self.team_index = team_index
        self.row_idx = row_idx
        options = [
            discord.SelectOption(label="— пропустить —", value=""),
        ]
        for name in team_roster(tournament, team_index)[:24]:
            options.append(discord.SelectOption(
                label=name[:100], value=name[:100],
            ))
        self.select = Select(
            placeholder="Discord-игрок для этой строки",
            options=options, min_values=1, max_values=1,
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        values = getattr(self.select, "values", None) or [""]
        name = values[0]
        if not name:
            await interaction.response.send_message(
                replace_emojis("ℹ️ Привязка отложена — можно назначить "
                               "позже через маппинг команды."),
                ephemeral=True,
            )
            return
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        async with _mapping_lock(
            _session_key(self.match_type, self.match_index)
        ):
            tournament = store.get(self.guild_id, self.channel_id)
            _, session = _rooms_session_fresh(
                self.guild_id, self.channel_id,
                self.match_type, self.match_index,
            )
            if not session or session.get("status") in (
                "completed", "cancelled", "finalizing"
            ):
                await interaction.response.send_message(
                    replace_emojis(
                        "⚠️ Сопоставление для этого матча уже завершено."
                    ),
                    ephemeral=True,
                )
                return
            rs = _room_session(session, self.room_index) if session else None
            tm = _team_map(rs, self.team_index) if rs else None
            rows = (tm or {}).get("rows") or []
            if self.row_idx >= len(rows):
                await interaction.response.send_message(
                    replace_emojis("❌ Строка не найдена."), ephemeral=True
                )
                return
            row = rows[self.row_idx]
            # Один Discord ID — одна строка в комнате: старые привязки
            # этого игрока в комнате снимаем.
            uid = tournament.player_user_ids.get(name)
            for tk, tm2 in _match_maps(rs or {}).items():
                for j, r2 in enumerate(tm2.get("rows") or []):
                    if (str(self.team_index), self.row_idx) == (tk, j):
                        continue
                    if r2.get("discord_id") and str(r2["discord_id"]) == str(uid):
                        r2["discord_name"] = None
                        r2["discord_id"] = None
                        r2["source"] = None
            row["discord_name"] = name
            row["discord_id"] = uid
            row["discord_display"] = name
            row["source"] = "manual"
            store.set(tournament)
        await interaction.response.send_message(
            replace_emojis(
                f"✅ Строка {self.row_idx + 1} привязана к **{name}**."
            ),
            ephemeral=True,
        )
