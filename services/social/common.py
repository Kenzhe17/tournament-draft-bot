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
    Path(__file__).resolve().parents[2] / "assets_ds_server" / "social"
)

BANNER_BIRTHDAYS = "dni_rozhdeniya.png"
BANNER_PROPOSAL = "predlozhenie.png"
BANNER_MARRIAGE = "brak.png"


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
