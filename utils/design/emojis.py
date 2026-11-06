"""Централизованная библиотека кастомных эмодзи r!z3.

Семантические ключи -> Discord custom emoji (<:name:id>) через env-переменные
EMOJI_* (см. config.py и .env.example), с unicode-фолбэком до загрузки ID.

Кнопки/селекты должны получать эмодзи через partial_emoji(),
текст embed — через emoji().
"""

from __future__ import annotations

import discord

from config import GAME_EMOJIS, ICON_EMOJIS, RANK_EMOJIS, STANDARD_EMOJIS

# Название custom emoji в Discord (часть <:name:id>).
# При загрузке пака на сервер имя можно переименовать — ID важнее.
EMOJI_NAMES = {
    "success": "success",
    "error": "error",
    "warning": "warning",
    "info": "info",
    "delete": "delete",
    "block": "block",
    "back": "back",
    "forward": "forward",
    "refresh": "refresh",
    "dropdown": "dropdown",
    "new": "new",
    "comment": "comment",
    "team": "team",
    "profile": "profile",
    "member": "member",
    "pencil": "pencil",
    "pin": "pin",
    "announce": "announce",
    "megaphone": "megaphone",
    "check": "check",
    "cross": "cross",
    "question": "question",
    "lock": "lock",
    "unlock": "unlock",
    "ban": "ban",
    "timer": "timer",
    "link": "link",
    "search": "search",
    "crown": "crown",
    "rating": "rating",
    "inventory": "inventory",
    "stats": "stats",
    "controller": "controller",
    "bell": "bell",
    "calendar": "calendar",
    "bolt": "bolt",
    "home": "home",
    "money": "money",
    "coin": "coin",
    "shop": "shop",
    "gift": "gift",
    "tag": "tag",
    "eye": "eye",
    "room": "room",
    "winner": "winner",
    "target": "target",
    "sword": "sword",
    "game": "game",
    "dice": "dice",
    "slots": "slots",
    "roulette": "roulette",
    "rps": "rps",
    "brain": "brain",
    "grow": "grow",
    "kill_death": "kill_death",
    "star": "star",
    "notes": "notes",
    "clock": "clock",
    "book": "book",
    "light_bulb": "light_bulb",
    "flash": "flash",
    "fire": "fire",
    "ice": "ice",
    "settings": "settings",
    "checkmark": "check",
    "cancel": "cross",
    "white_dot": "white_dot",
    "white_arrow": "white_arrow",
    "a_sparkle": "a_sparkle",
    "a_star": "a_star",
}

_ALL_ID_MAPS = (GAME_EMOJIS, ICON_EMOJIS, RANK_EMOJIS)


def _custom_id(key: str) -> str:
    """Найти загруженный ID кастомного эмодзи по семантическому ключу."""
    for mapping in _ALL_ID_MAPS:
        value = mapping.get(key)
        if value:
            return str(value)
    return ""


def _fallback(key: str) -> str:
    return STANDARD_EMOJIS.get(key, "")


def emoji(key: str) -> str:
    """Эмодзи для embed-текста: <:name:id> или unicode-фолбэк."""
    custom_id = _custom_id(key)
    if custom_id:
        name = EMOJI_NAMES.get(key, key)
        return f"<:{name}:{custom_id}>"
    return _fallback(key)


def partial_emoji(key: str):
    """Эмодзи для компонентов Discord UI (Button/Select).

    Возвращает discord.PartialEmoji для кастомного эмодзи,
    unicode-строку для фолбэка или None если эмодзи не определён.
    """
    custom_id = _custom_id(key)
    if custom_id:
        try:
            return discord.PartialEmoji(
                name=EMOJI_NAMES.get(key, key), id=int(custom_id)
            )
        except (ValueError, TypeError):
            return None
    fallback = _fallback(key)
    return fallback if fallback else None


def has_custom(key: str) -> bool:
    return bool(_custom_id(key))


class _EmojiProxy:
    """E.success / E.money / ... — удобный доступ к emoji()."""

    def __getattr__(self, key: str) -> str:
        return emoji(key)

    def __getitem__(self, key: str) -> str:
        return emoji(key)


E = _EmojiProxy()
