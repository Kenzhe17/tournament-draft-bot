"""PostgreSQL-хранилище ежедневной ротации роли «Доступ».

Таблицы:
- access_queue          — очередь заявок на доступ (status='queued')
- access_rotation_runs  — идемпотентные запуски ротации (run_key UNIQUE)
- access_rotations      — журнал замен владельцев доступа
- access_exemptions     — временная защита от автоматической ротации
- access_state          — ID панели/канала на гильдию

DDL ленивый (CREATE TABLE IF NOT EXISTS). Ошибки БД пробрасываются —
сервис ротации при недоступности PostgreSQL пропускает запуск,
а не считает участников неактивными.
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from storage.db import get_pool

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS access_queue (
    guild_id   BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'queued',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS access_rotation_runs (
    guild_id   BIGINT NOT NULL,
    run_key    TEXT NOT NULL,
    day        DATE NOT NULL,
    kind       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'running',
    detail     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, run_key)
);
CREATE TABLE IF NOT EXISTS access_rotations (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    run_key      TEXT NOT NULL,
    day          DATE NOT NULL,
    old_user_id  BIGINT,
    new_user_id  BIGINT,
    old_score    INTEGER,
    new_score    INTEGER,
    actor        TEXT NOT NULL DEFAULT 'auto',
    detail       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS access_exemptions (
    guild_id    BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    until       TIMESTAMPTZ NOT NULL,
    reason      TEXT,
    created_by  BIGINT,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS access_state (
    guild_id    BIGINT PRIMARY KEY,
    channel_id  BIGINT,
    message_id  BIGINT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS access_welcomed (
    guild_id    BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_access_queue_guild "
    "ON access_queue (guild_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_access_rotations_guild_day "
    "ON access_rotations (guild_id, day DESC)",
    "CREATE INDEX IF NOT EXISTS idx_access_rotations_new_user "
    "ON access_rotations (guild_id, new_user_id, created_at DESC)",
)

_tables_ready = False


async def _ensure(conn) -> None:
    global _tables_ready
    if _tables_ready:
        return
    await conn.execute(_DDL)
    for stmt in _INDEXES:
        await conn.execute(stmt)
    _tables_ready = True


# ---------------------------------------------------------------------------
# Очередь заявок
# ---------------------------------------------------------------------------


async def queue_join(guild_id: int, user_id: int) -> bool:
    """Поставить/вернуть в очередь. True — теперь в очереди."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        await conn.execute(
            """
            INSERT INTO access_queue (guild_id, user_id, status)
            VALUES ($1, $2, 'queued')
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET status='queued', updated_at=now()
            """,
            guild_id, user_id,
        )
    return True


async def queue_leave(guild_id: int, user_id: int) -> bool:
    """Убрать из очереди (статус withdrawn, строка остаётся для истории)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        tag = await conn.execute(
            """
            UPDATE access_queue SET status='withdrawn', updated_at=now()
            WHERE guild_id=$1 AND user_id=$2 AND status='queued'
            """,
            guild_id, user_id,
        )
    return tag.endswith(" 1")


async def queue_list(guild_id: int, limit: int = 100) -> list[int]:
    """user_id активных заявок в порядке подачи (старые первыми)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        rows = await conn.fetch(
            """
            SELECT user_id FROM access_queue
            WHERE guild_id=$1 AND status='queued'
            ORDER BY created_at ASC, user_id ASC
            LIMIT $2
            """,
            guild_id, limit,
        )
    return [int(r["user_id"]) for r in rows]


async def queue_count(guild_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        return int(await conn.fetchval(
            "SELECT count(*) FROM access_queue "
            "WHERE guild_id=$1 AND status='queued'",
            guild_id,
        ))


async def queue_remove(guild_id: int, user_id: int) -> None:
    """Удалить из очереди после получения роли (не воскрешать)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        await conn.execute(
            "DELETE FROM access_queue WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )


# ---------------------------------------------------------------------------
# Идемпотентные запуски ротации
# ---------------------------------------------------------------------------


async def begin_run(
    guild_id: int, run_key: str, day, kind: str,
) -> bool:
    """Зарезервировать запуск. False — такой run_key уже есть."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        tag = await conn.execute(
            """
            INSERT INTO access_rotation_runs (guild_id, run_key, day, kind)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, run_key) DO NOTHING
            """,
            guild_id, run_key, day, kind,
        )
    return tag.endswith(" 1")


async def finish_run(
    guild_id: int, run_key: str, status: str, detail: str | None = None,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        await conn.execute(
            "UPDATE access_rotation_runs SET status=$3, detail=$4 "
            "WHERE guild_id=$1 AND run_key=$2",
            guild_id, run_key, status, (detail or "")[:500] or None,
        )


async def get_run(guild_id: int, run_key: str) -> dict[str, Any] | None:
    """Строка запуска: status, detail (JSON-план), created_at."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        row = await conn.fetchrow(
            "SELECT run_key, day, kind, status, detail, created_at "
            "FROM access_rotation_runs WHERE guild_id=$1 AND run_key=$2",
            guild_id, run_key,
        )
    if not row:
        return None
    return dict(row)


async def stale_running_runs(
    guild_id: int, older_than: datetime,
) -> list[dict[str, Any]]:
    """Запуски, зависшие в status='running' старше older_than —
    кандидат на восстановление после рестарта/падения процесса."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        rows = await conn.fetch(
            "SELECT run_key, day, kind, detail, created_at "
            "FROM access_rotation_runs "
            "WHERE guild_id=$1 AND status='running' AND created_at < $2",
            guild_id, older_than,
        )
    return [dict(r) for r in rows]


async def set_run_plan(
    guild_id: int, run_key: str, plan: dict[str, Any],
) -> None:
    """Записать JSON-план запуска в detail (status остаётся
    'running'). Вызывается ПЕРЕД каждой Discord-мутацией —
    точка восстановления после рестарта."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        await conn.execute(
            "UPDATE access_rotation_runs SET detail=$3 "
            "WHERE guild_id=$1 AND run_key=$2",
            guild_id, run_key,
            json.dumps(plan, ensure_ascii=False)[:4000],
        )


# ---------------------------------------------------------------------------
# Журнал замен
# ---------------------------------------------------------------------------


async def record_rotation(
    guild_id: int,
    run_key: str,
    day,
    *,
    old_user_id: int | None,
    new_user_id: int | None,
    old_score: int | None = None,
    new_score: int | None = None,
    actor: str = "auto",
    detail: str | None = None,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        await conn.execute(
            """
            INSERT INTO access_rotations (
                guild_id, run_key, day, old_user_id, new_user_id,
                old_score, new_score, actor, detail
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            guild_id, run_key, day, old_user_id, new_user_id,
            old_score, new_score, actor,
            json.dumps(detail, ensure_ascii=False)
            if isinstance(detail, (dict, list))
            else (str(detail)[:500] if detail else None),
        )


async def find_rotation(
    guild_id: int,
    run_key: str,
    *,
    old_user_id: int | None = None,
    new_user_id: int | None = None,
) -> bool:
    """Есть ли журнальная запись замены для запуска (дедупликация
    при восстановлении после рестарта)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        row = await conn.fetchrow(
            """
            SELECT 1 FROM access_rotations
            WHERE guild_id=$1 AND run_key=$2
              AND ($3::bigint IS NULL OR old_user_id=$3)
              AND ($4::bigint IS NULL OR new_user_id=$4)
            LIMIT 1
            """,
            guild_id, run_key, old_user_id, new_user_id,
        )
    return row is not None


async def last_rotation(guild_id: int) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        row = await conn.fetchrow(
            """
            SELECT old_user_id, new_user_id, old_score, new_score,
                   actor, created_at
            FROM access_rotations
            WHERE guild_id=$1
            ORDER BY id DESC LIMIT 1
            """,
            guild_id,
        )
    if not row:
        return None
    return {
        "old_user_id": row["old_user_id"],
        "new_user_id": row["new_user_id"],
        "old_score": row["old_score"],
        "new_score": row["new_score"],
        "actor": row["actor"],
        "created_at": row["created_at"],
    }


async def last_grant_at(guild_id: int, user_id: int) -> datetime | None:
    """Время последней выдачи доступа пользователю (для 72ч-защиты)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        return await conn.fetchval(
            """
            SELECT created_at FROM access_rotations
            WHERE guild_id=$1 AND new_user_id=$2
            ORDER BY id DESC LIMIT 1
            """,
            guild_id, user_id,
        )


# ---------------------------------------------------------------------------
# Исключения (временная защита)
# ---------------------------------------------------------------------------


async def set_exemption(
    guild_id: int,
    user_id: int,
    until: datetime,
    *,
    reason: str | None = None,
    created_by: int | None = None,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        await conn.execute(
            """
            INSERT INTO access_exemptions
                (guild_id, user_id, until, reason, created_by)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET until=EXCLUDED.until,
                          reason=EXCLUDED.reason,
                          created_by=EXCLUDED.created_by
            """,
            guild_id, user_id, until, reason, created_by,
        )


async def clear_exemption(guild_id: int, user_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        tag = await conn.execute(
            "DELETE FROM access_exemptions WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )
    return tag.endswith(" 1")


async def active_exemptions(guild_id: int) -> dict[int, datetime]:
    """user_id -> until для ещё действующих исключений."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        rows = await conn.fetch(
            "SELECT user_id, until FROM access_exemptions "
            "WHERE guild_id=$1 AND until > now()",
            guild_id,
        )
    return {int(r["user_id"]): r["until"] for r in rows}


# ---------------------------------------------------------------------------
# Состояние панели
# ---------------------------------------------------------------------------


async def get_state(guild_id: int) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        row = await conn.fetchrow(
            "SELECT channel_id, message_id FROM access_state "
            "WHERE guild_id=$1",
            guild_id,
        )
    if not row:
        return {"channel_id": None, "message_id": None}
    return {
        "channel_id": int(row["channel_id"]) if row["channel_id"] else None,
        "message_id": int(row["message_id"]) if row["message_id"] else None,
    }


# ---------------------------------------------------------------------------
# Флаг приветственного ЛС (academy)
# ---------------------------------------------------------------------------


async def was_welcomed(guild_id: int, user_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        return bool(await conn.fetchval(
            "SELECT 1 FROM access_welcomed "
            "WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        ))


async def mark_welcomed(guild_id: int, user_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        await conn.execute(
            "INSERT INTO access_welcomed (guild_id, user_id) "
            "VALUES ($1,$2) ON CONFLICT DO NOTHING",
            guild_id, user_id,
        )


async def set_panel(
    guild_id: int, channel_id: int, message_id: int
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        await conn.execute(
            """
            INSERT INTO access_state (guild_id, channel_id, message_id)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id)
            DO UPDATE SET channel_id=$2, message_id=$3, updated_at=now()
            """,
            guild_id, channel_id, message_id,
        )


# ---------------------------------------------------------------------------
# Межпроцессная блокировка панели (advisory lock)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def panel_mutex(guild_id: int):
    """Межпроцессная блокировка обновления панели доступа.

    PostgreSQL advisory lock на выделенном соединении: один
    ``asyncio.Lock`` не защищает от параллельных процессов
    (Railway реплики / несколько воркеров). ``yield False`` —
    блокировку держит другой процесс, обновление нужно
    пропустить. Ошибки БД пробрасываются — вызывающий код
    обязан НЕ создавать канал/сообщение при сбое.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        # Стабильный int64-ключ из guild_id (переполнение Discord ID
        # не помещается в int4 — хэшируем в знаковый bigint).
        key = int.from_bytes(
            hashlib.md5(f"access-panel:{guild_id}".encode()).digest()[:8],
            "big", signed=True,
        )
        acquired = bool(await conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", key
        ))
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    await conn.execute(
                        "SELECT pg_advisory_unlock($1)", key
                    )
                except Exception:
                    # Соединение закрывается — сессионный lock
                    # снимается автоматически.
                    pass
