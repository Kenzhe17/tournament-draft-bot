"""Модель статистики игроков."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlayerStats:
    """Статистика игрока по всем турнирам."""

    guild_id: int
    name: str
    wins: int = 0
    games: int = 0

    @property
    def win_rate(self) -> float:
        """Процент побед."""
        if self.games == 0:
            return 0.0
        return (self.wins / self.games) * 100

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в словарь."""
        return {
            "guild_id": self.guild_id,
            "name": self.name,
            "wins": self.wins,
            "games": self.games,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlayerStats":
        """Десериализация из словаря."""
        return cls(
            guild_id=data.get("guild_id", 0),
            name=data["name"],
            wins=data.get("wins", 0),
            games=data.get("games", 0),
        )
