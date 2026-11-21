"""PNG-баннер ротации доступа (assets/access_rotation_banner.png).

Используется CV2-панелью ротации (views/access_panel.py,
cogs/access.py, services/access_rotation.py) через MediaGallery +
attachment://.

Правило вложений (как utils/banner.py): файл прикрепляется при
создании сообщения и при редактировании — только если баннер
отсутствует среди вложений (старое вложение сохраняется, дублей
и повторной отправки PNG при каждом обновлении нет).
"""

from __future__ import annotations

from pathlib import Path

import discord

FILENAME = "access_rotation_banner.png"
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
    [file] — прикрепить баннер (вложения отсутствуют).
    """
    if has_banner(message):
        return None
    f = banner_file()
    return [f] if f is not None else None
