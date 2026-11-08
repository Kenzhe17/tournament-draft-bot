"""Welcome-карточка Components V2 для новых участников.

Структура: MediaGallery(welcome.png) → приветствие → Separator →
информация → Separator → финальная строка. Без кнопок и эмодзи-UI.
"""

from __future__ import annotations

from pathlib import Path

import discord

from utils.design.layouts import DesignView

WELCOME_PNG = (
    Path(__file__).resolve().parents[1]
    / "assets_ds_server" / "welcome" / "welcome.png"
)
WELCOME_FILENAME = "welcome.png"


def build_welcome_view(
    member_number: int,
    member: discord.Member,
    info_channel_id: int | None = None,
) -> discord.ui.LayoutView:
    """Собрать V2-view приветствия. Файл прикрепляется к сообщению."""
    info_ref = f"<# {info_channel_id}>".replace(" ", "") if info_channel_id else "#информация"
    view = DesignView(timeout=None)
    view.component(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(media=f"attachment://{WELCOME_FILENAME}")
        )
    )
    view.sep(large=True)
    view.text(
        "₊˚⊹♡ **Добро пожаловать на сервер**, "
        f"{member.mention}!\n\n"
        f"Ты стал(а) нашим **{member_number}-м** участником ♡"
    )
    view.sep()
    view.text(
        "Рады видеть тебя среди нас. Ознакомься с информацией и "
        f"правилами в {info_ref}."
    )
    view.sep()
    view.text("୨୧ надеемся, тебе у нас понравится ୨୧")
    return view


def welcome_file() -> discord.File:
    return discord.File(str(WELCOME_PNG), filename=WELCOME_FILENAME)
