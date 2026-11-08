"""V2-карточка /birthdays: баннер + именинники сегодня и ближайшие."""

from __future__ import annotations

import discord
from discord.utils import escape_mentions

from services.social.common import (
    BANNER_BIRTHDAYS, banner_file, social_view,
)

_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _display_name(entry: dict) -> str:
    """@mention для участника сервера, безопасное имя для вышедших."""
    if entry.get("mention"):
        return entry["mention"]
    return escape_mentions(str(entry.get("name") or "Игрок"))


def _birthday_label(birthday: str) -> str:
    """'09.11.2001' -> '9 ноября' (без года)."""
    try:
        d, m = (int(x) for x in birthday.split(".")[:2])
        return f"{d} {_MONTHS[m - 1]}"
    except (ValueError, IndexError):
        return birthday


def build_birthdays_view(entries: list[dict]) -> discord.ui.LayoutView:
    """Собрать V2-view списка дней рождения.

    entries — результат collect_entries() (отсортирован по days).
    """
    view = social_view(BANNER_BIRTHDAYS, timeout=None)
    view.text("**Дни рождения участников**\nБлижайшие праздники сообщества r!z3")
    view.sep()

    if not entries:
        view.text("Пока нет сохранённых дней рождения.")
        view.sep()
        view.text("*Установить дату можно через `/player set birthday`.*")
        return view

    today_entries = [e for e in entries if e["days"] == 0]
    upcoming = [e for e in entries if e["days"] > 0][:5]

    if today_entries:
        view.text("**СЕГОДНЯ**")
        for e in today_entries:
            view.text(
                f"{_display_name(e)} — {_birthday_label(e['birthday'])}"
            )
        view.sep()

    if upcoming:
        view.text("**БЛИЖАЙШИЕ ДНИ РОЖДЕНИЯ**")
        for e in upcoming:
            view.text(
                f"{_display_name(e)} — {_birthday_label(e['birthday'])}"
            )
        view.sep()

    view.text("*Не забудь поздравить именинников!*")
    return view


def birthdays_file() -> discord.File:
    return banner_file(BANNER_BIRTHDAYS)
