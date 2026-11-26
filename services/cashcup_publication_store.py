"""PostgreSQL-хранилище публичных публикаций CASH CUP.

Таблица cashcup_publications связывает (tournament_id, publication_key)
с Discord-сообщением. Гарантии:
- уникальный индекс (tournament_id, publication_key) — один эмбед
  на матч и одна сетка на турнир даже при конкурентных sync;
- message_id/hash/status/retry позволяют восстановить сообщение
  после рестарта и не дублировать публикации;
- хранится ТОЛЬКО ссылка на сообщение — результаты турнира живут
  в cashcup_tournaments и не затрагиваются.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from storage.db import get_pool


class CashCupPublicationError(RuntimeError):
    pass


class CashCupPublicationStore:
    def __init__(self) -> None:
        self._ready = False
        self._schema_lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        if self._ready:
            return
        async with self._schema_lock:
            if self._ready:
                return
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS cashcup_publications (
                        id BIGSERIAL PRIMARY KEY,
                        tournament_id TEXT NOT NULL,
                        guild_id BIGINT NOT NULL,
                        publication_key TEXT NOT NULL,
                        channel_id BIGINT NOT NULL DEFAULT 0,
                        message_id BIGINT NOT NULL DEFAULT 0,
                        last_render_hash TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'pending',
                        pinged BOOLEAN NOT NULL DEFAULT FALSE,
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (tournament_id, publication_key)
                    )
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS cashcup_pub_msg_idx
                    ON cashcup_publications(guild_id, message_id)
                    WHERE message_id <> 0
                """)
            self._ready = True

    @staticmethod
    def _decode(row) -> dict[str, Any] | None:
        if row is None:
            return None
        return dict(row)

    async def get(self, tournament_id: str,
                  publication_key: str) -> dict[str, Any] | None:
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM cashcup_publications
                WHERE tournament_id=$1 AND publication_key=$2
            """, tournament_id, publication_key)
        return self._decode(row)

    async def get_by_message(self, guild_id: int,
                             message_id: int) -> dict[str, Any] | None:
        """Публикация по Discord message_id — для кнопки «Открыть матч»."""
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM cashcup_publications
                WHERE guild_id=$1 AND message_id=$2 LIMIT 1
            """, guild_id, message_id)
        return self._decode(row)

    async def list_for_tournament(self, tournament_id: str
                                  ) -> list[dict[str, Any]]:
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM cashcup_publications
                WHERE tournament_id=$1 ORDER BY publication_key
            """, tournament_id)
        return [dict(r) for r in rows]

    async def ensure_row(self, tournament_id: str, guild_id: int,
                         publication_key: str) -> dict[str, Any]:
        """Получить или создать строку публикации. Идемпотентно."""
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO cashcup_publications
                    (tournament_id, guild_id, publication_key)
                VALUES ($1,$2,$3)
                ON CONFLICT (tournament_id, publication_key) DO NOTHING
                RETURNING *
            """, tournament_id, guild_id, publication_key)
            if row is None:
                row = await conn.fetchrow("""
                    SELECT * FROM cashcup_publications
                    WHERE tournament_id=$1 AND publication_key=$2
                """, tournament_id, publication_key)
        if row is None:
            raise CashCupPublicationError("Не удалось создать запись публикации")
        return dict(row)

    async def update(self, publication_id: int, *,
                     channel_id: int | None = None,
                     message_id: int | None = None,
                     last_render_hash: str | None = None,
                     status: str | None = None,
                     pinged: bool | None = None,
                     retry_count: int | None = None,
                     next_attempt_at: datetime | None = None,
                     bump_retry: bool = False) -> None:
        """Адресное обновление одной публикации.

        bump_retry увеличивает retry_count и отодвигает next_attempt_at
        по экспоненциальному backoff — вызывается при HTTP-ошибках.
        """
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            if bump_retry:
                await conn.execute("""
                    UPDATE cashcup_publications
                    SET retry_count = retry_count + 1,
                        status = 'error',
                        next_attempt_at = NOW() +
                            (LEAST(30 * POWER(2, retry_count), 600)
                             * INTERVAL '1 second'),
                        updated_at = NOW()
                    WHERE id=$1
                """, publication_id)
                return
            await conn.execute("""
                UPDATE cashcup_publications
                SET channel_id = COALESCE($2, channel_id),
                    message_id = COALESCE($3, message_id),
                    last_render_hash = COALESCE($4, last_render_hash),
                    status = COALESCE($5, status),
                    pinged = COALESCE($6, pinged),
                    retry_count = COALESCE($7, retry_count),
                    next_attempt_at = $8,
                    updated_at = NOW()
                WHERE id=$1
            """, publication_id, channel_id, message_id,
                last_render_hash, status, pinged, retry_count,
                next_attempt_at)


def utcnow() -> datetime:
    return datetime.now(UTC)


cashcup_publication_store = CashCupPublicationStore()
