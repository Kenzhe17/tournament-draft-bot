"""Tournament service for business logic."""

from __future__ import annotations

import logging
from typing import Optional

from core.models import (
    TournamentState,
    TournamentPhase,
    TournamentSize,
    FormationMode,
    RegistrationState,
    Team,
    Match,
)
from storage.db import db_pool

logger = logging.getLogger(__name__)


class TournamentService:
    """Service for tournament business logic."""

    @staticmethod
    async def create_tournament(
        guild_id: int,
        channel_id: int,
        size: TournamentSize = TournamentSize.EIGHT,
        formation_mode: FormationMode = FormationMode.MANUAL,
        organizer_id: int = 0,
    ) -> TournamentState:
        """Create a new tournament."""
        tournament_id = f"{guild_id}_{channel_id}_{int(time.time())}"

        tournament = TournamentState(
            tournament_id=tournament_id,
            guild_id=guild_id,
            channel_id=channel_id,
            size=size,
            formation_mode=formation_mode,
            organizer_id=organizer_id,
            created_at=time.time(),
        )

        # Save to database
        await TournamentService._save_tournament(tournament)

        logger.info("Created tournament %s in channel %s", tournament_id, channel_id)
        return tournament

    @staticmethod
    async def get_tournament(guild_id: int, channel_id: int) -> Optional[TournamentState]:
        """Get tournament by guild and channel."""
        # TODO: Implement database query
        return None

    @staticmethod
    async def update_tournament(tournament: TournamentState) -> None:
        """Update tournament state."""
        await TournamentService._save_tournament(tournament)

    @staticmethod
    async def delete_tournament(guild_id: int, channel_id: int) -> None:
        """Delete tournament."""
        # TODO: Implement database query
        pass

    @staticmethod
    async def _save_tournament(tournament: TournamentState) -> None:
        """Save tournament to database."""
        # TODO: Implement PostgreSQL storage
        pass

    @staticmethod
    def add_player(tournament: TournamentState, player_id: int, player_name: str) -> bool:
        """Add player to tournament."""
        if tournament.is_player_registered(player_id):
            return False

        tournament.player_ids.append(player_id)
        tournament.player_names[player_id] = player_name
        return True

    @staticmethod
    def remove_player(tournament: TournamentState, player_id: int) -> bool:
        """Remove player from tournament."""
        if not tournament.is_player_registered(player_id):
            return False

        tournament.player_ids.remove(player_id)
        tournament.player_names.pop(player_id, None)

        # Remove from team if in one
        team = tournament.get_team_by_player(player_id)
        if team:
            team.players.remove(player_id)
            if len(team.players) < 4:
                team.is_complete = False

        return True

    @staticmethod
    def create_teams(tournament: TournamentState, teams_data: list[dict]) -> None:
        """Create teams from data."""
        tournament.teams.clear()

        for i, team_data in enumerate(teams_data):
            team = Team(
                team_id=i + 1,
                name=team_data.get("name", f"Team {i + 1}"),
                players=team_data.get("players", []),
                is_complete=len(team_data.get("players", [])) == 4,
            )
            tournament.teams[team.team_id] = team

    @staticmethod
    def create_matches(tournament: TournamentState) -> None:
        """Create matches based on tournament phase."""
        tournament.matches.clear()

        if tournament.phase == TournamentPhase.QUALIFIERS:
            # Create qualifier matches
            team_ids = list(tournament.teams.keys())
            for i in range(0, len(team_ids), 2):
                if i + 1 < len(team_ids):
                    match_id = f"{tournament.tournament_id}_qualifier_{i // 2}"
                    match = Match(
                        match_id=match_id,
                        team1_id=team_ids[i],
                        team2_id=team_ids[i + 1],
                    )
                    tournament.matches[match_id] = match

        elif tournament.phase == TournamentPhase.SEMIFINALS:
            # Create semifinal matches from qualifier winners
            # TODO: Implement based on qualifier results
            pass

        elif tournament.phase == TournamentPhase.FINAL:
            # Create final match
            # TODO: Implement based on semifinal results
            pass

    @staticmethod
    def confirm_match_winner(
        tournament: TournamentState,
        match_id: str,
        winning_team_id: int,
    ) -> bool:
        """Confirm match winner and update tournament state."""
        match = tournament.matches.get(match_id)
        if not match or match.is_completed:
            return False

        match.winner_id = winning_team_id
        match.is_completed = True
        match.stats_submitted = True

        # Update tournament phase if needed
        # TODO: Implement phase transitions

        return True

    @staticmethod
    def get_active_tournaments(guild_id: int) -> list[TournamentState]:
        """Get all active tournaments for guild."""
        # TODO: Implement database query
        return []
