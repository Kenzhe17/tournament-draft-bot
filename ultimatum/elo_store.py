"""Elo Rating Store for PostgreSQL."""

import logging
from datetime import datetime
from typing import Optional, List

import asyncpg

from storage.db import get_pool
from ultimatum.elo_calculator import EloCalculator, EloResult

logger = logging.getLogger(__name__)


class EloStore:
    """Store for Elo ratings."""

    def __init__(self):
        self.calculator = EloCalculator()

    async def get_or_create_player(self, discord_id: int, username: str) -> dict:
        """Get or create player Elo record."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Try to get existing
            row = await conn.fetchrow(
                "SELECT * FROM elo_ratings WHERE discord_id = $1",
                discord_id
            )

            if row:
                return dict(row)

            # Create new player
            await conn.execute(
                """
                INSERT INTO elo_ratings (discord_id, username, elo, peak_elo, rated_matches, wins, losses)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                discord_id, username,
                self.calculator.STARTING_ELO,
                self.calculator.STARTING_ELO,
                0, 0, 0
            )

            # Return new record
            row = await conn.fetchrow(
                "SELECT * FROM elo_ratings WHERE discord_id = $1",
                discord_id
            )
            return dict(row)

    async def get_player(self, discord_id: int) -> Optional[dict]:
        """Get player Elo record."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM elo_ratings WHERE discord_id = $1",
                discord_id
            )
            return dict(row) if row else None

    async def get_leaderboard(self, limit: int = 10, offset: int = 0) -> List[dict]:
        """Get global leaderboard."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM elo_ratings
                ORDER BY elo DESC
                LIMIT $1 OFFSET $2
                """,
                limit, offset
            )
            return [dict(row) for row in rows]

    async def get_leaderboard_count(self) -> int:
        """Get total number of rated players."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM elo_ratings")

    async def update_elo(
        self,
        match_id: int,
        tournament_id: str,
        winner_id: int,
        loser_id: int,
        winner_username: str,
        loser_username: str
    ) -> EloResult:
        """
        Update Elo ratings after a match.

        Returns EloResult with changes.
        """
        pool = await get_pool()

        async with pool.acquire() as conn:
            async with conn.transaction():
                # Advisory lock по match_id сериализует конкурентные
                # начисления ELO за один матч.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"ult_elo_match:{match_id}",
                )
                # ELO для этого матча уже начислялось — повтор не применяем.
                already = await conn.fetchval(
                    "SELECT COUNT(*) FROM elo_history WHERE match_id = $1",
                    match_id,
                )
                if already:
                    logger.info(
                        f"Elo already recorded for match {match_id}, skipping"
                    )
                    return EloResult(
                        winner_new_elo=0, loser_new_elo=0,
                        winner_change=0, loser_change=0,
                        winner_expected=0.0, loser_expected=0.0,
                        k_factor=0,
                    )

                # Get or create both players
                winner = await self.get_or_create_player(winner_id, winner_username)
                loser = await self.get_or_create_player(loser_id, loser_username)

                # Calculate Elo changes
                result = self.calculator.calculate(
                    winner_elo=winner['elo'],
                    loser_elo=loser['elo'],
                    winner_matches=winner['rated_matches'],
                    loser_matches=loser['rated_matches']
                )

                # Update winner
                new_winner_elo = result.winner_new_elo
                new_winner_peak = max(new_winner_elo, winner['peak_elo'])
                await conn.execute(
                    """
                    UPDATE elo_ratings
                    SET elo = $1, peak_elo = $2, rated_matches = rated_matches + 1,
                        wins = wins + 1, updated_at = NOW()
                    WHERE discord_id = $3
                    """,
                    new_winner_elo, new_winner_peak, winner_id
                )

                # Update loser
                new_loser_elo = result.loser_new_elo
                new_loser_peak = max(new_loser_elo, loser['peak_elo'])
                await conn.execute(
                    """
                    UPDATE elo_ratings
                    SET elo = $1, peak_elo = $2, rated_matches = rated_matches + 1,
                        losses = losses + 1, updated_at = NOW()
                    WHERE discord_id = $3
                    """,
                    new_loser_elo, new_loser_peak, loser_id
                )

                # Record history for winner
                await conn.execute(
                    """
                    INSERT INTO elo_history
                    (match_id, tournament_id, player_id, opponent_id, elo_before, elo_after,
                     elo_change, opponent_elo_before, expected_score, k_factor, result)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    """,
                    match_id, tournament_id, winner_id, loser_id,
                    winner['elo'], new_winner_elo, result.winner_change,
                    loser['elo'], result.winner_expected, result.k_factor, 'WIN'
                )

                # Record history for loser
                await conn.execute(
                    """
                    INSERT INTO elo_history
                    (match_id, tournament_id, player_id, opponent_id, elo_before, elo_after,
                     elo_change, opponent_elo_before, expected_score, k_factor, result)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    """,
                    match_id, tournament_id, loser_id, winner_id,
                    loser['elo'], new_loser_elo, result.loser_change,
                    winner['elo'], result.loser_expected, result.k_factor, 'LOSS'
                )

                logger.info(
                    f"Elo updated: {winner_username} {winner['elo']} -> {new_winner_elo} "
                    f"({result.winner_change:+d}), {loser_username} {loser['elo']} -> {new_loser_elo} "
                    f"({result.loser_change:+d})"
                )

                return result

    async def get_player_history(self, discord_id: int, limit: int = 10) -> List[dict]:
        """Get player's Elo history."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT eh.*, er.username as opponent_name
                FROM elo_history eh
                LEFT JOIN elo_ratings er ON eh.opponent_id = er.discord_id
                WHERE eh.player_id = $1
                ORDER BY eh.created_at DESC
                LIMIT $2
                """,
                discord_id, limit
            )
            return [dict(row) for row in rows]

    async def revert_match(
        self,
        match_id: int,
        tournament_id: str,
        winner_id: int,
        loser_id: int
    ) -> None:
        """
        Revert Elo changes for a match.

        This is complex - need to recalculate all subsequent matches.
        For now, this is a placeholder.
        """
        # TODO: Implement full recalculation
        # This requires:
        # 1. Find original history entries
        # 2. Restore original ratings
        # 3. Recalculate all subsequent matches chronologically
        # 4. Update all affected history entries
        logger.warning(f"Elo revert not fully implemented for match {match_id}")


# Global store instance
elo_store = EloStore()
