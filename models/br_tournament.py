"""Модель данных Battle Royale турнира."""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class BRTournamentPhase(str, Enum):
    """Фазы жизненного цикла BR турнира."""

    REGISTRATION = "registration"
    BERMUDA = "bermuda"
    PURGATORY = "purgatory"
    KALAHARI = "kalahari"
    RESULTS = "results"
    COMPLETE = "complete"


class BRTournamentFormation(str, Enum):
    """Формат BR турнира."""

    RANDOM = "random"
    TEAM = "team"


# Система очков за место
PLACE_POINTS = {
    1: 12,
    2: 9,
    3: 8,
    4: 7,
    5: 6,
    6: 5,
    7: 4,
    8: 3,
    9: 2,
    10: 1,
    11: 0,
    12: 0,
}


@dataclass
class BRTournament:
    """Модель Battle Royale турнира."""

    id: str
    guild_id: int
    channel_id: int
    message_id: int | None = None
    formation: BRTournamentFormation = BRTournamentFormation.RANDOM
    phase: BRTournamentPhase = BRTournamentPhase.REGISTRATION
    players: list[dict] = field(default_factory=list)  # [{"name": str, "id": int}]
    teams: list[dict] = field(default_factory=list)  # [{"captain": str, "players": list, "name": str, "stats": {}, "team_id": str}]
    registered_teams: list[str] = field(default_factory=list)  # List of team_ids for team format
    role_id: int = 0
    room_channel_id: int = 0
    current_map: str = ""
    team_rename_remaining: dict[int, bool] = field(default_factory=dict)  # team_index: can_rename
    room_data: dict[str, str] = field(default_factory=dict)  # {"id": str, "password": str}
    created_at: str = ""
    team_rename_start_time: str = ""  # Время начала фазы переименования
    organizer_id: int = 0  # ID первого игрока/организатора

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def add_player(self, name: str, user_id: int) -> bool:
        """Добавить игрока в турнир.

        Args:
            name: Имя игрока
            user_id: Discord ID игрока

        Returns:
            True если успешно, False если лимит превышен или дубликат
        """
        if len(self.players) >= 48:
            return False

        # Проверка на дубликат
        for player in self.players:
            if player["id"] == user_id:
                return False

        # Set organizer to first player
        if not self.organizer_id and len(self.players) == 0:
            self.organizer_id = user_id

        # Check if player is active in any regular tournament
        try:
            from storage.json_store import store
            all_tournaments = store.get_by_guild(self.guild_id)
            for tournament in all_tournaments:
                if tournament.is_player_active(user_id):
                    return False
        except Exception as e:
            import logging
            logging.warning(f"Error checking regular tournaments for BR: {e}")

        # Check if player is in any other BR tournament
        try:
            from storage.br_store import br_store
            all_br_tournaments = br_store.get_by_guild(self.guild_id)
            for br_tournament in all_br_tournaments:
                if br_tournament.id != self.id and br_tournament.phase.value != "complete":
                    for player in br_tournament.players:
                        if player["id"] == user_id:
                            import logging
                            logging.warning(f"BR add_player FAILED: player is active in another BR tournament (channel {br_tournament.channel_id})")
                            return False
        except Exception as e:
            import logging
            logging.warning(f"Error checking other BR tournaments: {e}")

        self.players.append({"name": name, "id": user_id})
        return True

    def remove_player(self, user_id: int) -> bool:
        """Удалить игрока из турнира.

        Args:
            user_id: Discord ID игрока

        Returns:
            True если успешно, False если игрок не найден
        """
        for i, player in enumerate(self.players):
            if player["id"] == user_id:
                self.players.pop(i)
                # Organizer remains unchanged - the creator stays organizer
                return True
        return False

    def clear_players(self) -> None:
        """Очистить всех игроков (для /brtest)."""
        self.players = []

    def form_teams_random(self) -> bool:
        """Случайно распределить игроков по командам по 4 человека.

        Returns:
            True если успешно, False если недостаточно игроков
        """
        if len(self.players) < 20 or len(self.players) % 4 != 0:
            return False

        # Перемешать игроков
        shuffled_players = self.players.copy()
        random.shuffle(shuffled_players)

        # Создать команды по 4 человека
        self.teams = []
        team_count = len(shuffled_players) // 4

        for i in range(team_count):
            team_players = shuffled_players[i * 4 : (i + 1) * 4]
            captain = team_players[0]["name"]  # Первый игрок = капитан
            player_names = [p["name"] for p in team_players]

            self.teams.append({
                "captain": captain,
                "players": player_names,
                "name": f"Команда {i + 1}",
                "stats": {}  # {"bermuda": {"place": int, "kills": int}, ...}
            })
            self.team_rename_remaining[i] = True  # Каждая команда может переименовать 1 раз

        self.phase = BRTournamentPhase.BERMUDA
        self.team_rename_start_time = datetime.now().isoformat()
        return True

    def form_teams_from_registered(self) -> bool:
        """Сформировать команды из зарегистрированных команд.

        Returns:
            True если успешно, False если недостаточно команд
        """
        if len(self.registered_teams) < 5:
            return False

        try:
            from storage.team_store import team_store

            self.teams = []
            for i, team_id in enumerate(self.registered_teams):
                team = team_store.get(team_id)
                if not team:
                    continue

                # Get member names from players list
                member_names = []
                for member_id in team.members:
                    # Find member in players list
                    for player in self.players:
                        if player["id"] == member_id:
                            member_names.append(player["name"])
                            break

                if len(member_names) < 4:
                    continue  # Skip incomplete teams

                captain = member_names[0] if member_names else team.team_name

                self.teams.append({
                    "captain": captain,
                    "players": member_names,
                    "name": team.team_name,
                    "stats": {},
                    "team_id": team_id
                })
                self.team_rename_remaining[i] = True

            if len(self.teams) < 5:
                return False

            self.phase = BRTournamentPhase.BERMUDA
            self.team_rename_start_time = datetime.now().isoformat()
            return True
        except Exception:
            return False

    def rename_team(self, team_index: int, new_name: str, captain_name: str) -> bool:
        """Изменить название команды.

        Args:
            team_index: Индекс команды
            new_name: Новое название
            captain_name: Имя капитана

        Returns:
            True если успешно, False если нет прав или уже переименовано
        """
        if team_index >= len(self.teams):
            return False

        team = self.teams[team_index]

        # Проверка что пользователь капитан
        if team["captain"] != captain_name:
            return False

        # Проверка что можно переименовать
        if not self.team_rename_remaining.get(team_index, False):
            return False

        team["name"] = new_name
        self.team_rename_remaining[team_index] = False
        return True

    def set_team_stats(self, team_index: int, map_name: str, place: int, kills: int) -> bool:
        """Записать статистику команды для карты.

        Args:
            team_index: Индекс команды
            map_name: Название карты (bermuda, purgatory, kalahari)
            place: Место (1-12)
            kills: Киллы (0-50)

        Returns:
            True если успешно, False если некорректные данные
        """
        if team_index >= len(self.teams):
            return False

        if place < 1 or place > 12:
            return False

        if kills < 0 or kills > 50:
            return False

        self.teams[team_index]["stats"][map_name] = {
            "place": place,
            "kills": kills
        }
        return True

    def are_all_stats_complete(self, map_name: str) -> bool:
        """Проверить заполнена ли статистика для всех команд для карты.

        Args:
            map_name: Название карты

        Returns:
            True если все команды заполнили статистику
        """
        for team in self.teams:
            if map_name not in team["stats"]:
                return False
        return True

    def calculate_final_results(self) -> list[dict]:
        """Подсчитать финальные очки для всех команд.

        Returns:
            Список команд с очками, отсортированный по убыванию очков
        """
        results = []

        for i, team in enumerate(self.teams):
            total_points = 0

            # Очки за каждую карту
            for map_name in ["bermuda", "purgatory", "kalahari"]:
                if map_name in team["stats"]:
                    stats = team["stats"][map_name]
                    place = stats["place"]
                    kills = stats["kills"]

                    # Очки за место
                    place_points = PLACE_POINTS.get(place, 0)
                    # Очки за киллы
                    kills_points = kills

                    total_points += place_points + kills_points

            results.append({
                "team_index": i,
                "name": team["name"],
                "total_points": total_points,
                "bermuda": team["stats"].get("bermuda", {}),
                "purgatory": team["stats"].get("purgatory", {}),
                "kalahari": team["stats"].get("kalahari", {}),
            })

        # Сортировать по убыванию очков
        results.sort(key=lambda x: x["total_points"], reverse=True)
        return results

    def to_dict(self) -> dict[str, Any]:
        """Конвертировать в словарь для сохранения в JSON."""
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "formation": self.formation.value,
            "phase": self.phase.value,
            "players": self.players,
            "teams": self.teams,
            "registered_teams": self.registered_teams,
            "role_id": self.role_id,
            "room_channel_id": self.room_channel_id,
            "current_map": self.current_map,
            "team_rename_remaining": self.team_rename_remaining,
            "room_data": self.room_data,
            "created_at": self.created_at,
            "team_rename_start_time": self.team_rename_start_time,
            "organizer_id": self.organizer_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BRTournament:
        """Создать объект из словаря из JSON."""
        return cls(
            id=data.get("id", ""),
            guild_id=data.get("guild_id", 0),
            channel_id=data.get("channel_id", 0),
            message_id=data.get("message_id"),
            formation=BRTournamentFormation(data.get("formation", "random")),
            phase=BRTournamentPhase(data.get("phase", "registration")),
            players=data.get("players", []),
            teams=data.get("teams", []),
            registered_teams=data.get("registered_teams", []),
            role_id=data.get("role_id", 0),
            room_channel_id=data.get("room_channel_id", 0),
            current_map=data.get("current_map", ""),
            team_rename_remaining=data.get("team_rename_remaining", {}),
            room_data=data.get("room_data", {}),
            created_at=data.get("created_at", ""),
            team_rename_start_time=data.get("team_rename_start_time", ""),
            organizer_id=data.get("organizer_id", 0),
        )
