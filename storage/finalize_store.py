"""Restart-safe идентификатор финализации матча в PostgreSQL.

match_finalizations.match_id — PK; status:
  processing  — заявка взята, статистика ещё не зафиксирована
  stats_done  — player_stats транзакция закоммичена (пишется ВНУТРИ
                той же транзакции, что и статистика — set_many)
  done        — история/подтверждение/payouts завершены

JSON-флаг processed_elo_matches остаётся быстрым кэшем; при расхождении
(DB authoritative) DB-статус решает, см. finalize_ocr_match.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS match_finalizations (
    match_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def get_status(guild_id: int, match_id: str) -> str | None:
    """Текущий статус финализации; None — записи нет или нет DB."""
    try:
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status FROM match_finalizations WHERE match_id = $1",
                match_id,
            )
            return row["status"] if row else None
    except Exception as e:
        logger.debug("finalize status read failed: %s", e)
        return None


async def mark_done(guild_id: int, match_id: str) -> None:
    """Пометить финализацию завершённой (после истории/подтверждения)."""
    try:
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
            await conn.execute(
                """
                INSERT INTO match_finalizations (match_id, guild_id, status)
                VALUES ($1, $2, 'done')
                ON CONFLICT (match_id)
                DO UPDATE SET status = 'done', updated_at = now()
                """,
                match_id, guild_id,
            )
    except Exception as e:
        logger.warning("finalize mark_done failed: %s", e)


async def mark_stats_done_in_tx(conn, guild_id: int, match_id: str) -> None:
    """ВЫЗЫВАТЬ ВНУТРИ транзакции set_many — атомарно со статистикой."""
    await conn.execute(_DDL)
    await conn.execute(
        """
        INSERT INTO match_finalizations (match_id, guild_id, status)
        VALUES ($1, $2, 'stats_done')
        ON CONFLICT (match_id)
        DO UPDATE SET status = 'stats_done', updated_at = now()
        """,
        match_id, guild_id,
    )
