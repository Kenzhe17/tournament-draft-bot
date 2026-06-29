"""Модель результата матча с K/D статистикой."""

from dataclasses import dataclass
from typing import Any


@dataclass
class PlayerMatchStats:
    """Статистика игрока в матче."""

    player_name: str
    user_id: int
    circle: int  # Круг, в котором был выбран игрок (1-4)
    kills: int
    deaths: int

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в словарь."""
        return {
            "player_name": self.player_name,
            "user_id": self.user_id,
            "circle": self.circle,
            "kills": self.kills,
            "deaths": self.deaths,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlayerMatchStats":
        """Десериализация из словаря."""
        return cls(
            player_name=data.get("player_name", ""),
            user_id=data.get("user_id", 0),
            circle=data.get("circle", 1),
            kills=data.get("kills", 0),
            deaths=data.get("deaths", 0),
        )


@dataclass
class MatchResult:
    """Результат матча с K/D статистикой всех игроков."""

    guild_id: int
    match_id: str  # Уникальный идентификатор матча
    winning_team_index: int  # Индекс победившей команды (0-based)
    team1_stats: list[PlayerMatchStats]
    team2_stats: list[PlayerMatchStats]
    match_type: str  # "qualifier", "semifinal", "final"

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в словарь."""
        return {
            "guild_id": self.guild_id,
            "match_id": self.match_id,
            "winning_team_index": self.winning_team_index,
            "team1_stats": [s.to_dict() for s in self.team1_stats],
            "team2_stats": [s.to_dict() for s in self.team2_stats],
            "match_type": self.match_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MatchResult":
        """Десериализация из словаря."""
        return cls(
            guild_id=data.get("guild_id", 0),
            match_id=data.get("match_id", ""),
            winning_team_index=data.get("winning_team_index", 0),
            team1_stats=[PlayerMatchStats.from_dict(s) for s in data.get("team1_stats", [])],
            team2_stats=[PlayerMatchStats.from_dict(s) for s in data.get("team2_stats", [])],
            match_type=data.get("match_type", "qualifier"),
        )
