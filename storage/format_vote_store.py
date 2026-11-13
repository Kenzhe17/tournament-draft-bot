"""PostgreSQL-хранилище итогов голосования за формат матча.

Таблица format_votes — зеркало состояния format_vote из
match_channels турнира (json_store остаётся основным хранилищем;
PG — durable-копия выбранного формата и голосов по ТЗ).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_TABLE_READY = False


async def _ensure_table(conn) -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS format_votes (
            match_id TEXT PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            match_type TEXT NOT NULL,
            match_index INTEGER NOT NULL,
            vote_message_id BIGINT NOT NULL DEFAULT 0,
            vote_channel_id BIGINT NOT NULL DEFAULT 0,
            vote_deadline DOUBLE PRECISION NOT NULL DEFAULT 0,
            votes JSONB NOT NULL DEFAULT '{}'::jsonb,
            selected_format TEXT,
            vote_status TEXT NOT NULL DEFAULT 'open',
            tie BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
        """
    )
    _TABLE_READY = True


async def save_vote(guild_id: int, channel_id: int, record: dict) -> None:
    """Upsert состояния голосования. Без DATABASE_URL — no-op."""
    from config import DATABASE_URL
    from storage.db import get_pool

    if not DATABASE_URL:
        return
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await _ensure_table(conn)
            await conn.execute(
                """
                INSERT INTO format_votes (
                    match_id, guild_id, channel_id, match_type,
                    match_index, vote_message_id, vote_channel_id,
                    vote_deadline, votes, selected_format,
                    vote_status, tie, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13)
                ON CONFLICT (match_id) DO UPDATE SET
                    vote_message_id = EXCLUDED.vote_message_id,
                    vote_channel_id = EXCLUDED.vote_channel_id,
                    vote_deadline = EXCLUDED.vote_deadline,
                    votes = EXCLUDED.votes,
                    selected_format = EXCLUDED.selected_format,
                    vote_status = EXCLUDED.vote_status,
                    tie = EXCLUDED.tie,
                    updated_at = EXCLUDED.updated_at
                """,
                record.get("match_id") or "",
                guild_id,
                channel_id,
                record.get("match_type") or "",
                int(record.get("match_index") or 0),
                int(record.get("vote_message_id") or 0),
                int(record.get("vote_channel_id") or 0),
                float(record.get("vote_deadline") or 0),
                json.dumps(record.get("votes") or {}),
                record.get("selected_format"),
                record.get("vote_status") or "open",
                bool(record.get("tie")),
                datetime.now(timezone.utc),
            )
    except Exception as exc:
        logger.debug("format_votes save failed: %s", exc)
