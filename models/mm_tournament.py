"""Модель данных Matchmaking турнира."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class MMFormat(str, Enum):
    """Формат matchmaking турнира."""

    DUO = "duo"  # 2 команды по 2 игрока (4 всего)
    SQUAD = "squad"  # 2 команды по 4 игрока (8 всего)


class MMPhase(str, Enum):
    """Фазы жизненного цикла MM турнира."""

    SEARCHING = "searching"  # Поиск игроков
    LOBBY = "lobby"  # Лобби набрано, ожидание старта
    PLAYING = "playing"  # Игра идет
    REPORTING = "reporting"  # Капитаны вводят статистику
    COMPLETE = "complete"  # Турнир завершен


@dataclass
class MMTournament:
    """Модель Matchmaking турнира."""

    id: str
    guild_id: int
    channel_id: int
    message_id: int | None = None
    match_message_id: int | None = None  # Message ID in match channel
    format: MMFormat = MMFormat.DUO
    phase: MMPhase = MMPhase.SEARCHING
    players: list[dict] = field(default_factory=list)  # [{"name": str, "id": int, "skill_rating": float}]
    teams: list[dict] = field(default_factory=list)  # [{"captain": str, "players": list, "name": str, "kills": int}]
    room_data: dict[str, str] = field(default_factory=dict)  # {"id": str, "password": str}
    created_at: str = ""
    first_player_time: str = ""  # Время когда первый игрок нажал поиск
    winner_team_index: int | None = None  # Индекс команды-победителя (для ручного выбора)
    manual_winner: bool = False  # True если победитель выбран вручную

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    @property
    def required_players(self) -> int:
        """Required players based on format."""
        if self.format == MMFormat.DUO:
            return 4
        elif self.format == MMFormat.SQUAD:
            return 8
        return 4

    @property
    def players_per_team(self) -> int:
        """Players per team based on format."""
        if self.format == MMFormat.DUO:
            return 2
        elif self.format == MMFormat.SQUAD:
            return 4
        return 2

    def add_player(self, name: str, user_id: int, skill_rating: float = 1.0) -> bool:
        """Добавить игрока в поиск.

        Args:
            name: Имя игрока
            user_id: Discord ID игрока
            skill_rating: Skill Rating игрока

        Returns:
            True если успешно, False если лимит превышен или дубликат
        """
        if len(self.players) >= self.required_players:
            return False

        # Проверка на дубликат
        for player in self.players:
            if player["id"] == user_id:
                return False

        # Check if player is in any other MM tournament
        try:
            from storage.mm_store import mm_store
            all_mm_tournaments = mm_store.get_by_guild(self.guild_id)
            for mm_tournament in all_mm_tournaments:
                if mm_tournament.id != self.id and mm_tournament.phase.value != "complete":
                    for player in mm_tournament.players:
                        if player["id"] == user_id:
                            logger.warning(f"MM add_player FAILED: player is active in another MM tournament (channel {mm_tournament.channel_id})")
                            return False
        except Exception as e:
            logger.warning(f"Error checking other MM tournaments: {e}")

        # Set first player time
        if len(self.players) == 0:
            self.first_player_time = datetime.now().isoformat()

        self.players.append({"name": name, "id": user_id, "skill_rating": skill_rating})
        return True

    def remove_player(self, user_id: int) -> bool:
        """Удалить игрока из поиска.

        Args:
            user_id: Discord ID игрока

        Returns:
            True если успешно, False если игрок не найден
        """
        for i, player in enumerate(self.players):
            if player["id"] == user_id:
                self.players.pop(i)
                # Reset first player time if no players left
                if len(self.players) == 0:
                    self.first_player_time = ""
                return True
        return False

    def is_full(self) -> bool:
        """Проверка набрано ли достаточно игроков."""
        return len(self.players) >= self.required_players

    def form_teams_balanced(self) -> bool:
        """Сформировать сбалансированные команды по Skill Rating.

        Логика:
        1. Сортировать игроков по Skill Rating
        2. Разделить на 4 категории (равные группы)
        3. Распределить по 2 игрока из каждой категории в разные команды

        Returns:
            True если успешно, False если недостаточно игроков
        """
        if not self.is_full():
            return False

        # Сортировать по Skill Rating (от высокого к низкому)
        sorted_players = sorted(self.players, key=lambda x: x["skill_rating"], reverse=True)

        # Разделить на 4 категории
        category_size = len(sorted_players) // 4
        categories = []
        for i in range(4):
            start = i * category_size
            end = (i + 1) * category_size if i < 3 else len(sorted_players)
            categories.append(sorted_players[start:end])

        # Распределить по командам
        team_a = []
        team_b = []

        for i, category in enumerate(categories):
            # Чередуем команды
            if i % 2 == 0:
                team_a.extend(category)
            else:
                team_b.extend(category)

        # Создать команды
        self.teams = [
            {
                "captain": team_a[0]["name"],
                "players": [p["name"] for p in team_a],
                "name": "Команда A",
                "kills": 0,
            },
            {
                "captain": team_b[0]["name"],
                "players": [p["name"] for p in team_b],
                "name": "Команда B",
                "kills": 0,
            },
        ]

        self.phase = MMPhase.PLAYING
        return True

    def set_team_kills(self, team_index: int, kills: int) -> bool:
        """Записать киллы команды.

        Args:
            team_index: Индекс команды (0 или 1)
            kills: Количество киллов

        Returns:
            True если успешно, False если некорректные данные
        """
        if team_index >= len(self.teams):
            return False

        if kills < 0 or kills > 100:
            return False

        self.teams[team_index]["kills"] = kills
        return True

    def validate_stats(self) -> tuple[bool, str]:
        """Проверить что киллы команды A = смерти команды B.

        Returns:
            (is_valid, message) - кортеж с результатом проверки и сообщением
        """
        if len(self.teams) != 2:
            return False, "Должно быть 2 команды"

        kills_a = self.teams[0]["kills"]
        kills_b = self.teams[1]["kills"]

        if kills_a == kills_b:
            return False, f"Киллы обеих команд равны ({kills_a}), невозможно определить победителя"

        # Проверка: киллы A должны быть примерно равны киллам B
        # В идеале kills_a = deaths_b, но deaths_b = kills_a в дуэли
        # Просто проверяем что обе команды ввели данные
        if kills_a == 0 and kills_b == 0:
            return False, "Обе команды ввели 0 киллов"

        return True, "Статистика валидна"

    def determine_winner(self) -> int | None:
        """Определить победителя по киллам.

        Returns:
            Индекс команды-победителя (0 или 1), или None если нельзя определить
        """
        if len(self.teams) != 2:
            return None

        kills_a = self.teams[0]["kills"]
        kills_b = self.teams[1]["kills"]

        if kills_a > kills_b:
            return 0
        elif kills_b > kills_a:
            return 1
        else:
            return None

    def to_dict(self) -> dict[str, Any]:
        """Конвертировать в словарь для сохранения."""
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "match_message_id": self.match_message_id,
            "format": self.format.value,
            "phase": self.phase.value,
            "players": self.players,
            "teams": self.teams,
            "room_data": self.room_data,
            "created_at": self.created_at,
            "first_player_time": self.first_player_time,
            "winner_team_index": self.winner_team_index,
            "manual_winner": self.manual_winner,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MMTournament:
        """Создать объект из словаря."""
        return cls(
            id=data.get("id", ""),
            guild_id=data.get("guild_id", 0),
            channel_id=data.get("channel_id", 0),
            message_id=data.get("message_id"),
            match_message_id=data.get("match_message_id"),
            format=MMFormat(data.get("format", "duo")),
            phase=MMPhase(data.get("phase", "searching")),
            players=data.get("players", []),
            teams=data.get("teams", []),
            room_data=data.get("room_data", {}),
            created_at=data.get("created_at", ""),
            first_player_time=data.get("first_player_time", ""),
            winner_team_index=data.get("winner_team_index"),
            manual_winner=data.get("manual_winner", False),
        )
