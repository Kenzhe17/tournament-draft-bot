"""Core domain models for tournament system."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class TournamentPhase(enum.Enum):
    SETUP = "setup"
    DRAFT = "draft"
    TEAMS = "teams"
    QUALIFIERS = "qualifiers"
    SEMIFINALS = "semifinals"
    FINAL = "final"
    COMPLETE = "complete"


class TournamentSize(enum.Enum):
    EIGHT = 8
    SIXTEEN = 16
    THIRTY_TWO = 32


class FormationMode(enum.Enum):
    MANUAL = "manual"
    RANDOM = "random"
    RANDOM_AUTO = "random_auto"
    DRAFT = "draft"


class RegistrationState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    ISOLATED = "isolated"


@dataclass
class Team:
    """Team representation."""
    team_id: int
    name: str
    players: list[int] = field(default_factory=list)  # user IDs
    is_complete: bool = False


@dataclass
class Match:
    """Match representation."""
    match_id: str
    team1_id: int
    team2_id: int
    winner_id: Optional[int] = None
    is_completed: bool = False
    stats_submitted: bool = False


@dataclass
class TournamentState:
    """Simplified tournament state."""
    tournament_id: str
    guild_id: int
    channel_id: int
    phase: TournamentPhase = TournamentPhase.SETUP
    size: TournamentSize = TournamentSize.EIGHT
    formation_mode: FormationMode = FormationMode.MANUAL
    registration_state: RegistrationState = RegistrationState.CLOSED

    # Teams and matches
    teams: dict[int, Team] = field(default_factory=dict)
    matches: dict[str, Match] = field(default_factory=dict)

    # Player tracking
    player_ids: list[int] = field(default_factory=list)
    player_names: dict[int, str] = field(default_factory=dict)

    # Metadata
    organizer_id: int = 0
    created_at: float = 0.0
    is_active: bool = True

    def get_team_by_player(self, player_id: int) -> Optional[Team]:
        """Get team containing player."""
        for team in self.teams.values():
            if player_id in team.players:
                return team
        return None

    def get_team_name(self, team_id: int) -> str:
        """Get team name."""
        team = self.teams.get(team_id)
        return team.name if team else f"Team {team_id}"

    def is_player_registered(self, player_id: int) -> bool:
        """Check if player is registered."""
        return player_id in self.player_ids
