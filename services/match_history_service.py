"""Match history service."""

from __future__ import annotations

import logging
from typing import Optional

from models.match_history import MatchHistory
from storage.db import get_pool

logger = logging.getLogger(__name__)


class MatchHistoryService:
    """Service for match history."""

    @staticmethod
    async def save_match_history(
        tournament_id: str,
        match_id: str,
        player_id: int,
        player_name: str,
        kills: int,
        deaths: int,
        won: bool,
        elo_change: int,
    ) -> None:
        """Save match history for player."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tourney_match_history (
                    tournament_id, match_id, player_id, player_name,
                    kills, deaths, won, elo_change
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                tournament_id,
                match_id,
                player_id,
                player_name,
                kills,
                deaths,
                won,
                elo_change,
            )

    @staticmethod
    async def get_player_history(player_id: int, limit: int = 10) -> list[MatchHistory]:
        """Get player match history."""
        # TODO: Implement database query
        return []

    @staticmethod
    async def _save_history(history: MatchHistory) -> None:
        """Save history to database."""
        # TODO: Implement PostgreSQL storage
        pass
