"""Модель для хранения истории матчей игрока."""
from datetime import datetime
from typing import List, Optional
from dataclasses import dataclass, field


@dataclass
class MatchHistory:
    """Запись об одном матче в истории игрока."""

    guild_id: int
    user_id: int
    match_id: str
    tournament_id: str
    date: datetime

    # Информация о матче
    match_type: str  # qualifier/semifinal/final
    match_index: int  # индекс матча в списке (0, 1, 2, ...)

    # Команда игрока
    team_index: int
    team_name: str
    teammates: List[dict]  # [{"name": "player1", "kills": 10, "deaths": 5}, ...]

    # Противники
    opponent_team_index: int
    opponent_team_name: str
    opponents: List[dict]  # [{"name": "player2", "kills": 8, "deaths": 6}, ...]

    # Статистика игрока
    kills: int
    deaths: int
    result: str  # win/loss
    elo_change: int

    # Ставки (опционально)
    bet_amount: int = 0
    bet_win: int = 0
    bet_team: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "match_id": self.match_id,
            "tournament_id": self.tournament_id,
            "date": self.date.isoformat() if isinstance(self.date, datetime) else self.date,
            "match_type": self.match_type,
            "match_index": self.match_index,
            "team_index": self.team_index,
            "team_name": self.team_name,
            "teammates": self.teammates,
            "opponent_team_index": self.opponent_team_index,
            "opponent_team_name": self.opponent_team_name,
            "opponents": self.opponents,
            "kills": self.kills,
            "deaths": self.deaths,
            "result": self.result,
            "elo_change": self.elo_change,
            "bet_amount": self.bet_amount,
            "bet_win": self.bet_win,
            "bet_team": self.bet_team
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MatchHistory":
        """Create from dictionary for JSON deserialization."""
        if isinstance(data.get("date"), str):
            data["date"] = datetime.fromisoformat(data["date"])
        return cls(**data)


@dataclass
class MatchHistoryList:
    """Список историй матчей для пользователя."""

    guild_id: int
    user_id: int
    matches: List[MatchHistory] = field(default_factory=list)
