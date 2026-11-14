"""Подсчёт активности и выбор участника дня.

Activity Score = floor(min(voice_seconds, 21600) / 300) + 10 * matches

- voice_seconds: только подтверждённое присутствие в войсе (сканер
  в cogs/banner.py), максимум 6 часов в сутки
- matches: DISTINCT подтверждённых турнирных матчей из match_history

Tiebreak: matches DESC -> voice_seconds DESC -> user_id ASC.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

logger = logging.getLogger(__name__)

TZ_NAME = "Asia/Almaty"
MAX_VOICE_SECONDS = 6 * 3600          # 6 часов в сутки
POINTS_PER_5MIN = 1                   # 1 очко за каждые полные 5 минут
MATCH_POINTS = 10                     # 10 очков за подтверждённый матч


def _almaty_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(TZ_NAME)


def today_almaty(now: datetime | None = None) -> date:
    """Текущая календарная дата в Asia/Almaty."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(_almaty_tz()).date()


def almaty_day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """Границы дня [00:00, 24:00) по Asia/Almaty как naive UTC-метки.

    match_history.date хранится как naive datetime в локальном времени
    сервера (Railway = UTC), поэтому сравниваем с naive UTC.
    """
    tz = _almaty_tz()
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )


def activity_score(voice_seconds: int, matches: int) -> int:
    capped = min(max(voice_seconds, 0), MAX_VOICE_SECONDS)
    return (capped // 300) * POINTS_PER_5MIN + MATCH_POINTS * max(matches, 0)


@dataclass
class DayEntry:
    user_id: int
    voice_seconds: int
    matches: int
    score: int


def rank_entries(
    voice_map: dict[int, int], match_map: dict[int, int]
) -> list[DayEntry]:
    """Собрать и отсортировать участников дня."""
    user_ids = set(voice_map) | set(match_map)
    entries = [
        DayEntry(
            user_id=uid,
            voice_seconds=voice_map.get(uid, 0),
            matches=match_map.get(uid, 0),
            score=activity_score(voice_map.get(uid, 0), match_map.get(uid, 0)),
        )
        for uid in user_ids
    ]
    entries.sort(
        key=lambda e: (-e.score, -e.matches, -e.voice_seconds, e.user_id)
    )
    return entries


async def compute_day(
    guild_id: int, day: date, store
) -> tuple[list[DayEntry], DayEntry | None]:
    """Полный расчёт дня: voice из banner_voice_daily,
    матчи из match_history. Сохраняет снапшот в banner_day_stats.
    Возвращает (все записи по убыванию, победитель|None)."""
    voice_map = await store.get_voice_day_map(guild_id, day)
    start, end = almaty_day_bounds_utc(day)
    match_map = await store.get_match_counts(guild_id, start, end)

    entries = rank_entries(voice_map, match_map)
    winner = entries[0] if entries and entries[0].score > 0 else None

    await store.save_day_stats(
        guild_id,
        day,
        [(e.user_id, e.voice_seconds, e.matches, e.score) for e in entries],
    )
    return entries, winner
