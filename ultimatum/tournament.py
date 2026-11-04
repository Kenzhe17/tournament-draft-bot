"""Tournament logic for Ultimatum Solo."""

import logging
from typing import List, Optional

from ultimatum.models import (
    Tournament, Player, Match, Round,
    TournamentStatus, PlayerStatus, MatchStatus, MatchType, RoundStatus
)
from ultimatum.store import UltimatumStore
from ultimatum.matchmaking import Matchmaker
from ultimatum.elo_store import elo_store

logger = logging.getLogger(__name__)


class UltimatumTournament:
    """Main tournament logic controller."""

    def __init__(self, store: UltimatumStore):
        self.store = store
        self.matchmaker = Matchmaker(store)

    async def start_tournament(self, tournament_id: str) -> None:
        """Start tournament after registration is complete."""
        tournament = await self.store.get_tournament(tournament_id)
        if not tournament:
            raise ValueError("Tournament not found")

        players = await self.store.get_players(tournament_id)
        if len(players) != 16:
            raise ValueError(f"Need exactly 16 players, got {len(players)}")

        # Update tournament status
        await self.store.update_tournament_status(
            tournament_id, TournamentStatus.IN_PROGRESS, current_round=1
        )

        # Create first round
        await self.create_round(tournament_id, 1)

        logger.info(f"Tournament {tournament_id} started with {len(players)} players")

    async def create_round(self, tournament_id: str, round_number: int) -> None:
        """Create a new round with matches."""
        tournament = await self.store.get_tournament(tournament_id)
        if not tournament:
            raise ValueError("Tournament not found")

        # Get active players
        players = await self.store.get_active_players(tournament_id)

        if len(players) < 2:
            logger.warning(f"Not enough active players for round {round_number}: {len(players)}")
            return

        # Check for grand final condition
        if await self._should_create_grand_final(tournament_id, players):
            await self._create_grand_final(tournament_id, players)
            return

        # Create round in database
        await self.store.create_round(tournament_id, round_number)

        # Create matches using matchmaking
        player_pairs = await self.matchmaker.create_matches(
            tournament_id, players, round_number
        )

        # Create match records
        for p1_id, p2_id in player_pairs:
            await self.store.create_match(
                tournament_id, round_number, p1_id, p2_id, MatchType.NORMAL
            )
            # Add to match history
            await self.store.add_match_history(tournament_id, p1_id, p2_id, round_number)

        # Update round status
        await self.store.update_round_status(
            tournament_id, round_number, RoundStatus.IN_PROGRESS
        )

        # Update tournament status
        await self.store.update_tournament_status(
            tournament_id, TournamentStatus.ROUND_IN_PROGRESS, round_number
        )

        logger.info(f"Round {round_number} created with {len(player_pairs)} matches")

    async def complete_match(
        self,
        match_id: int,
        winner_id: int,
        loser_id: int
    ) -> None:
        """Complete a match and update player stats."""
        # Get match details
        match = await self.store.get_match_by_id(match_id)
        if not match:
            raise ValueError("Match not found")

        # Update match in database
        await self.store.complete_match(match_id, winner_id, loser_id)

        # Get players
        winner = await self.store.get_player_by_discord_id(match.tournament_id, winner_id)
        loser = await self.store.get_player_by_discord_id(match.tournament_id, loser_id)

        if not winner or not loser:
            raise ValueError("Player not found")

        # Update player stats (wins/losses)
        await self.store.update_player_stats(
            match.tournament_id,
            winner_id,
            winner.wins + 1,
            winner.losses
        )

        await self.store.update_player_stats(
            match.tournament_id,
            loser_id,
            loser.wins,
            loser.losses + 1
        )

        # Check if loser should be eliminated (2 losses)
        if loser.losses + 1 >= 2:
            await self.eliminate_player(match.tournament_id, loser_id)

        # Update Elo ratings (only for normal matches, not for byes)
        if match.match_type == MatchType.NORMAL:
            try:
                await elo_store.update_elo(
                    match_id,
                    match.tournament_id,
                    winner_id,
                    loser_id,
                    winner.username,
                    loser.username
                )
                logger.info(f"Elo updated for match {match_id}")
            except Exception as e:
                logger.error(f"Failed to update Elo: {e}")

        # Check if round is complete
        await self._check_round_completion(match.tournament_id, match.round_number)

    async def _check_round_completion(self, tournament_id: str, round_number: int) -> None:
        """Check if all matches in round are complete and start next round."""
        matches = await self.store.get_matches(tournament_id, round_number)

        # Check if all matches are completed
        all_complete = all(m.status == MatchStatus.COMPLETED for m in matches)

        if not all_complete:
            return

        # Mark round as completed
        await self.store.update_round_status(
            tournament_id, round_number, RoundStatus.COMPLETED
        )

        # Update tournament status
        await self.store.update_tournament_status(
            tournament_id, TournamentStatus.ROUND_COMPLETED
        )

        logger.info(f"Round {round_number} completed for tournament {tournament_id}")

        # Start next round automatically
        next_round = round_number + 1
        await self.create_round(tournament_id, next_round)

    async def _should_create_grand_final(
        self,
        tournament_id: str,
        players: List[Player]
    ) -> bool:
        """Check if grand final conditions are met."""
        # Grand final when:
        # - One player has 4-0 (direct pass)
        # - One player emerges from 3-1 bracket
        # - Only 2 active players remain

        if len(players) != 2:
            return False

        # Check if one has 4-0
        has_4_0 = any(p.wins == 4 and p.losses == 0 for p in players)
        if has_4_0:
            return True

        return False

    async def _create_grand_final(
        self,
        tournament_id: str,
        players: List[Player]
    ) -> None:
        """Create grand final match."""
        player1, player2 = players[0], players[1]

        # Set grand finalist status
        await self.store.update_player_stats(
            tournament_id, player1.discord_id, player1.wins, player1.losses,
            PlayerStatus.GRAND_FINALIST
        )
        await self.store.update_player_stats(
            tournament_id, player2.discord_id, player2.wins, player2.losses,
            PlayerStatus.GRAND_FINALIST
        )

        # Create grand final match
        await self.store.create_match(
            tournament_id, 999, player1.discord_id, player2.discord_id, MatchType.GRAND_FINAL
        )

        # Update tournament status
        await self.store.update_tournament_status(
            tournament_id, TournamentStatus.GRAND_FINAL
        )

        logger.info(f"Grand final created: {player1.username} vs {player2.username}")

    async def eliminate_player(
        self,
        tournament_id: str,
        discord_id: int
    ) -> None:
        """Eliminate a player (2 losses)."""
        await self.store.update_player_stats(
            tournament_id, discord_id, None, None, PlayerStatus.ELIMINATED
        )
        logger.info(f"Player {discord_id} eliminated from tournament {tournament_id}")

    async def finalize_tournament(self, tournament_id: str) -> None:
        """Finalize tournament and calculate rankings."""
        # Get all players
        players = await self.store.get_players(tournament_id)

        # Sort by wins, then by fewer losses, then by elimination round
        # This is simplified - real implementation needs more sophisticated ranking
        sorted_players = sorted(
            players,
            key=lambda p: (-p.wins, p.losses if p.losses else 0)
        )

        # Assign places
        for idx, player in enumerate(sorted_players):
            place = idx + 1
            await self.store.update_player_stats(
                tournament_id, player.discord_id, player.wins, player.losses, player.status
            )
            # Update final_place in database (need to add this to store)

        # Update tournament status
        await self.store.update_tournament_status(
            tournament_id, TournamentStatus.COMPLETED
        )

        logger.info(f"Tournament {tournament_id} finalized")
