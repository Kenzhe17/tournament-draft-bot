"""Хранилище браков r!z3 (PostgreSQL).

marriages — реестр заключённых браков: упорядоченная пара
(user_a < user_b), дата заключения. Используется для уведомления
(дедупликация) и аудита. Не трогает player_stats.spouse_id —
бизнес-логика брака остаётся в player_stats_store.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from storage.db import get_pool

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS marriages (
    guild_id  BIGINT NOT NULL,
    user_a    BIGINT NOT NULL,
    user_b    BIGINT NOT NULL,
    married_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_a, user_b)
);
"""


class MarriageStore:
    _ready = False

    async def _ensure(self) -> None:
        if self._ready:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
        self._ready = True

    @staticmethod
    def _pair(a: int, b: int) -> tuple[int, int]:
        return (a, b) if a < b else (b, a)

    async def record_marriage(
        self, guild_id: int, user_a: int, user_b: int
    ) -> tuple[bool, datetime | None]:
        """Записать брак. Возвращает (is_new, married_at).

        is_new=False — брак уже был записан (дедупликация уведомления).
        """
        await self._ensure()
        a, b = self._pair(user_a, user_b)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO marriages (guild_id, user_a, user_b)
                VALUES ($1,$2,$3)
                ON CONFLICT (guild_id, user_a, user_b) DO NOTHING
                RETURNING married_at
                """,
                guild_id, a, b,
            )
            if row:
                return True, row["married_at"]
            row = await conn.fetchrow(
                "SELECT married_at FROM marriages "
                "WHERE guild_id=$1 AND user_a=$2 AND user_b=$3",
                guild_id, a, b,
            )
            return False, row["married_at"] if row else None

    async def get_marriage(
        self, guild_id: int, user_a: int, user_b: int
    ) -> datetime | None:
        await self._ensure()
        a, b = self._pair(user_a, user_b)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT married_at FROM marriages "
                "WHERE guild_id=$1 AND user_a=$2 AND user_b=$3",
                guild_id, a, b,
            )
        return row["married_at"] if row else None


marriage_store = MarriageStore()
