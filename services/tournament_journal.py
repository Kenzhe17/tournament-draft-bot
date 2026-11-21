"""Централизованный журнал турниров — Discord-представление аудита.

В канале ``TOURNAMENT_AUDIT_CHANNEL_ID`` каждому турниру
соответствует:
- одна основная CV2-карточка «ЖУРНАЛ ТУРНИРА #N»;
- одна Discord Thread с карточками каждого матча и сводкой
  общих событий турнира.

Сервис НЕ пишет события сам — он получает уже записанные
события из ``tournament_audit_service.record()`` через
``notify_event()`` и группирует их в карточки (debounce ~3с
на турнир). Хранилище Discord-ID — ``tournament_journal_store``
(PostgreSQL). Ссылки:
- итоговая карточка турнира → «Журнал турнира» (link-кнопка);
- карточка журнала → «Посмотреть итоги» (link-кнопка).

Гарантии:
- журнал никогда не ломает турнирную механику — все ошибки
  логируются, sync_status='error' + retry;
- одна карточка = одно сообщение (edit по сохранённому id);
- пароли/секреты не публикуются (события уже санитизированы);
- упоминания рендерятся без пингов (AllowedMentions.none()).
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import logging
import random
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import discord

from config import TOURNAMENT_AUDIT_CHANNEL_ID
from storage import tournament_audit_store as audit_store
from storage import tournament_journal_store as journal_store
from storage.json_store import store as tournament_store
from services import tournament_audit_service as audit_svc

if TYPE_CHECKING:
    from models.tournament import Tournament

logger = logging.getLogger(__name__)

# organizer.* события не публикуются в Discord-журнале —
# они остаются только в PostgreSQL (расследование инцидентов).
_JOURNAL_SOURCES = {"tournament", "room"}


def _is_organizer_event(ev: dict[str, Any]) -> bool:
    """Событие управления организаторами (скрыто из журнала)."""
    return (
        ev.get("source") == "organizer"
        or str(ev.get("action", "")).startswith("organizer.")
    )
_DEBOUNCE_SEC = 3.0
_MIN_EDIT_INTERVAL = 2.0
_VERIFY_TTL = 3600.0  # реже проверять существование неизменённых карточек
_FLUSH_LOCKS: dict[str, asyncio.Lock] = {}
_DIRTY: dict[str, dict[str, Any]] = {}
_EDIT_TS: dict[int, float] = {}
_VERIFY_TS: dict[int, float] = {}
_SIG_CACHE: dict[int, str] = {}
_URL_CACHE: dict[str, tuple[str | None, float]] = {}
_URL_CACHE_TTL = 300.0
_RECOVERED = False

# ---------------------------------------------------------------------------
# Устойчивая очередь восстановления (worker + retry_wait)
# ---------------------------------------------------------------------------
#
# Все (пере)публикации журналов проходят через единственный
# worker: операции сериализованы, между турнирами — пауза,
# временные ошибки Discord/PG переводят запись в retry_wait
# с exponential backoff + jitter и persisted next_attempt_at.
# Pending-состояние живёт в PostgreSQL — рестарт не теряет
# очередь.

_RECOVERY_HEAP: list[tuple[float, str]] = []  # (run_at monotonic, tid)
_RECOVERY_QUEUED: set[str] = set()
_RECOVERY_WORKER: asyncio.Task | None = None
_RETRY_ATTEMPTS: dict[str, int] = {}
_RECOVERY_STATS: dict[str, int] = {}

_WORKER_INTERVAL = 1.0        # пауза между flush разных турниров
_RETRY_BASE_SEC = 30.0        # первый backoff
_RETRY_MAX_SEC = 1800.0       # потолок backoff
_RETRY_JITTER = 0.2           # ±20%
_HISTORY_SCAN_LIMIT = 100     # сколько сообщений проверять
                              # при поиске уже опубликованной
                              # карточки (защита от дублей)


class _RetryableJournalError(Exception):
    """Временная ошибка (429/5xx/сеть/PG) — запись уходит
    в retry_wait и будет повторена worker'ом."""


class _PermanentJournalError(Exception):
    """Постоянная ошибка (403/404/конфиг) — sync_status='error',
    автоматический retry не выполняется."""

_PHASE_LABELS = {
    "qualifier": "ОТБОР",
    "semifinal": "ПОЛУФИНАЛ",
    "final": "ФИНАЛ",
}
_PHASE_ORDER = {"qualifier": 0, "semifinal": 1, "final": 2}
_STATUS_TITLES = {
    "setup": "Регистрация",
    "rules_voting": "Регистрация",
    "draft": "Драфт",
    "teams": "Формирование команд",
    "qualifiers": "Отбор",
    "semifinals": "Полуфинал",
    "final": "Финал",
    "complete": "Завершён",
}
_FORMATION_LABELS = {
    "draft": "Draft",
    "balanced-sr": "Balanced SR",
    "balanced_sr": "Balanced SR",
    "random": "Random",
}
_MATCH_ACTIONS_PREFIXES = ("match.", "final.")


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------


def bind_bot(bot: discord.Client) -> None:
    """Совместимость с audit.bind_bot — журнал использует тот же
    клиент через audit_svc._bot; дублируем на всякий случай."""
    audit_svc.bind_bot(bot)


def _bot() -> discord.Client | None:
    return audit_svc._bot


def _audit_channel_id() -> int:
    try:
        return int(TOURNAMENT_AUDIT_CHANNEL_ID or 0)
    except Exception:
        return 0


def normalize_match_key(tournament_id: str, match_id: Any) -> str | None:
    """Нормализовать match_id события к ``phase:index``.

    Встречаются два формата:
    - ``{tid}:{phase}:{idx}``  (room_view match_key);
    - ``{tid}_{phase}_{idx}``  (temp stats / история матчей).
    Возвращает None для событий без матча.
    """
    if not match_id:
        return None
    mid = str(match_id)
    tid = str(tournament_id)
    rest = mid
    for sep in (":", "_"):
        prefix = tid + sep
        if mid.startswith(prefix):
            rest = mid[len(prefix):]
            break
    # rest: "qualifier:0" | "qualifier_0" | "0" (уже сам phase? нет)
    if ":" in rest:
        phase, _, idx = rest.rpartition(":")
    elif "_" in rest:
        phase, _, idx = rest.rpartition("_")
    else:
        return None
    if phase not in _PHASE_LABELS:
        return None
    if not idx.isdigit():
        return None
    return f"{phase}:{int(idx)}"


def _fmt_ts(ts: Any) -> str:
    try:
        if ts is None:
            return "—"
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        elif isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            dt = ts
        return discord.utils.format_dt(dt, style="F")
    except Exception:
        return "—"


def _guild_id_of(tournament_id: str) -> int:
    try:
        return int(str(tournament_id).split("_", 1)[0])
    except (ValueError, IndexError):
        return 0


# ---------------------------------------------------------------------------
# Загрузка состояния турнира
# ---------------------------------------------------------------------------


async def _load_tournament(tournament_id: str) -> "Tournament | None":
    """Активный турнир из store, иначе архивный снимок."""
    tid = str(tournament_id)
    try:
        for t in tournament_store.all():
            if str(t.id) == tid:
                return t
    except Exception:
        pass
    try:
        from storage.tournament_archive import tournament_archive
        entry = tournament_archive.get(tid)
        if entry and entry.get("data"):
            from models.tournament import Tournament
            return Tournament.from_dict(entry["data"])
    except Exception as exc:
        logger.warning("journal: archive load %s failed: %s", tid, exc)
    return None


async def _tournament_events(
    guild_id: int, channel_id: int | None, tournament_id: str,
    limit: int = 300,
) -> list[dict[str, Any]]:
    """События аудита турнира (новые первыми).

    ``channel_id=None`` — все каналы гильдии (архивные турниры,
    где исходный канал неизвестен).
    """
    try:
        events = await audit_store.list_events(
            guild_id=guild_id,
            channel_id=channel_id,
            tournament_id=str(tournament_id),
            limit=limit,
            exclude_organizer=True,
        )
    except Exception as exc:
        logger.warning("journal: events load failed %s: %s",
                       tournament_id, exc)
        return []
    # organizer.* не показываем в карточках журнала (PG-записи
    # сохраняются для аудита, но в Discord не выводятся).
    # Фильтр на уровне SQL — до LIMIT; здесь — дополнительная защита.
    return [e for e in events if not _is_organizer_event(e)]


def _group_events(
    events: list[dict[str, Any]], tournament_id: str
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Разбить события на {match_key: [events]} и общие (без матча).

    События идут новые→старые; внутри групп сохраняем порядок.
    """
    by_match: dict[str, list[dict[str, Any]]] = {}
    general: list[dict[str, Any]] = []
    for ev in events:
        mk = normalize_match_key(tournament_id, ev.get("match_id"))
        if mk:
            by_match.setdefault(mk, []).append(ev)
        else:
            general.append(ev)
    return by_match, general


# ---------------------------------------------------------------------------
# Составы матчей
# ---------------------------------------------------------------------------


def _team_name(tournament: "Tournament", idx: int) -> str:
    team = (
        tournament.teams[idx]
        if 0 <= idx < len(getattr(tournament, "teams", []) or [])
        else {}
    )
    captain = team.get("captain", f"П{idx + 1}")
    return str(tournament.team_names.get(idx, captain))


def _team_roster(tournament: "Tournament", idx: int) -> list[str]:
    from utils.embeds import team_roster_names
    team = (
        tournament.teams[idx]
        if 0 <= idx < len(getattr(tournament, "teams", []) or [])
        else {}
    )
    return [str(n) for n in team_roster_names(team)]


def _display_name(tournament: "Tournament", name: str) -> str:
    """Имя с косметикой (тег/значок), без пинга."""
    try:
        from utils.cosmetics import format_player_name
        uid = tournament.player_user_ids.get(name)
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            uid = 0
        if uid > 0:
            return format_player_name(
                int(getattr(tournament, "guild_id", 0) or 0),
                uid, name)
    except Exception:
        pass
    return name


def iter_all_matches(tournament: "Tournament") -> list[dict[str, Any]]:
    """ВСЕ матчи турнира (все стадии), стабильный порядок.

    Возвращает dict: key ('phase:idx'), phase, index, teams (a,b),
    num (номер #N по порядку стадий), active, label.
    """
    out: list[dict[str, Any]] = []
    num = 0
    for phase, matches in (
        ("qualifier", getattr(tournament, "qualifier_matches", []) or []),
        ("semifinal", getattr(tournament, "semifinal_matches", []) or []),
    ):
        for i, (a, b) in enumerate(matches):
            num += 1
            out.append({
                "key": f"{phase}:{i}", "phase": phase, "index": i,
                "teams": (a, b), "num": num,
                "label": f"МАТЧ #{num} — {_PHASE_LABELS[phase]}",
            })
    final_teams = getattr(tournament, "final_teams", []) or []
    if len(final_teams) >= 2:
        num += 1
        a, b = final_teams[0], final_teams[1]
        out.append({
            "key": "final:0", "phase": "final", "index": 0,
            "teams": (a, b), "num": num,
            "label": f"МАТЧ #{num} — {_PHASE_LABELS['final']}",
        })
    return out


def _match_winner(
    tournament: "Tournament", m: dict[str, Any]
) -> int | None:
    """Подтверждённый победитель матча (team index) или None."""
    phase, idx = m["phase"], m["index"]
    try:
        if phase == "qualifier":
            w = tournament.qualifier_winners
            return w[idx] if idx < len(w) else None
        if phase == "semifinal":
            # Подтверждённый winner — semifinal_winners;
            # pending — выбран, но не подтверждён.
            w = tournament.semifinal_winners
            if idx < len(w) and w[idx] is not None:
                return w[idx]
            return None
        # final
        return tournament.winner_team_index
    except Exception:
        return None


def _match_status(
    tournament: "Tournament", m: dict[str, Any],
    events: list[dict[str, Any]],
) -> str:
    if _match_winner(tournament, m) is not None:
        return "Завершён"
    if any(str(e.get("action", "")).startswith(_MATCH_ACTIONS_PREFIXES)
           for e in events):
        return "Идёт"
    return "Ожидает"


# ---------------------------------------------------------------------------
# Карточки CV2
# ---------------------------------------------------------------------------


def _link_button(label: str, url: str) -> discord.ui.Button:
    return discord.ui.Button(
        style=discord.ButtonStyle.link, label=label, url=url)


def build_main_card(
    tournament: "Tournament | None",
    entry: dict[str, Any],
    *,
    status_override: str | None = None,
    events_general: list[dict[str, Any]] | None = None,
) -> discord.ui.LayoutView:
    """Основная карточка «ЖУРНАЛ ТУРНИРА #N»."""
    tid = str(entry.get("tournament_id") or "")
    no = entry.get("journal_no") or "?"
    lines = [f"## ЖУРНАЛ ТУРНИРА #{no}"]

    organizer = "—"
    participants = "—"
    formation = "—"
    created = "—"
    started = "—"
    phase_label = "—"
    matches_done = "—"
    matches_total = "—"
    winner = ""
    cancelled = status_override == "cancelled"

    if tournament is not None:
        organizer = (
            f"<@{tournament.organizer_id}>"
            if getattr(tournament, "organizer_id", 0) else "—"
        )
        # Участники — уникальные имена во всех составах команд
        # (all_players отражает только пул регистрации).
        try:
            names: set[str] = set()
            for idx, _team in enumerate(
                    getattr(tournament, "teams", []) or []):
                names.update(_team_roster(tournament, idx))
            participants = str(len(names)) if names else "—"
        except Exception:
            participants = "—"
        fm = getattr(tournament, "formation_mode", "")
        fm_val = getattr(fm, "value", str(fm)).lower()
        formation = _FORMATION_LABELS.get(fm_val, fm_val or "—")
        created = _fmt_ts(getattr(tournament, "creation_time", None))
        started = _fmt_ts(getattr(tournament, "start_time", None))
        phase = getattr(tournament, "phase", None)
        phase_label = _STATUS_TITLES.get(
            getattr(phase, "value", str(phase)), "—")
        all_matches = iter_all_matches(tournament)
        matches_total = str(len(all_matches)) if all_matches else "—"
        done = sum(
            1 for m in all_matches
            if _match_winner(tournament, m) is not None
        )
        matches_done = f"{done}/{len(all_matches)}" if all_matches else "—"
        widx = getattr(tournament, "winner_team_index", None)
        if widx is not None:
            winner = _team_name(tournament, widx)

    if cancelled:
        status = "Отменён"
    elif tournament is not None and str(
            getattr(getattr(tournament, "phase", ""), "value", "")
    ) == "complete":
        status = "Завершён"
    elif status_override:
        status = status_override
    else:
        status = phase_label

    lines += [
        f"**ID:** `{tid}`",
        f"**Организатор:** {organizer}",
        f"**Участников:** {participants}",
        f"**Формат:** {formation}",
        f"**Создан:** {created} · **Старт:** {started}",
        f"**Этап:** {phase_label}",
        f"**Матчи:** {matches_done} завершено из {matches_total}",
        f"**Статус:** {status}",
    ]
    if winner:
        lines.append(f"**Победитель:** {winner}")

    # Стадии — фактические матчи по фазам.
    if tournament is not None:
        counts: dict[str, int] = {}
        for m in iter_all_matches(tournament):
            counts[m["phase"]] = counts.get(m["phase"], 0) + 1
        if counts:
            stage_s = " · ".join(
                f"{_PHASE_LABELS[p]}: {counts[p]}"
                for p in sorted(counts, key=lambda p: _PHASE_ORDER[p])
            )
            lines.append(f"**Стадии:** {stage_s}")

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(
        accent_colour=discord.Colour.blurple())
    container.add_item(discord.ui.TextDisplay(content="\n".join(lines)))

    # Кнопка «Посмотреть итоги» — только для ЗАВЕРШЁННОГО
    # турнира с известным итоговым сообщением.
    is_complete = tournament is not None and str(
        getattr(getattr(tournament, "phase", ""), "value", "")
    ) == "complete"
    result_url = _result_url(entry, tournament) if is_complete else None
    if result_url:
        row = discord.ui.ActionRow()
        row.add_item(_link_button("Посмотреть итоги", result_url))
        container.add_item(row)
    view.add_item(container)
    return view


def _result_url(
    entry: dict[str, Any], tournament: "Tournament | None"
) -> str | None:
    """URL итогового сообщения турнира (если известно).

    Приоритет — актуальные guild/channel/message_id турнира
    (archive snapshot или активный store), иначе сохранённые
    в записи журнала.
    """
    gid = int(entry.get("guild_id") or 0)
    cid = int(entry.get("channel_id") or 0)
    mid = int(entry.get("result_message_id") or 0)
    if tournament is not None:
        gid = int(getattr(tournament, "guild_id", 0) or gid)
        cid = int(getattr(tournament, "channel_id", 0) or cid)
        mid = int(getattr(tournament, "message_id", 0) or mid)
    if not (gid and cid and mid):
        return None
    return f"https://discord.com/channels/{gid}/{cid}/{mid}"


def _journal_url(entry: dict[str, Any]) -> str | None:
    """URL основной карточки журнала."""
    ch = int(entry.get("audit_channel_id") or 0)
    mid = int(entry.get("audit_message_id") or 0)
    gid = int(entry.get("guild_id") or 0)
    if not (ch and mid and gid):
        return None
    return f"https://discord.com/channels/{gid}/{ch}/{mid}"


async def journal_url_for(tournament: "Tournament") -> str | None:
    """URL карточки журнала для турнира (с кэшем).

    Используется итоговой карточкой для кнопки «Журнал турнира».
    """
    tid = str(getattr(tournament, "id", ""))
    if not tid:
        return None
    cached = _URL_CACHE.get(tid)
    if cached and time.monotonic() - cached[1] < _URL_CACHE_TTL:
        return cached[0]
    url = None
    try:
        entry = await journal_store.get_main(tid)
        if entry:
            url = _journal_url(entry)
    except Exception as exc:
        logger.debug("journal url lookup failed %s: %s", tid, exc)
    _URL_CACHE[tid] = (url, time.monotonic())
    return url


def _match_events_lines(events: list[dict[str, Any]], limit: int = 8
                        ) -> list[str]:
    """Компактная история изменений матча (новые первыми)."""
    out = []
    for ev in events[:limit]:
        title = audit_svc._ACTION_TITLES.get(
            ev.get("action"), str(ev.get("action", "?")))
        actor = (
            f"<@{ev['actor_user_id']}>"
            if ev.get("actor_user_id") else "система"
        )
        out.append(f"- `{audit_svc._fmt_dt(ev.get('created_at'))}` "
                   f"**{title}** — {actor}")
    return out


async def _match_history_rows(
    guild_id: int, tournament_id: str, match_key: str
) -> list[Any]:
    """Записи match_history для матча (ELO/счёт — только
    подтверждённые данные)."""
    phase, idx = match_key.split(":", 1)
    match_id = f"{tournament_id}_{phase}_{idx}"
    try:
        from storage.match_history_store import match_history_store
        return await match_history_store.get_by_match(guild_id, match_id)
    except Exception as exc:
        logger.debug("journal: match history %s failed: %s",
                     match_id, exc)
        return []


async def build_match_card(
    tournament: "Tournament | None",
    m: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    tournament_id: str,
    guild_id: int,
) -> discord.ui.LayoutView:
    """Карточка одного матча в ветке журнала."""
    lines = [f"## {m['label']}"]
    tid = str(tournament_id)

    # Команды и составы — только если турнир доступен.
    team_blocks: list[str] = []
    if tournament is not None:
        a, b = m["teams"]
        for idx in (a, b):
            name = _team_name(tournament, idx)
            roster = _team_roster(tournament, idx)
            disp = [_display_name(tournament, n) for n in roster]
            team_blocks.append(
                f"**{name}**\n" + "\n".join(disp)
                if disp else f"**{name}**\n—"
            )
        lines += team_blocks
    else:
        lines.append("*Данные турнира недоступны — показаны "
                     "только события аудита.*")

    # Информация.
    started = next(
        (e for e in reversed(events)
         if str(e.get("action", "")).startswith(_MATCH_ACTIONS_PREFIXES)),
        None,
    )
    confirmed = next(
        (e for e in events
         if e.get("action") in (
             audit_svc.A.RESULT_CONFIRM, audit_svc.A.FINAL_CONFIRM)),
        None,
    )
    status = "Ожидает"
    if tournament is not None:
        status = _match_status(tournament, m, events)
    elif confirmed is not None:
        status = "Завершён"

    lines += [
        "",
        f"**Матч:** `{tid}:{m['phase']}:{m['index']}`",
        f"**Стадия:** {_PHASE_LABELS[m['phase']]} · **Статус:** {status}",
        f"**Начало:** {_fmt_ts(started.get('created_at')) if started else '—'}"
        f" · **Подтверждён:** "
        f"{_fmt_ts(confirmed.get('created_at')) if confirmed else '—'}",
    ]

    # Результат.
    winner_idx = _match_winner(tournament, m) if tournament else None
    if winner_idx is not None:
        wname = _team_name(tournament, winner_idx)
        confirmer = (
            f"<@{confirmed['actor_user_id']}>"
            if confirmed and confirmed.get("actor_user_id")
            else "—"
        )
        lines.append(f"**Результат:** {wname} — победитель "
                     f"(подтвердил {confirmer})")

    # Счёт и статистика.
    hist = await _match_history_rows(guild_id, tid, m["key"])
    if hist:
        # Счёт по командам: сумма kills победившей/проигравшей.
        score = _score_from_history(tournament, m, hist)
        if score:
            lines.append(f"**Счёт:** {score}")
        stat_lines = _stats_lines(tournament, m, hist)
        if stat_lines:
            lines.append("**Статистика:**")
            lines.extend(stat_lines)
    elif tournament is not None:
        # K/D введены, но результат не подтверждён — помечаем.
        kd = _kd_from_temp_stats(tournament, m)
        if kd:
            lines.append("**Статистика (введена, не подтверждена):**")
            lines.extend(kd)

    # История изменений.
    hist_lines = _match_events_lines(events)
    if hist_lines:
        lines.append("**История:**")
        lines.extend(hist_lines)

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(
        accent_colour=discord.Colour.dark_grey())
    container.add_item(discord.ui.TextDisplay(
        content="\n".join(lines)[:4000]))
    view.add_item(container)
    return view


def _score_from_history(tournament, m, hist) -> str:
    """Счёт kills по командам из подтверждённой истории."""
    if tournament is None:
        return ""
    a, b = m["teams"]
    ka = kb = 0
    for h in hist:
        if h.team_index == a:
            ka += h.kills or 0
        elif h.team_index == b:
            kb += h.kills or 0
    if ka or kb:
        return f"{_team_name(tournament, a)} {ka} : {kb} {_team_name(tournament, b)}"
    return ""


def _stats_lines(tournament, m, hist) -> list[str]:
    """K/D + ELO по игрокам из match_history."""
    if tournament is None:
        return []
    out = []
    for h in hist:
        name = ""
        for pname, uid in (tournament.player_user_ids or {}).items():
            if uid == h.user_id:
                name = pname
                break
        disp = _display_name(tournament, name) if name else f"<@{h.user_id}>"
        elo = h.elo_change or 0
        elo_s = f" (ELO {elo:+d})" if elo else ""
        out.append(
            f"- {disp}: {h.kills or 0}/{h.deaths or 0} K/D{elo_s}")
    return out


def _kd_from_temp_stats(tournament, m) -> list[str]:
    """K/D из temp_match_stats (неподтверждённый ввод)."""
    tid = str(tournament.id)
    key = f"{tid}_{m['phase']}_{m['index']}"
    stats = (getattr(tournament, "temp_match_stats", {}) or {}).get(key)
    if not stats:
        return []
    a, b = m["teams"]
    out = []
    for idx in (a, b):
        team = tournament.teams[idx] if idx < len(tournament.teams) else {}
        from utils.embeds import team_roster_names
        for pname in team_roster_names(team):
            st = stats.get(pname) or {}
            if st:
                out.append(
                    f"- {_display_name(tournament, pname)}: "
                    f"{st.get('kills', 0)}/{st.get('deaths', 0)} K/D")
    return out


def build_summary_card(
    tournament_label: str,
    general_events: list[dict[str, Any]],
    limit: int = 15,
) -> discord.ui.LayoutView:
    """Сводка общих (не-матчевых) событий турнира в ветке."""
    lines = ["## СОБЫТИЯ ТУРНИРА", f"-# {tournament_label}"]
    if not general_events:
        lines.append("\nСобытий пока нет.")
    else:
        for ev in general_events[:limit]:
            title = audit_svc._ACTION_TITLES.get(
                ev.get("action"), str(ev.get("action", "?")))
            actor = (
                f"<@{ev['actor_user_id']}>"
                if ev.get("actor_user_id") else "система"
            )
            lines.append(
                f"- `{audit_svc._fmt_dt(ev.get('created_at'))}` "
                f"**{title}** — {actor}")
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(
        accent_colour=discord.Colour.dark_grey())
    container.add_item(discord.ui.TextDisplay(
        content="\n".join(lines)[:4000]))
    view.add_item(container)
    return view


def _view_signature(view: discord.ui.LayoutView) -> str:
    """Сигнатура контента карточки — для пропуска лишних edit."""
    parts: list[str] = []

    def _walk(item):
        c = getattr(item, "content", None)
        if isinstance(c, str):
            parts.append(c)
        cid = getattr(item, "custom_id", None)
        url = getattr(item, "url", None)
        if cid:
            parts.append(str(cid))
        if url:
            parts.append(str(url))
        for ch in getattr(item, "children", []) or []:
            _walk(ch)

    for ch in getattr(view, "children", []) or []:
        _walk(ch)
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Публикация / редактирование сообщений
# ---------------------------------------------------------------------------


async def _audit_channel(bot: discord.Client):
    cid = _audit_channel_id()
    if not cid:
        return None
    ch = bot.get_channel(cid)
    if ch is None:
        try:
            ch = await bot.fetch_channel(cid)
        except Exception as exc:
            logger.warning("journal: audit channel %s unavailable: %s",
                           cid, exc)
            return None
    return ch


async def _send_or_edit(
    channel,
    *,
    message_id: int,
    view: discord.ui.LayoutView,
    sig_key: int,
) -> int:
    """Отправить или отредактировать сообщение карточки.

    Возвращает message_id. Идемпотентно: одинаковый контент
    не вызывает edit; удалённое сообщение пересоздаётся.
    Ошибки Discord классифицируются: временные (429/5xx/сеть)
    → _RetryableJournalError, постоянные (403/404/4xx) →
    _PermanentJournalError — «0 при ошибке» не возвращается,
    чтобы вызывающий код не мог принять сбой за успех.
    """
    sig = _view_signature(view)
    now = time.monotonic()
    if message_id and _SIG_CACHE.get(sig_key) == sig:
        # Контент не менялся — edit не нужен. Но раз в
        # _VERIFY_TTL проверяем, что сообщение не удалено
        # в Discord (иначе карточка потеряется навсегда).
        if now - _VERIFY_TS.get(message_id, 0.0) < _VERIFY_TTL:
            return message_id
        try:
            await channel.fetch_message(message_id)
            _VERIFY_TS[message_id] = now
            return message_id
        except discord.NotFound:
            _SIG_CACHE.pop(sig_key, None)
            message_id = 0  # удалено — пересоздаём ниже
        except Exception:
            return message_id  # Discord недоступен — не трогаем
    if message_id:
        last = _EDIT_TS.get(message_id, 0.0)
        wait = _MIN_EDIT_INTERVAL - (now - last)
        if wait > 0:
            # Недавний edit — выжидаем интервал и всё равно
            # применяем: пропуск потерял бы обновление, если
            # новых событий не будет (flush — фоновая задача).
            await asyncio.sleep(wait + 0.05)
            now = time.monotonic()
    try:
        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                _EDIT_TS[message_id] = now
                _VERIFY_TS[message_id] = now
                _SIG_CACHE[sig_key] = sig
                return message_id
            except discord.NotFound:
                message_id = 0  # сообщение удалено — создаём новое
        msg = await channel.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        _EDIT_TS[int(msg.id)] = now
        _VERIFY_TS[int(msg.id)] = now
        _SIG_CACHE[sig_key] = sig
        return int(msg.id)
    except discord.Forbidden as exc:
        logger.warning("journal: send/edit forbidden: %s", exc)
        raise _PermanentJournalError(f"Discord 403: {exc}") from exc
    except discord.NotFound as exc:
        # Канал/ветка недоступны — постоянная ошибка конфигурации.
        logger.warning("journal: send/edit not found: %s", exc)
        raise _PermanentJournalError(f"Discord 404: {exc}") from exc
    except discord.HTTPException as exc:
        status = getattr(exc, "status", None)
        retry_after = getattr(exc, "retry_after", None)
        if status == 429 or (status is not None and status >= 500):
            logger.info("journal: send/edit retryable %s: %s",
                        status, exc)
            err = _RetryableJournalError(f"Discord {status}: {exc}")
            err.retry_after = retry_after  # type: ignore[attr-defined]
            raise err from exc
        logger.warning("journal: send/edit http %s: %s", status, exc)
        raise _PermanentJournalError(f"Discord {status}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — сеть/таймауты = временное
        logger.info("journal: send/edit transient failure: %s", exc)
        raise _RetryableJournalError(str(exc)) from exc


async def _thread_candidates(message: discord.Message) -> list:
    """Активные и заархивированные ветки, связанные с сообщением.

    discord.py 2.7: ``guild.fetch_threads`` НЕ существует —
    активные ветки берутся через ``await guild.active_threads()``,
    архивные — через ``channel.archived_threads()`` родительского
    текстового канала.
    """
    out: list = []
    guild = getattr(message, "guild", None)
    channel = getattr(message, "channel", None)
    if guild is not None:
        try:
            out.extend(await guild.active_threads())
        except Exception as exc:
            logger.info("journal: active_threads failed: %s", exc)
    if channel is not None:
        try:
            async for th in channel.archived_threads(limit=100):
                out.append(th)
        except Exception as exc:
            logger.info("journal: archived_threads failed: %s", exc)
    return out


async def _find_thread_for_message(
    message: discord.Message, name: str
) -> discord.Thread | None:
    """Найти существующую ветку журнала для сообщения.

    Приоритет — надёжный идентификатор ``parent_id == message.id``
    (ветка, привязанная к конкретному сообщению-карточке), а не
    имя. Поиск по имени — только fallback.
    """
    try:
        candidates = await _thread_candidates(message)
    except Exception as exc:
        logger.warning("journal: thread candidates failed: %s", exc)
        return None
    mid = int(getattr(message, "id", 0) or 0)
    for th in candidates:
        if mid and int(getattr(th, "parent_id", 0) or 0) == mid:
            return th
    for th in candidates:
        if getattr(th, "name", None) == name[:100]:
            return th
    return None


async def _unarchive_thread(thread) -> None:
    if getattr(thread, "archived", False):
        try:
            await thread.edit(archived=False)
        except Exception as exc:
            logger.warning("journal: unarchive thread failed: %s", exc)


async def _ensure_thread(
    bot: discord.Client, message: discord.Message, name: str
) -> discord.Thread | None:
    """Создать ветку журнала или найти уже существующую.

    Идемпотентно: ошибка 160004 (thread already exists), потерянный
    ответ API после успешного создания или устаревший thread_id
    приводят к поиску существующей ветки по parent_id/name —
    повторное создание не выполняется. Временные ошибки
    (429/5xx/сеть) пробрасываются как _RetryableJournalError.
    """
    code = None
    try:
        thread = await message.create_thread(
            name=name[:100],
            auto_archive_duration=10080,  # 7 дней
        )
        return thread
    except discord.HTTPException as exc:
        status = getattr(exc, "status", None)
        code = getattr(exc, "code", None)
        if status == 429 or (status is not None and status >= 500):
            logger.info("journal: create_thread retryable %s: %s",
                        status, exc)
            err = _RetryableJournalError(f"Discord {status}: {exc}")
            err.retry_after = getattr(exc, "retry_after", None)  # type: ignore[attr-defined]
            raise err from exc
        if status in (403, 404) or code == 50001:
            logger.warning("journal: create_thread permanent "
                           "%s/%s: %s", status, code, exc)
            raise _PermanentJournalError(
                f"Discord {status}/{code}: {exc}") from exc
        logger.info("journal: create_thread %s (code=%s) — ищем "
                    "существующую", exc, code)
    except Exception as exc:  # noqa: BLE001 — сеть/таймаут = временное
        logger.info("journal: create_thread transient: %s", exc)
        raise _RetryableJournalError(str(exc)) from exc

    # Ветка могла существовать до потери thread_id или создаться
    # при потерянном ответе — находим по parent_id, затем по имени.
    found = await _find_thread_for_message(message, name)
    if found is not None:
        await _unarchive_thread(found)
        return found
    # 160004 «thread already exists», но поиск ничего не дал —
    # маловероятно, но не создаём вслепую повторно: считаем
    # временной ошибкой и отдаём на retry.
    if code == 160004:
        raise _RetryableJournalError(
            "thread exists per Discord but lookup failed")
    return None


async def _get_thread(
    bot: discord.Client, thread_id: int,
    *, expected_parent_id: int | None = None,
):
    """Получить ветку по id. Если задан expected_parent_id и у
    объекта есть parent_id, несовпадение = устаревшая ссылка →
    None (ветка будет найдена/создана через _ensure_thread)."""
    ch = bot.get_channel(thread_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(thread_id)
        except Exception:
            return None
    pid = getattr(ch, "parent_id", None)
    if (expected_parent_id is not None and pid is not None
            and int(pid) != int(expected_parent_id)):
        logger.info("journal: thread %s parent mismatch "
                    "(%s != %s) — stale id", thread_id, pid,
                    expected_parent_id)
        return None
    if getattr(ch, "archived", False):
        try:
            await ch.edit(archived=False)
        except Exception as exc:
            logger.warning("journal: unarchive thread failed: %s", exc)
    return ch


# ---------------------------------------------------------------------------
# Идемпотентный поиск уже опубликованных карточек
# ---------------------------------------------------------------------------


def _message_visible_text(message) -> str:
    """Весь видимый текст сообщения: content + Components V2.

    Карточки журнала — LayoutView: текст живёт в TextDisplay
    внутри components, message.content пуст.
    """
    parts: list[str] = []
    content = getattr(message, "content", None)
    if isinstance(content, str) and content:
        parts.append(content)

    def _walk(item):
        c = getattr(item, "content", None)
        if isinstance(c, str) and c:
            parts.append(c)
        for ch in getattr(item, "children", []) or []:
            _walk(ch)

    for comp in getattr(message, "components", []) or []:
        _walk(comp)
    return "\n".join(parts)


async def _find_card_message(
    channel, bot: discord.Client | None, markers: list[str],
) -> Any:
    """Найти в истории канала карточку журнала по маркерам.

    Используется когда Discord-сообщение создано, но его id не
    сохранился в PostgreSQL — вместо нового send принимаем
    существующее сообщение (иначе получим дубль карточки).

    Возвращает:
    - Message — карточка найдена;
    - None — поиск завершён, карточки нет;
    - поднимает _RetryableJournalError/_PermanentJournalError —
      поиск не удался, новый send запрещён (неизвестное
      состояние → не создаём дубликат).
    """
    bot_id = getattr(getattr(bot, "user", None), "id", 0)
    try:
        history = channel.history(limit=_HISTORY_SCAN_LIMIT)
        async for msg in history:
            author = getattr(msg, "author", None)
            if bot_id and getattr(author, "id", None) != bot_id:
                continue
            text = _message_visible_text(msg)
            if text and all(m in text for m in markers):
                return msg
    except discord.Forbidden as exc:
        raise _PermanentJournalError(
            f"history forbidden: {exc}") from exc
    except discord.NotFound as exc:
        raise _PermanentJournalError(
            f"history not found: {exc}") from exc
    except discord.HTTPException as exc:
        status = getattr(exc, "status", None)
        if status == 429 or (status is not None and status >= 500):
            raise _RetryableJournalError(
                f"history {status}: {exc}") from exc
        raise _PermanentJournalError(
            f"history http {status}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise _RetryableJournalError(f"history failed: {exc}") from exc
    return None


# ---------------------------------------------------------------------------
# Основной flush одного турнира
# ---------------------------------------------------------------------------


async def _flush_tournament(tournament_id: str) -> str:
    """Полная пересборка карточек журнала турнира (идемпотентно).

    Возвращает итоговый sync_status: 'synced' | 'partial' |
    'error'. Временные ошибки Discord/PG пробрасываются как
    _RetryableJournalError — запись уходит в retry_wait.
    """
    bot = _bot()
    if bot is None:
        return
    channel = await _audit_channel(bot)
    if channel is None:
        raise RuntimeError("audit channel unavailable")

    tid = str(tournament_id)
    gid = _guild_id_of(tid)

    # Запись журнала — создать при отсутствии.
    try:
        main = await journal_store.get_main(tid)
        if main is None:
            # channel_id источника — из событий (без фильтра
            # по каналу: записи разных каналов гильдии).
            evs = await _tournament_events(gid, None, tid, limit=5)
            src_cid = int(evs[0].get("channel_id") or 0) if evs else 0
            main = await journal_store.upsert_main(
                guild_id=gid, channel_id=src_cid, tournament_id=tid)
        # Отметить обработку — при падении процесса запись
        # «зависнет» в processing и будет подобрана list_pending
        # как устаревшая.
        try:
            await journal_store.set_processing(int(main["id"]))
        except Exception:
            pass
    except Exception as exc:
        # PG недоступен/сбой — временная ошибка: без подтверждённого
        # состояния в БД никаких Discord-операций не выполняем.
        logger.error("journal: main entry failed %s: %s", tid, exc)
        raise _RetryableJournalError(
            f"journal store: {exc}") from exc

    tournament = await _load_tournament(tid)
    # channel_id источника — уточняем из турнира/событий.
    src_cid = int(main.get("channel_id") or 0)
    if tournament is not None:
        src_cid = int(getattr(tournament, "channel_id", 0) or src_cid)
    if src_cid and src_cid != int(main.get("channel_id") or 0):
        try:
            await journal_store.update_ids(
                int(main["id"]), sync_status="pending")
        except Exception:
            pass

    events = await _tournament_events(
        gid, src_cid or None, tid)
    by_match, general = _group_events(events, tid)

    # --- Основная карточка ---
    cancelled = any(
        e.get("action") in (
            audit_svc.A.TOURNAMENT_DELETE, audit_svc.A.TOURNAMENT_CANCEL)
        for e in general[:3]
    )
    status_override = "cancelled" if cancelled else None
    main_view = build_main_card(
        tournament, main,
        status_override=status_override,
        events_general=general,
    )
    # Если id основной карточки не сохранён (Discord send прошёл,
    # а запись в PG упала) — сначала ищем уже опубликованное
    # сообщение в истории, иначе создадим дубль.
    main_mid = int(main.get("audit_message_id") or 0)
    if not main_mid:
        found = await _find_card_message(
            channel, bot, [f"**ID:** `{tid}`"])
        if found is not None:
            main_mid = int(found.id)
            logger.info("journal: adopted existing main card %s "
                        "for %s", main_mid, tid)
    new_main_mid = await _send_or_edit(
        channel,
        message_id=main_mid,
        view=main_view,
        sig_key=int(main["id"]),
    )
    if not new_main_mid:
        await journal_store.set_sync(
            int(main["id"]), "error", "main card publish failed")
        return "error"

    # Обновить result_message_id если турнир известен.
    result_mid = int(getattr(tournament, "message_id", 0) or 0) if (
        tournament is not None) else 0
    thread_id = int(main.get("thread_id") or 0)

    # --- Ветка ---
    thread = None
    if thread_id:
        thread = await _get_thread(
            bot, thread_id, expected_parent_id=new_main_mid)
    if thread is None:
        try:
            msg = await channel.fetch_message(new_main_mid)
            thread = await _ensure_thread(
                bot, msg, f"Журнал {tid}")
        except _PermanentJournalError as exc:
            logger.warning("journal: thread setup permanent %s: %s",
                           tid, exc)
            await journal_store.set_sync(
                int(main["id"]), "error", str(exc)[:400])
            return "error"
        # _RetryableJournalError и неожиданные ошибки
        # классифицируются в _flush_guarded → retry_wait.

    # --- Карточки матчей ---
    all_matches = (
        iter_all_matches(tournament) if tournament is not None else []
    )
    match_keys: set[str] = {m["key"] for m in all_matches}
    match_keys |= set(by_match.keys())
    match_by_key = {m["key"]: m for m in all_matches}

    summary_mid = int(main.get("summary_message_id") or 0)
    if thread is not None:
        # Сводка общих событий — одно сообщение в ветке.
        # При потерянном summary_message_id сначала ищем
        # существующую сводку — не создаём дубль.
        if not summary_mid:
            found = await _find_card_message(
                thread, bot, ["СОБЫТИЯ ТУРНИРА", tid])
            if found is not None:
                summary_mid = int(found.id)
        summary_view = build_summary_card(tid, general)
        try:
            summary_mid = await _send_or_edit(
                thread,
                message_id=summary_mid,
                view=summary_view,
                sig_key=int(main["id"]) * 10 + 1,
            )
        except _PermanentJournalError as exc:
            logger.warning("journal: summary permanent %s: %s",
                           tid, exc)
            summary_mid = 0

        for mk in sorted(
                match_keys,
                key=lambda k: (
                    _PHASE_ORDER.get(k.split(":")[0], 9),
                    int(k.split(":")[1]) if k.split(":")[1].isdigit()
                    else 0)):
            m = match_by_key.get(mk)
            if m is None:
                # Матч из событий, но нет в сетке (редкий случай) —
                # минимальная карточка только по событиям.
                phase, _, idx_s = mk.partition(":")
                try:
                    idx = int(idx_s)
                except ValueError:
                    continue
                m = {
                    "key": mk, "phase": phase, "index": idx,
                    "teams": (0, 1), "num": 0,
                    "label": f"МАТЧ — {_PHASE_LABELS.get(phase, phase)}",
                }
            entry = await journal_store.upsert_match(
                guild_id=gid, channel_id=src_cid,
                tournament_id=tid, match_key=mk)
            card = await build_match_card(
                tournament, m, by_match.get(mk, []),
                tournament_id=tid, guild_id=gid)
            # Потерянный audit_message_id → ищем карточку матча
            # в истории ветки до send (защита от дублей).
            m_mid = int(entry.get("audit_message_id") or 0)
            if not m_mid:
                phase_, _, idx_ = mk.partition(":")
                found = await _find_card_message(
                    thread, bot,
                    [f"**Матч:** `{tid}:{phase_}:{idx_}`"])
                if found is not None:
                    m_mid = int(found.id)
            try:
                mid = await _send_or_edit(
                    thread,
                    message_id=m_mid,
                    view=card,
                    sig_key=int(entry["id"]),
                )
            except _PermanentJournalError as exc:
                logger.warning("journal: match card permanent "
                               "%s %s: %s", tid, mk, exc)
                try:
                    await journal_store.set_sync(
                        int(entry["id"]), "error", str(exc)[:400])
                except Exception:
                    pass
                continue
            if mid and mid != int(entry.get("audit_message_id") or 0):
                try:
                    await journal_store.update_ids(
                        int(entry["id"]),
                        audit_channel_id=int(thread.id),
                        audit_message_id=mid,
                        sync_status="synced")
                except Exception as exc:
                    # Не прерываем остальные карточки — запись
                    # матча останется pending и будет повторена.
                    logger.warning(
                        "journal: match ids update failed %s: %s",
                        mk, exc)
            elif mid:
                await journal_store.set_sync(
                    int(entry["id"]), "synced")

    # --- Финализация записей ---
    # Честный статус: 'synced' только когда опубликовано ВСЁ —
    # карточка, ветка и сводка. Иначе 'partial': list_pending
    # подхватит запись после рестарта (thread_id IS NULL или
    # sync_status <> 'synced').
    incomplete = thread is None or not summary_mid
    final_status = "partial" if incomplete else "synced"
    await journal_store.update_ids(
        int(main["id"]),
        audit_channel_id=int(channel.id),
        audit_message_id=new_main_mid,
        thread_id=int(thread.id) if thread is not None else None,
        result_message_id=result_mid or None,
        summary_message_id=summary_mid or None,
        status=("cancelled" if cancelled else None),
        sync_status=final_status,
    )
    _URL_CACHE.pop(tid, None)

    # Если турнир завершён — добавить ссылку «Журнал турнира»
    # в итоговую карточку (если её ещё нет).
    if tournament is not None and str(
            getattr(getattr(tournament, "phase", ""), "value", "")
    ) == "complete":
        await attach_result_link(tid)

    return final_status


def _retry_delay(attempts: int, retry_after: float | None = None
                 ) -> float:
    """Exponential backoff + jitter; retry_after Discord имеет
    приоритет (но не меньше backoff — не ддосим API)."""
    base = min(_RETRY_BASE_SEC * (2 ** max(0, attempts - 1)),
               _RETRY_MAX_SEC)
    delay = base * (1 + random.uniform(-_RETRY_JITTER, _RETRY_JITTER))
    if retry_after:
        delay = max(delay, float(retry_after) + 1.0)
    return delay


async def _schedule_retry(tournament_id: str, exc: Exception) -> str:
    """Перевести турнир в retry_wait и поставить в очередь.

    Персистентно (next_attempt_at/retry_count в PG); после
    _RETRY_MAX_ATTEMPTS запись становится 'error'.
    """
    tid = str(tournament_id)
    attempts = _RETRY_ATTEMPTS.get(tid, 0) + 1
    _RETRY_ATTEMPTS[tid] = attempts
    retry_after = getattr(exc, "retry_after", None)
    delay = _retry_delay(attempts, retry_after)
    try:
        main = await journal_store.get_main(tid)
        if main is not None:
            if attempts >= 10:
                await journal_store.set_sync(
                    int(main["id"]), "error",
                    f"retry limit: {str(exc)[:300]}")
                _RETRY_ATTEMPTS.pop(tid, None)
                return "error"
            await journal_store.set_retry(
                int(main["id"]), delay, str(exc)[:400])
    except Exception as store_exc:  # noqa: BLE001
        # PG недоступен — состояние не сохранить, но in-memory
        # retry всё равно планируем: при следующей попытке PG
        # может уже отвечать.
        logger.warning("journal: set_retry failed %s: %s",
                       tid, store_exc)
    schedule_recovery(tid, delay=delay)
    logger.info("journal: %s → retry_wait in %.0fs (attempt %d): %s",
                tid, delay, attempts, exc)
    return "retry_wait"


_RETRY_MAX_ATTEMPTS = 10


async def _flush_guarded(tournament_id: str) -> str:
    """Flush под per-tournament lock с классификацией ошибок.

    Возвращает итоговый статус: 'synced' | 'partial' | 'retry_wait'
    | 'error'. Никогда не бросает исключений наружу.
    """
    tid = str(tournament_id)
    lock = _FLUSH_LOCKS.setdefault(tid, asyncio.Lock())
    async with lock:
        try:
            result = await _flush_tournament(tid)
            _RECOVERY_STATS[result] = _RECOVERY_STATS.get(result, 0) + 1
            if result in ("synced", "partial"):
                _RETRY_ATTEMPTS.pop(tid, None)
            return result
        except _RetryableJournalError as exc:
            result = await _schedule_retry(tid, exc)
            _RECOVERY_STATS[result] = \
                _RECOVERY_STATS.get(result, 0) + 1
            return result
        except _PermanentJournalError as exc:
            logger.warning("journal flush permanent %s: %s", tid, exc)
            try:
                main = await journal_store.get_main(tid)
                if main:
                    await journal_store.set_sync(
                        int(main["id"]), "error", str(exc)[:400])
            except Exception:
                pass
            _RECOVERY_STATS["error"] = \
                _RECOVERY_STATS.get("error", 0) + 1
            return "error"
        except Exception as exc:  # noqa: BLE001
            # Неожиданная ошибка — считаем временной: терять
            # запись журнала хуже, чем повторить (attempts
            # ограничены → со временем 'error').
            logger.error("journal flush failed %s: %s", tid, exc)
            result = await _schedule_retry(tid, exc)
            _RECOVERY_STATS[result] = \
                _RECOVERY_STATS.get(result, 0) + 1
            return result


# ---------------------------------------------------------------------------
# Worker очереди восстановления
# ---------------------------------------------------------------------------


def schedule_recovery(tournament_id: str, *,
                      delay: float = 0.0) -> None:
    """Поставить турнир в очередь worker'а (dedup по tid).

    delay — секунды до выполнения (для retry_wait); в PG
    дублируется next_attempt_at для восстановления после рестарта.
    """
    tid = str(tournament_id)
    if tid in _RECOVERY_QUEUED:
        return
    _RECOVERY_QUEUED.add(tid)
    heapq.heappush(_RECOVERY_HEAP,
                   (time.monotonic() + max(0.0, delay), tid))
    _ensure_recovery_worker()


def _ensure_recovery_worker() -> None:
    global _RECOVERY_WORKER
    if (_RECOVERY_WORKER is None
            or _RECOVERY_WORKER.done()):
        try:
            _RECOVERY_WORKER = asyncio.create_task(
                _recovery_worker())
        except RuntimeError:
            _RECOVERY_WORKER = None  # нет loop — pending в PG


async def _recovery_worker() -> None:
    """Единственный worker (пере)публикации журналов.

    Сериализует flush'и: не более одной операции за раз,
    пауза _WORKER_INTERVAL между турнирами — Discord API
    не получает массовый поток запросов после рестарта.
    """
    while True:
        try:
            if not _RECOVERY_HEAP:
                await asyncio.sleep(0.5)
                continue
            run_at, tid = _RECOVERY_HEAP[0]
            wait = run_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(min(wait, 5.0))
                continue
            heapq.heappop(_RECOVERY_HEAP)
            _RECOVERY_QUEUED.discard(tid)
            try:
                await _flush_guarded(tid)
            except Exception as exc:  # noqa: BLE001
                logger.error("journal worker flush %s: %s",
                             tid, exc)
            await asyncio.sleep(_WORKER_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("journal recovery worker error: %s", exc)
            await asyncio.sleep(5.0)


async def _report_recovery_done() -> None:
    """Дождаться опустошения очереди и залогировать итог
    восстановления (checked/restored/partial/retry/error)."""
    try:
        # Ждём пока heap пуст и текущий flush завершён.
        for _ in range(720):  # до ~1 часа с паузами по 5с
            if not _RECOVERY_HEAP and not _RECOVERY_QUEUED:
                await asyncio.sleep(2.0)
                if not _RECOVERY_HEAP and not _RECOVERY_QUEUED:
                    break
            await asyncio.sleep(5.0)
        stats = dict(_RECOVERY_STATS)
        if stats:
            logger.info(
                "journal recover done: synced=%d partial=%d "
                "retry_wait=%d error=%d",
                stats.get("synced", 0), stats.get("partial", 0),
                stats.get("retry_wait", 0), stats.get("error", 0))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("journal recover report failed: %s", exc)


# ---------------------------------------------------------------------------
# Точка входа от аудита
# ---------------------------------------------------------------------------


def notify_event(ev: dict[str, Any]) -> None:
    """Получить событие аудита и запланировать flush журнала.

    Вызывается из audit.record() после успешной записи в PG.
    События не из турнирных источников игнорируются (MM, магазин,
    системные — у них свои каналы/карточки).
    """
    try:
        if not isinstance(ev, dict):
            return
        if ev.get("persisted") != "pg":
            # pending-события догонит flush_pending → notify повторится
            return
        if ev.get("source") not in _JOURNAL_SOURCES:
            return
        if _is_organizer_event(ev):
            return
        tid = ev.get("tournament_id")
        if not tid:
            return
        tid = str(tid)
        _DIRTY[tid] = {"ts": time.monotonic()}
        task = _DIRTY[tid].get("task")
        if task is None or task.done():
            coro = _debounced_flush(tid)
            try:
                _DIRTY[tid]["task"] = asyncio.create_task(coro)
            except RuntimeError:
                coro.close()  # нет запущенного loop — pending догонит
            except Exception:
                coro.close()
                raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("journal notify failed: %s", exc)


async def _debounced_flush(tournament_id: str) -> None:
    try:
        await asyncio.sleep(_DEBOUNCE_SEC)
        # Если за время сна пришли новые события — подождём ещё
        # один цикл, но не дольше 3 итераций.
        for _ in range(3):
            dirty = _DIRTY.get(tournament_id)
            if dirty is None:
                return
            if time.monotonic() - dirty["ts"] >= _DEBOUNCE_SEC:
                break
            await asyncio.sleep(_DEBOUNCE_SEC)
        _DIRTY.pop(tournament_id, None)
        await _flush_guarded(tournament_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("journal debounced flush failed: %s", exc)


# ---------------------------------------------------------------------------
# Ссылки итогам ↔ журналу
# ---------------------------------------------------------------------------


async def attach_result_link(tournament_id: str) -> bool:
    """Добавить «Журнал турнира» в итоговую карточку турнира.

    Вызывается после публикации основной карточки журнала.
    Идемпотентно: перерисовывает карточку с link-кнопкой.
    """
    try:
        tid = str(tournament_id)
        main = await journal_store.get_main(tid)
        if not main or not main.get("audit_message_id"):
            return False
        jurl = _journal_url(main)
        if not jurl:
            return False
        tournament = await _load_tournament(tid)
        if tournament is None or not getattr(
                tournament, "message_id", 0):
            return False
        bot = _bot()
        if bot is None:
            return False
        guild = bot.get_guild(int(getattr(tournament, "guild_id", 0)))
        if guild is None:
            try:
                guild = await bot.fetch_guild(
                    int(tournament.guild_id))
            except Exception:
                return False
        channel = guild.get_channel(int(tournament.channel_id))
        if channel is None:
            try:
                channel = await guild.fetch_channel(
                    int(tournament.channel_id))
            except Exception:
                return False
        message = await channel.fetch_message(tournament.message_id)
        from services.tournament_lifecycle import (
            render_final_card_to_message)
        return await render_final_card_to_message(
            message, tournament, guild, journal_url=jurl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("journal: attach_result_link %s failed: %s",
                       tournament_id, exc)
        return False


# ---------------------------------------------------------------------------
# Восстановление после рестарта
# ---------------------------------------------------------------------------


async def recover(bot: discord.Client | None = None) -> dict[str, int]:
    """Согласованность журнала после рестарта.

    1. Собрать все pending-записи (sync_status <> 'synced' или
       без message_id/thread_id/summary) и поставить их
       турниры в очередь единого worker'а — без массовой
       параллельной публикации.
    2. Для активных турниров в store без записи журнала —
       тоже в очередь.
    3. Для завершённых — ссылка на журнал в итоговой карточке.

    Возвращает статистику постановки в очередь
    {checked, scheduled, active_created, links}. Фактические
    итоги синхронизации логирует _report_recovery_done после
    опустошения очереди. Не бросает исключений и не блокирует
    турнирную механику.
    """
    global _RECOVERED
    stats = {"checked": 0, "scheduled": 0,
             "active_created": 0, "links": 0}
    if _RECOVERED:
        return stats
    _RECOVERED = True
    _RECOVERY_STATS.clear()
    if bot is not None:
        bind_bot(bot)
    if _bot() is None:
        return stats

    tids: set[str] = set()
    try:
        pending = await journal_store.list_pending(limit=200)
    except Exception as exc:
        logger.warning("journal recover: list_pending failed: %s",
                       exc)
        pending = []
    for entry in pending:
        tid = str(entry.get("tournament_id") or "")
        if tid:
            tids.add(tid)
    stats["checked"] = len(pending)

    # Активные турниры без записи журнала — тоже в очередь.
    try:
        for t in tournament_store.all():
            tid = str(t.id)
            try:
                if await journal_store.get_main(tid) is None:
                    tids.add(tid)
                    stats["active_created"] += 1
            except Exception:
                continue
    except Exception as exc:
        logger.warning("journal recover: store scan failed: %s",
                       exc)

    for tid in sorted(tids):
        schedule_recovery(tid)
        stats["scheduled"] += 1

    # Завершённые — ссылка на журнал в итогах (отдельная
    # фоновая задача, не блокирует worker).
    try:
        for t in tournament_store.all():
            if str(getattr(getattr(t, "phase", ""), "value", "")) \
                    == "complete":
                stats["links"] += 1
                asyncio.create_task(attach_result_link(str(t.id)))
    except Exception:
        pass

    if stats["scheduled"] or stats["links"]:
        _ensure_recovery_worker()
        try:
            asyncio.create_task(_report_recovery_done())
        except RuntimeError:
            pass
        logger.info("journal recover scheduled: %s", stats)
    return stats
