"""Общие строители V2-карточек социальных функций.

Все сообщения: один Container с MediaGallery(баннер) сверху,
TextDisplay-блоками и нативными Separator. Файл баннера
прикрепляется к сообщению через attachment://.
"""

from __future__ import annotations

from pathlib import Path

import discord

from utils.design.layouts import DesignView

SOCIAL_DIR = (
    Path(__file__).resolve().parents[2] / "assets" / "social"
)

BANNER_BIRTHDAYS = "birthdays.png"
BANNER_PROPOSAL = "proposal.png"
BANNER_MARRIAGE = "marriage.png"
BANNER_BIRTHDAY_CONGRATS = "birthday_congrats.png"
BANNER_MARRIAGES = "marriages.png"


def banner_path(filename: str) -> Path:
    return SOCIAL_DIR / filename


def banner_file(filename: str) -> discord.File:
    return discord.File(str(banner_path(filename)), filename=filename)


def social_view(banner_filename: str, *, timeout: float | None = None) -> DesignView:
    """DesignView с баннером MediaGallery первым элементом."""
    view = DesignView(timeout=timeout)
    view.component(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(media=f"attachment://{banner_filename}")
        )
    )
    view.sep(large=True)
    return view
