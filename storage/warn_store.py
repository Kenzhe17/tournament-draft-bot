"""Warning system storage."""

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from config import MUTE_ROLE_ID
from storage.db import get_pool

logger = logging.getLogger(__name__)


class Warn:
    """Warning data model."""

    def __init__(self, warn_id: int, user_id: int, guild_id: int,
                 timestamp: datetime, reason: str, active: bool = True, moderator_id: int = None, expires_at: datetime = None):
        self.warn_id = warn_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.timestamp = timestamp
        self.reason = reason
        self.active = active
        self.moderator_id = moderator_id
        self.expires_at = expires_at


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
                    expires_at TIMESTAMP
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

            logger.info("Warnings table initialized")
    
    async def add_warn(self, user_id: int, guild_id: int,
                      moderator_id: int, reason: str = "Без причины") -> Warn:
        """Add a warning to the database."""
        if not self.enabled:
            logger.warning("WarnStore: Database not enabled, cannot add warning")
            return None

        pool = await get_pool()
        async with pool.acquire() as conn:
            expires_at = datetime.utcnow() + timedelta(days=7)
            warn_id = await conn.fetchval("""
                INSERT INTO warnings (user_id, guild_id, moderator_id, reason, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING warn_id
            """, user_id, guild_id, moderator_id, reason, expires_at)

            timestamp = datetime.utcnow()
            return Warn(warn_id, user_id, guild_id, timestamp, reason, True, moderator_id, expires_at)
    
    async def get_active_warns(self, user_id: int, guild_id: int) -> List[Warn]:
        """Get all active warnings for a user."""
        if not self.enabled:
            return []

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT warn_id, user_id, guild_id, timestamp, reason, active, moderator_id, expires_at
                FROM warnings
                WHERE user_id = $1 AND guild_id = $2 AND active = TRUE AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                ORDER BY timestamp DESC
            """, user_id, guild_id)

            return [Warn(row['warn_id'], row['user_id'], row['guild_id'],
                        row['timestamp'], row['reason'], row['active'], row['moderator_id'], row['expires_at']) for row in rows]
    
    async def get_all_warns(self, user_id: int, guild_id: int) -> List[Warn]:
        """Get all warnings for a user (including inactive)."""
        if not self.enabled:
            return []

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT warn_id, user_id, guild_id, timestamp, reason, active, moderator_id, expires_at
                FROM warnings
                WHERE user_id = $1 AND guild_id = $2
                ORDER BY timestamp DESC
            """, user_id, guild_id)

            return [Warn(row['warn_id'], row['user_id'], row['guild_id'],
                        row['timestamp'], row['reason'], row['active'], row['moderator_id'], row['expires_at']) for row in rows]
    
    async def remove_last_warn(self, user_id: int, guild_id: int) -> Optional[Warn]:
        """Remove the most recent active warning."""
        if not self.enabled:
            return None

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE warnings
                SET active = FALSE
                WHERE warn_id = (
                    SELECT warn_id FROM warnings
                    WHERE user_id = $1 AND guild_id = $2 AND active = TRUE
                    ORDER BY timestamp DESC
                    LIMIT 1
                )
                RETURNING warn_id, user_id, guild_id, timestamp, reason, active, moderator_id, expires_at
            """, user_id, guild_id)

            if row:
                return Warn(row['warn_id'], row['user_id'], row['guild_id'],
                           row['timestamp'], row['reason'], row['active'], row['moderator_id'], row['expires_at'])
            return None
    
    async def set_mute_until(self, user_id: int, guild_id: int, mute_until: datetime):
        """Set mute expiration time for a user."""
        if not self.enabled:
            return
        
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE warnings
                SET mute_until = $1
                WHERE user_id = $2 AND guild_id = $3 AND active = TRUE
            """, mute_until, user_id, guild_id)
    
    async def get_mute_until(self, user_id: int, guild_id: int) -> Optional[datetime]:
        """Get the earliest mute expiration time for a user."""
        if not self.enabled:
            return None
        
        pool = await get_pool()
        async with pool.acquire() as conn:
            mute_until = await conn.fetchval("""
                SELECT MIN(mute_until)
                FROM warnings
                WHERE user_id = $1 AND guild_id = $2 AND active = TRUE
                  AND mute_until > CURRENT_TIMESTAMP
            """, user_id, guild_id)
            
            return mute_until
    
    async def clear_expired_mutes(self) -> List[int]:
        """Get users with expired mutes."""
        if not self.enabled:
            return []

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT user_id, guild_id
                FROM warnings
                WHERE active = TRUE
                  AND mute_until IS NOT NULL
                  AND mute_until <= CURRENT_TIMESTAMP
            """)

            return [(row['user_id'], row['guild_id']) for row in rows]

    async def clear_expired_warns(self) -> List[int]:
        """Clear expired warnings and return affected user IDs."""
        if not self.enabled:
            return []

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                UPDATE warnings
                SET active = FALSE
                WHERE active = TRUE
                  AND expires_at IS NOT NULL
                  AND expires_at <= CURRENT_TIMESTAMP
                RETURNING user_id, guild_id
            """)

            return [(row['user_id'], row['guild_id']) for row in rows]


warn_store = WarnStore()
