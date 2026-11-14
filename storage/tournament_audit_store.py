"""PostgreSQL-хранилище журнала аудита турниров.

Таблица ``tournament_audit_log`` — append-only журнал значимых
действий (турниры, матчи, комнаты, организаторы, MM).

DDL выполняется лениво (CREATE TABLE IF NOT EXISTS + индексы) —
миграция не трогает существующие таблицы. Ошибки БД пробрасываются
вызывающему коду: сервис аудита решает, писать ли событие в
pending-файл, но «успех» без попытки записи не имитируется.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS tournament_audit_log (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    tournament_id TEXT,
    match_id TEXT,
    actor_user_id BIGINT,
    target_user_id BIGINT,
    action TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'tournament',
    before_data JSONB,
    after_data JSONB,
    status TEXT NOT NULL DEFAULT 'success',
    interaction_id TEXT,
    dedup_key TEXT,
    discord_message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_audit_tournament "
    "ON tournament_audit_log (guild_id, channel_id, tournament_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_match "
    "ON tournament_audit_log (match_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_actor "
    "ON tournament_audit_log (actor_user_id, id DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_dedup "
    "ON tournament_audit_log (dedup_key) WHERE dedup_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_audit_time "
    "ON tournament_audit_log (guild_id, channel_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_unpublished "
    "ON tournament_audit_log (discord_message_id) "
    "WHERE discord_message_id IS NULL",
)


_table_ready = False


async def ensure_table(conn) -> None:
    """Создать таблицу и индексы, если их ещё нет.

    Выполняется один раз на процесс — последующие вызовы no-op,
    иначе каждая запись аудита стоила бы 7 лишних round-trip'ов.
    """
    global _table_ready
    if _table_ready:
        return
    await conn.execute(_DDL)
    for stmt in _INDEXES:
        await conn.execute(stmt)
    _table_ready = True


def _safe_get(row, key: str):
    """asyncpg.Record не имеет .get — читаем по ключу с fallback."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "event_id": str(row["event_id"]),
        "guild_id": row["guild_id"],
        "channel_id": row["channel_id"],
        "tournament_id": row["tournament_id"],
        "match_id": row["match_id"],
        "actor_user_id": row["actor_user_id"],
        "target_user_id": row["target_user_id"],
        "action": row["action"],
        "source": row["source"],
        "before_data": row["before_data"],
        "after_data": row["after_data"],
        "status": row["status"],
        "interaction_id": row["interaction_id"],
        "dedup_key": _safe_get(row, "dedup_key"),
        "discord_message_id": row["discord_message_id"],
        "created_at": row["created_at"],
    }


async def insert_event(
    conn,
    *,
    event_id: str,
    guild_id: int,
    channel_id: int,
    tournament_id: str | None = None,
    match_id: str | None = None,
    actor_user_id: int | None = None,
    target_user_id: int | None = None,
    action: str,
    source: str = "tournament",
    before_data: dict | None = None,
    after_data: dict | None = None,
    status: str = "success",
    interaction_id: str | None = None,
    dedup_key: str | None = None,
) -> bool:
    """Вставить событие. True — записано, False — дедупликация.

    При переданном dedup_key повторная вставка с тем же ключом
    возвращает False (ON CONFLICT DO NOTHING) — двойное нажатие
    или повторная доставка Interaction не создаёт дубль.
    """
    tag = await conn.execute(
        """
        INSERT INTO tournament_audit_log (
            event_id, guild_id, channel_id, tournament_id, match_id,
            actor_user_id, target_user_id, action, source,
            before_data, after_data, status, interaction_id,
            dedup_key
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,
                  $12,$13,$14)
        ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL
        DO NOTHING
        """,
        event_id, guild_id, channel_id, tournament_id, match_id,
        actor_user_id, target_user_id, action, source,
        json.dumps(before_data) if before_data is not None else None,
        json.dumps(after_data) if after_data is not None else None,
        status, interaction_id, dedup_key,
    )
    # asyncpg возвращает "INSERT 0 1" / "INSERT 0 0"
    return not tag.endswith(" 0")


async def add_event(
    *,
    event_id: str,
    guild_id: int,
    channel_id: int,
    tournament_id: str | None = None,
    match_id: str | None = None,
    actor_user_id: int | None = None,
    target_user_id: int | None = None,
    action: str,
    source: str = "tournament",
    before_data: dict | None = None,
    after_data: dict | None = None,
    status: str = "success",
    interaction_id: str | None = None,
    dedup_key: str | None = None,
) -> bool:
    """Вставить событие через общий пул. True — записано."""
    from storage.db import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        return await insert_event(
            conn,
            event_id=event_id, guild_id=guild_id, channel_id=channel_id,
            tournament_id=tournament_id, match_id=match_id,
            actor_user_id=actor_user_id, target_user_id=target_user_id,
            action=action, source=source,
            before_data=before_data, after_data=after_data,
            status=status, interaction_id=interaction_id,
            dedup_key=dedup_key,
        )


# ---------------------------------------------------------------------------
# Чтение для Discord-панели журнала
# ---------------------------------------------------------------------------


async def list_events(
    *,
    guild_id: int,
    channel_id: int,
    tournament_id: str | None = None,
    source: str | None = None,
    match_id: str | None = None,
    actor_user_id: int | None = None,
    limit: int = 10,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Последние события (id DESC), с фильтрами."""
    from storage.db import get_pool

    clauses = ["guild_id = $1", "channel_id = $2"]
    params: list[Any] = [guild_id, channel_id]
    if tournament_id is not None:
        params.append(tournament_id)
        clauses.append(f"tournament_id = ${len(params)}")
    if match_id is not None:
        params.append(match_id)
        clauses.append(f"match_id = ${len(params)}")
    if actor_user_id is not None:
        params.append(actor_user_id)
        clauses.append(f"actor_user_id = ${len(params)}")
    if source is not None:
        params.append(source)
        clauses.append(f"source = ${len(params)}")
    params.extend([limit, offset])
    lim_i, off_i = len(params) - 1, len(params)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        rows = await conn.fetch(
            f"""
            SELECT * FROM tournament_audit_log
            WHERE {' AND '.join(clauses)}
            ORDER BY id DESC
            LIMIT ${lim_i} OFFSET ${off_i}
            """,
            *params,
        )
        return [_row_to_dict(r) for r in rows]


async def count_events(
    *,
    guild_id: int,
    channel_id: int,
    tournament_id: str | None = None,
    source: str | None = None,
    match_id: str | None = None,
    actor_user_id: int | None = None,
) -> int:
    """Количество событий под текущий фильтр (для пагинации)."""
    from storage.db import get_pool

    clauses = ["guild_id = $1", "channel_id = $2"]
    params: list[Any] = [guild_id, channel_id]
    if tournament_id is not None:
        params.append(tournament_id)
        clauses.append(f"tournament_id = ${len(params)}")
    if match_id is not None:
        params.append(match_id)
        clauses.append(f"match_id = ${len(params)}")
    if actor_user_id is not None:
        params.append(actor_user_id)
        clauses.append(f"actor_user_id = ${len(params)}")
    if source is not None:
        params.append(source)
        clauses.append(f"source = ${len(params)}")

    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        return await conn.fetchval(
            f"SELECT count(*) FROM tournament_audit_log "
            f"WHERE {' AND '.join(clauses)}",
            *params,
        )


async def latest_tournament_id(
    guild_id: int, channel_id: int
) -> str | None:
    """Последний tournament_id с событиями в канале (для журнала
    удалённого/архивного турнира)."""
    from storage.db import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        return await conn.fetchval(
            """
            SELECT tournament_id FROM tournament_audit_log
            WHERE guild_id = $1 AND channel_id = $2
              AND tournament_id IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            guild_id, channel_id,
        )


# ---------------------------------------------------------------------------
# Публикация в Discord-канал журнала (идемпотентно по event_id)
# ---------------------------------------------------------------------------


async def mark_discord_posted(
    conn, event_id: str, message_id: int
) -> None:
    await conn.execute(
        "UPDATE tournament_audit_log SET discord_message_id = $2 "
        "WHERE event_id = $1 AND discord_message_id IS NULL",
        event_id, message_id,
    )


async def unpublished_events(limit: int = 50) -> list[dict[str, Any]]:
    """События, для которых ещё не отправлена карточка в канал."""
    from storage.db import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_table(conn)
        rows = await conn.fetch(
            """
            SELECT * FROM tournament_audit_log
            WHERE discord_message_id IS NULL
              AND status IN ('success', 'pending')
            ORDER BY id ASC LIMIT $1
            """,
            limit,
        )
        return [_row_to_dict(r) for r in rows]
