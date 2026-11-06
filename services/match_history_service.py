"""Match history service."""

from __future__ import annotations

import logging
from typing import Optional

from models.match_history import MatchHistory
from storage.db import db_pool

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
        history = MatchHistory(
            tournament_id=tournament_id,
            match_id=match_id,
            player_id=player_id,
            player_name=player_name,
            kills=kills,
            deaths=deaths,
            won=won,
            elo_change=elo_change,
        )

        await MatchHistoryService._save_history(history)

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
