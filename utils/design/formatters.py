"""Единые форматтеры чисел, валюты, процентов и рейтингов.

Правила: разделители тысяч, единый формат процентов, SR с разделителями.
"""

from __future__ import annotations

from utils.design.emojis import emoji


def fmt_int(value) -> str:
    """Целое число с разделителями тысяч: 3,190."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def fmt_float(value, digits: int = 1) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def fmt_money(value) -> str:
    """Сумма с валютным эмодзи: 1,250 🪙"""
    return f"{fmt_int(value)} {emoji('money')}"


def fmt_percent(value, digits: int = 1) -> str:
    """Процент единым форматом: 10% / 12.5%."""
    try:
        v = float(value)
        if v == int(v):
            return f"{int(v)}%"
        return f"{v:.{digits}f}%"
    except (TypeError, ValueError):
        return f"{value}%"


def fmt_rating(value) -> str:
    """Рейтинг с разделителями: 1,250 SR."""
    return f"{fmt_int(round(float(value)))} SR"


def fmt_skill(value) -> str:
    """Skill Rating: 7.5 SR."""
    return f"{fmt_float(value, 1)} SR"


def fmt_games(count) -> str:
    """Склонение 'игр/игра/игру' для русского языка."""
    try:
        n = int(count)
    except (TypeError, ValueError):
        return f"{count} игр"
    if 11 <= n % 100 <= 14:
        word = "игр"
    else:
        word = {1: "игра", 2: "игры", 3: "игры", 4: "игры"}.get(n % 10, "игр")
    return f"{fmt_int(n)} {word}"


def fmt_elo_change(value) -> str:
    """Изменение ELO со знаком: +120 / -85."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"+{v}" if v > 0 else str(v)


def fmt_xp(value) -> str:
    return f"{fmt_int(value)} XP"
