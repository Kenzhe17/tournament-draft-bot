"""Модель статистики игроков."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlayerStats:
    """Статистика игрока по всем турнирам."""

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
            "name": self.name,
            "wins": self.wins,
            "games": self.games,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlayerStats":
        """Десериализация из словаря."""
        return cls(
            name=data["name"],
            wins=data.get("wins", 0),
            games=data.get("games", 0),
        )
