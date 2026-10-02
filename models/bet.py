"""Data models for betting system."""

from dataclasses import dataclass
from typing import Any


@dataclass
class Bet:
    """Represents a bet on a match."""
    guild_id: int
    user_id: int
    user_name: str
    match_id: str
    team_name: str
    team_index: int  # Индекс команды (0 или 1)
    amount: int
    odds: float  # Odds at the time of betting (fixed for payout)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "match_id": self.match_id,
            "team_name": self.team_name,
            "team_index": self.team_index,
            "amount": self.amount,
            "odds": self.odds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Bet":
        """Deserialize from dictionary."""
        return cls(
            guild_id=data.get("guild_id", 0),
            user_id=data.get("user_id", 0),
            user_name=data.get("user_name", "Unknown"),
            match_id=data.get("match_id", ""),
            team_name=data.get("team_name", ""),
            team_index=data.get("team_index", 0),
            amount=data.get("amount", 0),
            odds=data.get("odds", 1.9),
        )


@dataclass
class MatchOdds:
    """Represents current odds and buffer for a match."""
    team_a_name: str
    team_b_name: str
    team_a_odds: float
    team_b_odds: float
    team_a_buffer: int = 0  # Buffer when odds hit floor
    team_b_buffer: int = 0  # Buffer when odds hit floor

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "team_a_name": self.team_a_name,
            "team_b_name": self.team_b_name,
            "team_a_odds": self.team_a_odds,
            "team_b_odds": self.team_b_odds,
            "team_a_buffer": self.team_a_buffer,
            "team_b_buffer": self.team_b_buffer,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MatchOdds":
        """Deserialize from dictionary."""
        return cls(
            team_a_name=data.get("team_a_name", ""),
            team_b_name=data.get("team_b_name", ""),
            team_a_odds=data.get("team_a_odds", 1.9),
            team_b_odds=data.get("team_b_odds", 1.9),
            team_a_buffer=data.get("team_a_buffer", 0),
            team_b_buffer=data.get("team_b_buffer", 0),
        )
