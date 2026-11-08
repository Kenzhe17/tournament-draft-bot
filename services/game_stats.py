"""Статистика мини-игр: сколько поставлено, сколько выплачено, сколько
изъяла из экономики система (house edge).

Хранится в bot_settings (PostgreSQL) с JSON-fallback, ключ
`game_stats` на guild_id: JSON {game: {"wagered": int, "paid": int}}.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from storage.db import DATABASE_URL, get_pool

logger = logging.getLogger(__name__)

FALLBACK_PATH = Path(__file__).resolve().parent.parent / "data" / "game_stats.json"
_mem: dict[int, dict] = {}
_loaded = False


def _fallback() -> dict:
    try:
        if FALLBACK_PATH.exists():
            return json.loads(FALLBACK_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_fallback() -> None:
    try:
        FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        FALLBACK_PATH.write_text(json.dumps(_mem), encoding="utf-8")
    except Exception:
        logger.warning("game_stats fallback save failed", exc_info=True)


async def _load(guild_id: int) -> dict:
    global _loaded
    if _loaded:
        return _mem.get(guild_id, {})
    if not DATABASE_URL:
        _mem.update({int(k): v for k, v in _fallback().items()})
        _loaded = True
        return _mem.get(guild_id, {})
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT guild_id, value FROM bot_settings WHERE key='game_stats'"
            )
            for r in rows:
                try:
                    _mem[int(r["guild_id"])] = json.loads(r["value"])
                except Exception:
                    pass
    except Exception:
        logger.warning("game_stats load failed", exc_info=True)
        _mem.update({int(k): v for k, v in _fallback().items()})
    _loaded = True
    return _mem.get(guild_id, {})


async def record_game(
    guild_id: int, game: str, wagered: int, paid: int
) -> None:
    """Записать исход одной партии: поставлено и выплачено (без возврата
    ставки — возвраты при отмене/таймауте не считаются)."""
    stats = await _load(guild_id)
    entry = stats.setdefault(game, {"wagered": 0, "paid": 0})
    entry["wagered"] += int(wagered)
    entry["paid"] += int(paid)
    _mem[guild_id] = stats
    value = json.dumps(stats)
    if DATABASE_URL:
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO bot_settings (guild_id, key, value)
                    VALUES ($1,'game_stats',$2)
                    ON CONFLICT (guild_id, key) DO UPDATE SET value=$2
                    """,
                    guild_id, value,
                )
            return
        except Exception:
            logger.warning("game_stats save failed", exc_info=True)
    _save_fallback()


async def get_stats(guild_id: int) -> dict:
    """Все записи guild'а: {game: {wagered, paid}}."""
    return await _load(guild_id)
