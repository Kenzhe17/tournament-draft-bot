"""Matchmaking session management."""

from __future__ annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from models.match import Match, MatchPhase, MatchStatus, Team

if TYPE_CHECKING:
    from bot import TournamentBot


class MatchmakingSession:
    """Сессия matchmaking для одного матча."""

    def __init__(self, guild_id: int, main_channel_id: int):
        self.match_id = str(uuid.uuid4())
        self.guild_id = guild_id
        self.main_channel_id = main_channel_id
        self.channel_id = 0  # Будет установлен при создании канала
        self.match = Match(
            match_id=self.match_id,
            guild_id=guild_id,
            channel_id=0,
            main_channel_id=main_channel_id,
        )
        self.created_at = datetime.utcnow()

    def add_player(self, user_id: int, user_name: str) -> bool:
        """Добавить игрока в сессию. Возвращает True если добавлен, False если уже в сессии."""
        if user_id in self.match.players:
            return False
        if self.match.is_full:
            return False

        self.match.players.append(user_id)
        self.match.player_names[user_id] = user_name
        return True

    def remove_player(self, user_id: int) -> bool:
        """Удалить игрока из сессии. Возвращает True если удален, False если не был в сессии."""
        if user_id not in self.match.players:
            return False

        self.match.players.remove(user_id)
        self.match.player_names.pop(user_id, None)
        return True

    def is_player_in_session(self, user_id: int) -> bool:
        """Проверить, находится ли игрок в сессии."""
        return user_id in self.match.players

    def get_player_count(self) -> int:
        """Получить количество игроков в сессии."""
        return len(self.match.players)

    def is_full(self) -> bool:
        """Проверить, собрана ли полная команда (8 игроков)."""
        return self.match.is_full

    def start_draft(self) -> None:
        """Начать драфт после сбора 8 игроков."""
        self.match.phase = MatchPhase.DRAFT
        self.match.status = MatchStatus.MATCH_FOUND

    def create_teams(self, captain1_id: int, captain1_name: str, captain2_id: int, captain2_name: str) -> None:
        """Создать 2 команды с капитанами."""
        team1 = Team(
            team_id=0,
            captain_id=captain1_id,
            captain_name=captain1_name,
            name=f"Team {captain1_name}",
        )
        team2 = Team(
            team_id=1,
            captain_id=captain2_id,
            captain_name=captain2_name,
            name=f"Team {captain2_name}",
        )
        self.match.teams = [team1, team2]

    def start_team_setup(self) -> None:
        """Начать фазу настройки команд."""
        self.match.phase = MatchPhase.TEAM_SETUP

    def set_team_ready(self, team_id: int, ready: bool) -> None:
        """Установить готовность команды."""
        for team in self.match.teams:
            if team.team_id == team_id:
                team.ready = ready
                break

    def start_match(self) -> None:
        """Начать матч."""
        self.match.phase = MatchPhase.IN_PROGRESS
        self.match.status = MatchStatus.STARTED
        self.match.started_at = datetime.utcnow()

    def complete_match(self, winner_team_id: int) -> None:
        """Завершить матч."""
        self.match.phase = MatchPhase.COMPLETED
        self.match.status = MatchStatus.FINISHED
        self.match.winner_team_id = winner_team_id
        self.match.completed_at = datetime.utcnow()
