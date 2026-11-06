"""Adapters for backward compatibility between old and new systems."""

from __future__ import annotations

import logging
from typing import Optional

from core.models import TournamentState, TournamentPhase, TournamentSize, FormationMode, RegistrationState, Team, Match
from models.tournament import Tournament as OldTournament
from services.tournament_service import TournamentService

logger = logging.getLogger(__name__)


class TournamentAdapter:
    """Adapter to convert between old Tournament and new TournamentState."""

    @staticmethod
    def old_to_new(old_tournament: OldTournament) -> TournamentState:
        """Convert old Tournament to new TournamentState."""
        # Map old phase to new phase
        phase_map = {
            "setup": TournamentPhase.SETUP,
            "draft": TournamentPhase.DRAFT,
            "teams": TournamentPhase.TEAMS,
            "qualifiers": TournamentPhase.QUALIFIERS,
            "semifinals": TournamentPhase.SEMIFINALS,
            "final": TournamentPhase.FINAL,
            "complete": TournamentPhase.COMPLETE,
        }

        # Map old size to new size
        size_map = {
            8: TournamentSize.EIGHT,
            16: TournamentSize.SIXTEEN,
            32: TournamentSize.THIRTY_TWO,
        }

        # Map old formation mode to new formation mode
        formation_map = {
            "manual": FormationMode.MANUAL,
            "random": FormationMode.RANDOM,
            "random_auto": FormationMode.RANDOM_AUTO,
            "draft": FormationMode.DRAFT,
        }

        # Map old registration state to new registration state
        reg_state_map = {
            "closed": RegistrationState.CLOSED,
            "open": RegistrationState.OPEN,
            "isolated": RegistrationState.ISOLATED,
        }

        new_tournament = TournamentState(
            tournament_id=f"{old_tournament.guild_id}_{old_tournament.channel_id}_{old_tournament.creation_time}",
            guild_id=old_tournament.guild_id,
            channel_id=old_tournament.channel_id,
            phase=phase_map.get(old_tournament.phase.value if hasattr(old_tournament.phase, 'value') else str(old_tournament.phase), TournamentPhase.SETUP),
            size=size_map.get(old_tournament.size.value if hasattr(old_tournament.size, 'value') else old_tournament.size, TournamentSize.EIGHT),
            formation_mode=formation_map.get(old_tournament.formation_mode.value if hasattr(old_tournament.formation_mode, 'value') else str(old_tournament.formation_mode), FormationMode.MANUAL),
            registration_state=reg_state_map.get(old_tournament.registration_state.value if hasattr(old_tournament.registration_state, 'value') else str(old_tournament.registration_state), RegistrationState.CLOSED),
            organizer_id=old_tournament.organizer_id,
            created_at=old_tournament.creation_time,
            is_active=old_tournament.phase != TournamentPhase.COMPLETE,
        )

        # Convert teams
        for team_id, team_data in old_tournament.teams.items():
            team = Team(
                team_id=team_id,
                name=team_data.get("name", f"Team {team_id}"),
                players=team_data.get("players", []),
                is_complete=len(team_data.get("players", [])) == 4,
            )
            new_tournament.teams[team_id] = team

        # Convert matches (simplified - old system has complex match structure)
        # This is a simplified conversion - full match conversion would be complex

        return new_tournament

    @staticmethod
    def new_to_old(new_tournament: TournamentState) -> OldTournament:
        """Convert new TournamentState to old Tournament."""
        # This is more complex as old Tournament has many more fields
        # For now, return None to indicate conversion not fully supported
        logger.warning("new_to_old conversion not fully implemented")
        return None


class StoreAdapter:
    """Adapter to make old store interface work with new services."""

    @staticmethod
    async def get(guild_id: int, channel_id: int) -> Optional[OldTournament]:
        """Get tournament using new service, convert to old format."""
        new_tournament = await TournamentService.get_tournament(guild_id, channel_id)
        if new_tournament:
            return TournamentAdapter.new_to_old(new_tournament)
        return None

    @staticmethod
    async def set(tournament: OldTournament) -> None:
        """Save tournament using new service."""
        new_tournament = TournamentAdapter.old_to_new(tournament)
        await TournamentService.update_tournament(new_tournament)

    @staticmethod
    async def delete(guild_id: int, channel_id: int) -> None:
        """Delete tournament using new service."""
        await TournamentService.delete_tournament(guild_id, channel_id)

    @staticmethod
    async def all() -> list[OldTournament]:
        """Get all tournaments."""
        # This would need to get all guilds and their tournaments
        # For now, return empty list
        return []

    @staticmethod
    async def get_by_guild(guild_id: int) -> list[OldTournament]:
        """Get tournaments by guild."""
        new_tournaments = await TournamentService.get_active_tournaments(guild_id)
        return [TournamentAdapter.new_to_old(t) for t in new_tournaments if TournamentAdapter.new_to_old(t)]
