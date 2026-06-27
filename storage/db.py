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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS player_stats (
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

        # Add new columns if they don't exist (for existing databases)
        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS user_id BIGINT")
        except asyncpg.DuplicateColumnError:
            pass

        # Migrate primary key from (guild_id, name) to (guild_id, user_id)
        # Check if old primary key exists
        try:
            # Get current primary key
            pk_info = await conn.fetchval("""
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = 'player_stats'::regclass
                AND contype = 'p'
            """)
            if pk_info and pk_info != "player_stats_pkey":
                # Drop old primary key
                await conn.execute(f"ALTER TABLE player_stats DROP CONSTRAINT {pk_info}")
                # Add new primary key
                await conn.execute("ALTER TABLE player_stats ADD PRIMARY KEY (guild_id, user_id)")
        except Exception:
            # If migration fails, recreate table
            await conn.execute("DROP TABLE IF EXISTS player_stats")
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
