"""PostgreSQL database schema for tournament system.

NOTE: table names use the ``tourney_`` prefix to avoid collisions with
existing tables (``teams``, ``match_history``) used by other features.
The canonical schema lives in ``storage/db.py::init_db``; this module
is kept for reference/documentation.
"""

from __future__ import annotations

import asyncpg
import logging

logger = logging.getLogger(__name__)


async def init_database(pool: asyncpg.Pool) -> None:
    """Initialize database schema."""
    async with pool.acquire() as conn:
        # Tournaments table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tourney_tournaments (
                tournament_id TEXT PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                phase TEXT NOT NULL DEFAULT 'setup',
                size INTEGER NOT NULL DEFAULT 8,
                formation_mode TEXT NOT NULL DEFAULT 'manual',
                registration_state TEXT NOT NULL DEFAULT 'closed',
                organizer_id BIGINT NOT NULL DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                UNIQUE(guild_id, channel_id)
            )
        """)

        # Teams table (composite PK: tournament_id + team_id)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tourney_teams (
                team_id INTEGER NOT NULL,
                tournament_id TEXT NOT NULL REFERENCES tourney_tournaments(tournament_id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                is_complete BOOLEAN NOT NULL DEFAULT FALSE,
                PRIMARY KEY (tournament_id, team_id)
            )
        """)

        # Team players table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tourney_team_players (
                tournament_id TEXT NOT NULL,
                team_id INTEGER NOT NULL,
                player_id BIGINT NOT NULL,
                PRIMARY KEY (tournament_id, team_id, player_id),
                FOREIGN KEY (tournament_id, team_id) REFERENCES tourney_teams(tournament_id, team_id) ON DELETE CASCADE
            )
        """)

        # Matches table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tourney_matches (
                match_id TEXT PRIMARY KEY,
                tournament_id TEXT NOT NULL REFERENCES tourney_tournaments(tournament_id) ON DELETE CASCADE,
                team1_id INTEGER NOT NULL,
                team2_id INTEGER NOT NULL,
                winner_id INTEGER,
                is_completed BOOLEAN NOT NULL DEFAULT FALSE,
                stats_submitted BOOLEAN NOT NULL DEFAULT FALSE,
                match_type TEXT NOT NULL
            )
        """)

        # Match history table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tourney_match_history (
                id SERIAL PRIMARY KEY,
                tournament_id TEXT NOT NULL,
                match_id TEXT NOT NULL,
                player_id BIGINT NOT NULL,
                player_name TEXT NOT NULL,
                kills INTEGER NOT NULL,
                deaths INTEGER NOT NULL,
                won BOOLEAN NOT NULL,
                elo_change INTEGER NOT NULL,
                played_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)

        # MM searches table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tourney_mm_searches (
                search_id TEXT PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                format_type TEXT NOT NULL,
                organizer_id BIGINT NOT NULL DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                UNIQUE(guild_id, channel_id)
            )
        """)

        # MM search players table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tourney_mm_search_players (
                search_id TEXT NOT NULL REFERENCES tourney_mm_searches(search_id) ON DELETE CASCADE,
                player_id BIGINT NOT NULL,
                player_name TEXT NOT NULL,
                joined_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (search_id, player_id)
            )
        """)

        logger.info("Database schema initialized")
