"""Database connection and utilities."""

import asyncpg
from config import DATABASE_URL

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Get or create database connection pool."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL)
    return _pool


async def close_pool() -> None:
    """Close database connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def init_db() -> None:
    """Initialize database tables."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Check if table exists with old primary key
        table_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'player_stats'
            )
        """)

        if table_exists:
            # Check primary key constraint
            pk_info = await conn.fetchval("""
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = 'player_stats'::regclass
                AND contype = 'p'
            """)

            # If primary key is not (guild_id, user_id), recreate table
            if not pk_info or "user_id" not in str(pk_info):
                await conn.execute("DROP TABLE IF EXISTS player_stats CASCADE")
                table_exists = False

        if not table_exists:
            await conn.execute("""
                CREATE TABLE player_stats (
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    elo INTEGER DEFAULT 1000,
                    wins INTEGER DEFAULT 0,
                    finals INTEGER DEFAULT 0,
                    games INTEGER DEFAULT 0,
                    current_streak INTEGER DEFAULT 0,
                    best_win_streak INTEGER DEFAULT 0,
                    best_loss_streak INTEGER DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS elo INTEGER DEFAULT 1000")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS finals INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS current_streak INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS best_win_streak INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS best_loss_streak INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass
