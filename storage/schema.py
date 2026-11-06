"""PostgreSQL database schema for tournament system."""

from __future__ import annotations

import asyncpg
import logging

logger = logging.getLogger(__name__)


async def init_database(pool: asyncpg.Pool) -> None:
    """Initialize database schema."""
    async with pool.acquire() as conn:
        # Tournaments table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tournaments (
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

        # Teams table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                team_id INTEGER PRIMARY KEY,
                tournament_id TEXT NOT NULL REFERENCES tournaments(tournament_id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                is_complete BOOLEAN NOT NULL DEFAULT FALSE,
                UNIQUE(tournament_id, team_id)
            )
        """)

        # Team players table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS team_players (
                tournament_id TEXT NOT NULL,
                team_id INTEGER NOT NULL,
                player_id BIGINT NOT NULL,
                PRIMARY KEY (tournament_id, team_id, player_id),
                FOREIGN KEY (tournament_id, team_id) REFERENCES teams(tournament_id, team_id) ON DELETE CASCADE
            )
        """)

        # Matches table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                match_id TEXT PRIMARY KEY,
                tournament_id TEXT NOT NULL REFERENCES tournaments(tournament_id) ON DELETE CASCADE,
                team1_id INTEGER NOT NULL,
                team2_id INTEGER NOT NULL,
                winner_id INTEGER,
                is_completed BOOLEAN NOT NULL DEFAULT FALSE,
                stats_submitted BOOLEAN NOT NULL DEFAULT FALSE,
                match_type TEXT NOT NULL
            )
        """)

        # Player stats table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS player_stats (
                user_id BIGINT NOT NULL,
                guild_id BIGINT NOT NULL,
                username TEXT NOT NULL,
                total_kills INTEGER NOT NULL DEFAULT 0,
                total_deaths INTEGER NOT NULL DEFAULT 0,
                total_wins INTEGER NOT NULL DEFAULT 0,
                total_losses INTEGER NOT NULL DEFAULT 0,
                elo INTEGER NOT NULL DEFAULT 1000,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, guild_id)
            )
        """)

        # Match history table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS match_history (
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
            CREATE TABLE IF NOT EXISTS mm_searches (
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
            CREATE TABLE IF NOT EXISTS mm_search_players (
                search_id TEXT NOT NULL REFERENCES mm_searches(search_id) ON DELETE CASCADE,
                player_id BIGINT NOT NULL,
                player_name TEXT NOT NULL,
                joined_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (search_id, player_id)
            )
        """)

        logger.info("Database schema initialized")
