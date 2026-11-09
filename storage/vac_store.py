"""Хранилище VAC (Tournament Ban) — PostgreSQL.

vac_bans — журнал блокировок участия в турнирах. Запись считается
активной пока revoked_at IS NULL AND expired_at IS NULL AND
expires_at > now(). Истёкшие записи архивируются (expired_at),
история сохраняется для аудита.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from storage.db import get_pool

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS vac_bans (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    issued_by BIGINT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoked_by BIGINT,
    expired_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT vac_expiry_check CHECK (expires_at > issued_at)
);
CREATE INDEX IF NOT EXISTS idx_vac_user
    ON vac_bans(guild_id, user_id, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vac_one_unrevoked
    ON vac_bans(guild_id, user_id)
    WHERE revoked_at IS NULL AND expired_at IS NULL;
"""


class VacStore:
    _ready = False

    async def _ensure(self) -> None:
        if self._ready:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
        self._ready = True

    async def issue(
        self, guild_id: int, user_id: int, issued_by: int,
        expires_at: datetime,
    ) -> dict | None:
        """Выдать VAC. None — уже есть активная блокировка."""
        await self._ensure()
        await self._archive_expired(guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO vac_bans (guild_id, user_id, issued_by, expires_at)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT DO NOTHING
                RETURNING id, issued_at, expires_at
                """,
                guild_id, user_id, issued_by, expires_at,
            )
            # ON CONFLICT не ловит частичный уникальный индекс —
            # проверяем активную запись отдельно.
            if row is None:
                return None
            return dict(row)

    async def get_active(self, guild_id: int, user_id: int) -> dict | None:
        """Активный VAC пользователя (не отозван, не истёк, не архивирован)."""
        await self._ensure()
        await self._archive_expired(guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, issued_by, issued_at, expires_at
                FROM vac_bans
                WHERE guild_id=$1 AND user_id=$2
                  AND revoked_at IS NULL AND expired_at IS NULL
                  AND expires_at > now()
                ORDER BY id DESC LIMIT 1
                """,
                guild_id, user_id,
            )
            return dict(row) if row else None

    async def list_active(self, guild_id: int) -> list[dict]:
        await self._ensure()
        await self._archive_expired(guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, user_id, issued_by, issued_at, expires_at
                FROM vac_bans
                WHERE guild_id=$1
                  AND revoked_at IS NULL AND expired_at IS NULL
                  AND expires_at > now()
                ORDER BY expires_at ASC
                """,
                guild_id,
            )
            return [dict(r) for r in rows]

    async def revoke(self, guild_id: int, user_id: int, revoked_by: int) -> dict | None:
        """Снять активный VAC. Возвращает запись или None."""
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE vac_bans
                SET revoked_at = now(), revoked_by = $3
                WHERE id = (
                    SELECT id FROM vac_bans
                    WHERE guild_id=$1 AND user_id=$2
                      AND revoked_at IS NULL AND expired_at IS NULL
                      AND expires_at > now()
                    ORDER BY id DESC LIMIT 1
                )
                RETURNING id, user_id, issued_by, issued_at, expires_at
                """,
                guild_id, user_id, revoked_by,
            )
            return dict(row) if row else None

    async def _archive_expired(self, guild_id: int) -> int:
        """Пометить истёкшие записи expired_at=now() (история сохраняется)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                """
                UPDATE vac_bans SET expired_at = now()
                WHERE guild_id=$1 AND revoked_at IS NULL
                  AND expired_at IS NULL AND expires_at <= now()
                """,
                guild_id,
            )
            # "UPDATE n"
            try:
                return int(res.split()[-1])
            except Exception:
                return 0


vac_store = VacStore()
