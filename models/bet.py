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
    amount: int
    
    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "match_id": self.match_id,
            "team_name": self.team_name,
            "amount": self.amount,
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
            amount=data.get("amount", 0),
        )
