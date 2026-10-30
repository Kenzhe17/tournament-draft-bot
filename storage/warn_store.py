"""Warning system storage."""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from config import MUTE_ROLE_ID
from storage.db import get_pool

logger = logging.getLogger(__name__)

# Timezone offset for GMT+5
TIMEZONE_OFFSET = timezone(timedelta(hours=5))


class Warn:
    """Warning data model."""

    def __init__(self, warn_id: int, user_id: int, guild_id: int,
                 timestamp: datetime, reason: str, active: bool = True, moderator_id: int = None, expires_at: datetime = None, case_id: int = None, warn_number: int = None):
        self.warn_id = warn_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.timestamp = timestamp
        self.reason = reason
        self.active = active
        self.moderator_id = moderator_id
        self.expires_at = expires_at
        self.case_id = case_id
        self.warn_number = warn_number


class WarnStore:
    """Storage for warning system."""
    
    def __init__(self):
        self.enabled = False
    
    def enable_db(self):
        """Enable database storage."""
        self.enabled = True
        logger.info("WarnStore: Database storage enabled")
        logger.info(f"WarnStore: enabled={self.enabled}")
    
    async def init_table(self):
        """Initialize warnings table in database."""
        if not self.enabled:
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS warnings (
                    warn_id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    guild_id BIGINT NOT NULL,
                    moderator_id BIGINT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reason TEXT,
                    active BOOLEAN DEFAULT TRUE,
                    mute_until TIMESTAMP,
                    expires_at TIMESTAMP,
                    case_id INTEGER,
                    warn_number INTEGER
                )
            """)

            # Add expires_at column if it doesn't exist (migration)
            try:
                await conn.execute("""
                    ALTER TABLE warnings
                    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP
                """)
                logger.info("Added expires_at column to warnings table")
            except Exception as e:
                logger.info(f"expires_at column may already exist: {e}")

            # Add case_id column if it doesn't exist (migration)
            try:
                await conn.execute("""
                    ALTER TABLE warnings
                    ADD COLUMN IF NOT EXISTS case_id INTEGER
                """)
                logger.info("Added case_id column to warnings table")
            except Exception as e:
                logger.info(f"case_id column may already exist: {e}")

            # Add warn_number column if it doesn't exist (migration)
            try:
                await conn.execute("""
                    ALTER TABLE warnings
                    ADD COLUMN IF NOT EXISTS warn_number INTEGER
                """)
                logger.info("Added warn_number column to warnings table")
            except Exception as e:
                logger.info(f"warn_number column may already exist: {e}")

            logger.info("Warnings table initialized")
    
    async def get_next_case_id(self, guild_id: int) -> int:
        """Get the next case ID for a guild."""
        if not self.enabled:
            return 1

        pool = await get_pool()
        async with pool.acquire() as conn:
            max_case = await conn.fetchval("""
                SELECT MAX(case_id)
                FROM warnings
                WHERE guild_id = $1
            """, guild_id)
            return (max_case or 0) + 1

    async def get_next_warn_number(self, user_id: int, guild_id: int) -> int:
        """Get the next warning number for a user in a guild."""
        if not self.enabled:
            return 1

        pool = await get_pool()
        async with pool.acquire() as conn:
            max_warn = await conn.fetchval("""
                SELECT MAX(warn_number)
                FROM warnings
                WHERE user_id = $1 AND guild_id = $2
            """, user_id, guild_id)
            return (max_warn or 0) + 1

    async def add_warn(self, user_id: int, guild_id: int,
                      moderator_id: int, reason: str = "Без причины") -> Warn:
        """Add a warning to the database."""
        if not self.enabled:
            logger.warning("WarnStore: Database not enabled, cannot add warning")
            return None

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Use naive datetime for PostgreSQL compatibility
            expires_at = datetime.now() + timedelta(days=7)
            case_id = await self.get_next_case_id(guild_id)
            warn_number = await self.get_next_warn_number(user_id, guild_id)

            warn_id = await conn.fetchval("""
                INSERT INTO warnings (user_id, guild_id, moderator_id, reason, expires_at, case_id, warn_number)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING warn_id
            """, user_id, guild_id, moderator_id, reason, expires_at, case_id, warn_number)

            timestamp = datetime.now()
            return Warn(
                warn_id=warn_id,
                user_id=user_id,
                guild_id=guild_id,
                timestamp=timestamp,
                reason=reason,
                active=True,
                moderator_id=moderator_id,
                expires_at=expires_at,
                case_id=case_id,
                warn_number=warn_number
            )
    
    async def get_active_warns(self, user_id: int, guild_id: int) -> List[Warn]:
        """Get all active warnings for a user."""
        if not self.enabled:
            return []

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT warn_id, user_id, guild_id, timestamp, reason, active, moderator_id, expires_at, case_id, warn_number
                FROM warnings
                WHERE user_id = $1 AND guild_id = $2 AND active = TRUE AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                ORDER BY timestamp DESC
            """, user_id, guild_id)

            return [Warn(
                warn_id=row['warn_id'],
                user_id=row['user_id'],
                guild_id=row['guild_id'],
                timestamp=row['timestamp'],
                reason=row['reason'],
                active=row['active'],
                moderator_id=row['moderator_id'],
                expires_at=row['expires_at'],
                case_id=row.get('case_id'),
                warn_number=row.get('warn_number')
            ) for row in rows]
    
    async def get_all_warns(self, user_id: int, guild_id: int) -> List[Warn]:
        """Get all warnings for a user (including inactive)."""
        if not self.enabled:
            return []

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT warn_id, user_id, guild_id, timestamp, reason, active, moderator_id, expires_at, case_id, warn_number
                FROM warnings
                WHERE user_id = $1 AND guild_id = $2
                ORDER BY timestamp DESC
            """, user_id, guild_id)

            return [Warn(
                warn_id=row['warn_id'],
                user_id=row['user_id'],
                guild_id=row['guild_id'],
                timestamp=row['timestamp'],
                reason=row['reason'],
                active=row['active'],
                moderator_id=row['moderator_id'],
                expires_at=row['expires_at'],
                case_id=row.get('case_id'),
                warn_number=row.get('warn_number')
            ) for row in rows]
    
    async def remove_last_warn(self, user_id: int, guild_id: int) -> Optional[Warn]:
        """Remove the most recent active warning."""
        if not self.enabled:
            return None

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                DELETE FROM warnings
                WHERE warn_id = (
                    SELECT warn_id FROM warnings
                    WHERE user_id = $1 AND guild_id = $2 AND active = TRUE
                    ORDER BY timestamp DESC
                    LIMIT 1
                )
                RETURNING warn_id, user_id, guild_id, timestamp, reason, active, moderator_id, expires_at, case_id, warn_number
            """, user_id, guild_id)

            if row:
                return Warn(
                    warn_id=row['warn_id'],
                    user_id=row['user_id'],
                    guild_id=row['guild_id'],
                    timestamp=row['timestamp'],
                    reason=row['reason'],
                    active=row['active'],
                    moderator_id=row['moderator_id'],
                    expires_at=row['expires_at'],
                    case_id=row.get('case_id'),
                    warn_number=row.get('warn_number')
                )
            return None
    
    async def set_mute_until(self, user_id: int, guild_id: int, mute_until: datetime):
        """Set mute expiration time for a user."""
        if not self.enabled:
            return

        # Ensure mute_until is naive (no timezone) for database consistency
        if mute_until.tzinfo is not None:
            mute_until = mute_until.replace(tzinfo=None)

        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE warnings
                SET mute_until = $1
                WHERE user_id = $2 AND guild_id = $3 AND active = TRUE
            """, mute_until, user_id, guild_id)
            logger.info(f"set_mute_until: user_id={user_id}, guild_id={guild_id}, mute_until={mute_until}, affected={result}")

    async def clear_mute_until(self, user_id: int, guild_id: int):
        """Clear mute expiration time for a user."""
        if not self.enabled:
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE warnings
                SET mute_until = NULL
                WHERE user_id = $1 AND guild_id = $2
            """, user_id, guild_id)
    
    async def get_mute_until(self, user_id: int, guild_id: int) -> Optional[datetime]:
        """Get the earliest mute expiration time for a user."""
        if not self.enabled:
            return None

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Use naive datetime for comparison
            current_time = datetime.now().replace(tzinfo=None)
            mute_until = await conn.fetchval("""
                SELECT MIN(mute_until)
                FROM warnings
                WHERE user_id = $1 AND guild_id = $2 AND active = TRUE
                  AND mute_until > $1
            """, user_id, guild_id, current_time)

            return mute_until
    
    async def clear_expired_mutes(self) -> List[int]:
        """Get users with expired mutes and clear mute_until."""
        if not self.enabled:
            return []

        pool = await get_pool()
        async with pool.acquire() as conn:
            # First, log all active mutes with their mute_until values for debugging
            all_mutes = await conn.fetch("""
                SELECT user_id, guild_id, mute_until
                FROM warnings
                WHERE active = TRUE
                  AND mute_until IS NOT NULL
            """)
            logger.info(f"clear_expired_mutes: total active mutes in DB: {len(all_mutes)}")
            for row in all_mutes:
                logger.info(f"  - user_id={row['user_id']}, guild_id={row['guild_id']}, mute_until={row['mute_until']}")

            # Use naive datetime for comparison (mute_until is stored as naive)
            current_time = datetime.now().replace(tzinfo=None)
            rows = await conn.fetch("""
                SELECT DISTINCT user_id, guild_id
                FROM warnings
                WHERE active = TRUE
                  AND mute_until IS NOT NULL
                  AND mute_until <= $1
            """, current_time)

            affected = [(row['user_id'], row['guild_id']) for row in rows]
            logger.info(f"clear_expired_mutes: found {len(affected)} expired mute records in DB")

            # Clear mute_until for expired mutes
            if affected:
                result = await conn.execute("""
                    UPDATE warnings
                    SET mute_until = NULL
                    WHERE active = TRUE
                      AND mute_until IS NOT NULL
                      AND mute_until <= $1
                """, current_time)
                logger.info(f"clear_expired_mutes: cleared mute_until for {result} records")

            return affected

    async def clear_expired_warns(self) -> List[int]:
        """Clear expired warnings and return affected user IDs."""
        if not self.enabled:
            return []

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Use naive datetime for comparison
            current_time = datetime.now().replace(tzinfo=None)
            rows = await conn.fetch("""
                DELETE FROM warnings
                WHERE active = TRUE
                  AND expires_at IS NOT NULL
                  AND expires_at <= $1
                RETURNING user_id, guild_id
            """, current_time)

            affected = [(row['user_id'], row['guild_id']) for row in rows]

            # Recalculate warn_numbers for affected users
            for user_id, guild_id in affected:
                await self.recalculate_warn_numbers(user_id, guild_id)

            return affected

    async def recalculate_warn_numbers(self, user_id: int, guild_id: int):
        """Recalculate warn_numbers for a user to keep them sequential."""
        if not self.enabled:
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Get all warnings for the user ordered by timestamp
            rows = await conn.fetch("""
                SELECT warn_id
                FROM warnings
                WHERE user_id = $1 AND guild_id = $2
                ORDER BY timestamp ASC
            """, user_id, guild_id)

            # Update warn_numbers sequentially
            for idx, row in enumerate(rows, start=1):
                await conn.execute("""
                    UPDATE warnings
                    SET warn_number = $1
                    WHERE warn_id = $2
                """, idx, row['warn_id'])


warn_store = WarnStore()
