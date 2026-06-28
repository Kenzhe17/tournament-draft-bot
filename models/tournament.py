"""Модель данных турнира."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TournamentSize(str, Enum):
    """Размер турнира."""

    EIGHT = "8"
    SIXTEEN = "16"
    THIRTY_TWO = "32"


class TournamentPhase(str, Enum):
    """Фазы жизненного цикла турнира."""

    SETUP = "setup"
    DRAFT = "draft"
    TEAMS = "teams"
    QUALIFIERS = "qualifiers"
    SEMIFINALS = "semifinals"
    FINAL = "final"
    COMPLETE = "complete"


class RegistrationState(str, Enum):
    """Состояние регистрации игроков."""

    CLOSED = "closed"
    OPEN = "open"


class FormationMode(str, Enum):
    """Режим формирования кругов."""

    MANUAL = "manual"
    ELO = "elo"


# Порядок выбора по кругам для разного количества капитанов
# Rotation Draft: круг 2 - прямой, круг 3 - обратный, круг 4 - с середины к концу, потом к началу
PICK_ORDERS: dict[int, dict[str, list[int] | int]] = {
    2: {
        "2": {"order": [0, 1], "auto": 1},  # Круг 2: 1 → 2
        "3": {"order": [1, 0], "auto": 0},  # Круг 3: 2 → 1
        "4": {"order": [1, 0], "auto": 0},  # Круг 4: 2 → 1
    },
    4: {
        "2": {"order": [0, 1, 2, 3], "auto": 3},  # Круг 2: 1 → 2 → 3 → 4
        "3": {"order": [3, 2, 1, 0], "auto": 0},  # Круг 3: 4 → 3 → 2 → 1
        "4": {"order": [2, 3, 1, 0], "auto": 0},  # Круг 4: 3 → 4 → 2 → 1
    },
    8: {
        "2": {"order": [0, 1, 2, 3, 4, 5, 6, 7], "auto": 7},  # Круг 2: 1 → 2 → ... → 8
        "3": {"order": [7, 6, 5, 4, 3, 2, 1, 0], "auto": 0},  # Круг 3: 8 → 7 → ... → 1
        "4": {"order": [4, 5, 6, 7, 3, 2, 1, 0], "auto": 0},  # Круг 4: 5 → 6 → 7 → 8 → 4 → 3 → 2 → 1
    },
    16: {
        "2": {"order": [0, 1, 2, 3], "auto": 3},  # Круг 2: 1 → 2 → 3 → 4
        "3": {"order": [1, 2, 3, 0], "auto": 0},  # Круг 3: 2 → 3 → 4 → 1
        "4": {"order": [3, 2, 0, 1], "auto": 0},  # Круг 4: 4 → 3 → 1 → 2
    },
    32: {
        "2": {"order": [0, 1, 2, 3, 4, 5, 6, 7], "auto": 7},  # Круг 2: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8
        "3": {"order": [4, 7, 3, 6, 2, 5, 1, 0], "auto": 0},  # Круг 3: 5 → 8 → 4 → 7 → 3 → 6 → 2 → 1
        "4": {"order": [5, 6, 7, 1, 0, 2, 3, 4], "auto": 0},  # Круг 4: 6 → 7 → 8 → 2 → 1 → 3 → 4 → 5
    },
}

# Возможные пары полуфиналов (индексы команд 0-based)
SEMIFINAL_PAIRINGS: list[list[tuple[int, int]]] = [
    [(0, 1), (2, 3)],
    [(0, 2), (1, 3)],
    [(0, 3), (1, 2)],
]


@dataclass
class Tournament:
    """Состояние одного турнира на сервере."""

    guild_id: int
    channel_id: int
    message_id: int = 0

    # Настройка
    size: TournamentSize = TournamentSize.EIGHT
    registration: RegistrationState = RegistrationState.CLOSED
    formation_mode: FormationMode = FormationMode.MANUAL
    captains: list[str] = field(default_factory=list)  # Display names
    circle1: list[str] = field(default_factory=list)  # Captain circle (display names)
    circle2: list[str] = field(default_factory=list)
    circle3: list[str] = field(default_factory=list)
    circle4: list[str] = field(default_factory=list)

    phase: TournamentPhase = TournamentPhase.SETUP
    is_test: bool = False

    # Лимиты для кругов (True = лимит включен, False = без лимита)
    circle_limits_enabled: dict[int, bool] = field(default_factory=lambda: {2: True, 3: True, 4: True})

    # Названия команд (индекс команды -> название)
    team_names: dict[int, str] = field(default_factory=dict)

    # Отображение имени на user_id для статистики
    player_user_ids: dict[str, int] = field(default_factory=dict)

    @property
    def captain_count(self) -> int:
        """Количество капитанов на основе размера турнира."""
        if self.size == TournamentSize.EIGHT:
            return 2
        elif self.size == TournamentSize.SIXTEEN:
            return 4
        else:  # 32 players
            return 8

    def circle_limit(self, circle: int) -> int:
        """Лимит игроков для круга на основе размера турнира и настроек."""
        if circle == 1:
            return self.captain_count  # circle1 = captains
        elif circle == 4:
            # circle4 unlimited by default, but can be limited
            if self.circle_limits_enabled.get(4, True):
                return self.captain_count
            return float('inf')
        else:
            # circle2, circle3
            if self.circle_limits_enabled.get(circle, True):
                return self.captain_count
            return float('inf')

    # Драфт
    captain_order: list[int] = field(default_factory=list)
    picks: dict[str, dict[str, str]] = field(default_factory=dict)
    current_circle: int = 2
    pick_index: int = 0
    available: dict[str, list[str]] = field(default_factory=dict)

    # Команды и плей-офф
    teams: list[dict[str, Any]] = field(default_factory=list)
    qualifier_matches: list[tuple[int, int]] = field(default_factory=list)
    qualifier_winners: list[int | None] = field(default_factory=list)
    semifinal_matches: list[tuple[int, int]] = field(default_factory=list)
    semifinal_winners: list[int | None] = field(default_factory=list)
    final_teams: list[int] = field(default_factory=list)
    winner_team_index: int | None = None

    # --- Свойства ---

    @property
    def all_players(self) -> set[int]:
        """Все добавленные игроки (круги 1–4) - user IDs."""
        return set(self.circle1 + self.circle2 + self.circle3 + self.circle4)

    @property
    def is_setup_complete(self) -> bool:
        """Готов ли турнир к запуску драфта."""
        # Use circle1 as captains (they are the same)
        if len(self.circle1) != self.captain_count:
            return False

        # Check circle2, circle3, circle4 - all need at least captain_count players
        for circle in [2, 3, 4]:
            circle_list = self.circle_list(circle)
            if len(circle_list) < self.captain_count:
                return False

        return True

    def circle_list(self, circle: int) -> list[str]:
        """Получить список игроков круга (display names)."""
        return getattr(self, f"circle{circle}")

    def set_circle_list(self, circle: int, players: list[str]) -> None:
        """Установить список игроков круга (display names)."""
        setattr(self, f"circle{circle}", players)

    # --- Добавление игроков ---

    def add_players(self, names: list[str]) -> tuple[list[str], list[str]]:
        """
        Добавить игроков в круги 1→2→3→4 по порядку заполнения.
        Circle4 без лимита. Возвращает (добавленные, отклонённые).
        """
        added: list[str] = []
        rejected: list[str] = []

        for name in names:
            name = name.strip()
            if not name:
                continue
            if name in self.all_players:
                rejected.append(name)
                continue

            placed = False
            # Circle1, circle2, circle3 - по captain_count игроков
            # Circle4 - без лимита
            for circle in range(1, 5):
                circle_players = self.circle_list(circle)
                # Circle4 без лимита, остальные по captain_count
                if circle == 4 or len(circle_players) < self.captain_count:
                    circle_players.append(name)
                    self.set_circle_list(circle, circle_players)
                    added.append(name)
                    placed = True
                    break

            if not placed:
                rejected.append(name)

        return added, rejected

    def add_player_to_circle(self, circle: int, name: str, user_id: int | None = None) -> bool:
        """Добавить игрока в конкретный круг. Возвращает True если успешно."""
        name = name.strip()
        if not name or name in self.all_players:
            return False

        circle_players = self.circle_list(circle)
        limit = self.circle_limit(circle)

        # Check limit if not unlimited
        if limit != float('inf') and len(circle_players) >= limit:
            return False

        circle_players.append(name)
        self.set_circle_list(circle, circle_players)

        # Register user_id if provided
        if user_id is not None:
            self.player_user_ids[name] = user_id

        return True

    def remove_player(self, name: str) -> bool:
        """Удалить игрока из любого круга. Возвращает True если найден и удален."""
        name = name.strip()
        for circle in range(1, 5):
            circle_players = self.circle_list(circle)
            # Case-insensitive search
            for i, player in enumerate(circle_players):
                if player.strip().lower() == name.lower():
                    circle_players.pop(i)
                    self.set_circle_list(circle, circle_players)
                    return True
        return False

    # --- Формирование кругов по ELO ---

    async def distribute_by_elo(self, guild_id: int) -> None:
        """Распределить игроков по кругам на основе ELO."""
        from storage.player_stats_store import player_stats_store

        # Collect all players from all circles
        all_players = []
        for circle in range(1, 5):
            circle_list = self.circle_list(circle)
            for player_name in circle_list:
                user_id = self.player_user_ids.get(player_name, 0)
                all_players.append((player_name, user_id))

        # Get ELO for each player
        players_with_elo = []
        for player_name, user_id in all_players:
            stats = await player_stats_store.get(guild_id, user_id)
            elo = stats.elo if stats else 1000  # Default ELO for new players
            players_with_elo.append((player_name, user_id, elo))

        # Sort by ELO (descending)
        players_with_elo.sort(key=lambda x: x[2], reverse=True)

        # Clear all circles
        self.circle1 = []
        self.circle2 = []
        self.circle3 = []
        self.circle4 = []

        # Distribute to circles based on tournament size
        captain_count = self.captain_count

        # Top players become captains (circle1)
        for i in range(captain_count):
            if i < len(players_with_elo):
                player_name, user_id, _ = players_with_elo[i]
                self.circle1.append(player_name)
                self.player_user_ids[player_name] = user_id

        # Next group goes to circle2
        for i in range(captain_count, captain_count * 2):
            if i < len(players_with_elo):
                player_name, user_id, _ = players_with_elo[i]
                self.circle2.append(player_name)
                self.player_user_ids[player_name] = user_id

        # Next group goes to circle3
        for i in range(captain_count * 2, captain_count * 3):
            if i < len(players_with_elo):
                player_name, user_id, _ = players_with_elo[i]
                self.circle3.append(player_name)
                self.player_user_ids[player_name] = user_id

        # Last group goes to circle4
        for i in range(captain_count * 3, captain_count * 4):
            if i < len(players_with_elo):
                player_name, user_id, _ = players_with_elo[i]
                self.circle4.append(player_name)
                self.player_user_ids[player_name] = user_id

    # --- Драфт ---

    def start_draft(self) -> None:
        """Перемешать капитанов и начать драфт."""
        # Sync captains with circle1
        self.captains = list(self.circle1)

        # Shuffle captains randomly
        random.shuffle(self.captains)

        # captain_order is fixed [0, 1, 2, 3...] for PICK_ORDERS to work
        self.captain_order = list(range(self.captain_count))
        self.picks = {str(i): {} for i in range(self.captain_count)}
        self.current_circle = 2
        self.pick_index = 0
        
        # Initialize available players for circles 2, 3, 4
        # All players from circle4 are available for draft
        self.available = {
            "2": list(self.circle2),
            "3": list(self.circle3),
            "4": list(self.circle4),  # All players from circle4
        }

        self.phase = TournamentPhase.DRAFT

    def current_picker_position(self) -> int | None:
        """Позиция капитана, который сейчас выбирает."""
        if self.phase != TournamentPhase.DRAFT:
            return None

        # Check if current circle has available players
        key = str(self.current_circle)
        if key not in self.available or not self.available[key]:
            return None

        circle_orders = PICK_ORDERS.get(self.captain_count, {})
        order_data = circle_orders.get(key, {})
        order = order_data.get("order", [])
        if self.pick_index < len(order):
            # Return the captain index directly from pick order
            return order[self.pick_index]
        return None

    def pick_player(self, position: int, player: str) -> None:
        """Зафиксировать выбор игрока капитаном на позиции position."""
        key = str(self.current_circle)
        # Store picks by position in captain_order
        self.picks[str(position)][key] = player
        self.available[key].remove(player)

    def advance_after_pick(self) -> bool:
        """
        Перейти к следующему шагу драфта.
        Возвращает True, если драфт завершён.
        """
        # Check if current circle has no more players
        key = str(self.current_circle)
        remaining = self.available.get(key, [])
        if not remaining:
            # All players picked, advance to next phase
            return self._advance_circle()

        circle_orders = PICK_ORDERS.get(self.captain_count, {})
        order_data = circle_orders.get(key, {})
        order = order_data.get("order", [])
        self.pick_index += 1

        if self.pick_index >= len(order):
            return self._advance_circle()

        return False


    def _advance_circle(self) -> bool:
        """Перейти к следующему кругу или завершить драфт."""
        self.pick_index = 0

        if self.current_circle >= 4:
            self._build_teams()
            self.phase = TournamentPhase.TEAMS
            return True

        self.current_circle += 1
        return False

    def _build_teams(self) -> None:
        """Сформировать команды из результатов драфта."""
        self.teams = []
        
        for pos in range(self.captain_count):
            captain_idx = self.captain_order[pos]
            captain_name = self.captains[captain_idx]
            picks = self.picks[str(pos)]
            
            team_data = {
                "captain": captain_name,
                "circle1": captain_name,  # Captain is in circle1
                "circle2": picks.get("2", ""),
                "circle3": picks.get("3", ""),
                "circle4": picks.get("4", ""),
            }
            
            self.teams.append(team_data)

    def generate_bracket(self) -> None:
        """Сгенерировать сетку на основе размера турнира."""
        # Only generate if we have teams
        if not self.teams:
            return

        if self.size == TournamentSize.EIGHT:
            # 8 players: straight to final
            self.final_teams = [0, 1]  # First two teams
            self.phase = TournamentPhase.FINAL
        elif self.size == TournamentSize.SIXTEEN:
            # 16 players: semifinals + final
            self.generate_semifinals()
        else:
            # 32 players: qualifiers + semifinals + final
            self.generate_qualifiers()

    def generate_qualifiers(self) -> None:
        """Сгенерировать отборочные матчи для 32 игроков."""
        # All 8 teams play qualifiers: 4 matches, 4 winners advance to semifinals
        # Teams 0 vs 1, 2 vs 3, 4 vs 5, 6 vs 7 - winners advance to semifinals
        self.qualifier_matches = [(0, 1), (2, 3), (4, 5), (6, 7)]
        self.qualifier_winners = [None, None, None, None]
        self.phase = TournamentPhase.QUALIFIERS

    def set_qualifier_winner(self, match_index: int, team_index: int) -> bool:
        """
        Записать победителя отборочного матча.
        Возвращает True, если все отборочные матчи завершены.
        """
        self.qualifier_winners[match_index] = team_index
        if all(w is not None for w in self.qualifier_winners):
            # Qualifier winners advance to semifinals
            self.generate_semifinals_from_qualifiers()
            return True
        return False

    def generate_semifinals_from_qualifiers(self) -> None:
        """Сгенерировать полуфиналы из победителей отборочных."""
        # 4 qualifier winners advance to semifinals: 2 matches
        # Winners of qualifiers[0] vs qualifiers[1], qualifiers[2] vs qualifiers[3]
        winners = self.qualifier_winners
        self.semifinal_matches = [(winners[0], winners[1]), (winners[2], winners[3])]
        self.semifinal_winners = [None, None]
        self.phase = TournamentPhase.SEMIFINALS

    def generate_semifinals(self) -> None:
        """Случайно сгенерировать пары полуфиналов."""
        pairing = random.choice(SEMIFINAL_PAIRINGS)
        self.semifinal_matches = list(pairing)
        self.semifinal_winners = [None, None]
        self.phase = TournamentPhase.SEMIFINALS

    def set_semifinal_winner(self, match_index: int, team_index: int) -> bool:
        """
        Записать победителя полуфинала.
        Возвращает True, если оба полуфинала завершены.
        """
        self.semifinal_winners[match_index] = team_index
        if all(w is not None for w in self.semifinal_winners):
            self.final_teams = list(self.semifinal_winners)  # type: ignore[arg-type]
            self.phase = TournamentPhase.FINAL
            return True
        return False

    def set_final_winner(self, team_index: int) -> None:
        """Записать победителя финала."""
        self.winner_team_index = team_index
        self.phase = TournamentPhase.COMPLETE

    # --- Сериализация ---

    def to_dict(self) -> dict[str, Any]:
        """Преобразовать в словарь для JSON."""
        return {
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "size": self.size.value,
            "registration": self.registration.value,
            "captains": self.captains,
            "circle1": self.circle1,
            "circle2": self.circle2,
            "circle3": self.circle3,
            "circle4": self.circle4,
            "phase": self.phase.value,
            "is_test": self.is_test,
            "circle_limits_enabled": self.circle_limits_enabled,
            "team_names": self.team_names,
            "player_user_ids": self.player_user_ids,
            "captain_order": self.captain_order,
            "picks": self.picks,
            "current_circle": self.current_circle,
            "pick_index": self.pick_index,
            "available": self.available,
            "teams": self.teams,
            "qualifier_matches": [list(m) for m in self.qualifier_matches],
            "qualifier_winners": self.qualifier_winners,
            "semifinal_matches": [list(m) for m in self.semifinal_matches],
            "semifinal_winners": self.semifinal_winners,
            "final_teams": self.final_teams,
            "winner_team_index": self.winner_team_index,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tournament:
        """Восстановить из словаря JSON."""
        t = cls(
            guild_id=data["guild_id"],
            channel_id=data["channel_id"],
            message_id=data.get("message_id", 0),
            size=TournamentSize(data.get("size", "8")),
            registration=RegistrationState(data.get("registration", "closed")),
            captains=data.get("captains", []),
            circle1=data.get("circle1", []),
            circle2=data.get("circle2", []),
            circle3=data.get("circle3", []),
            circle4=data.get("circle4", []),
            phase=TournamentPhase(data.get("phase", "setup")),
            is_test=data.get("is_test", False),
            circle_limits_enabled=data.get("circle_limits_enabled", {2: True, 3: True, 4: True}),
            team_names=data.get("team_names", {}),
            player_user_ids=data.get("player_user_ids", {}),
            captain_order=data.get("captain_order", []),
            picks=data.get("picks", {}),
            current_circle=data.get("current_circle", 2),
            pick_index=data.get("pick_index", 0),
            available=data.get("available", {}),
            teams=data.get("teams", []),
            qualifier_matches=[
                tuple(m) for m in data.get("qualifier_matches", [])
            ],
            qualifier_winners=data.get("qualifier_winners", []),
            semifinal_matches=[
                tuple(m) for m in data.get("semifinal_matches", [])
            ],
            semifinal_winners=data.get("semifinal_winners", []),
            final_teams=data.get("final_teams", []),
            winner_team_index=data.get("winner_team_index"),
        )
        return t
