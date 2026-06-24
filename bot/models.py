"""Модели данных турнира."""

from __future__ import annotations

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


@dataclass
class DraftState:
    """Состояние активного драфта."""

    captain_order: list[int] = field(default_factory=list)
    current_circle: int = 2
    pick_index: int = 0
    picks: dict[int, dict[int, str]] = field(default_factory=dict)
    teams: dict[int, list[str]] = field(default_factory=dict)
    auto_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "captain_order": self.captain_order,
            "current_circle": self.current_circle,
            "pick_index": self.pick_index,
            "picks": {str(k): v for k, v in self.picks.items()},
            "teams": {str(k): v for k, v in self.teams.items()},
            "auto_message": self.auto_message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DraftState | None:
        if not data:
            return None
        picks = {int(k): v for k, v in data.get("picks", {}).items()}
        teams = {int(k): v for k, v in data.get("teams", {}).items()}
        return cls(
            captain_order=data.get("captain_order", []),
            current_circle=data.get("current_circle", 2),
            pick_index=data.get("pick_index", 0),
            picks=picks,
            teams=teams,
            auto_message=data.get("auto_message"),
        )


@dataclass
class BracketState:
    """Состояние плей-офф после драфта."""

    semifinal_pairs: list[tuple[int, int]] = field(default_factory=list)
    semifinal_winners: dict[int, int] = field(default_factory=dict)
    final_pair: tuple[int, int] | None = None
    winner_team: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "semifinal_pairs": [list(p) for p in self.semifinal_pairs],
            "semifinal_winners": {str(k): v for k, v in self.semifinal_winners.items()},
            "final_pair": list(self.final_pair) if self.final_pair else None,
            "winner_team": self.winner_team,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> BracketState | None:
        if not data:
            return None
        pairs = [tuple(p) for p in data.get("semifinal_pairs", [])]
        winners = {int(k): v for k, v in data.get("semifinal_winners", {}).items()}
        final_raw = data.get("final_pair")
        final_pair = tuple(final_raw) if final_raw else None
        return cls(
            semifinal_pairs=pairs,
            semifinal_winners=winners,
            final_pair=final_pair,
            winner_team=data.get("winner_team"),
        )


@dataclass
class Tournament:
    """Полное состояние турнира в одном канале."""

    guild_id: int
    channel_id: int
    message_id: int | None = None
    captains: list[int] = field(default_factory=list)
    circles: dict[str, list[str]] = field(default_factory=lambda: {"2": [], "3": [], "4": []})
    phase: TournamentPhase = TournamentPhase.SETUP
    draft: DraftState | None = None
    bracket: BracketState | None = None

    def all_players(self) -> set[str]:
        """Все добавленные игроки (без капитанов)."""
        players: set[str] = set()
        for circle_players in self.circles.values():
            players.update(circle_players)
        return players

    def is_setup_complete(self) -> bool:
        """Проверка готовности к драфту."""
        if len(self.captains) != 4:
            return False
        for circle in ("2", "3", "4"):
            if len(self.circles.get(circle, [])) != 4:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "captains": self.captains,
            "circles": self.circles,
            "phase": self.phase.value,
            "draft": self.draft.to_dict() if self.draft else None,
            "bracket": self.bracket.to_dict() if self.bracket else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tournament:
        return cls(
            guild_id=data["guild_id"],
            channel_id=data["channel_id"],
            message_id=data.get("message_id"),
            captains=data.get("captains", []),
            circles=data.get("circles", {"2": [], "3": [], "4": []}),
            phase=TournamentPhase(data.get("phase", TournamentPhase.SETUP.value)),
            draft=DraftState.from_dict(data.get("draft")),
            bracket=BracketState.from_dict(data.get("bracket")),
        )
