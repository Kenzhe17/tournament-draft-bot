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
        # Check if table exists
        table_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'player_stats'
            )
        """)

        if table_exists:
            # Check if user_id column exists
            user_id_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT FROM information_schema.columns
                    WHERE table_name = 'player_stats'
                    AND column_name = 'user_id'
                )
            """)

            if not user_id_exists:
                # Add user_id column
                await conn.execute("ALTER TABLE player_stats ADD COLUMN user_id BIGINT DEFAULT 0")

            # Check primary key constraint
            pk_info = await conn.fetchval("""
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = 'player_stats'::regclass
                AND contype = 'p'
            """)

            # If primary key is not (guild_id, user_id), migrate it
            if not pk_info or "user_id" not in str(pk_info):
                # Drop old primary key
                await conn.execute("ALTER TABLE player_stats DROP CONSTRAINT IF EXISTS player_stats_pkey")
                # Add new primary key
                await conn.execute("ALTER TABLE player_stats ADD PRIMARY KEY (guild_id, user_id)")

            # Check for missing columns
            columns = await conn.fetch("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'player_stats'
            """)
            column_names = {row["column_name"] for row in columns}

            required_columns = ["elo", "finals", "current_streak", "best_win_streak", "best_loss_streak"]
            for col in required_columns:
                if col not in column_names:
                    default = "1000" if col == "elo" else "0"
                    await conn.execute(f"ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS {col} INTEGER DEFAULT {default}")
        else:
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

        # Add new columns for detailed rating system
        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS total_kills INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS total_deaths INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS best_match_kills INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS total_elo_change INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS last_elo_change INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        # Add new columns for level and XP system
        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS xp INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS level INTEGER DEFAULT 1")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS xp_to_next_level INTEGER DEFAULT 100")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS total_earnings INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS tournament_participations INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        # Create cases table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                case_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                price INTEGER NOT NULL,
                drop_rates JSON,
                is_active BOOLEAN DEFAULT TRUE,
                UNIQUE(guild_id, case_id)
            )
        """)

        # Create case_history table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS case_history (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                case_id TEXT NOT NULL,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                item_id TEXT NOT NULL,
                rarity TEXT NOT NULL
            )
        """)

        # Create user_balance table for betting system
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_balance (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                balance INTEGER DEFAULT 100,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        # Create bets table for betting system
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                user_name TEXT NOT NULL,
                match_id TEXT NOT NULL,
                team_name TEXT NOT NULL,
                amount INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id, match_id)
            )
        """)
        
        # Migrations for bets table
        # Drop and recreate table if it exists with wrong schema
        # Check if table exists and has correct primary key
        table_info = await conn.fetch("""
            SELECT conname, contype
            FROM pg_constraint
            WHERE conrelid = 'bets'::regclass
        """)
        
        has_correct_pk = any(
            row['conname'] == 'bets_pkey' and row['contype'] == 'p'
            for row in table_info
        )
        
        if not has_correct_pk:
            # Drop existing table and recreate with correct schema
            await conn.execute("DROP TABLE IF EXISTS bets CASCADE")
            await conn.execute("""
                CREATE TABLE bets (
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    user_name TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    team_name TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id, match_id)
                )
            """)

        # Create betting_stats table for betting statistics
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS betting_stats (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                total_won INTEGER DEFAULT 0,
                total_lost INTEGER DEFAULT 0,
                total_bets INTEGER DEFAULT 0,
                successful_bets INTEGER DEFAULT 0,
                best_win INTEGER DEFAULT 0,
                worst_loss INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        # Create bonus_cooldowns table for tracking daily bonuses
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bonus_cooldowns (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                last_claim TIMESTAMP,
                last_streak_date DATE,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        # Reset betting statistics (migration)
        migration_run = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'db_migrations')"
        )
        if not migration_run:
            # Create migrations table
            await conn.execute("""
                CREATE TABLE db_migrations (
                    migration_name TEXT PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        else:
            # Check if this specific migration has been run
            migration_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM db_migrations WHERE migration_name = 'reset_betting_stats_2024')"
            )
            if migration_exists:
                return  # Migration already run

        # Apply migration: reset betting statistics
        await conn.execute("TRUNCATE TABLE betting_stats")

        # Record migration
        await conn.execute("""
            INSERT INTO db_migrations (migration_name)
            VALUES ('reset_betting_stats_2024')
            ON CONFLICT (migration_name) DO NOTHING
        """)

        # Create minigames table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS minigames (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                category TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                min_bet INTEGER NOT NULL,
                max_bet INTEGER NOT NULL,
                multiplier REAL NOT NULL,
                is_pvp BOOLEAN DEFAULT FALSE,
                is_pve BOOLEAN DEFAULT TRUE,
                is_active BOOLEAN DEFAULT TRUE
            )
        """)

        # Create minigame_sessions table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS minigame_sessions (
                session_id TEXT PRIMARY KEY,
                game_id TEXT NOT NULL,
                guild_id BIGINT NOT NULL,
                player1_id BIGINT NOT NULL,
                player1_bet INTEGER NOT NULL,
                player2_id BIGINT,
                player2_bet INTEGER,
                status TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                winner_id BIGINT,
                winnings INTEGER,
                payout_processed BOOLEAN DEFAULT FALSE
            )
        """)

        # Create minigame_stats table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS minigame_stats (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                game_id TEXT NOT NULL,
                games_played INTEGER DEFAULT 0,
                games_won INTEGER DEFAULT 0,
                total_bet INTEGER DEFAULT 0,
                total_won INTEGER DEFAULT 0,
                net_profit INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, game_id)
            )
        """)

        # Create indexes for performance
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_player_stats_guild_elo
            ON player_stats(guild_id, elo DESC)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_player_stats_guild_level
            ON player_stats(guild_id, level DESC, xp DESC)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_balance_guild
            ON user_balance(guild_id, balance DESC)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_minigame_stats_guild_game
            ON minigame_stats(guild_id, game_id)
        """)

        # Initialize mini-games
        from storage.minigame_init import initialize_minigames
        await initialize_minigames()

        # Initialize cases in database (not just JSON)
        from storage.case_store import case_store
        # Force initialization to ensure cases exist
        if not case_store.get_all_cases():
            case_store._initialize_default_cases()
