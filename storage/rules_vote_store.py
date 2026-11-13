"""PostgreSQL-хранилище голосования за правила турнира.

Таблица tournament_rules_votes — durable-зеркало состояния
``tournament.rules_vote`` (json_store остаётся основным
хранилищем; PG — копия голосов и итогового ruleset по ТЗ).
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
        CREATE TABLE IF NOT EXISTS tournament_rules_votes (
            tournament_id TEXT PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            vote_status TEXT NOT NULL DEFAULT 'open',
            vote_deadline DOUBLE PRECISION NOT NULL DEFAULT 0,
            vote_message_id BIGINT NOT NULL DEFAULT 0,
            vote_channel_id BIGINT NOT NULL DEFAULT 0,
            votes JSONB NOT NULL DEFAULT '{}'::jsonb,
            selected_ruleset TEXT,
            ruleset TEXT NOT NULL DEFAULT '',
            created_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            closed_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
        """
    )
    _TABLE_READY = True


async def save_vote(guild_id: int, channel_id: int,
                    record: dict) -> None:
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
                INSERT INTO tournament_rules_votes (
                    tournament_id, guild_id, channel_id, vote_status,
                    vote_deadline, vote_message_id, vote_channel_id,
                    votes, selected_ruleset, ruleset,
                    created_at, closed_at, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13)
                ON CONFLICT (tournament_id) DO UPDATE SET
                    vote_status = EXCLUDED.vote_status,
                    vote_deadline = EXCLUDED.vote_deadline,
                    vote_message_id = EXCLUDED.vote_message_id,
                    vote_channel_id = EXCLUDED.vote_channel_id,
                    votes = EXCLUDED.votes,
                    selected_ruleset = EXCLUDED.selected_ruleset,
                    ruleset = EXCLUDED.ruleset,
                    created_at = EXCLUDED.created_at,
                    closed_at = EXCLUDED.closed_at,
                    updated_at = EXCLUDED.updated_at
                """,
                record.get("tournament_id") or "",
                guild_id,
                channel_id,
                record.get("vote_status") or "open",
                float(record.get("vote_deadline") or 0),
                int(record.get("vote_message_id") or 0),
                int(record.get("vote_channel_id") or 0),
                json.dumps(record.get("votes") or {}),
                record.get("selected_ruleset"),
                record.get("ruleset") or "",
                float(record.get("created_at") or 0),
                float(record.get("closed_at") or 0),
                datetime.now(timezone.utc),
            )
    except Exception as exc:
        logger.debug("tournament_rules_votes save failed: %s", exc)
