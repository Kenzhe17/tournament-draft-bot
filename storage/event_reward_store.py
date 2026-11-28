"""Долговечное хранилище наград спонтанных событий 2.0 (PostgreSQL).

Таблица ``event_rewards`` — источник истины для ВСЕХ четырёх типов
наград новых событий: ``coins``, ``xp``, ``rep``, ``item``.
Жизненный цикл идентичен ``drop_coin_rewards``:

    pending → processing → completed
                  ↘ pending (retry с backoff, next_attempt_at)

Отличия от legacy-таблицы:
- ``kind`` хранит тип награды, а не режим ивента;
- ``item_id`` — фиксированный ID предмета магазина для kind='item'
  (выбирается ДО публикации карточки, раскрывается в результате);
- ``amount`` для item хранит 0.

Гарантии — те же, что у drop_reward_store: grant_key PK,
record() — единственная точка фиксации победителя, lease +
fencing по attempts, начисление идемпотентно по grant_key на
уровне целевого хранилища (balance/xp_grants/rep event_key/
инвентарь). JSON-фолбэка нет: при недоступности PG вызывающий
код пишет обязательство в event_pending_store (тот же
атомарный JSON-мост, что и для legacy drop_pending_store).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS event_rewards (
    grant_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0,
    item_id TEXT,
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
CREATE INDEX IF NOT EXISTS event_rewards_due_idx
ON event_rewards (next_attempt_at)
WHERE status = 'pending'
"""

LEASE_SECONDS = 120


class EventRewardStore:
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
        amount: int = 0,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        """Зафиксировать обязательство. Идемпотентно по grant_key."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            row = await conn.fetchrow(
                """
                INSERT INTO event_rewards
                    (grant_key, kind, guild_id, user_id, amount,
                     item_id, status, next_attempt_at)
                VALUES ($1, $2, $3, $4, $5, $6, 'pending', now())
                ON CONFLICT (grant_key) DO NOTHING
                RETURNING *
                """,
                grant_key, kind, guild_id, user_id, amount, item_id,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT * FROM event_rewards WHERE grant_key = $1",
                    grant_key,
                )
            return dict(row) if row else {}

    async def claim_key(self, grant_key: str,
                        lease_seconds: int = LEASE_SECONDS
                        ) -> dict[str, Any] | None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            row = await conn.fetchrow(
                """
                UPDATE event_rewards
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
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            rows = await conn.fetch(
                """
                UPDATE event_rewards r
                SET status = 'processing',
                    attempts = r.attempts + 1,
                    locked_until = now() + ($2 || ' seconds')::interval
                WHERE r.grant_key IN (
                    SELECT grant_key FROM event_rewards
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
        pool = await self._pool()
        async with pool.acquire() as conn:
            if attempts is None:
                res = await conn.execute(
                    """
                    UPDATE event_rewards
                    SET status = 'completed', completed_at = now(),
                        locked_until = NULL, last_error = NULL
                    WHERE grant_key = $1 AND status = 'processing'
                    """,
                    grant_key,
                )
            else:
                res = await conn.execute(
                    """
                    UPDATE event_rewards
                    SET status = 'completed', completed_at = now(),
                        locked_until = NULL, last_error = NULL
                    WHERE grant_key = $1 AND status = 'processing'
                      AND attempts = $2
                    """,
                    grant_key, attempts,
                )
            return res == "UPDATE 1"

    async def retry(self, grant_key: str, attempts: int,
                    error: str, delay_seconds: float) -> bool:
        pool = await self._pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                """
                UPDATE event_rewards
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
                "SELECT count(*) AS c FROM event_rewards "
                "WHERE status != 'completed'"
            )
            return int(row["c"]) if row else 0


event_reward_store = EventRewardStore()
