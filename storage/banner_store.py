"""Хранилище ежедневного баннера сервера (PostgreSQL).

Таблицы:
- banner_voice_daily — зачтённые секунды в голосовых каналах по дням (Asia/Almaty)
- banner_day_stats  — агрегаты дня: voice/matches/score по каждому участнику
- banner_updates    — журнал публикаций Server Banner (идемпотентность)
- banner_settings   — флаг автообновления на гильдию
"""

from __future__ import annotations

import logging
from datetime import date

from storage.db import get_pool

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS banner_voice_daily (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    day      DATE   NOT NULL,
    seconds  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, day)
);
CREATE TABLE IF NOT EXISTS banner_day_stats (
    guild_id      BIGINT NOT NULL,
    day           DATE   NOT NULL,
    user_id       BIGINT NOT NULL,
    voice_seconds INTEGER NOT NULL DEFAULT 0,
    matches       INTEGER NOT NULL DEFAULT 0,
    score         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, day, user_id)
);
CREATE TABLE IF NOT EXISTS banner_updates (
    guild_id     BIGINT NOT NULL,
    day          DATE   NOT NULL,
    winner_id    BIGINT,
    status       TEXT NOT NULL,
    detail       TEXT,
    published_at TIMESTAMPTZ,
    PRIMARY KEY (guild_id, day)
);
CREATE TABLE IF NOT EXISTS banner_settings (
    guild_id     BIGINT PRIMARY KEY,
    auto_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class BannerStore:
    _tables_ready = False

    async def _ensure(self) -> None:
        if self._tables_ready:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
        self._tables_ready = True

    # ---------------- voice ----------------

    async def add_voice_seconds(
        self, guild_id: int, user_id: int, day: date, seconds: int
    ) -> None:
        if seconds <= 0:
            return
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO banner_voice_daily (guild_id, user_id, day, seconds)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (guild_id, user_id, day)
                DO UPDATE SET seconds = banner_voice_daily.seconds + EXCLUDED.seconds
                """,
                guild_id, user_id, day, seconds,
            )

    async def add_voice_seconds_batch(
        self, guild_id: int, day: date, rows: list[tuple[int, int]]
    ) -> None:
        """Пакетное начисление: rows = [(user_id, seconds), ...].

        Один executemany вместо N отдельных UPSERT'ов — сканер
        голосовых каналов пишет всех участников гильдии за проход
        одним round-trip'ом.
        """
        rows = [(uid, s) for uid, s in rows if s > 0]
        if not rows:
            return
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO banner_voice_daily (guild_id, user_id, day, seconds)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (guild_id, user_id, day)
                DO UPDATE SET seconds = banner_voice_daily.seconds + EXCLUDED.seconds
                """,
                [(guild_id, uid, day, s) for uid, s in rows],
            )

    async def get_voice_seconds(
        self, guild_id: int, user_id: int, day: date
    ) -> int:
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT seconds FROM banner_voice_daily "
                "WHERE guild_id=$1 AND user_id=$2 AND day=$3",
                guild_id, user_id, day,
            )
        return int(row["seconds"]) if row else 0

    async def get_voice_day_map(
        self, guild_id: int, day: date
    ) -> dict[int, int]:
        """user_id -> capped voice seconds for the day."""
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, seconds FROM banner_voice_daily "
                "WHERE guild_id=$1 AND day=$2",
                guild_id, day,
            )
        return {int(r["user_id"]): min(int(r["seconds"]), 21600) for r in rows}

    # ---------------- tournament matches ----------------

    async def get_match_counts(
        self, guild_id: int, day_start, day_end
    ) -> dict[int, int]:
        """user_id -> количество подтверждённых матчей за период.

        Источник — match_history (пишется только при подтверждении
        результата администратором). DISTINCT match_id защищает от
        повторного подтверждения.
        """
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, COUNT(DISTINCT match_id) AS cnt
                FROM match_history
                WHERE guild_id=$1 AND date >= $2 AND date < $3
                  AND match_id <> ''
                GROUP BY user_id
                """,
                guild_id, day_start, day_end,
            )
        return {int(r["user_id"]): int(r["cnt"]) for r in rows}

    # ---------------- day stats ----------------

    async def save_day_stats(
        self, guild_id: int, day: date, rows: list[tuple[int, int, int, int]]
    ) -> None:
        """rows: (user_id, voice_seconds, matches, score)."""
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO banner_day_stats
                    (guild_id, day, user_id, voice_seconds, matches, score)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (guild_id, day, user_id) DO UPDATE SET
                    voice_seconds=EXCLUDED.voice_seconds,
                    matches=EXCLUDED.matches,
                    score=EXCLUDED.score
                """,
                [(guild_id, day, uid, vs, m, s) for uid, vs, m, s in rows],
            )

    async def get_day_stats(
        self, guild_id: int, day: date, limit: int = 5
    ) -> list[dict]:
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, voice_seconds, matches, score
                FROM banner_day_stats
                WHERE guild_id=$1 AND day=$2
                ORDER BY score DESC, matches DESC, voice_seconds DESC, user_id
                LIMIT $3
                """,
                guild_id, day, limit,
            )
        return [dict(r) for r in rows]

    # ---------------- updates ----------------

    async def get_update(self, guild_id: int, day: date) -> dict | None:
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT winner_id, status, detail, published_at "
                "FROM banner_updates WHERE guild_id=$1 AND day=$2",
                guild_id, day,
            )
        return dict(row) if row else None

    async def record_update(
        self,
        guild_id: int,
        day: date,
        status: str,
        winner_id: int | None = None,
        detail: str | None = None,
        published: bool = False,
    ) -> None:
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO banner_updates
                    (guild_id, day, winner_id, status, detail, published_at)
                VALUES ($1,$2,$3,$4,$5, CASE WHEN $6 THEN now() ELSE NULL END)
                ON CONFLICT (guild_id, day) DO UPDATE SET
                    winner_id=EXCLUDED.winner_id,
                    status=EXCLUDED.status,
                    detail=EXCLUDED.detail,
                    published_at=COALESCE(banner_updates.published_at,
                                          EXCLUDED.published_at)
                """,
                guild_id, day, winner_id, status, detail, published,
            )

    # ---------------- settings ----------------

    async def is_auto_enabled(self, guild_id: int) -> bool:
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT auto_enabled FROM banner_settings WHERE guild_id=$1",
                guild_id,
            )
        return True if row is None else bool(row["auto_enabled"])

    async def set_auto_enabled(self, guild_id: int, enabled: bool) -> None:
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO banner_settings (guild_id, auto_enabled, updated_at)
                VALUES ($1,$2,now())
                ON CONFLICT (guild_id) DO UPDATE SET
                    auto_enabled=EXCLUDED.auto_enabled, updated_at=now()
                """,
                guild_id, enabled,
            )


banner_store = BannerStore()
