"""PostgreSQL-хранилище Discord-представлений журнала турниров.

Таблица ``tournament_journal`` — по одной записи на:
- основную карточку журнала турнира (kind='tournament',
  match_key IS NULL);
- карточку матча в ветке журнала (kind='match',
  match_key='phase:index').

Хранит ID опубликованных Discord-сообщений/ветки, статус
синхронизации и номер журнала. Записи создаются лениво
(CREATE TABLE IF NOT EXISTS) — существующая история аудита
не затрагивается. Ошибки БД пробрасываются сервису журнала,
который решает retry — «успех» без записи не имитируется.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS tournament_journal (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    tournament_id TEXT NOT NULL,
    journal_no INTEGER,
    kind TEXT NOT NULL DEFAULT 'tournament',
    match_key TEXT,
    audit_channel_id BIGINT,
    audit_message_id BIGINT,
    thread_id BIGINT,
    result_message_id BIGINT,
    summary_message_id BIGINT,
    status TEXT NOT NULL DEFAULT 'active',
    sync_status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_INDEXES = (
    # Одна основная карточка на турнир.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_journal_main "
    "ON tournament_journal (tournament_id) "
    "WHERE kind = 'tournament'",
    # Одна карточка на матч внутри турнира.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_journal_match "
    "ON tournament_journal (tournament_id, match_key) "
    "WHERE kind = 'match'",
    # Уникальный номер журнала внутри гильдии — защита от
    # гонки при одновременном создании двух турниров.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_journal_no "
    "ON tournament_journal (guild_id, journal_no) "
    "WHERE kind = 'tournament' AND journal_no IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_journal_sync "
    "ON tournament_journal (sync_status) "
    "WHERE sync_status <> 'synced'",
    "CREATE INDEX IF NOT EXISTS idx_journal_guild "
    "ON tournament_journal (guild_id, tournament_id)",
)

_table_ready = False


async def ensure_table(conn) -> None:
    """Создать таблицу и индексы один раз на процесс."""
    global _table_ready
    if _table_ready:
        return
    await conn.execute(_DDL)
    for stmt in _INDEXES:
        await conn.execute(stmt)
    _table_ready = True


def _row_to_dict(row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------------


async def get_main(tournament_id: str) -> dict[str, Any] | None:
    """Основная запись журнала турнира."""
    from storage.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        row = await conn.fetchrow(
            "SELECT * FROM tournament_journal "
            "WHERE tournament_id = $1 AND kind = 'tournament'",
            str(tournament_id),
        )
        return _row_to_dict(row) if row else None


async def get_match(tournament_id: str, match_key: str) -> dict[str, Any] | None:
    from storage.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        row = await conn.fetchrow(
            "SELECT * FROM tournament_journal "
            "WHERE tournament_id = $1 AND kind = 'match' "
            "AND match_key = $2",
            str(tournament_id), str(match_key),
        )
        return _row_to_dict(row) if row else None


async def list_matches(tournament_id: str) -> list[dict[str, Any]]:
    from storage.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        rows = await conn.fetch(
            "SELECT * FROM tournament_journal "
            "WHERE tournament_id = $1 AND kind = 'match' "
            "ORDER BY match_key",
            str(tournament_id),
        )
        return [_row_to_dict(r) for r in rows]


async def list_pending(limit: int = 100) -> list[dict[str, Any]]:
    """Записи, требующие (пере)публикации — для recovery."""
    from storage.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        rows = await conn.fetch(
            "SELECT * FROM tournament_journal "
            "WHERE sync_status <> 'synced' "
            "   OR audit_message_id IS NULL "
            "   OR (kind = 'tournament' AND thread_id IS NULL) "
            "   OR (kind = 'tournament' AND thread_id IS NOT NULL "
            "       AND summary_message_id IS NULL) "
            "ORDER BY updated_at ASC LIMIT $1",
            limit,
        )
        return [_row_to_dict(r) for r in rows]


async def journal_count(guild_id: int) -> int:
    """Количество журналов гильдии — для номера «ЖУРНАЛ #N»."""
    from storage.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        return await conn.fetchval(
            "SELECT count(*) FROM tournament_journal "
            "WHERE guild_id = $1 AND kind = 'tournament'",
            guild_id,
        )


# ---------------------------------------------------------------------------
# Запись
# ---------------------------------------------------------------------------


async def upsert_main(
    *,
    guild_id: int,
    channel_id: int,
    tournament_id: str,
) -> dict[str, Any]:
    """Получить или создать основную запись журнала.

    Номер журнала присваивается один раз (count+1 на момент
    первой вставки). Возвращает запись.
    """
    from storage.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        row = await conn.fetchrow(
            "SELECT * FROM tournament_journal "
            "WHERE tournament_id = $1 AND kind = 'tournament'",
            str(tournament_id),
        )
        if row:
            return _row_to_dict(row)
        # Гонка journal_no: два flush разных турниров одного
        # гильдии могут одновременно прочитать count+1.
        # Advisory xact-lock по guild_id сериализует присвоение
        # номера; уникальный индекс idx_journal_no — последний
        # рубеж (при конфликте — retry снаружи через flush).
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtext('tournament_journal:' || $1::text))",
                str(guild_id),
            )
            # MAX+1, а не COUNT+1: после удаления промежуточных
            # записей count может дать уже занятый номер.
            # Advisory lock выше сериализует параллельные
            # транзакции, уникальный индекс — последний рубеж.
            no = await conn.fetchval(
                "SELECT COALESCE(MAX(journal_no), 0) + 1 "
                "FROM tournament_journal "
                "WHERE guild_id = $1 AND kind = 'tournament'",
                guild_id,
            )
            row = await conn.fetchrow(
                """
                INSERT INTO tournament_journal
                    (guild_id, channel_id, tournament_id, journal_no,
                     kind, sync_status)
                VALUES ($1,$2,$3,$4,'tournament','pending')
                ON CONFLICT (tournament_id) WHERE kind = 'tournament'
                DO UPDATE SET updated_at = now()
                RETURNING *
                """,
                guild_id, channel_id, str(tournament_id), no,
            )
        return _row_to_dict(row)


async def upsert_match(
    *,
    guild_id: int,
    channel_id: int,
    tournament_id: str,
    match_key: str,
) -> dict[str, Any]:
    """Получить или создать запись карточки матча."""
    from storage.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        row = await conn.fetchrow(
            """
            INSERT INTO tournament_journal
                (guild_id, channel_id, tournament_id, kind,
                 match_key, sync_status)
            VALUES ($1,$2,$3,'match',$4,'pending')
            ON CONFLICT (tournament_id, match_key)
                WHERE kind = 'match'
            DO UPDATE SET updated_at = now()
            RETURNING *
            """,
            guild_id, channel_id, str(tournament_id),
            str(match_key),
        )
        return _row_to_dict(row)


async def update_ids(
    entry_id: int,
    *,
    audit_channel_id: int | None = None,
    audit_message_id: int | None = None,
    thread_id: int | None = None,
    result_message_id: int | None = None,
    summary_message_id: int | None = None,
    status: str | None = None,
    sync_status: str = "synced",
    last_error: str | None = None,
) -> None:
    """Обновить ID сообщений и статус синхронизации записи."""
    from storage.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        await conn.execute(
            """
            UPDATE tournament_journal SET
                audit_channel_id = COALESCE($2, audit_channel_id),
                audit_message_id = COALESCE($3, audit_message_id),
                thread_id = COALESCE($4, thread_id),
                result_message_id = COALESCE($5, result_message_id),
                summary_message_id = COALESCE($6, summary_message_id),
                status = COALESCE($7, status),
                sync_status = $8,
                last_error = $9,
                updated_at = now()
            WHERE id = $1
            """,
            entry_id,
            audit_channel_id, audit_message_id, thread_id,
            result_message_id, summary_message_id, status,
            sync_status, last_error,
        )


async def set_sync(entry_id: int, sync_status: str,
                   last_error: str | None = None) -> None:
    from storage.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        await conn.execute(
            "UPDATE tournament_journal SET sync_status = $2, "
            "last_error = $3, updated_at = now() WHERE id = $1",
            entry_id, sync_status, last_error,
        )
