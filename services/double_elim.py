"""Double Elimination engine v2 — граф матчей для 16/32 игроков.

Чистый Python-модуль без Discord-зависимостей. Состояние
сериализуется в tournament.bracket_v2 и переживает рестарт
Railway вместе с ocr_sessions.

Матчи идентифицируются стабильными ID: U1..U7 (верхняя сетка),
L1..L6 (нижняя), G1/G2 (гранд-финал). В OCR-интеграции
match_type="bracket_v2", match_index — числовой индекс,
однозначно сопоставленный match_id (см. MATCH_INDEX).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class MatchStatus(str, Enum):
    WAITING = "waiting"      # участники не определены
    READY = "ready"          # обе команды известны
    PLAYING = "playing"      # матч начался (идёт загрузка OCR)
    REVIEW = "review"        # результат на проверке
    COMPLETED = "completed"  # результат подтверждён
    CANCELLED = "cancelled"  # административное аннулирование


# Источник слота: ("team", team_index) | ("match", match_id, "winner"|"loser")
SlotSource = tuple


@dataclass
class DEMatch:
    match_id: str
    bracket: str            # "upper" | "lower" | "grand_final"
    round_number: int
    slot_a_source: SlotSource | None = None
    slot_b_source: SlotSource | None = None
    team_a: int | None = None
    team_b: int | None = None
    status: MatchStatus = MatchStatus.WAITING
    winner_team: int | None = None
    loser_team: int | None = None
    started_at: str = ""
    completed_at: str = ""
    ocr_session_key: str = ""
    result_version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "bracket": self.bracket,
            "round_number": self.round_number,
            "slot_a_source": list(self.slot_a_source) if self.slot_a_source else None,
            "slot_b_source": list(self.slot_b_source) if self.slot_b_source else None,
            "team_a": self.team_a,
            "team_b": self.team_b,
            "status": self.status.value,
            "winner_team": self.winner_team,
            "loser_team": self.loser_team,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "ocr_session_key": self.ocr_session_key,
            "result_version": self.result_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DEMatch":
        def _src(v):
            if not v:
                return None
            t = tuple(v)
            # ("team", idx) или ("match", mid, side)
            return t

        return cls(
            match_id=d["match_id"],
            bracket=d["bracket"],
            round_number=int(d.get("round_number", 0)),
            slot_a_source=_src(d.get("slot_a_source")),
            slot_b_source=_src(d.get("slot_b_source")),
            team_a=d.get("team_a"),
            team_b=d.get("team_b"),
            status=MatchStatus(d.get("status", "waiting")),
            winner_team=d.get("winner_team"),
            loser_team=d.get("loser_team"),
            started_at=d.get("started_at", ""),
            completed_at=d.get("completed_at", ""),
            ocr_session_key=d.get("ocr_session_key", ""),
            result_version=int(d.get("result_version", 0)),
        )


# ---------------------------------------------------------------------------
# Определения сеток: (match_id, bracket, round, slot_a_source, slot_b_source)
# Источники: ("seed", n) — исходная команда по seed; ("match", mid, side).
# ---------------------------------------------------------------------------

_BRACKET_4_TEAMS: list[tuple] = [
    # Верхняя сетка
    ("U1", "upper", 1, ("seed", 1), ("seed", 4)),
    ("U2", "upper", 1, ("seed", 2), ("seed", 3)),
    ("U3", "upper", 2, ("match", "U1", "winner"), ("match", "U2", "winner")),
    # Нижняя сетка
    ("L1", "lower", 1, ("match", "U1", "loser"), ("match", "U2", "loser")),
    ("L2", "lower", 2, ("match", "L1", "winner"), ("match", "U3", "loser")),
    # Гранд-финал
    ("G1", "grand_final", 1,
     ("match", "U3", "winner"), ("match", "L2", "winner")),
]

_BRACKET_8_TEAMS: list[tuple] = [
    # Верхняя сетка
    ("U1", "upper", 1, ("seed", 1), ("seed", 8)),
    ("U2", "upper", 1, ("seed", 4), ("seed", 5)),
    ("U3", "upper", 1, ("seed", 2), ("seed", 7)),
    ("U4", "upper", 1, ("seed", 3), ("seed", 6)),
    ("U5", "upper", 2, ("match", "U1", "winner"), ("match", "U2", "winner")),
    ("U6", "upper", 2, ("match", "U3", "winner"), ("match", "U4", "winner")),
    ("U7", "upper", 3, ("match", "U5", "winner"), ("match", "U6", "winner")),
    # Нижняя сетка
    ("L1", "lower", 1, ("match", "U1", "loser"), ("match", "U2", "loser")),
    ("L2", "lower", 1, ("match", "U3", "loser"), ("match", "U4", "loser")),
    ("L3", "lower", 2, ("match", "L1", "winner"), ("match", "U6", "loser")),
    ("L4", "lower", 2, ("match", "L2", "winner"), ("match", "U5", "loser")),
    ("L5", "lower", 3, ("match", "L3", "winner"), ("match", "L4", "winner")),
    ("L6", "lower", 4, ("match", "L5", "winner"), ("match", "U7", "loser")),
    # Гранд-финал
    ("G1", "grand_final", 1,
     ("match", "U7", "winner"), ("match", "L6", "winner")),
]

# Стабильный числовой match_index для OCR/Discord:
# bracket_v2:<match_index> ↔ match_id.
MATCH_INDEX: dict[str, int] = {
    mid: i + 1
    for i, (mid, *_rest) in enumerate(_BRACKET_4_TEAMS + _BRACKET_8_TEAMS)
    if mid not in {"G2"}
}
# G2 не имеет фиксированного индекса в базовой сетке — создаётся
# лениво; его индекс = max+1 соответствующего размера.
_MATCH_INDEX_BY_SIZE = {
    4: {mid: i + 1 for i, (mid, *_r) in enumerate(_BRACKET_4_TEAMS)},
    8: {mid: i + 1 for i, (mid, *_r) in enumerate(_BRACKET_8_TEAMS)},
}
G2_INDEX = {4: len(_BRACKET_4_TEAMS) + 1, 8: len(_BRACKET_8_TEAMS) + 1}


def match_index_for(match_id: str, team_count: int) -> int:
    """Числовой match_index для OCR session key."""
    if match_id == "G2":
        return G2_INDEX.get(team_count, 16)
    return _MATCH_INDEX_BY_SIZE.get(team_count, {}).get(match_id, 0)


def match_id_for_index(match_index: int, team_count: int) -> str | None:
    inv = {v: k for k, v in _MATCH_INDEX_BY_SIZE.get(team_count, {}).items()}
    if match_index == G2_INDEX.get(team_count):
        return "G2"
    return inv.get(match_index)


class DoubleElimEngine:
    """Граф Double Elimination. team_a/team_b — индексы команд
    в tournament.teams (0-based)."""

    def __init__(self, team_count: int, seeded_teams: list[int],
                 tournament_id: str = ""):
        if team_count not in (4, 8):
            raise ValueError("team_count must be 4 or 8")
        if len(seeded_teams) != team_count:
            raise ValueError(
                f"seeded_teams must contain {team_count} teams"
            )
        if len(set(seeded_teams)) != team_count:
            raise ValueError("seeded_teams contains duplicates")
        self.team_count = team_count
        self.tournament_id = tournament_id
        # seed n (1-based) → team index
        self._seeds = {n: t for n, t in enumerate(seeded_teams, start=1)}
        self._losses: dict[int, int] = {t: 0 for t in seeded_teams}
        self._matches: dict[str, DEMatch] = {}
        spec = _BRACKET_4_TEAMS if team_count == 4 else _BRACKET_8_TEAMS
        for mid, bracket, rnd, sa, sb in spec:
            m = DEMatch(match_id=mid, bracket=bracket, round_number=rnd)
            m.slot_a_source = self._resolve_static(sa)
            m.slot_b_source = self._resolve_static(sb)
            self._matches[mid] = m
        self._finalize_ready()

    # -- внутреннее ------------------------------------------------------

    def _resolve_static(self, src: SlotSource) -> SlotSource:
        """("seed", n) → ("team", team_index); match-источники как есть."""
        if src[0] == "seed":
            return ("team", self._seeds[src[1]])
        return src

    def _slot_team(self, src: SlotSource | None) -> int | None:
        if src is None:
            return None
        if src[0] == "team":
            return src[1]
        if src[0] == "match":
            dep = self._matches.get(src[1])
            if dep is None or dep.status != MatchStatus.COMPLETED:
                return None
            return dep.winner_team if src[2] == "winner" else dep.loser_team
        return None

    def _refresh_match(self, m: DEMatch) -> None:
        """Пересчитать слоты/статус из источников. Не трогает
        завершённые/отменённые матчи."""
        if m.status in (MatchStatus.COMPLETED, MatchStatus.CANCELLED):
            return
        a = self._slot_team(m.slot_a_source)
        b = self._slot_team(m.slot_b_source)
        if m.team_a is None and a is not None:
            m.team_a = a
        if m.team_b is None and b is not None:
            m.team_b = b
        if m.team_a is not None and m.team_b is not None:
            if m.status == MatchStatus.WAITING:
                m.status = MatchStatus.READY
        else:
            if m.status == MatchStatus.READY:
                m.status = MatchStatus.WAITING

    def _finalize_ready(self) -> None:
        for m in self._matches.values():
            self._refresh_match(m)

    # -- публичное API ---------------------------------------------------

    def get_match(self, match_id: str) -> DEMatch | None:
        return self._matches.get(match_id)

    def get_ready_matches(self) -> list[DEMatch]:
        self._finalize_ready()
        return [
            m for m in self._matches.values()
            if m.status == MatchStatus.READY
        ]

    def start_match(self, match_id: str) -> DEMatch:
        m = self._matches[match_id]
        self._refresh_match(m)
        if m.status == MatchStatus.READY:
            m.status = MatchStatus.PLAYING
            m.started_at = datetime.now().isoformat()
        return m

    def cancel_match(self, match_id: str) -> DEMatch:
        m = self._matches[match_id]
        if m.status not in (MatchStatus.COMPLETED,):
            m.status = MatchStatus.CANCELLED
        return m

    def complete_match(self, match_id: str, winner_team: int) -> DEMatch:
        """Зафиксировать результат. Идемпотентно: повторный вызов
        с тем же победителем — no-op; с другим — ValueError."""
        m = self._matches[match_id]
        if m.status == MatchStatus.COMPLETED:
            if m.winner_team == winner_team:
                return m
            raise ValueError(
                f"match {match_id} already completed with "
                f"winner {m.winner_team}"
            )
        if m.status == MatchStatus.CANCELLED:
            raise ValueError(f"match {match_id} is cancelled")
        self._refresh_match(m)
        if m.status not in (MatchStatus.READY, MatchStatus.PLAYING,
                            MatchStatus.REVIEW):
            raise ValueError(
                f"match {match_id} not ready (status={m.status.value})"
            )
        if winner_team not in (m.team_a, m.team_b):
            raise ValueError(
                f"team {winner_team} does not play in {match_id}"
            )
        loser = m.team_b if winner_team == m.team_a else m.team_a
        m.winner_team = winner_team
        m.loser_team = loser
        m.status = MatchStatus.COMPLETED
        m.completed_at = datetime.now().isoformat()
        self._losses[winner_team] = self._losses.get(winner_team, 0)
        self._losses[loser] = self._losses.get(loser, 0) + 1
        self._maybe_create_g2(m)
        self._finalize_ready()
        return m

    def _maybe_create_g2(self, g1: DEMatch) -> None:
        """G1: победа верхней сетки → чемпион; победа нижней → G2."""
        if g1.match_id != "G1" or "G2" in self._matches:
            return
        upper_winner_src = g1.slot_a_source  # G1.a = w(U3/U7)
        # Определяем, какая ветка выиграла: сравниваем команду-
        # источник. team_a G1 — финалист верхней сетки.
        if g1.winner_team == g1.team_a:
            return  # верхняя выиграла — G2 не нужен
        # Нижняя выиграла G1 → обе по 1 поражению → G2:
        # слот A = проигравший G1 (верхняя), слот B = победивший.
        g2 = DEMatch(
            match_id="G2", bracket="grand_final", round_number=2,
            slot_a_source=("match", "G1", "loser"),
            slot_b_source=("match", "G1", "winner"),
        )
        self._matches["G2"] = g2
        self._refresh_match(g2)

    def get_champion(self) -> int | None:
        g1 = self._matches.get("G1")
        g2 = self._matches.get("G2")
        if g2 is not None:
            if g2.status == MatchStatus.COMPLETED:
                return g2.winner_team
            return None
        if g1 is not None and g1.status == MatchStatus.COMPLETED:
            # G2 не создан → победила верхняя сетка
            if g1.winner_team == g1.team_a:
                return g1.winner_team
        return None

    def is_finished(self) -> bool:
        return self.get_champion() is not None

    def get_standings(self) -> list[tuple[int, int]]:
        """Итоговые места: [(place, team_index)] по реальным
        результатам сетки (проигравший матч выбывания)."""
        champ = self.get_champion()
        places: list[tuple[int, int]] = []
        if champ is not None:
            places.append((1, champ))
        # Второе место — финалист
        g1 = self._matches.get("G1")
        g2 = self._matches.get("G2")
        final = g2 if g2 is not None else g1
        if final is not None and final.status == MatchStatus.COMPLETED:
            runner = (
                final.loser_team if g2 is not None
                else (final.team_b if final.winner_team == final.team_a
                      else final.team_a)
            )
            # G1 без G2: финалист — команда нижней сетки (team_b)
            if g2 is None:
                runner = final.team_b
            if runner is not None and runner != champ:
                places.append((2, runner))
        # Места 3+ — по таблице size
        if self.team_count == 4:
            elim = [("L2", 3), ("L1", 4)]
        else:
            elim = [
                ("L6", 3), ("L5", 4),
                ("L3", 5), ("L4", 6),
                ("L1", 7), ("L2", 8),
            ]
            # L3/L4 и L1/L2 — пары с одинаковым местом: порядок
            # внутри пары не различим → обе получают диапазон.
        for mid, place in elim:
            m = self._matches.get(mid)
            if m is not None and m.status == MatchStatus.COMPLETED:
                if m.loser_team is not None:
                    places.append((place, m.loser_team))
        # Нормализация дублей мест для 32 (L3/L4 → 5–6 и т.д.)
        return places

    def team_losses(self, team_index: int) -> int:
        return self._losses.get(team_index, 0)

    # -- сериализация ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 2,
            "team_count": self.team_count,
            "tournament_id": self.tournament_id,
            "seeds": {str(k): v for k, v in self._seeds.items()},
            "losses": {str(k): v for k, v in self._losses.items()},
            "matches": {
                mid: m.to_dict() for mid, m in self._matches.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DoubleElimEngine":
        eng = cls.__new__(cls)
        eng.team_count = int(d["team_count"])
        eng.tournament_id = d.get("tournament_id", "")
        eng._seeds = {int(k): v for k, v in (d.get("seeds") or {}).items()}
        eng._losses = {
            int(k): v for k, v in (d.get("losses") or {}).items()
        }
        eng._matches = {
            mid: DEMatch.from_dict(md)
            for mid, md in (d.get("matches") or {}).items()
        }
        eng._finalize_ready()
        return eng
