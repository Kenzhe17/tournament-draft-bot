"""Утилиты дней рождения: сбор записей и embed поздравления."""

from datetime import date, datetime, timedelta, timezone

import discord

from storage.player_stats_store import player_stats_store


def astana_today() -> date:
    """Текущая дата по Астане (UTC+5, без перехода на летнее время)."""
    return (datetime.now(timezone.utc) + timedelta(hours=5)).date()


def days_until_birthday(birthday: str, today: date | None = None) -> int | None:
    """Дней до ближайшего дня рождения. birthday — 'ДД.ММ.ГГГГ'."""
    if not birthday:
        return None
    try:
        day_s, month_s = birthday.split(".")[:2]
        day, month = int(day_s), int(month_s)
    except (ValueError, AttributeError):
        return None
    today = today or astana_today()
    try:
        this_year = date(today.year, month, day)
    except ValueError:
        return None
    nxt = this_year if this_year >= today else date(today.year + 1, month, day)
    return (nxt - today).days


async def collect_entries(guild: discord.Guild) -> list[dict]:
    """Собрать дни рождения сервера, отсортированные по ближайшим."""
    stats_list = await player_stats_store.get_all(guild.id)
    today = astana_today()
    entries = []
    for s in stats_list:
        birthday = getattr(s, "birthday", "") or ""
        days = days_until_birthday(birthday, today)
        if days is None:
            continue
        member = guild.get_member(s.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(s.user_id)
            except Exception:
                member = None
        if member is None:
            continue  # участник покинул сервер — не показываем
        entries.append({
            "user_id": s.user_id,
            "name": getattr(s, "name", "") or "Unknown",
            "birthday": birthday,
            "days": days,
            "mention": member.mention,
        })
    entries.sort(key=lambda e: (e["days"], e["name"].lower()))
    return entries
