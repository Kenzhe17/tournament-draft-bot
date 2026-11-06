"""Централизованная дизайн-система r!z3 (Minimal Dark UI).

Все визуальные стандарты бота: цвета, эмодзи, embeds, компоненты, форматтеры.
Новые команды должны использовать этот пакет вместо прямого discord.Embed/discord.ui.Button.
"""

from utils.design.colors import (
    BG,
    DIVIDER,
    TEXT,
    TEXT_SECONDARY,
    TEXT_MUTED,
    Category,
    category_color,
)
from utils.design.emojis import emoji, partial_emoji, E
from utils.design.formatters import (
    fmt_int,
    fmt_money,
    fmt_percent,
    fmt_rating,
    fmt_games,
    fmt_xp,
)
from utils.design.embeds import DesignEmbed, section, kv
from utils.design.components import (
    ButtonRole,
    design_button,
    design_select,
)

__all__ = [
    "BG", "DIVIDER", "TEXT", "TEXT_SECONDARY", "TEXT_MUTED",
    "Category", "category_color",
    "emoji", "partial_emoji", "E",
    "fmt_int", "fmt_money", "fmt_percent", "fmt_rating", "fmt_games", "fmt_xp",
    "DesignEmbed", "section", "kv",
    "ButtonRole", "design_button", "design_select",
]
