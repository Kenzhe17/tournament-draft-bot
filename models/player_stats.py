"""Модель статистики игроков."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlayerStats:
    """Статистика игрока по всем турнирам."""

    guild_id: int
    user_id: int
    name: str
    elo: int = 1000
    wins: int = 0
    finals: int = 0
    games: int = 0
    current_streak: int = 0
    best_win_streak: int = 0
    best_loss_streak: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в словарь."""
        return {
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "name": self.name,
            "elo": self.elo,
            "wins": self.wins,
            "finals": self.finals,
            "games": self.games,
            "current_streak": self.current_streak,
            "best_win_streak": self.best_win_streak,
            "best_loss_streak": self.best_loss_streak,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlayerStats":
        """Десериализация из словарЯ."""
        return cls(
            guild_id=data.get("guild_id", 0),
            user_id=data.get("user_id", 0),
            name=data.get("name", "Unknown"),
            elo=data.get("elo", 1000),
            wins=data.get("wins", 0),
            finals=data.get("finals", 0),
            games=data.get("games", 0),
            current_streak=data.get("current_streak", 0),
            best_win_streak=data.get("best_win_streak", 0),
            best_loss_streak=data.get("best_loss_streak", 0),
        )
