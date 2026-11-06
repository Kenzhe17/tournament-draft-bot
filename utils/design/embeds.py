"""EmbedBuilder — единый шаблон embed для дизайн-системы r!z3.

Композиция: title -> description (1-2 строки) -> секции через fields.
Разделители между секциями — нативные Discord embed fields (inline=False).
"""

from __future__ import annotations

import discord

from utils.design.colors import category_color
from utils.design.emojis import emoji


def section(name: str, value: str) -> dict:
    """Секция embed: name + value, всегда inline=False (визуальный разделитель)."""
    return {"name": name, "value": value, "inline": False}


def kv(name: str, value: str, inline: bool = True) -> dict:
    """Пара «параметр: значение» для компактных строк."""
    return {"name": name, "value": value, "inline": inline}


class DesignEmbed(discord.Embed):
    """Embed дизайн-системы.

    category: success | error | warning | info | economy | neutral
              (или доменный алиас: tournament, profile, shop, game...)
    author: discord.Member/User для set_author(name, icon_url)
    footer: текст футера (добавлять только при необходимости)
    """

    def __init__(
        self,
        *,
        title: str | None = None,
        description: str | None = None,
        category: str | None = None,
        color: discord.Color | int | None = None,
        author=None,
        footer: str | None = None,
        url: str | None = None,
        timestamp: bool = False,
        thumbnail: str | None = None,
        image: str | None = None,
    ) -> None:
        super().__init__(
            title=title,
            description=description,
            color=color if color is not None else category_color(category),
            url=url,
        )
        if author is not None:
            name = getattr(author, "display_name", None) or getattr(author, "name", "")
            icon = getattr(author, "display_avatar", None)
            icon_url = getattr(icon, "url", None) if icon else None
            self.set_author(name=name, icon_url=icon_url)
        if footer:
            self.set_footer(text=footer)
        if timestamp:
            self.timestamp = discord.utils.utcnow()
        if thumbnail:
            self.set_thumbnail(url=thumbnail)
        if image:
            self.set_image(url=image)

    # --- Полевые помощники -------------------------------------------------

    def add_section(self, name: str, value: str) -> "DesignEmbed":
        """Полноширинная секция — Discord сам рисует разделитель между полями."""
        self.add_field(name=name, value=value or "\u200b", inline=False)
        return self

    def add_kv(self, name: str, value: str, inline: bool = True) -> "DesignEmbed":
        self.add_field(name=name, value=value, inline=inline)
        return self

    def add_result(self, text: str) -> "DesignEmbed":
        """Секция результата (успех/ошибка задаётся категорией embed)."""
        return self.add_section(emoji("a_star"), text)


def build(
    *,
    title: str | None = None,
    description: str | None = None,
    category: str | None = None,
    author=None,
    footer: str | None = None,
    sections: list[dict] | None = None,
    **kwargs,
) -> DesignEmbed:
    """Функциональный конструктор: build(category='success', title=..., sections=[...])."""
    embed = DesignEmbed(
        title=title,
        description=description,
        category=category,
        author=author,
        footer=footer,
        **kwargs,
    )
    for field in sections or []:
        embed.add_field(**field)
    return embed
