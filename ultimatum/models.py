"""Data models for Ultimatum Solo Tournament System."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional
from datetime import datetime


class TournamentStatus(str, Enum):
    """Tournament status."""
    DRAFT = "DRAFT"
    REGISTRATION = "REGISTRATION"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    ROUND_IN_PROGRESS = "ROUND_IN_PROGRESS"
    ROUND_COMPLETED = "ROUND_COMPLETED"
    GRAND_FINAL = "GRAND_FINAL"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class PlayerStatus(str, Enum):
    """Player status."""
    ACTIVE = "ACTIVE"
    ELIMINATED = "ELIMINATED"
    GRAND_FINALIST = "GRAND_FINALIST"
    CHAMPION = "CHAMPION"
    GRAND_FINAL_LOSER = "GRAND_FINAL_LOSER"


class MatchStatus(str, Enum):
    """Match status."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class MatchType(str, Enum):
    """Match type."""
    NORMAL = "NORMAL"
    GRAND_FINAL = "GRAND_FINAL"


class RoundStatus(str, Enum):
    """Round status."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


@dataclass
class Vote:
    """Vote for match winner."""
    id: str
    match_id: int
    user_id: int
    voted_winner_id: int
    created_at: datetime


@dataclass
class Tournament:
    """Tournament model."""
    id: str
    name: str
    guild_id: int
    status: TournamentStatus = TournamentStatus.DRAFT
    current_round: int = 0
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_by: int = 0
    is_open: bool = False  # If False, only players with r!z3 tag can join

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "guild_id": self.guild_id,
            "status": self.status.value,
            "current_round": self.current_round,
            "is_open": self.is_open,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Tournament":
        """Create from dictionary."""
        return cls(
            id=data["id"],
            name=data["name"],
            guild_id=data["guild_id"],
            status=TournamentStatus(data["status"]),
            current_round=data["current_round"],
            is_open=data.get("is_open", False),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
            created_by=data["created_by"],
        )


@dataclass
class Player:
    """Player model."""
    id: Optional[int] = None  # Database ID
    tournament_id: str = ""
    discord_id: int = 0
    username: str = ""
    wins: int = 0
    losses: int = 0
    status: PlayerStatus = PlayerStatus.ACTIVE
    final_place: Optional[int] = None
    elimination_round: Optional[int] = None  # Round when player was eliminated
    created_at: Optional[datetime] = None

    @property
    def record(self) -> str:
        """Get win-loss record."""
        return f"{self.wins}-{self.losses}"

    @property
    def is_eliminated(self) -> bool:
        """Check if player is eliminated."""
        return self.losses >= 2

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "tournament_id": self.tournament_id,
            "discord_id": self.discord_id,
            "username": self.username,
            "wins": self.wins,
            "losses": self.losses,
            "status": self.status.value,
            "final_place": self.final_place,
            "elimination_round": self.elimination_round,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Player":
        """Create from dictionary."""
        return cls(
            id=data.get("id"),
            tournament_id=data["tournament_id"],
            discord_id=data["discord_id"],
            username=data["username"],
            wins=data["wins"],
            losses=data["losses"],
            status=PlayerStatus(data["status"]),
            final_place=data.get("final_place"),
            elimination_round=data.get("elimination_round"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
        )


@dataclass
class Match:
    """Match model."""
    id: Optional[int] = None  # Database ID
    tournament_id: str = ""
    round_number: int = 0
    player1_id: int = 0
    player2_id: int = 0
    winner_id: Optional[int] = None
    loser_id: Optional[int] = None
    status: MatchStatus = MatchStatus.PENDING
    match_type: MatchType = MatchType.NORMAL
    room_id: Optional[str] = None
    room_password: Optional[str] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "tournament_id": self.tournament_id,
            "round_number": self.round_number,
            "player1_id": self.player1_id,
            "player2_id": self.player2_id,
            "winner_id": self.winner_id,
            "loser_id": self.loser_id,
            "status": self.status.value,
            "match_type": self.match_type.value,
            "room_id": self.room_id,
            "room_password": self.room_password,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Match":
        """Create from dictionary."""
        return cls(
            id=data.get("id"),
            tournament_id=data["tournament_id"],
            round_number=data["round_number"],
            player1_id=data["player1_id"],
            player2_id=data["player2_id"],
            winner_id=data.get("winner_id"),
            loser_id=data.get("loser_id"),
            status=MatchStatus(data["status"]),
            match_type=MatchType(data["match_type"]),
            room_id=data.get("room_id"),
            room_password=data.get("room_password"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
        )


@dataclass
class Round:
    """Round model."""
    id: Optional[int] = None  # Database ID
    tournament_id: str = ""
    round_number: int = 0
    status: RoundStatus = RoundStatus.PENDING
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "tournament_id": self.tournament_id,
            "round_number": self.round_number,
            "status": self.status.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Round":
        """Create from dictionary."""
        return cls(
            id=data.get("id"),
            tournament_id=data["tournament_id"],
            round_number=data["round_number"],
            status=RoundStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
        )


@dataclass
class Ranking:
    """Final ranking model."""
    id: Optional[int] = None  # Database ID
    tournament_id: str = ""
    player_id: int = 0
    final_place: int = 0
    rank_group: Optional[str] = None
    finalized: bool = False
    created_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "tournament_id": self.tournament_id,
            "player_id": self.player_id,
            "final_place": self.final_place,
            "rank_group": self.rank_group,
            "finalized": self.finalized,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Ranking":
        """Create from dictionary."""
        return cls(
            id=data.get("id"),
            tournament_id=data["tournament_id"],
            player_id=data["player_id"],
            final_place=data["final_place"],
            rank_group=data.get("rank_group"),
            finalized=data["finalized"],
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
        )


@dataclass
class MatchHistory:
    """Match history for avoiding rematches."""
    id: Optional[int] = None  # Database ID
    tournament_id: str = ""
    player1_id: int = 0
    player2_id: int = 0
    round_number: int = 0
    created_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "tournament_id": self.tournament_id,
            "player1_id": self.player1_id,
            "player2_id": self.player2_id,
            "round_number": self.round_number,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MatchHistory":
        """Create from dictionary."""
        return cls(
            id=data.get("id"),
            tournament_id=data["tournament_id"],
            player1_id=data["player1_id"],
            player2_id=data["player2_id"],
            round_number=data["round_number"],
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
        )


@dataclass
class AuditLog:
    """Audit log entry."""
    id: Optional[int] = None  # Database ID
    tournament_id: str = ""
    actor_discord_id: int = 0
    action: str = ""
    details: Optional[dict] = None
    created_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "tournament_id": self.tournament_id,
            "actor_discord_id": self.actor_discord_id,
            "action": self.action,
            "details": self.details,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuditLog":
        """Create from dictionary."""
        return cls(
            id=data.get("id"),
            tournament_id=data["tournament_id"],
            actor_discord_id=data["actor_discord_id"],
            action=data["action"],
            details=data.get("details"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
        )
