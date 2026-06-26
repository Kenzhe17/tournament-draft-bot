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


# Порядок выбора по кругам: индексы позиций капитанов (0–3)
PICK_ORDERS: dict[int, dict[str, list[int] | int]] = {
    2: {"order": [0, 1, 2], "auto": 3},
    3: {"order": [3, 2, 1], "auto": 0},
    4: {"order": [0, 1, 2], "auto": 3},
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
    captains: list[str] = field(default_factory=list)  # Now nicknames, not user IDs
    circle1: list[str] = field(default_factory=list)  # Captain circle (for display)
    circle2: list[str] = field(default_factory=list)
    circle3: list[str] = field(default_factory=list)
    circle4: list[str] = field(default_factory=list)

    phase: TournamentPhase = TournamentPhase.SETUP
    is_test: bool = False

    # Драфт
    captain_order: list[int] = field(default_factory=list)
    picks: dict[str, dict[str, str]] = field(default_factory=dict)
    current_circle: int = 2
    pick_index: int = 0
    available: dict[str, list[str]] = field(default_factory=dict)
    last_auto_pick_message: str = ""

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
    def all_players(self) -> set[str]:
        """Все добавленные игроки (круги 1–4)."""
        return set(self.circle1 + self.circle2 + self.circle3 + self.circle4)

    @property
    def is_setup_complete(self) -> bool:
        """Готов ли турнир к запуску драфта."""
        if len(self.captains) != 4:
            return False
        
        # Circle1, circle2, circle3 должны быть по 4 игрока
        # Circle4 без лимита
        for circle_num in range(1, 4):
            circle_list = getattr(self, f"circle{circle_num}")
            if len(circle_list) != 4:
                return False
        return True

    def circle_list(self, circle: int) -> list[str]:
        """Получить список игроков круга."""
        return getattr(self, f"circle{circle}")

    def set_circle_list(self, circle: int, players: list[str]) -> None:
        """Установить список игроков круга."""
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
            # Circle1, circle2, circle3 - по 4 игрока
            # Circle4 - без лимита
            for circle in range(1, 5):
                circle_players = self.circle_list(circle)
                # Circle4 без лимита, остальные по 4
                if circle == 4 or len(circle_players) < 4:
                    circle_players.append(name)
                    self.set_circle_list(circle, circle_players)
                    added.append(name)
                    placed = True
                    break

            if not placed:
                rejected.append(name)

        return added, rejected

    def add_player_to_circle(self, circle: int, name: str) -> bool:
        """Добавить игрока в конкретный круг. Возвращает True если успешно."""
        name = name.strip()
        if not name or name in self.all_players:
            return False
        
        circle_players = self.circle_list(circle)
        # Circle4 без лимита, остальные по 4
        if circle != 4 and len(circle_players) >= 4:
            return False
        
        circle_players.append(name)
        self.set_circle_list(circle, circle_players)
        return True

    def remove_player(self, name: str) -> bool:
        """Удалить игрока из любого круга. Возвращает True если найден и удален."""
        name = name.strip()
        for circle in range(1, 5):
            circle_players = self.circle_list(circle)
            if name in circle_players:
                circle_players.remove(name)
                self.set_circle_list(circle, circle_players)
                return True
        return False

    # --- Драфт ---

    def start_draft(self) -> None:
        """Перемешать капитанов и начать драфт."""
        self.captain_order = list(range(4))
        random.shuffle(self.captain_order)
        self.picks = {str(i): {} for i in range(4)}
        self.current_circle = 2
        self.pick_index = 0
        
        # Initialize available players for circles 2, 3, 4
        # All players from circle4 are available for draft
        self.available = {
            "2": list(self.circle2),
            "3": list(self.circle3),
            "4": list(self.circle4),
        }
        
        self.last_auto_pick_message = ""
        self.phase = TournamentPhase.DRAFT

    def current_picker_position(self) -> int | None:
        """Позиция капитана (0–3), который сейчас выбирает."""
        if self.phase != TournamentPhase.DRAFT:
            return None
        if self.current_circle > 4:
            return None
        
        order = PICK_ORDERS[self.current_circle]["order"]
        if self.pick_index < len(order):
            return order[self.pick_index]
        return None

    def auto_picker_position(self) -> int:
        """Позиция капитана с автоматическим выбором в текущем круге."""
        return PICK_ORDERS[self.current_circle]["auto"]  # type: ignore[return-value]

    def pick_player(self, position: int, player: str) -> None:
        """Зафиксировать выбор игрока капитаном на позиции position."""
        self.last_auto_pick_message = ""
        key = str(self.current_circle)
        self.picks[str(position)][key] = player
        self.available[key].remove(player)

    def advance_after_pick(self) -> bool:
        """
        Перейти к следующему шагу драфта.
        Возвращает True, если драфт завершён.
        """
        # Special handling for circle4 with more than 4 players
        if self.current_circle == 4:
            key = str(self.current_circle)
            remaining = self.available.get(key, [])
            if len(remaining) > 0:
                # Continue round-robin until all players are picked
                self.pick_index += 1
                return False
            else:
                # All players picked, advance to next phase
                return self._advance_circle()
        
        order = PICK_ORDERS[self.current_circle]["order"]
        self.pick_index += 1

        if self.pick_index >= len(order):
            self._do_auto_pick()
            return self._advance_circle()

        return False

    def _do_auto_pick(self) -> None:
        """Автоматически назначить последнего игрока круга."""
        # Skip auto-pick for circle4 if there are more than 4 players initially
        if self.current_circle == 4:
            key = str(self.current_circle)
            remaining = self.available.get(key, [])
            if len(remaining) > 1:
                # More than 1 player remaining - skip auto-pick, continue drafting
                return
        
        auto_pos = self.auto_picker_position()
        key = str(self.current_circle)
        remaining = self.available.get(key, [])
        if remaining:
            player = remaining[0]
            self.pick_player(auto_pos, player)
            captain_name = self.captains[self.captain_order[auto_pos]]
            self.last_auto_pick_message = (
                f" **{captain_name}** автоматически получает **{player}**"
            )

    def _advance_circle(self) -> bool:
        """Перейти к следующему кругу или завершить драфт."""
        self.pick_index = 0

        if self.current_circle >= 4:
            self.last_auto_pick_message = ""
            self._build_teams()
            self.phase = TournamentPhase.TEAMS
            return True

        self.current_circle += 1
        return False

    def _build_teams(self) -> None:
        """Сформировать команды из результатов драфта."""
        self.teams = []
        
        # Collect all circle4 picks in order
        circle4_picks = []
        for pos in range(4):
            pick = self.picks[str(pos)].get("4", "")
            if pick:
                circle4_picks.append((pos, pick))
        
        # Only first 4 circle4 picks participate in tournament
        participating_circle4 = circle4_picks[:4]
        participating_by_pos = {pos: name for pos, name in participating_circle4}
        
        for pos in range(4):
            captain_idx = self.captain_order[pos]
            captain_name = self.captains[captain_idx]
            picks = self.picks[str(pos)]
            
            team_data = {
                "captain": captain_name,
                "circle1": captain_name,  # Captain is in circle1
                "circle2": picks.get("2", ""),
                "circle3": picks.get("3", ""),
                "circle4": participating_by_pos.get(pos, ""),  # Only if in first 4
            }
            
            self.teams.append(team_data)

    def generate_bracket(self) -> None:
        """Сгенерировать сетку на основе размера турнира."""
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
        # Simple bracket: 4 teams play qualifiers, winners go to semifinals
        # Teams 0 vs 1, 2 vs 3 - winners advance to semifinals
        self.qualifier_matches = [(0, 1), (2, 3)]
        self.qualifier_winners = [None, None]
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
        # Qualifier winners become the semifinal participants
        pairing = random.choice(SEMIFINAL_PAIRINGS)
        # Map qualifier winners to semifinal positions
        self.semifinal_matches = list(pairing)
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
            "captain_order": self.captain_order,
            "picks": self.picks,
            "current_circle": self.current_circle,
            "pick_index": self.pick_index,
            "available": self.available,
            "last_auto_pick_message": self.last_auto_pick_message,
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
            captain_order=data.get("captain_order", []),
            picks=data.get("picks", {}),
            current_circle=data.get("current_circle", 2),
            pick_index=data.get("pick_index", 0),
            available=data.get("available", {}),
            last_auto_pick_message=data.get("last_auto_pick_message", ""),
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
