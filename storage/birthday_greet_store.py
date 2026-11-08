"""Дедупликация автоматических поздравлений с днём рождения (PostgreSQL).

birthday_greetings — факт отправки поздравления: одна запись на
участника в конкретную календарную дату. Атомарный INSERT делает
повторные отправки после рестарта невозможными.
"""

from __future__ import annotations

import logging
from datetime import date

from storage.db import get_pool

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS birthday_greetings (
    guild_id   BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    greet_date DATE   NOT NULL,
    sent_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id, greet_date)
);
"""


class BirthdayGreetStore:
    _ready = False

    async def _ensure(self) -> None:
        if self._ready:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
        self._ready = True

    async def try_mark(
        self, guild_id: int, user_id: int, greet_date: date
    ) -> bool:
        """Зарезервировать поздравление на дату.

        True — поздравление ещё не отправлялось (и теперь помечено).
        False — уже отправлялось (дубль/рестарт).
        """
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO birthday_greetings (guild_id, user_id, greet_date)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, user_id, greet_date) DO NOTHING
                RETURNING user_id
                """,
                guild_id, user_id, greet_date,
            )
            return row is not None


birthday_greet_store = BirthdayGreetStore()
