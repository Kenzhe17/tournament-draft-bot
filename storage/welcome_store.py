"""Нумерация участников сервера для welcome-сообщений (PostgreSQL).

welcome_counters — атомарный счётчик на гильдию.
welcome_members  — номер, выданный конкретному пользователю
                   (rejoin сохраняет прежний номер).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from storage.db import get_pool

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS welcome_counters (
    guild_id    BIGINT PRIMARY KEY,
    last_number INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS welcome_members (
    guild_id  BIGINT NOT NULL,
    user_id   BIGINT NOT NULL,
    number    INTEGER NOT NULL,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);
"""


class WelcomeStore:
    _ready = False

    async def _ensure(self) -> None:
        if self._ready:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
        self._ready = True

    async def member_number(self, guild_id: int, user_id: int) -> int:
        """Порядковый номер участника; выдаёт новый при первом входе."""
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT number FROM welcome_members "
                "WHERE guild_id=$1 AND user_id=$2",
                guild_id, user_id,
            )
            if row:
                return int(row["number"])
            # Атомарный инкремент счётчика гильдии
            row = await conn.fetchrow(
                """
                INSERT INTO welcome_counters (guild_id, last_number)
                VALUES ($1, 1)
                ON CONFLICT (guild_id) DO UPDATE
                    SET last_number = welcome_counters.last_number + 1
                RETURNING last_number
                """,
                guild_id,
            )
            number = int(row["last_number"])
            try:
                await conn.execute(
                    """
                    INSERT INTO welcome_members (guild_id, user_id, number)
                    VALUES ($1,$2,$3)
                    ON CONFLICT (guild_id, user_id) DO NOTHING
                    """,
                    guild_id, user_id, number,
                )
            except Exception:
                logger.exception("welcome_members insert failed")
            return number


welcome_store = WelcomeStore()
