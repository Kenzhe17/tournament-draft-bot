"""Matchmaking service for MM system.

Wraps the legacy ``mm_search_store`` (JSON) and ``MMSearch`` model.
PostgreSQL persistence can be layered on later; the public API is
async so callers are already migration-ready.
"""

from __future__ import annotations

import logging
from typing import Optional

from models.mm_search import MMFormat, MMSearch
from storage.mm_search_store import mm_search_store

logger = logging.getLogger(__name__)


class MatchmakingService:
    """Service for matchmaking logic."""

    @staticmethod
    async def create_search(
        guild_id: int,
        channel_id: int,
        format_type: str = MMFormat.SQUAD.value,
    ) -> MMSearch:
        """Create a new MM search."""
        search = MMSearch(
            guild_id=guild_id,
            channel_id=channel_id,
            format=MMFormat(format_type),
        )
        mm_search_store.set(search)
        logger.info("Created MM search in channel %s", channel_id)
        return search

    @staticmethod
    async def get_search(guild_id: int, channel_id: int) -> Optional[MMSearch]:
        """Get MM search by guild and channel."""
        return mm_search_store.get(guild_id, channel_id)

    @staticmethod
    async def join_search(search: MMSearch, player_name: str, user_id: int) -> bool:
        """Add player to search."""
        added = search.add_player(player_name, user_id)
        if added:
            mm_search_store.set(search)
        return added

    @staticmethod
    async def leave_search(search: MMSearch, player_name: str) -> bool:
        """Remove player from search."""
        removed = search.remove_player(player_name)
        if removed:
            mm_search_store.set(search)
        return removed

    @staticmethod
    async def is_search_full(search: MMSearch) -> bool:
        """Check if search is full."""
        return search.is_full

    @staticmethod
    async def delete_search(guild_id: int, channel_id: int) -> None:
        """Delete search."""
        mm_search_store.delete(guild_id, channel_id)

    @staticmethod
    async def get_active_searches(guild_id: int) -> list[MMSearch]:
        """Get all active searches for guild."""
        return mm_search_store.get_by_guild(guild_id)
