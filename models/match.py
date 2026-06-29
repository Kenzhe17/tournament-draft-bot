"""Модель данных для Matchmaking."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from datetime import datetime


class MatchPhase(str, Enum):
    """Фазы жизненного цикла матча."""

    SEARCHING = "searching"      # Поиск игроков
    DRAFT = "draft"              # Драфт команд
    TEAM_SETUP = "team_setup"   # Настройка команд (названия, ready)
    READY_CHECK = "ready_check"  # Проверка готовности
    IN_PROGRESS = "in_progress" # Матч идет
    COMPLETED = "completed"      # Матч завершен


class MatchStatus(str, Enum):
    """Статус матча."""

    SEARCHING = "searching"      # Поиск игроков
    MATCH_FOUND = "match_found"  # 8 игроков найдены
    READY = "ready"              # Обе команды готовы
    STARTED = "started"          # Матч начат
    FINISHED = "finished"        # Матч завершен


@dataclass
class Team:
    """Команда в матче."""

    team_id: int  # 0 или 1
    captain_id: int  # Discord user ID капитана
    captain_name: str  # Имя капитана
    name: str  # Название команды
    players: list[int] = field(default_factory=list)  # Discord user ID игроков
    ready: bool = False  # Готова ли команда

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в словарь."""
        return {
            "team_id": self.team_id,
            "captain_id": self.captain_id,
            "captain_name": self.captain_name,
            "name": self.name,
            "players": self.players,
            "ready": self.ready,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Team":
        """Десериализация из словаря."""
        return cls(
            team_id=data.get("team_id", 0),
            captain_id=data.get("captain_id", 0),
            captain_name=data.get("captain_name", ""),
            name=data.get("name", f"Team {data.get('team_id', 0) + 1}"),
            players=data.get("players", []),
            ready=data.get("ready", False),
        )


@dataclass
class Match:
    """Состояние матча в matchmaking."""

    match_id: str  # Уникальный ID матча
    guild_id: int
    channel_id: int  # ID закрытого канала для матча
    message_id: int = 0  # ID сообщения в закрытом канале
    main_channel_id: int = 1521101891235221594  # ID главного канала matchmaking
    main_message_id: int = 0  # ID сообщения в главном канале

    # Игроки
    players: list[int] = field(default_factory=list)  # Discord user ID всех игроков
    player_names: dict[int, str] = field(default_factory=dict)  # user_id -> name

    # Фазы
    phase: MatchPhase = MatchPhase.SEARCHING
    status: MatchStatus = MatchStatus.SEARCHING

    # Команды
    teams: list[Team] = field(default_factory=list)

    # Драфт (используем существующую систему)
    draft_data: dict[str, Any] = field(default_factory=dict)

    # Время
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # Победитель
    winner_team_id: int | None = None

    # Подтверждение победы
    pending_winner_team_id: int | None = None
    pending_winner_captain_id: int | None = None

    # Ставки
    betting_open: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в словарь."""
        return {
            "match_id": self.match_id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "main_channel_id": self.main_channel_id,
            "main_message_id": self.main_message_id,
            "players": self.players,
            "player_names": self.player_names,
            "phase": self.phase.value,
            "status": self.status.value,
            "teams": [team.to_dict() for team in self.teams],
            "draft_data": self.draft_data,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "winner_team_id": self.winner_team_id,
            "betting_open": self.betting_open,
            "pending_winner_team_id": self.pending_winner_team_id,
            "pending_winner_captain_id": self.pending_winner_captain_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Match":
        """Десериализация из словаря."""
        return cls(
            match_id=data.get("match_id", ""),
            guild_id=data.get("guild_id", 0),
            channel_id=data.get("channel_id", 0),
            message_id=data.get("message_id", 0),
            main_channel_id=data.get("main_channel_id", 1521101891235221594),
            main_message_id=data.get("main_message_id", 0),
            players=data.get("players", []),
            player_names=data.get("player_names", {}),
            phase=MatchPhase(data.get("phase", "searching")),
            status=MatchStatus(data.get("status", "searching")),
            teams=[Team.from_dict(t) for t in data.get("teams", [])],
            draft_data=data.get("draft_data", {}),
            created_at=datetime.fromisoformat(data.get("created_at", datetime.utcnow().isoformat())),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
            winner_team_id=data.get("winner_team_id"),
            betting_open=data.get("betting_open", True),
            pending_winner_team_id=data.get("pending_winner_team_id"),
            pending_winner_captain_id=data.get("pending_winner_captain_id"),
        )

    @property
    def is_full(self) -> bool:
        """Собраны ли все 8 игроков."""
        return len(self.players) >= 8

    @property
    def is_ready(self) -> bool:
        """Готовы ли обе команды."""
        return len(self.teams) == 2 and all(team.ready for team in self.teams)

    def get_captain_team(self, user_id: int) -> Team | None:
        """Получить команду, где пользователь капитан."""
        for team in self.teams:
            if team.captain_id == user_id:
                return team
        return None

    def get_player_team(self, user_id: int) -> Team | None:
        """Получить команду, где пользователь игрок."""
        for team in self.teams:
            if user_id in team.players or user_id == team.captain_id:
                return team
        return None
