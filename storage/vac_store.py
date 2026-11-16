"""Хранилище VAC (Tournament Ban) — PostgreSQL.

vac_bans — журнал блокировок участия в турнирах. Запись считается
активной пока revoked_at IS NULL AND expired_at IS NULL AND
expires_at > now(). Истёкшие записи архивируются (expired_at),
история сохраняется для аудита.

vac_profiles — прогрессивный уровень нарушителя (1..6). Уровень
живёт дольше самой блокировки: истечение ban не сбрасывает его.
Каждые полные 24 часа без нарушений уровень снижается на 1
(вычисляется лениво при следующем нарушении и при чтении).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from storage.db import get_pool

logger = logging.getLogger(__name__)

# Лестница автоматических наказаний: уровень 1..6 -> срок.
VAC_LADDER: tuple[timedelta, ...] = (
    timedelta(minutes=3),    # 1
    timedelta(minutes=10),   # 2
    timedelta(minutes=30),   # 3
    timedelta(hours=1),      # 4
    timedelta(hours=3),      # 5
    timedelta(hours=6),      # 6
)
MAX_VAC_LEVEL = len(VAC_LADDER)
# За каждые полные сутки без нарушений уровень снижается на 1.
VIOLATION_DECAY = timedelta(hours=24)


def effective_level(
    level: int,
    last_violation_at: datetime | None,
    now: datetime | None = None,
) -> int:
    """Уровень с учётом снижения за время без нарушений (0..MAX)."""
    if level <= 0:
        return 0
    if last_violation_at is None:
        return min(level, MAX_VAC_LEVEL)
    now = now or datetime.now(timezone.utc)
    if last_violation_at.tzinfo is None:
        last_violation_at = last_violation_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    periods = int((now - last_violation_at).total_seconds()
                  // VIOLATION_DECAY.total_seconds())
    return max(0, min(level - periods, MAX_VAC_LEVEL))


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
    level INT,
    reason TEXT,
    event_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT vac_expiry_check CHECK (expires_at > issued_at)
);
-- Миграция старых таблиц: новые колонки добавляются ДО индексов,
-- которые на них ссылаются (event_key в частичном уникальном индексе).
ALTER TABLE vac_bans ADD COLUMN IF NOT EXISTS level INT;
ALTER TABLE vac_bans ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE vac_bans ADD COLUMN IF NOT EXISTS event_key TEXT;
CREATE INDEX IF NOT EXISTS idx_vac_user
    ON vac_bans(guild_id, user_id, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vac_one_unrevoked
    ON vac_bans(guild_id, user_id)
    WHERE revoked_at IS NULL AND expired_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_vac_event_key
    ON vac_bans(guild_id, user_id, event_key)
    WHERE event_key IS NOT NULL
      AND revoked_at IS NULL AND expired_at IS NULL;

CREATE TABLE IF NOT EXISTS vac_profiles (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    level INT NOT NULL DEFAULT 0,
    violation_count INT NOT NULL DEFAULT 0,
    last_violation_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);
"""


class VacStore:
    _ready = False
    _lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _ensure(self) -> None:
        if self._ready:
            return
        async with self._get_lock():
            if self._ready:
                return
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(_DDL)
            self._ready = True

    async def ensure_schema(self) -> None:
        """Публичная идемпотентная инициализация схемы VAC.

        Нужна код-путям, которые читают vac_bans напрямую внутри
        своей транзакции (чтобы таблица существовала и в свежих БД).
        """
        await self._ensure()

    async def issue(
        self, guild_id: int, user_id: int, issued_by: int,
        expires_at: datetime, reason: str | None = None,
    ) -> dict | None:
        """Выдать VAC (ручной /vac). None — уже есть активная блокировка."""
        await self._ensure()
        await self._archive_expired(guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO vac_bans
                    (guild_id, user_id, issued_by, expires_at, reason)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT DO NOTHING
                RETURNING id, issued_at, expires_at, reason
                """,
                guild_id, user_id, issued_by, expires_at, reason,
            )
            # ON CONFLICT не ловит частичный уникальный индекс —
            # проверяем активную запись отдельно.
            if row is None:
                return None
            return dict(row)

    async def record_violation(
        self,
        guild_id: int,
        user_id: int,
        issued_by: int,
        reason: str,
        event_key: str | None = None,
    ) -> dict | None:
        """Автоматическое нарушение: уровень+1 (макс. 6), срок по лестнице.

        Атомарно в одной транзакции: профиль блокируется FOR UPDATE,
        decay за 24ч без нарушений применяется перед повышением.
        event_key защищает от повторной выдачи за одно событие
        (дубли Discord-callback/таймаутов) — возвращает None.
        Истечение ban НЕ сбрасывает уровень; /unvac тоже.
        """
        await self._ensure()
        await self._archive_expired(guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                now = await conn.fetchval("SELECT now()")

                # Профиль: создать при первом нарушении, заблокировать.
                await conn.execute(
                    """
                    INSERT INTO vac_profiles (guild_id, user_id)
                    VALUES ($1, $2)
                    ON CONFLICT (guild_id, user_id) DO NOTHING
                    """,
                    guild_id, user_id,
                )
                prof = await conn.fetchrow(
                    """
                    SELECT level, last_violation_at
                    FROM vac_profiles
                    WHERE guild_id=$1 AND user_id=$2
                    FOR UPDATE
                    """,
                    guild_id, user_id,
                )
                level = int(prof["level"] or 0)
                last_v = prof["last_violation_at"]

                # Дедупликация по событию: то же событие не наказывает дважды.
                if event_key:
                    dup = await conn.fetchval(
                        """
                        SELECT id FROM vac_bans
                        WHERE guild_id=$1 AND user_id=$2 AND event_key=$3
                          AND revoked_at IS NULL AND expired_at IS NULL
                        LIMIT 1
                        """,
                        guild_id, user_id, event_key,
                    )
                    if dup is not None:
                        return None

                eff = effective_level(level, last_v, now)
                new_level = min(MAX_VAC_LEVEL, eff + 1)
                duration = VAC_LADDER[new_level - 1]
                expires_at = now + duration

                # Активная блокировка есть — продлеваем её до now+duration
                # и поднимаем level на записи (нарушение во время бана).
                active = await conn.fetchrow(
                    """
                    SELECT id FROM vac_bans
                    WHERE guild_id=$1 AND user_id=$2
                      AND revoked_at IS NULL AND expired_at IS NULL
                      AND expires_at > $3
                    ORDER BY id DESC LIMIT 1
                    """,
                    guild_id, user_id, now,
                )
                if active is not None:
                    row = await conn.fetchrow(
                        """
                        UPDATE vac_bans
                        SET issued_at=$4, expires_at=$5, level=$6,
                            reason=$7, event_key=$8
                        WHERE id=$1
                        RETURNING id, issued_at, expires_at, level, reason
                        """,
                        active["id"], now, expires_at, new_level,
                        reason, event_key,
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO vac_bans
                            (guild_id, user_id, issued_by, issued_at,
                             expires_at, level, reason, event_key)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                        RETURNING id, issued_at, expires_at, level, reason
                        """,
                        guild_id, user_id, issued_by, now, expires_at,
                        new_level, reason, event_key,
                    )

                await conn.execute(
                    """
                    UPDATE vac_profiles
                    SET level=$3, violation_count=violation_count+1,
                        last_violation_at=$4, updated_at=now()
                    WHERE guild_id=$1 AND user_id=$2
                    """,
                    guild_id, user_id, new_level, now,
                )
                out = dict(row)
                out["new_level"] = new_level
                return out

    async def get_profile(
        self, guild_id: int, user_id: int
    ) -> dict | None:
        """Профиль нарушителя (уровень с учётом decay, счётчик)."""
        await self._ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT level, violation_count, last_violation_at
                FROM vac_profiles
                WHERE guild_id=$1 AND user_id=$2
                """,
                guild_id, user_id,
            )
            if row is None:
                return None
            out = dict(row)
            out["effective_level"] = effective_level(
                int(out["level"] or 0), out["last_violation_at"]
            )
            return out

    async def get_active(self, guild_id: int, user_id: int) -> dict | None:
        """Активный VAC пользователя (не отозван, не истёк, не архивирован)."""
        await self._ensure()
        await self._archive_expired(guild_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, issued_by, issued_at, expires_at,
                       level, reason, event_key
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
                SELECT id, user_id, issued_by, issued_at, expires_at,
                       level, reason
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
        """Снять активный VAC. Возвращает запись или None.

        Профиль нарушителя НЕ трогает — /unvac не сбрасывает историю.
        """
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
