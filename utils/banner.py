"""Общий PNG-баннер «Поиск игроков» (assets/queue_banner.png).

Один исходный файл используется панелью MM-очереди
(views/mm_search_view.py) и панелью организатора
(views/org_role_view.py) через MediaGallery + attachment://.

Правило вложений: файл прикрепляется при создании сообщения и
при редактировании — только если баннер отсутствует среди
вложений (старое вложение сохраняется, дублей не создаётся).
"""

from __future__ import annotations

from pathlib import Path

import discord

FILENAME = "queue_banner.png"
MEDIA = f"attachment://{FILENAME}"
_PATH = Path(__file__).resolve().parents[1] / "assets" / FILENAME


def available() -> bool:
    return _PATH.exists()


def banner_file() -> discord.File | None:
    """discord.File для attachment:// (None — файла нет на диске)."""
    if _PATH.exists():
        return discord.File(str(_PATH), filename=FILENAME)
    return None


def has_banner(message) -> bool:
    """Баннер уже есть среди вложений сообщения."""
    for att in getattr(message, "attachments", None) or []:
        if getattr(att, "filename", "") == FILENAME:
            return True
    return False


def edit_attachments(message) -> list[discord.File] | None:
    """Вложения для edit сообщения.

    None — вложения не трогать (баннер уже прикреплён);
    [file] — прикрепить/заменить баннер (вложения отсутствуют
    или устарели — например ranked.png у старой панели).
    """
    if has_banner(message):
        return None
    f = banner_file()
    return [f] if f is not None else None
