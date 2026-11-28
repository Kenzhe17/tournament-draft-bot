"""Restart-safe идентификатор финализации матча в PostgreSQL.

match_finalizations.match_id — PK; status:
  processing  — заявка взята, статистика ещё не зафиксирована
  stats_done  — player_stats транзакция закоммичена (пишется ВНУТРИ
                той же транзакции, что и статистика — set_many)
  done        — история/подтверждение/payouts завершены

JSON-флаг processed_elo_matches остаётся быстрым кэшем; при расхождении
(DB authoritative) DB-статус решает.

ВАЖНО: ошибки БД здесь НЕ маскируются. «Записи нет» (None) и «БД
недоступна» (исключение) — разные исходы: финализация не имеет права
принять сбой чтения за отсутствие маркера и начислить статистику
повторно.
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
    """Текущий статус финализации.

    None — записи нет. Ошибки БД пробрасываются вызывающему коду:
    трактовать недоступность БД как «нет записи» привело бы к
    повторному начислению ELO/монет.
    """
    from storage.db import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Таблица создаётся лениво и при записи; на свежей БД её ещё
        # нет — без DDL чтение падало бы UndefinedTableError и
        # блокировало первую в истории финализацию.
        await conn.execute(_DDL)
        row = await conn.fetchrow(
            "SELECT status FROM match_finalizations WHERE match_id = $1",
            match_id,
        )
        return row["status"] if row else None


async def mark_done(guild_id: int, match_id: str) -> None:
    """Пометить финализацию завершённой (после истории/подтверждения).

    Ошибки БД пробрасываются — 'done' это единственное доказательство
    полного завершения, его потеря хуже повторного безопасного прохода.
    Повторный вызов идемпотентен (то же значение 'done').
    """
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


async def mark_stats_done_in_tx(conn, guild_id: int, match_id: str) -> None:
    """ВЫЗЫВАТЬ ВНУТРИ транзакции set_many — атомарно со статистикой.

    WHERE status <> 'done' запрещает регрессию done → stats_done:
    если финализация уже полностью завершена, повторный прогон
    статистики не должен «откатывать» маркер.
    """
    await conn.execute(_DDL)
    await conn.execute(
        """
        INSERT INTO match_finalizations (match_id, guild_id, status)
        VALUES ($1, $2, 'stats_done')
        ON CONFLICT (match_id)
        DO UPDATE SET status = 'stats_done', updated_at = now()
        WHERE match_finalizations.status <> 'done'
        """,
        match_id, guild_id,
    )
