"""Matchmaking service for MM system."""

from __future__ import annotations

import logging
import time
from typing import Optional

from models.mm_search import MMSearch
from storage.db import get_pool

logger = logging.getLogger(__name__)


class MatchmakingService:
    """Service for matchmaking logic."""

    @staticmethod
    async def create_search(
        guild_id: int,
        channel_id: int,
        format_type: str,
        organizer_id: int = 0,
    ) -> MMSearch:
        """Create a new MM search."""
        search_id = f"{guild_id}_{channel_id}_{int(time.time())}"

        search = MMSearch(
            search_id=search_id,
            guild_id=guild_id,
            channel_id=channel_id,
            format_type=format_type,
            organizer_id=organizer_id,
            created_at=time.time(),
        )

        # Save to database
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO mm_searches (
                    search_id, guild_id, channel_id, format_type,
                    organizer_id, created_at, is_active
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                search.search_id,
                search.guild_id,
                search.channel_id,
                search.format_type,
                search.organizer_id,
                search.created_at,
                search.is_active,
            )

        logger.info("Created MM search %s in channel %s", search.search_id, channel_id)
        return search

    @staticmethod
    async def get_search(guild_id: int, channel_id: int) -> Optional[MMSearch]:
        """Get MM search by guild and channel."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM mm_searches WHERE guild_id = $1 AND channel_id = $2 AND is_active = TRUE",
                guild_id, channel_id
            )

            if not row:
                return None

            # Load players
            player_rows = await conn.fetch(
                "SELECT player_id, player_name FROM mm_search_players WHERE search_id = $1",
                row["search_id"]
            )

            search = MMSearch(
                search_id=row["search_id"],
                guild_id=row["guild_id"],
                channel_id=row["channel_id"],
                format_type=row["format_type"],
                organizer_id=row["organizer_id"],
                created_at=row["created_at"],
                is_active=row["is_active"],
            )

            for player_row in player_rows:
                search.player_ids.append(player_row["player_id"])
                search.player_names[player_row["player_id"]] = player_row["player_name"]

            return search

    @staticmethod
    async def join_search(search: MMSearch, player_id: int, player_name: str) -> bool:
        """Add player to search."""
        if player_id in search.player_ids:
            return False

        if len(search.player_ids) >= search.required_players:
            return False

        # Save to database
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO mm_search_players (search_id, player_id, player_name, joined_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (search_id, player_id) DO NOTHING
                """,
                search.search_id,
                player_id,
                player_name,
                time.time(),
            )

        search.player_ids.append(player_id)
        search.player_names[player_id] = player_name
        search.joined_at[player_id] = time.time()

        return True

    @staticmethod
    async def leave_search(search: MMSearch, player_id: int) -> bool:
        """Remove player from search."""
        if player_id not in search.player_ids:
            return False

        # Remove from database
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM mm_search_players WHERE search_id = $1 AND player_id = $2",
                search.search_id, player_id
            )

        search.player_ids.remove(player_id)
        search.player_names.pop(player_id, None)
        search.joined_at.pop(player_id, None)

        return True

    @staticmethod
    async def is_search_full(search: MMSearch) -> bool:
        """Check if search is full."""
        return len(search.player_ids) >= search.required_players

    @staticmethod
    async def get_active_searches(guild_id: int) -> list[MMSearch]:
        """Get all active searches for guild."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT search_id, channel_id FROM mm_searches WHERE guild_id = $1 AND is_active = TRUE",
                guild_id
            )

            searches = []
            for row in rows:
                search = await MatchmakingService.get_search(guild_id, row["channel_id"])
                if search:
                    searches.append(search)

            return searches
