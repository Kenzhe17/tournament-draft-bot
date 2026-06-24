"""Модель данных турнира."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TournamentPhase(str, Enum):
    """Фазы жизненного цикла турнира."""

    SETUP = "setup"
    DRAFT = "draft"
    TEAMS = "teams"
    SEMIFINALS = "semifinals"
    FINAL = "final"
    COMPLETE = "complete"


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
    captains: list[int] = field(default_factory=list)
    circle2: list[str] = field(default_factory=list)
    circle3: list[str] = field(default_factory=list)
    circle4: list[str] = field(default_factory=list)

    phase: TournamentPhase = TournamentPhase.SETUP

    # Драфт
    captain_order: list[int] = field(default_factory=list)
    picks: dict[str, dict[str, str]] = field(default_factory=dict)
    current_circle: int = 2
    pick_index: int = 0
    available: dict[str, list[str]] = field(default_factory=dict)
    last_auto_pick_message: str = ""

    # Команды и плей-офф
    teams: list[dict[str, Any]] = field(default_factory=list)
    semifinal_matches: list[tuple[int, int]] = field(default_factory=list)
    semifinal_winners: list[int | None] = field(default_factory=list)
    final_teams: list[int] = field(default_factory=list)
    winner_team_index: int | None = None

    # --- Свойства ---

    @property
    def all_players(self) -> set[str]:
        """Все добавленные игроки (круги 2–4)."""
        return set(self.circle2 + self.circle3 + self.circle4)

    @property
    def is_setup_complete(self) -> bool:
        """Готов ли турнир к запуску драфта."""
        return (
            len(self.captains) == 4
            and len(self.circle2) == 4
            and len(self.circle3) == 4
            and len(self.circle4) == 4
        )

    def circle_list(self, circle: int) -> list[str]:
        """Получить список игроков круга."""
        return getattr(self, f"circle{circle}")

    def set_circle_list(self, circle: int, players: list[str]) -> None:
        """Установить список игроков круга."""
        setattr(self, f"circle{circle}", players)

    # --- Добавление игроков ---

    def add_players(self, names: list[str]) -> tuple[list[str], list[str]]:
        """
        Добавить игроков в круги 2→3→4 по порядку заполнения.
        Возвращает (добавленные, отклонённые).
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
            for circle in (2, 3, 4):
                circle_players = self.circle_list(circle)
                if len(circle_players) < 4:
                    circle_players.append(name)
                    self.set_circle_list(circle, circle_players)
                    added.append(name)
                    placed = True
                    break

            if not placed:
                rejected.append(name)

        return added, rejected

    # --- Драфт ---

    def start_draft(self) -> None:
        """Перемешать капитанов и начать драфт."""
        self.captain_order = list(range(4))
        random.shuffle(self.captain_order)
        self.picks = {str(i): {} for i in range(4)}
        self.current_circle = 2
        self.pick_index = 0
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
        order = PICK_ORDERS[self.current_circle]["order"]
        self.pick_index += 1

        if self.pick_index >= len(order):
            self._do_auto_pick()
            return self._advance_circle()

        return False

    def _do_auto_pick(self) -> None:
        """Автоматически назначить последнего игрока круга."""
        auto_pos = self.auto_picker_position()
        key = str(self.current_circle)
        remaining = self.available[key]
        if remaining:
            player = remaining[0]
            self.pick_player(auto_pos, player)
            self.last_auto_pick_message = (
                f"✅ <@{self.captains[self.captain_order[auto_pos]]}> "
                f"автоматически получает **{player}**"
            )

    def _advance_circle(self) -> bool:
        """Перейти к следующему кругу или завершить драфт."""
        self.pick_index = 0

        if self.current_circle == 4:
            self.last_auto_pick_message = ""
            self._build_teams()
            self.phase = TournamentPhase.TEAMS
            return True

        self.current_circle += 1
        return False

    def _build_teams(self) -> None:
        """Сформировать команды из результатов драфта."""
        self.teams = []
        for pos in range(4):
            captain_idx = self.captain_order[pos]
            captain_id = self.captains[captain_idx]
            picks = self.picks[str(pos)]
            self.teams.append(
                {
                    "captain_id": captain_id,
                    "circle2": picks.get("2", ""),
                    "circle3": picks.get("3", ""),
                    "circle4": picks.get("4", ""),
                }
            )

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
            "captains": self.captains,
            "circle2": self.circle2,
            "circle3": self.circle3,
            "circle4": self.circle4,
            "phase": self.phase.value,
            "captain_order": self.captain_order,
            "picks": self.picks,
            "current_circle": self.current_circle,
            "pick_index": self.pick_index,
            "available": self.available,
            "last_auto_pick_message": self.last_auto_pick_message,
            "teams": self.teams,
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
            captains=data.get("captains", []),
            circle2=data.get("circle2", []),
            circle3=data.get("circle3", []),
            circle4=data.get("circle4", []),
            phase=TournamentPhase(data.get("phase", "setup")),
            captain_order=data.get("captain_order", []),
            picks=data.get("picks", {}),
            current_circle=data.get("current_circle", 2),
            pick_index=data.get("pick_index", 0),
            available=data.get("available", {}),
            last_auto_pick_message=data.get("last_auto_pick_message", ""),
            teams=data.get("teams", []),
            semifinal_matches=[
                tuple(m) for m in data.get("semifinal_matches", [])
            ],
            semifinal_winners=data.get("semifinal_winners", []),
            final_teams=data.get("final_teams", []),
            winner_team_index=data.get("winner_team_index"),
        )
        return t
