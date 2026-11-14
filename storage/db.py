"""Database connection and utilities."""

import logging
import os

import asyncpg
from config import DATABASE_URL

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


async def get_pool() -> asyncpg.Pool:
    """Get or create database connection pool.

    Настройки — через env (Railway Variables), дефолты
    консервативные и не превышают лимиты Railway PostgreSQL:
    - PG_POOL_MIN (default 2): всегда тёплые соединения.
    - PG_POOL_MAX (default 10): не увеличиваем сверх дефолта
      asyncpg — запросы короткие, очередь acquire дешевле
      лишних соединений к PG.
    - PG_COMMAND_TIMEOUT (default 30s): защита от зависших
      запросов — соединение возвращается в пул по таймауту.
    - PG_POOL_MAX_INACTIVE (default 300s): рециклинг
      неактивных соединений.
    - PG_CONNECT_TIMEOUT (default 10s): таймаут установки
      новых соединений.
    """
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=_env_int("PG_POOL_MIN", 2),
            max_size=_env_int("PG_POOL_MAX", 10),
            command_timeout=_env_int("PG_COMMAND_TIMEOUT", 30),
            max_inactive_connection_lifetime=_env_int(
                "PG_POOL_MAX_INACTIVE", 300
            ),
            timeout=_env_int("PG_CONNECT_TIMEOUT", 10),
        )
        logger.info(
            "PostgreSQL pool: min=%s max=%s command_timeout=%ss",
            _env_int("PG_POOL_MIN", 2),
            _env_int("PG_POOL_MAX", 10),
            _env_int("PG_COMMAND_TIMEOUT", 30),
        )
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

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS reputation INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS birthday TEXT DEFAULT ''")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS spouse_id BIGINT DEFAULT 0")
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

        # K/D/A + damage stats for KDA-based Skill Rating
        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS total_assists INTEGER DEFAULT 0")
        except asyncpg.DuplicateColumnError:
            pass

        try:
            await conn.execute("ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS total_damage INTEGER DEFAULT 0")
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
        # (таблица может ещё не существовать на свежей БД —
        # CREATE TABLE ниже)
        try:
            await conn.execute("ALTER TABLE minigames ADD COLUMN IF NOT EXISTS command_name TEXT")
        except (asyncpg.DuplicateColumnError, asyncpg.UndefinedTableError):
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

        # Журнал расчёта ставок: одна строка на матч.
        # settlement_key = "tournament_id:match_id" — идемпотентность
        # и защита от двойной выплаты при повторном вызове/перезапуске.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bet_settlements (
                settlement_key TEXT PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                tournament_id TEXT NOT NULL,
                match_id TEXT NOT NULL,
                winning_team_name TEXT NOT NULL DEFAULT '',
                winning_team_index INTEGER NOT NULL DEFAULT 0,
                payouts JSONB NOT NULL DEFAULT '{}'::jsonb,
                payouts_by_name JSONB NOT NULL DEFAULT '{}'::jsonb,
                settled_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
                is_active BOOLEAN DEFAULT TRUE,
                command_name TEXT
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

        # Create Ultimatum tournament tables
        try:
            from ultimatum import ULTIMATUM_SCHEMA, ELO_SCHEMA
            await conn.execute(ULTIMATUM_SCHEMA)
            await conn.execute(ELO_SCHEMA)
            # Разовый перенос старого Solo ELO в ult_elo_ratings
            # (ON CONFLICT DO NOTHING — идемпотентно, история
            # elo_history остаётся в легаси-таблице).
            try:
                from ultimatum.elo import ult_elo_store
                migrated = await ult_elo_store.migrate_legacy_solo()
                if migrated:
                    logger.info("Migrated %d legacy ultimatum ELO rows", migrated)
            except Exception as e:
                logger.warning("Legacy ultimatum ELO migration skipped: %s", e)
            logger.info("Ultimatum tables initialized successfully")
        except ImportError as e:
            logger.error(f"Failed to initialize database: {e}")

        # Create teams table for BR tournament team format
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                team_id TEXT PRIMARY KEY,
                team_name TEXT NOT NULL,
                password TEXT NOT NULL,
                captain_id BIGINT NOT NULL,
                guild_id BIGINT NOT NULL,
                members BIGINT[] NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
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

        # K/D/A + damage per match (Hall of Fame per-match records)
        for _col in ("assists", "damage", "duration_seconds"):
            await conn.execute(
                f"ALTER TABLE match_history ADD COLUMN IF NOT EXISTS {_col} INTEGER DEFAULT 0")

        # Режим матча (normal / no_ruin)
        await conn.execute(
            "ALTER TABLE match_history"
            " ADD COLUMN IF NOT EXISTS match_mode TEXT DEFAULT 'normal'")

        # Create index for match_history queries
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_match_history_user_date
            ON match_history(guild_id, user_id, date DESC)
        """)
        try:
            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_match_history_user_match
                ON match_history(guild_id, user_id, match_id)
            """)
        except Exception:
            pass

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

        # Ready Check («Найден матч») для Ranked Matchmaking — аддитивная таблица
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ready_checks (
                match_id TEXT PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                search_channel_id BIGINT NOT NULL,
                tournament_channel_id BIGINT NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'READY_CHECK',
                deadline DOUBLE PRECISION NOT NULL DEFAULT 0,
                channel_id BIGINT NOT NULL DEFAULT 0,
                message_id BIGINT NOT NULL DEFAULT 0,
                cancel_reason TEXT NOT NULL DEFAULT '',
                created_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                match_num INTEGER NOT NULL DEFAULT 0,
                players JSONB NOT NULL DEFAULT '[]'::jsonb
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ready_checks_guild_status
            ON ready_checks(guild_id, status)
        """)

        # Один поиск = максимум один АКТИВНЫЙ Ready Check.
        # Терминальные статусы (LAUNCHED/DECLINED/TIMEOUT/CANCELLED)
        # из индекса выпадают — следующий матч того же поиска легален.
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ready_checks_active_search
            ON ready_checks(guild_id, search_channel_id)
            WHERE status NOT IN ('LAUNCHED', 'DECLINED', 'TIMEOUT', 'CANCELLED')
        """)

        # Канал матча, созданный на этапе ready-check (cs-4x4-match-N)
        await conn.execute("""
            ALTER TABLE ready_checks
            ADD COLUMN IF NOT EXISTS match_channel_id BIGINT NOT NULL DEFAULT 0
        """)

        # Тип очереди-источника (normal / no_ruin) — аддитивная колонка.
        # Уникальный индекс активного RC расширяем queue_type, чтобы
        # «Обычка» и «БЕЗ РУИН» могли иметь по одному активному матчу
        # в одном канале поиска.
        await conn.execute("""
            ALTER TABLE ready_checks
            ADD COLUMN IF NOT EXISTS queue_type TEXT NOT NULL DEFAULT 'normal'
        """)
        await conn.execute("DROP INDEX IF EXISTS uq_ready_checks_active_search")
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ready_checks_active_search
            ON ready_checks(guild_id, search_channel_id, queue_type)
            WHERE status NOT IN ('LAUNCHED', 'DECLINED', 'TIMEOUT', 'CANCELLED')
        """)

        # Очередь Ranked Matchmaking (персистентная панель поиска)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS mm_queues (
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL DEFAULT 0,
                format TEXT NOT NULL DEFAULT 'squad',
                state TEXT NOT NULL DEFAULT 'open',
                players JSONB NOT NULL DEFAULT '[]'::jsonb,
                player_user_ids JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TEXT NOT NULL DEFAULT '',
                timer_started BOOLEAN NOT NULL DEFAULT FALSE,
                tournament_channel_id BIGINT NOT NULL DEFAULT 0,
                match_num INTEGER NOT NULL DEFAULT 0,
                queue_type TEXT NOT NULL DEFAULT 'normal',
                PRIMARY KEY (guild_id, channel_id, queue_type)
            )
        """)

        # Миграция существующей таблицы: добавить queue_type и
        # расширить первичный ключ, чтобы в одном канале жили две
        # очереди («Обычка» и «БЕЗ РУИН»).
        await conn.execute("""
            ALTER TABLE mm_queues
            ADD COLUMN IF NOT EXISTS queue_type TEXT NOT NULL DEFAULT 'normal'
        """)
        # Псевдонимы участников очереди (анонимное отображение панели)
        await conn.execute("""
            ALTER TABLE mm_queues
            ADD COLUMN IF NOT EXISTS aliases JSONB NOT NULL DEFAULT '{}'::jsonb
        """)
        # Динамический таймер очереди + состояния уведомлений
        await conn.execute("""
            ALTER TABLE mm_queues
            ADD COLUMN IF NOT EXISTS session_started_at DOUBLE PRECISION NOT NULL DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE mm_queues
            ADD COLUMN IF NOT EXISTS effective_limit_seconds INTEGER NOT NULL DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE mm_queues
            ADD COLUMN IF NOT EXISTS first_notification_status TEXT NOT NULL DEFAULT 'pending'
        """)
        await conn.execute("""
            ALTER TABLE mm_queues
            ADD COLUMN IF NOT EXISTS first_notification_sent_at DOUBLE PRECISION NOT NULL DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE mm_queues
            ADD COLUMN IF NOT EXISTS first_notification_message_id BIGINT NOT NULL DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE mm_queues
            ADD COLUMN IF NOT EXISTS second_notification_status TEXT NOT NULL DEFAULT 'pending'
        """)
        await conn.execute("""
            ALTER TABLE mm_queues
            ADD COLUMN IF NOT EXISTS second_notification_sent_at DOUBLE PRECISION NOT NULL DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE mm_queues
            ADD COLUMN IF NOT EXISTS second_notification_message_id BIGINT NOT NULL DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE mm_queues
            ADD COLUMN IF NOT EXISTS notify_suppressed_until DOUBLE PRECISION NOT NULL DEFAULT 0
        """)
        await conn.execute("ALTER TABLE mm_queues DROP CONSTRAINT IF EXISTS mm_queues_pkey")
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'mm_queues'::regclass AND contype = 'p'
                ) THEN
                    ALTER TABLE mm_queues
                    ADD PRIMARY KEY (guild_id, channel_id, queue_type);
                END IF;
            END $$;
        """)

        # Режим «БЕЗ РУИН» удалён из бота — чистим его активное
        # состояние: недоигранные поиски и не-запущенные ready-checks.
        # Завершённые матчи, история и статистика не затрагиваются;
        # уже запущенный матч (LAUNCHED) доигрывается штатно.
        await conn.execute(
            "DELETE FROM mm_queues WHERE queue_type <> 'normal'"
        )
        await conn.execute("""
            UPDATE ready_checks
            SET status = 'CANCELLED',
                cancel_reason = 'queue_removed'
            WHERE queue_type <> 'normal'
              AND status NOT IN ('LAUNCHED', 'DECLINED', 'TIMEOUT', 'CANCELLED')
        """)

        # Глобальная нумерация матчей (карточка «РЕЗУЛЬТАТ МАТЧА» + embed запуска)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS match_counter (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                value INTEGER NOT NULL DEFAULT 0
            )
        """)
        await conn.execute("""
            INSERT INTO match_counter (id, value) VALUES (1, 0)
            ON CONFLICT (id) DO NOTHING
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS match_numbers (
                match_key TEXT PRIMARY KEY,
                number INTEGER NOT NULL,
                announced BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)

        # Reputation System 2.0
        # Журнал всех начислений REP (MANUAL_REP / INVITE / BOOST / ADMIN_ADJUSTMENT)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reputation_transactions (
                id BIGSERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                source TEXT NOT NULL,
                amount INTEGER NOT NULL,
                actor_user_id BIGINT NOT NULL DEFAULT 0,
                subject_user_id BIGINT NOT NULL DEFAULT 0,
                event_key TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rep_tx_user
            ON reputation_transactions(guild_id, user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rep_tx_giver
            ON reputation_transactions(guild_id, actor_user_id)
            WHERE source = 'MANUAL_REP'
        """)

        # Состояние выдачи /rep: скользящий cooldown 24ч на отправителя
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rep_giver_state (
                guild_id BIGINT NOT NULL,
                giver_id BIGINT NOT NULL,
                last_rep_at TIMESTAMPTZ,
                PRIMARY KEY (guild_id, giver_id)
            )
        """)

        # Ожидающие/подтверждённые награды за приглашения
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rep_invites (
                id BIGSERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                inviter_id BIGINT,
                invitee_id BIGINT NOT NULL,
                invite_code TEXT NOT NULL DEFAULT '',
                joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                verify_until TIMESTAMPTZ NOT NULL,
                attribution TEXT NOT NULL DEFAULT 'pending',
                reward_status TEXT NOT NULL DEFAULT 'pending',
                rewarded_at TIMESTAMPTZ,
                reject_reason TEXT NOT NULL DEFAULT '',
                UNIQUE (guild_id, invitee_id)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rep_invites_pending
            ON rep_invites(guild_id, reward_status, verify_until)
        """)

        # Ожидающие/подтверждённые награды за бусты
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rep_boosts (
                id BIGSERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                booster_id BIGINT NOT NULL,
                boost_started_at TIMESTAMPTZ NOT NULL,
                verify_until TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                rewarded_at TIMESTAMPTZ,
                UNIQUE (guild_id, booster_id)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rep_boosts_pending
            ON rep_boosts(guild_id, status, verify_until)
        """)

        # Outbox публичных логов репутации: запись создаётся в той же
        # транзакции, что и начисление REP. UNIQUE(transaction_id) —
        # событие не может быть поставлено в очередь дважды.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rep_log_outbox (
                id BIGSERIAL PRIMARY KEY,
                transaction_id BIGINT NOT NULL UNIQUE,
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                source TEXT NOT NULL,
                amount INTEGER NOT NULL,
                balance_after INTEGER NOT NULL DEFAULT 0,
                actor_user_id BIGINT NOT NULL DEFAULT 0,
                subject_user_id BIGINT NOT NULL DEFAULT 0,
                log_status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                log_message_id BIGINT,
                published_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rep_log_outbox_pending
            ON rep_log_outbox(log_status, id)
            WHERE log_status = 'pending'
        """)

        # История статусов приглашений: каждый переход pending/rejected/
        # cancelled/rewarded и каждая переатрибуция фиксируются здесь.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rep_invite_events (
                id BIGSERIAL PRIMARY KEY,
                invite_id BIGINT NOT NULL,
                guild_id BIGINT NOT NULL,
                invitee_id BIGINT NOT NULL,
                old_attribution TEXT NOT NULL DEFAULT '',
                new_attribution TEXT NOT NULL DEFAULT '',
                old_status TEXT NOT NULL DEFAULT '',
                new_status TEXT NOT NULL DEFAULT '',
                inviter_id BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rep_invite_events_invite
            ON rep_invite_events(invite_id, created_at)
        """)

        # Персональные пригласительные ссылки: code -> owner_id.
        # Создаются кнопкой «Моя ссылка» в /invites.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rep_invite_owners (
                guild_id BIGINT NOT NULL,
                code TEXT NOT NULL,
                owner_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (guild_id, code)
            )
        """)

        # Одна действующая персональная ссылка на пользователя.
        # invite_code='' — слот зарезервирован, ссылка создаётся
        # (защита от конкурентного создания между процессами).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rep_user_invites (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                invite_code TEXT NOT NULL DEFAULT '',
                channel_id BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        # Мелкие настройки бота per-guild (id служебных каналов и т.п.)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                guild_id BIGINT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (guild_id, key)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS match_channel_slots (
                channel_id BIGINT PRIMARY KEY,
                guild_id BIGINT NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'free',
                owner TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
