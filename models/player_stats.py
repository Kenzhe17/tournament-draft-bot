"""Модель статистики игроков."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlayerStats:
    """Статистика игрока по всем турнирам."""

    guild_id: int
    name: str
    elo: int = 1000
    wins: int = 0
    finals: int = 0
    games: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в словарь."""
        return {
            "guild_id": self.guild_id,
            "name": self.name,
            "elo": self.elo,
            "wins": self.wins,
            "finals": self.finals,
            "games": self.games,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlayerStats":
        """Десериализация из словаря."""
        return cls(
            guild_id=data.get("guild_id", 0),
            name=data["name"],
            elo=data.get("elo", 1000),
            wins=data.get("wins", 0),
            finals=data.get("finals", 0),
            games=data.get("games", 0),
        )
