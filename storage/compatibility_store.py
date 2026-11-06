"""Compatibility store to bridge old and new systems during migration."""

from __future__ import annotations

import logging
from typing import Optional

from services.compatibility import StoreAdapter, TournamentAdapter
from models.tournament import Tournament

logger = logging.getLogger(__name__)


class CompatibilityStore:
    """Store that uses new services but provides old interface."""

    def __init__(self):
        self._cache: dict[tuple[int, int], Tournament] = {}

    async def get(self, guild_id: int, channel_id: int) -> Optional[Tournament]:
        """Get tournament (async version)."""
        # Check cache first
        cache_key = (guild_id, channel_id)
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Try to get from new service
        tournament = await StoreAdapter.get(guild_id, channel_id)
        if tournament:
            self._cache[cache_key] = tournament
        return tournament

    def get_sync(self, guild_id: int, channel_id: int) -> Optional[Tournament]:
        """Get tournament (sync version for backward compatibility)."""
        # This is a problem - old code expects sync, new is async
        # For now, return from cache only
        cache_key = (guild_id, channel_id)
        return self._cache.get(cache_key)

    async def set(self, tournament: Tournament) -> None:
        """Save tournament."""
        cache_key = (tournament.guild_id, tournament.channel_id)
        self._cache[cache_key] = tournament
        await StoreAdapter.set(tournament)

    def set_sync(self, tournament: Tournament) -> None:
        """Save tournament (sync version)."""
        cache_key = (tournament.guild_id, tournament.channel_id)
        self._cache[cache_key] = tournament
        # Can't call async from sync, just cache for now

    async def delete(self, guild_id: int, channel_id: int) -> None:
        """Delete tournament."""
        cache_key = (guild_id, channel_id)
        self._cache.pop(cache_key, None)
        await StoreAdapter.delete(guild_id, channel_id)

    def delete_sync(self, guild_id: int, channel_id: int) -> None:
        """Delete tournament (sync version)."""
        cache_key = (guild_id, channel_id)
        self._cache.pop(cache_key, None)

    async def all(self) -> list[Tournament]:
        """Get all tournaments."""
        return list(self._cache.values())

    def all_sync(self) -> list[Tournament]:
        """Get all tournaments (sync version)."""
        return list(self._cache.values())

    async def get_by_guild(self, guild_id: int) -> list[Tournament]:
        """Get tournaments by guild."""
        return [t for t in self._cache.values() if t.guild_id == guild_id]

    def get_by_guild_sync(self, guild_id: int) -> list[Tournament]:
        """Get tournaments by guild (sync version)."""
        return [t for t in self._cache.values() if t.guild_id == guild_id]


# Global instance for compatibility
compatibility_store = CompatibilityStore()
