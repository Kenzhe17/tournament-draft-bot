"""Database connection and utilities."""

import logging
import asyncpg
from config import DATABASE_URL

logger = logging.getLogger(__name__)

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
    logger.info("init_db() called - starting database initialization")
    pool = await get_pool()
    async with pool.acquire() as conn:
        logger.info("Database connection acquired, starting table creation")
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
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS xp_to_next_level INTEGER DEFAULT 250")
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

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS description TEXT DEFAULT ''")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS avatar_url TEXT DEFAULT NULL")
        except asyncpg.DuplicateColumnError:
            pass

        # Add daily tournament coin limit columns
        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS daily_tournament_coins INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS daily_coins_date TEXT DEFAULT ''")
        except asyncpg.DuplicateColumnError:
            pass

        # Create user_balance table for economy system
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_balance (
                guild_id BIGINT,
                user_id BIGINT,
                balance INTEGER DEFAULT 100,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        # Create user_bank table for bank system
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_bank (
                guild_id BIGINT,
                user_id BIGINT,
                bank_balance INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        # Create questions table for quiz games
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                category TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                hints JSON,
                options JSON,
                is_active BOOLEAN DEFAULT TRUE
            )
        """)

        # Create question_history table for anti-memorization
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS question_history (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                question_id TEXT NOT NULL,
                answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                was_correct BOOLEAN DEFAULT FALSE
            )
        """)

        # Add command_name column to minigames if not exists
        try:
            await conn.execute("ALTER TABLE minigames ADD COLUMN IF NOT EXISTS command_name TEXT")
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

        # Create whoami_cooldowns table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS whoami_cooldowns (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                last_use TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id)
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
                tournament_id TEXT NOT NULL,
                match_id TEXT NOT NULL,
                team_name TEXT NOT NULL,
                team_index INTEGER NOT NULL DEFAULT 0,
                amount INTEGER NOT NULL,
                odds REAL NOT NULL DEFAULT 1.9,
                PRIMARY KEY (guild_id, user_id, tournament_id, match_id)
            )
        """)
        
        # Migration: add tournament_id column if it doesn't exist
        try:
            await conn.execute("ALTER TABLE bets ADD COLUMN IF NOT EXISTS tournament_id TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass  # Column might already exist

        # Migration: add team_index column if it doesn't exist
        try:
            await conn.execute("ALTER TABLE bets ADD COLUMN IF NOT EXISTS team_index INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # Column might already exist

        # Migration: add match_type column if it doesn't exist
        try:
            await conn.execute("ALTER TABLE bets ADD COLUMN IF NOT EXISTS match_type TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass  # Column might already exist

        # Migration: ensure PRIMARY KEY constraint exists on bets table
        try:
            # Drop existing primary key if it exists (might be different)
            await conn.execute("ALTER TABLE bets DROP CONSTRAINT IF EXISTS bets_pkey")
        except Exception:
            pass  # Constraint might not exist

        # Add correct PRIMARY KEY constraint
        try:
            await conn.execute(
                "ALTER TABLE bets ADD PRIMARY KEY (guild_id, user_id, tournament_id, match_id)"
            )
        except Exception:
            pass  # Might already exist or table recreated
        else:
            # Add odds column if it doesn't exist
            try:
                await conn.execute("ALTER TABLE bets ADD COLUMN IF NOT EXISTS odds REAL NOT NULL DEFAULT 1.9")
            except Exception:
                pass  # Column might already exist

        # Create match_odds table for dynamic odds tracking
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS match_odds (
                match_id TEXT PRIMARY KEY,
                team_a_name TEXT NOT NULL DEFAULT '',
                team_b_name TEXT NOT NULL DEFAULT '',
                team_a_odds REAL NOT NULL DEFAULT 1.9,
                team_b_odds REAL NOT NULL DEFAULT 1.9,
                team_a_buffer INTEGER NOT NULL DEFAULT 0,
                team_b_buffer INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Add team name columns if they don't exist
        try:
            await conn.execute("ALTER TABLE match_odds ADD COLUMN IF NOT EXISTS team_a_name TEXT NOT NULL DEFAULT ''")
            await conn.execute("ALTER TABLE match_odds ADD COLUMN IF NOT EXISTS team_b_name TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass  # Columns might already exist

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
                streak INTEGER DEFAULT 1,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        # Add streak column if it doesn't exist (migration)
        column_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'bonus_cooldowns' AND column_name = 'streak'
            )
        """)
        if not column_exists:
            await conn.execute("ALTER TABLE bonus_cooldowns ADD COLUMN streak INTEGER DEFAULT 1")

        # Create shop_items table for store system
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shop_items (
                item_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                price INTEGER NOT NULL,
                category TEXT NOT NULL,
                rarity TEXT NOT NULL,
                is_sellable BOOLEAN DEFAULT TRUE,
                emoji TEXT,
                is_active BOOLEAN DEFAULT TRUE
            )
        """)

        # Create user_inventory table for player inventories
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_inventory (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                item_id TEXT NOT NULL,
                obtained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id, item_id),
                FOREIGN KEY (item_id) REFERENCES shop_items(item_id) ON DELETE CASCADE
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
                pass  # Migration already run, skip but continue with table creation
            else:
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

        # Create match_history table for player match history
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS match_history (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                match_id TEXT NOT NULL,
                tournament_id TEXT NOT NULL,
                date TIMESTAMP NOT NULL,
                match_type TEXT NOT NULL,
                match_index INTEGER NOT NULL,
                team_index INTEGER NOT NULL,
                team_name TEXT NOT NULL,
                teammates JSON NOT NULL,
                opponent_team_index INTEGER NOT NULL,
                opponent_team_name TEXT NOT NULL,
                opponents JSON NOT NULL,
                kills INTEGER NOT NULL,
                deaths INTEGER NOT NULL,
                result TEXT NOT NULL,
                elo_change INTEGER NOT NULL,
                bet_amount INTEGER DEFAULT 0,
                bet_win INTEGER DEFAULT 0,
                bet_team TEXT DEFAULT ''
            )
        """)

        # Create index for match_history queries
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_match_history_user_date
            ON match_history(guild_id, user_id, date DESC)
        """)
        # Force initialization to ensure cases exist
        if not case_store.get_all_cases():
            case_store._initialize_default_cases()

        # Create pets table
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pets (
                    pet_id BIGSERIAL PRIMARY KEY,
                    owner_1_id BIGINT NOT NULL,
                    owner_2_id BIGINT NULL,
                    pet_type TEXT NOT NULL,
                    rarity TEXT NOT NULL,
                    pet_name TEXT NOT NULL,
                    level INTEGER NOT NULL CHECK (level >= 1 AND level <= 100),
                    exp INTEGER NOT NULL CHECK (exp >= 0),
                    fullness INTEGER NOT NULL CHECK (fullness >= 0 AND fullness <= 100),
                    energy INTEGER NOT NULL CHECK (energy >= 0 AND energy <= 100),
                    last_fed_at TIMESTAMPTZ NOT NULL,
                    last_energy_update_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    protected_item_id TEXT NULL,
                    hunger_started_at TIMESTAMPTZ NULL,
                    CHECK (owner_2_id IS NULL OR owner_2_id != owner_1_id)
                )
            """)
        except Exception as e:
            logger.error(f"Error creating pets table: {e}", exc_info=True)
            raise  # Re-raise to see full error

        # Add hunger_started_at column if it doesn't exist (for existing tables)
        try:
            await conn.execute("""
                ALTER TABLE pets ADD COLUMN IF NOT EXISTS hunger_started_at TIMESTAMPTZ NULL
            """)
        except Exception as e:
            logger.error(f"Error adding hunger_started_at column: {e}")

        # Migration: update level check constraint from 10 to 100
        try:
            # Drop old constraint if it exists
            await conn.execute("ALTER TABLE pets DROP CONSTRAINT IF EXISTS pets_level_check")
        except Exception as e:
            logger.error(f"Error dropping old level constraint: {e}")

        try:
            # Add new constraint with level <= 100
            await conn.execute("ALTER TABLE pets ADD CONSTRAINT pets_level_check CHECK (level >= 1 AND level <= 100)")
        except Exception as e:
            logger.error(f"Error adding new level constraint: {e}")

        # Create indexes for pets
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_pets_owner_1 ON pets(owner_1_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_pets_owner_2 ON pets(owner_2_id)")
        except Exception as e:
            logger.error(f"Error creating pets indexes: {e}")

        # Create pet_invitations table for co-ownership
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pet_invitations (
                    invitation_id BIGSERIAL PRIMARY KEY,
                    inviter_id BIGINT NOT NULL,
                    invitee_id BIGINT NOT NULL,
                    pet_type TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    UNIQUE (inviter_id, invitee_id, status)
                )
            """)
        except Exception as e:
            logger.error(f"Error creating pet_invitations table: {e}")

        # Create index for pet_invitations
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_pet_invitations_invitee ON pet_invitations(invitee_id, status)")
        except Exception as e:
            logger.error(f"Error creating pet_invitations index: {e}")
