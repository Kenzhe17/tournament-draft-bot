"""Цветовая палитра Minimal Dark UI.

Embed color в Discord — это цветная вертикальная полоса слева.
Каждое сообщение относится к категории, которая определяет цвет полосы.
"""

from __future__ import annotations

import discord

# --- Базовая палитра (для справки, не задаётся напрямую в Embed) ---
BG = 0x232428          # основной фон embed (справочно)
TEXT = 0xE6E6E8        # основной текст (справочно)
TEXT_SECONDARY = 0xA5A5AA
TEXT_MUTED = 0x77777F
DIVIDER = 0x414146     # тонкие разделители между секциями


class Category:
    """Категории сообщений -> цвет вертикальной полосы слева."""

    SUCCESS = "success"      # #2ECC71
    ERROR = "error"          # #E74C3C
    WARNING = "warning"      # #F1C40F
    INFO = "info"            # #5865F2
    ECONOMY = "economy"      # #F0B232
    NEUTRAL = "neutral"      # #747F8D


_CATEGORY_COLORS = {
    Category.SUCCESS: discord.Color(0x2ECC71),
    Category.ERROR: discord.Color(0xE74C3C),
    Category.WARNING: discord.Color(0xF1C40F),
    Category.INFO: discord.Color(0x5865F2),
    Category.ECONOMY: discord.Color(0xF0B232),
    Category.NEUTRAL: discord.Color(0x747F8D),
}

# Доменные алиасы категорий
CATEGORY_ALIASES = {
    "tournament": Category.INFO,
    "profile": Category.NEUTRAL,
    "stats": Category.INFO,
    "economy": Category.ECONOMY,
    "shop": Category.ECONOMY,
    "game": Category.INFO,
    "minigame": Category.INFO,
    "moderation": Category.WARNING,
    "admin": Category.WARNING,
    "error": Category.ERROR,
    "success": Category.SUCCESS,
    "warning": Category.WARNING,
    "info": Category.INFO,
    "neutral": Category.NEUTRAL,
}


def category_color(category: str | None) -> discord.Color:
    """Вернуть discord.Color для категории (или алиаса)."""
    if not category:
        return _CATEGORY_COLORS[Category.NEUTRAL]
    key = CATEGORY_ALIASES.get(category, category)
    return _CATEGORY_COLORS.get(key, _CATEGORY_COLORS[Category.NEUTRAL])
