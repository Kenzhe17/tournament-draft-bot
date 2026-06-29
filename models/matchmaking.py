"""Модель данных для Matchmaking."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MatchmakingPhase(str, Enum):
    """Фазы жизненного цикла matchmaking."""

    SETUP = "setup"           # Регистрация игроков
    DRAFT = "draft"           # Драфт команд
    TEAMS = "teams"           # Настройка команд
    READY_CHECK = "ready_check"  # Проверка готовности
    IN_PROGRESS = "in_progress" # Матч идет
    COMPLETE = "complete"     # Матч завершен


@dataclass
class Matchmaking:
    """Состояние одного matchmaking на сервере."""

    guild_id: int
    channel_id: int
    message_id: int = 0

    # Фаза
    phase: MatchmakingPhase = MatchmakingPhase.SETUP

    # Игроки (display names)
    players: list[str] = field(default_factory=list)
    player_ids: dict[str, int] = field(default_factory=dict)  # name -> user_id

    # Команды
    team1_players: list[str] = field(default_factory=list)
    team2_players: list[str] = field(default_factory=list)
    team1_name: str = "Team 1"
    team2_name: str = "Team 2"
    team1_captain: str = ""
    team2_captain: str = ""

    # Готовность команд
    team1_ready: bool = False
    team2_ready: bool = False

    # Драфт
    draft_circle: int = 1  # Текущий круг драфта
    draft_picker: int = 0  # Кто выбирает (0 = team1, 1 = team2)
    available_players: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в словарь."""
        return {
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "phase": self.phase.value,
            "players": self.players,
            "player_ids": self.player_ids,
            "team1_players": self.team1_players,
            "team2_players": self.team2_players,
            "team1_name": self.team1_name,
            "team2_name": self.team2_name,
            "team1_captain": self.team1_captain,
            "team2_captain": self.team2_captain,
            "team1_ready": self.team1_ready,
            "team2_ready": self.team2_ready,
            "draft_circle": self.draft_circle,
            "draft_picker": self.draft_picker,
            "available_players": self.available_players,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Matchmaking":
        """Десериализация из словаря."""
        return cls(
            guild_id=data.get("guild_id", 0),
            channel_id=data.get("channel_id", 0),
            message_id=data.get("message_id", 0),
            phase=MatchmakingPhase(data.get("phase", "setup")),
            players=data.get("players", []),
            player_ids=data.get("player_ids", {}),
            team1_players=data.get("team1_players", []),
            team2_players=data.get("team2_players", []),
            team1_name=data.get("team1_name", "Team 1"),
            team2_name=data.get("team2_name", "Team 2"),
            team1_captain=data.get("team1_captain", ""),
            team2_captain=data.get("team2_captain", ""),
            team1_ready=data.get("team1_ready", False),
            team2_ready=data.get("team2_ready", False),
            draft_circle=data.get("draft_circle", 1),
            draft_picker=data.get("draft_picker", 0),
            available_players=data.get("available_players", []),
        )

    @property
    def is_full(self) -> bool:
        """Собраны ли все 8 игроков."""
        return len(self.players) >= 8

    @property
    def is_ready(self) -> bool:
        """Готовы ли обе команды."""
        return self.team1_ready and self.team2_ready

    def add_player(self, name: str, user_id: int) -> bool:
        """Добавить игрока. Возвращает True если добавлен."""
        if name in self.players:
            return False
        if self.is_full:
            return False
        self.players.append(name)
        self.player_ids[name] = user_id
        return True

    def remove_player(self, name: str) -> bool:
        """Удалить игрока. Возвращает True если удален."""
        if name not in self.players:
            return False
        self.players.remove(name)
        self.player_ids.pop(name, None)
        return True

    def is_player_in_matchmaking(self, name: str) -> bool:
        """Проверить, находится ли игрок в matchmaking."""
        return name in self.players

    async def distribute_by_elo(self, guild_id: int) -> None:
        """Распределить игроков по командам на основе ELO."""
        from storage.player_stats_store import player_stats_store

        # Get ELO for each player
        players_with_elo = []
        for player_name in self.players:
            user_id = self.player_ids.get(player_name, 0)
            stats = await player_stats_store.get(guild_id, user_id)
            elo = stats.elo if stats else 1000  # Default ELO for new players
            players_with_elo.append((player_name, user_id, elo))

        # Sort by ELO (descending)
        players_with_elo.sort(key=lambda x: x[2], reverse=True)

        # Top 2 become captains
        if len(players_with_elo) >= 2:
            self.team1_captain = players_with_elo[0][0]
            self.team2_captain = players_with_elo[1][0]

        # Distribute players to teams (snake draft by ELO)
        self.team1_players = [self.team1_captain] if self.team1_captain else []
        self.team2_players = [self.team2_captain] if self.team2_captain else []

        # Remaining players (skip captains if they were added)
        remaining = players_with_elo[2:] if len(players_with_elo) > 2 else []

        # Snake draft: T1, T2, T2, T1, T2, T1 for 6 players
        for i, (player_name, user_id, _) in enumerate(remaining):
            if i % 3 == 0:
                self.team1_players.append(player_name)
            elif i % 3 == 1:
                self.team2_players.append(player_name)
            elif i % 3 == 2:
                self.team2_players.append(player_name)

        # Set available players for draft (all non-captains)
        self.available_players = [p for p in self.players if p not in [self.team1_captain, self.team2_captain]]

        # Set team names
        self.team1_name = f"Team {self.team1_captain}" if self.team1_captain else "Team 1"
        self.team2_name = f"Team {self.team2_captain}" if self.team2_captain else "Team 2"
