"""Matchmaking service for MM system."""

from __future__ import annotations

import logging
from typing import Optional

from models.mm_search import MMSearch
from storage.db import db_pool

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
        search = MMSearch(
            search_id=f"{guild_id}_{channel_id}_{int(time.time())}",
            guild_id=guild_id,
            channel_id=channel_id,
            format_type=format_type,
            organizer_id=organizer_id,
        )

        await MatchmakingService._save_search(search)
        logger.info("Created MM search %s in channel %s", search.search_id, channel_id)
        return search

    @staticmethod
    async def get_search(guild_id: int, channel_id: int) -> Optional[MMSearch]:
        """Get MM search by guild and channel."""
        # TODO: Implement database query
        return None

    @staticmethod
    async def join_search(search: MMSearch, player_id: int, player_name: str) -> bool:
        """Add player to search."""
        if player_id in search.player_ids:
            return False

        if len(search.player_ids) >= search.required_players:
            return False

        search.player_ids.append(player_id)
        search.player_names[player_id] = player_name
        search.joined_at[player_id] = time.time()

        await MatchmakingService._save_search(search)
        return True

    @staticmethod
    async def leave_search(search: MMSearch, player_id: int) -> bool:
        """Remove player from search."""
        if player_id not in search.player_ids:
            return False

        search.player_ids.remove(player_id)
        search.player_names.pop(player_id, None)
        search.joined_at.pop(player_id, None)

        await MatchmakingService._save_search(search)
        return True

    @staticmethod
    async def is_search_full(search: MMSearch) -> bool:
        """Check if search is full."""
        return len(search.player_ids) >= search.required_players

    @staticmethod
    async def get_active_searches(guild_id: int) -> list[MMSearch]:
        """Get all active searches for guild."""
        # TODO: Implement database query
        return []

    @staticmethod
    async def _save_search(search: MMSearch) -> None:
        """Save search to database."""
        # TODO: Implement PostgreSQL storage
        pass
