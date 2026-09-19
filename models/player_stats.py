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

    # New fields for detailed rating system
    total_kills: int = 0
    total_deaths: int = 0
    best_match_kills: int = 0
    total_elo_change: int = 0
    last_elo_change: int = 0  # Sum of all ELO changes

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
            "total_kills": self.total_kills,
            "total_deaths": self.total_deaths,
            "best_match_kills": self.best_match_kills,
            "total_elo_change": self.total_elo_change,
            "last_elo_change": self.last_elo_change,
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
            total_kills=data.get("total_kills", 0),
            total_deaths=data.get("total_deaths", 0),
            best_match_kills=data.get("best_match_kills", 0),
            total_elo_change=data.get("total_elo_change", 0),
            last_elo_change=data.get("last_elo_change", 0),
        )

    @property
    def avg_kills(self) -> float:
        """Среднее количество убийств за игру."""
        return self.total_kills / self.games if self.games > 0 else 0.0

    @property
    def avg_deaths(self) -> float:
        """Среднее количество смертей за игру."""
        return self.total_deaths / self.games if self.games > 0 else 0.0

    @property
    def kd_ratio(self) -> float:
        """Соотношение убийств к смертям."""
        return self.total_kills / self.total_deaths if self.total_deaths > 0 else 0.0

    @property
    def avg_elo_change(self) -> float:
        """Среднее изменение ELO за игру."""
        return self.total_elo_change / self.games if self.games > 0 else 0.0

    @property
    def win_rate(self) -> float:
        """Процент побед."""
        return (self.wins / self.games * 100) if self.games > 0 else 0.0
