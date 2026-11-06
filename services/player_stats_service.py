"""Player statistics service."""

from __future__ import annotations

import logging
from typing import Optional

from models.player_stats import PlayerStats
from storage.db import db_pool

logger = logging.getLogger(__name__)


class PlayerStatsService:
    """Service for player statistics."""

    @staticmethod
    async def get_player_stats(user_id: int, guild_id: int) -> Optional[PlayerStats]:
        """Get player statistics."""
        # TODO: Implement database query
        return None

    @staticmethod
    async def update_player_stats(
        user_id: int,
        guild_id: int,
        kills: int = 0,
        deaths: int = 0,
        wins: int = 0,
        losses: int = 0,
        elo_change: int = 0,
    ) -> None:
        """Update player statistics."""
        stats = await PlayerStatsService.get_player_stats(user_id, guild_id)
        if not stats:
            stats = PlayerStats(
                user_id=user_id,
                guild_id=guild_id,
                username="",
            )

        # Update stats
        stats.total_kills += kills
        stats.total_deaths += deaths
        stats.total_wins += wins
        stats.total_losses += losses
        stats.elo += elo_change

        # Save to database
        await PlayerStatsService._save_player_stats(stats)

    @staticmethod
    async def _save_player_stats(stats: PlayerStats) -> None:
        """Save player stats to database."""
        # TODO: Implement PostgreSQL storage
        pass

    @staticmethod
    async def get_leaderboard(guild_id: int, limit: int = 10) -> list[PlayerStats]:
        """Get leaderboard."""
        # TODO: Implement database query
        return []
