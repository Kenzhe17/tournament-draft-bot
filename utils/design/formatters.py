"""Единое форматирование значений дизайн-системы.

- числа с разделителями тысяч
- валюта через единый эмодзи
- проценты, рейтинги, даты
"""

from __future__ import annotations

import time

import discord

from utils.design.emojis import em


def num(value: float | int, decimals: int = 0) -> str:
    """Число с разделителями тысяч. num(1234567) -> '1 234 567'."""
    if decimals <= 0:
        return f"{int(round(value)):,}".replace(",", " ")
    return f"{value:,.{decimals}f}".replace(",", " ")


def money(value: float | int) -> str:
    """Сумма в валюте бота: '1 234 🪙'."""
    return f"**{num(value)}** {em('coin')}"


def pct(value: float, decimals: int = 1) -> str:
    """Процент в едином формате: '45.5%'."""
    return f"{value:.{decimals}f}%"


def rating(value: float | int, suffix: str = "/10") -> str:
    """Рейтинг: '7.5/10'."""
    if isinstance(value, float):
        return f"**{value:.1f}**{suffix}"
    return f"**{num(value)}**{suffix}"


def kda(kills: float, deaths: float, assists: float = 0) -> str:
    """K/D/A строка: '12/8/5'."""
    if assists:
        return f"{num(kills)}/{num(deaths)}/{num(assists)}"
    return f"{num(kills)}/{num(deaths)}"


def mention(user_id: int | str) -> str:
    """Discord-упоминание пользователя."""
    return f"<@{user_id}>"


def role_mention(role_id: int | str) -> str:
    return f"<@&{role_id}>"


def channel_mention(channel_id: int | str) -> str:
    return f"<#{channel_id}>"


def dt(timestamp: float | int | None = None, style: str = "R") -> str:
    """Discord timestamp. dt() -> 'через 5 минут' (relative)."""
    ts = int(timestamp if timestamp is not None else time.time())
    return f"<t:{ts}:{style}>"


def bold(text: object) -> str:
    return f"**{text}**"


def muted(text: object) -> str:
    """Приглушённый текст — мелким шрифтом/secondary-стилем markdown."""
    return f"-# {text}"


def code(text: object) -> str:
    return f"`{text}`"


def truncate(text: str, limit: int = 100, suffix: str = "…") -> str:
    """Обрезать строку до limit символов."""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - len(suffix)] + suffix


def inline_field(label: str, value: object) -> str:
    """Строка вида 'Label: **value**' для TextDisplay."""
    return f"{label}: **{value}**"


def progress_bar(current: int, total: int, length: int = 10) -> str:
    """Текстовый прогресс-бар '████░░░░░░ 4/10'."""
    if total <= 0:
        filled = 0
    else:
        filled = int((current / total) * length)
    return f"{'█' * filled}{'░' * (length - filled)} {current}/{total}"
