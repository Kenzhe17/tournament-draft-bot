"""Рендер карточек информационного центра (Components V2).

Структура сообщения: MediaGallery(баннер) → сепаратор → по каждому
блоку контента «> ## Заголовок» + текст. Отправка:
channel.send(view=view, file=discord.File(banner)) — banner
подставляется через attachment://.
"""

from __future__ import annotations

import discord

from services.info_content import InfoSection, parse_blocks
from utils.design.containers import separator
from utils.design.layouts import DesignView


def banner_filename(section: InfoSection) -> str:
    return section.banner


def build_info_view(section: InfoSection) -> discord.ui.LayoutView:
    """Собрать V2-карточку раздела (без файла — он крепится к сообщению)."""
    view = DesignView(timeout=None)
    view.component(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(
                media=f"attachment://{banner_filename(section)}"
            )
        )
    )
    view.container.add_item(separator(large=True))

    for heading, body in parse_blocks(section):
        view.text(f"> ## {heading[:80]}")
        view.text(body)
        view.container.add_item(separator())
    return view
