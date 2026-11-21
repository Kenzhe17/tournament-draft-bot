"""Долговечное хранилище ожидающих наград Coin Drop (PostgreSQL).

Таблица `drop_coin_rewards` — источник истины для наград
спонтанных ивентов (speed drop / raffle). Жизненный цикл:

    pending → processing → completed
                  ↘ pending (retry с backoff, next_attempt_at)

Гарантии:
- `grant_key` — PK: одна награда на событие, повторная запись
  идемпотентна (ON CONFLICT DO NOTHING).
- Победитель фиксируется атомарно с созданием обязательства:
  record() — единственная точка входа, до любого начисления.
- claim_due/claim_key используют FOR UPDATE SKIP LOCKED +
  lease (locked_until): параллельные worker не берут одну
  награду; зависший worker возвращает её по истечении lease.
- Начисление — через user_balance_store.add_balance_once с тем
  же grant_key: даже при потерянном ответе после commit
  повторная обработка не даст двойного зачисления.
- JSON-фолбэка НЕТ: награды — деньги, при недоступности PG
  store бросает исключение, вызывающий код обязан залогировать
  и поставить в очередь повторной фиксации.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS drop_coin_rewards (
    grant_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    locked_until TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
)
"""
INDEX_DDL = """
CREATE INDEX IF NOT EXISTS drop_coin_rewards_due_idx
ON drop_coin_rewards (next_attempt_at)
WHERE status = 'pending'
"""

LEASE_SECONDS = 120


class DropRewardStore:
    def __init__(self) -> None:
        self._ddl_done = False

    async def _pool(self):
        from storage.db import get_pool
        return await get_pool()

    async def _ensure_ddl(self, conn) -> None:
        if not self._ddl_done:
            await conn.execute(DDL)
            await conn.execute(INDEX_DDL)
            self._ddl_done = True

    async def record(
        self,
        grant_key: str,
        kind: str,
        guild_id: int,
        user_id: int,
        amount: int,
    ) -> dict[str, Any]:
        """Зафиксировать обязательство по награде. Идемпотентно
        по grant_key: повторный вызов возвращает существующую
        строку и ничего не меняет."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            row = await conn.fetchrow(
                """
                INSERT INTO drop_coin_rewards
                    (grant_key, kind, guild_id, user_id, amount,
                     status, next_attempt_at)
                VALUES ($1, $2, $3, $4, $5, 'pending', now())
                ON CONFLICT (grant_key) DO NOTHING
                RETURNING *
                """,
                grant_key, kind, guild_id, user_id, amount,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT * FROM drop_coin_rewards WHERE grant_key = $1",
                    grant_key,
                )
            return dict(row) if row else {}

    async def claim_key(self, grant_key: str,
                        lease_seconds: int = LEASE_SECONDS
                        ) -> dict[str, Any] | None:
        """Атомарно перевести конкретную pending-награду в
        processing под lease. None — уже занята/завершена."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            row = await conn.fetchrow(
                """
                UPDATE drop_coin_rewards
                SET status = 'processing',
                    attempts = attempts + 1,
                    locked_until = now() + ($2 || ' seconds')::interval
                WHERE grant_key = $1
                  AND (status = 'pending'
                       OR (status = 'processing'
                           AND locked_until < now()))
                RETURNING *
                """,
                grant_key, str(lease_seconds),
            )
            return dict(row) if row else None

    async def claim_due(self, limit: int = 10,
                        lease_seconds: int = LEASE_SECONDS
                        ) -> list[dict[str, Any]]:
        """Захватить пакет дозревших pending-наград под lease.
        SKIP LOCKED — параллельные worker получают разные строки;
        processing с истёкшим lease возвращается в обработку
        (краш worker между начислением и complete)."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            rows = await conn.fetch(
                """
                UPDATE drop_coin_rewards r
                SET status = 'processing',
                    attempts = r.attempts + 1,
                    locked_until = now() + ($2 || ' seconds')::interval
                WHERE r.grant_key IN (
                    SELECT grant_key FROM drop_coin_rewards
                    WHERE (status = 'pending'
                           AND (next_attempt_at IS NULL
                                OR next_attempt_at <= now()))
                       OR (status = 'processing'
                           AND locked_until < now())
                    ORDER BY created_at
                    LIMIT $1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING r.*
                """,
                limit, str(lease_seconds),
            )
            return [dict(r) for r in rows]

    async def complete(self, grant_key: str,
                       attempts: int | None = None) -> bool:
        """Отметить награду выданной. Только из статуса processing
        и только владельцем текущего lease (fencing по attempts):
        устаревший worker после перехвата строки не сможет
        «завершить» чужую обработку. completed→pending невозможен
        по построению (retry требует processing)."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            if attempts is None:
                res = await conn.execute(
                    """
                    UPDATE drop_coin_rewards
                    SET status = 'completed', completed_at = now(),
                        locked_until = NULL, last_error = NULL
                    WHERE grant_key = $1 AND status = 'processing'
                    """,
                    grant_key,
                )
            else:
                res = await conn.execute(
                    """
                    UPDATE drop_coin_rewards
                    SET status = 'completed', completed_at = now(),
                        locked_until = NULL, last_error = NULL
                    WHERE grant_key = $1 AND status = 'processing'
                      AND attempts = $2
                    """,
                    grant_key, attempts,
                )
            # asyncpg execute → "UPDATE n"
            return res == "UPDATE 1"

    async def retry(self, grant_key: str, attempts: int,
                    error: str, delay_seconds: float) -> bool:
        """Вернуть награду в pending с backoff — только владельцу
        текущего lease (status='processing' AND attempts=$2).
        Переход completed→pending невозможен: completed-строки
        не матчат WHERE. Технический код ошибки без секретов,
        обрезан до 200 символов."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                """
                UPDATE drop_coin_rewards
                SET status = 'pending',
                    locked_until = NULL,
                    next_attempt_at = now() + ($3 || ' seconds')::interval,
                    last_error = $4
                WHERE grant_key = $1 AND status = 'processing'
                  AND attempts = $2
                """,
                grant_key, attempts, str(max(0.0, delay_seconds)),
                (error or "")[:200],
            )
            return res == "UPDATE 1"

    async def pending_count(self) -> int:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*) AS c FROM drop_coin_rewards "
                "WHERE status != 'completed'"
            )
            return int(row["c"]) if row else 0


drop_reward_store = DropRewardStore()
