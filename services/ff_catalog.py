"""Каталог предметов Free Fire (ff-catalog, AfnanTawsif/ff-catalog).

Данные: assets/item-data.msgpack.gz — msgpack+gzip, ~34k предметов.
Иконки: https://cdn.jsdelivr.net/gh/AfnanTawsif/ff-catalog@main/Item-icons/{itemID}.webp

Поля предмета: icon, itemID, name, description, rarity, type.
"""

from __future__ import annotations

import gzip
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "item-data.msgpack.gz"
)
ICON_BASE = (
    "https://cdn.jsdelivr.net/gh/AfnanTawsif/ff-catalog@main/Item-icons"
)

def _rarity_color(name: str):
    import discord

    return {
        "WHITE": discord.Color.light_grey(),
        "GREEN": discord.Color.green(),
        "BLUE": discord.Color.blue(),
        "PURPLE": discord.Color.purple(),
        "PURPLE_PLUS": discord.Color.from_rgb(200, 120, 255),
        "ORANGE": discord.Color.orange(),
        "ORANGE_PLUS": discord.Color.from_rgb(255, 160, 60),
        "RED": discord.Color.red(),
        "NONE": discord.Color.dark_grey(),
    }.get((name or "NONE").upper(), discord.Color.dark_grey())


@lru_cache(maxsize=1)
def load_items() -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """Возвращает (по_id, список). Кэшируется на процесс."""
    try:
        import msgpack
    except ImportError:
        logger.error("msgpack не установлен — каталог FF недоступен")
        return {}, []

    try:
        raw = gzip.decompress(_DB_PATH.read_bytes())
        data = msgpack.unpackb(raw, raw=False)
    except FileNotFoundError:
        logger.error("item-data.msgpack.gz не найден: %s", _DB_PATH)
        return {}, []
    except Exception:
        logger.exception("Не удалось загрузить каталог FF")
        return {}, []

    items = [it for it in data[1:] if isinstance(it, dict) and it.get("itemID")]
    by_id = {int(it["itemID"]): it for it in items}
    logger.info("FF catalog loaded: %d items", len(items))
    return by_id, items


def get_item(item_id: int | str) -> dict[str, Any] | None:
    by_id, _ = load_items()
    try:
        return by_id.get(int(item_id))
    except (TypeError, ValueError):
        return None


def search_items(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Поиск по имени (подстрока, case-insensitive). Точные совпадения — первыми."""
    _, items = load_items()
    q = query.strip().lower()
    if not q:
        return []
    exact, partial = [], []
    for it in items:
        name = str(it.get("name") or "").lower()
        if name == q:
            exact.append(it)
        elif q in name:
            partial.append(it)
    return (exact + partial)[:limit]


@lru_cache(maxsize=1)
def _items_by_type() -> dict[str, list[dict[str, Any]]]:
    _, items = load_items()
    by_type: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        t = str(it.get("type") or "")
        if t:
            by_type.setdefault(t, []).append(it)
    for lst in by_type.values():
        lst.sort(key=lambda x: str(x.get("name") or "").lower())
    return by_type


def items_of_types(types: set[str] | frozenset[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    """Все предметы указанных типов, отсортированные по имени."""
    by_type = _items_by_type()
    result: list[dict[str, Any]] = []
    for t in types:
        result.extend(by_type.get(t, []))
    result.sort(key=lambda x: str(x.get("name") or "").lower())
    return result


def icon_url(item_id: int | str) -> str:
    return f"{ICON_BASE}/{item_id}.webp"


def item_name(item_id: int | str) -> str | None:
    it = get_item(item_id)
    return it.get("name") if it else None


def rarity_color(rarity: str):
    return _rarity_color(rarity)
