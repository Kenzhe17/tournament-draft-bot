"""Движок сетки «Ультиматум» 2.0 — Double Elimination.

Чистый Python без зависимостей от Discord/БД: все переходы
предопределены шаблоном, слоты ссылаются на конкретные матчи
(winner/loser) или прямые entrant'ы. Используется и «вживую»
(через store/service), и в симуляциях тестов.

Форматы:
- SOLO: 16 entrants (1 игрок = 1 entrant), 30–31 матч.
- DUO:  8 entrants (команда = 2 игрока), 14–15 матчей.

Статусы матчей:
WAITING / READY / IN_PROGRESS / AWAITING_CONFIRMATION /
DISPUTED / COMPLETED / CANCELLED.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Format(str, Enum):
    SOLO = "solo"
    DUO = "duo"


class MatchStatus(str, Enum):
    WAITING = "WAITING"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    DISPUTED = "DISPUTED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class EntrantStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ELIMINATED = "ELIMINATED"
    CHAMPION = "CHAMPION"
    RUNNER_UP = "RUNNER_UP"


@dataclass
class Entrant:
    """Участник сетки: одиночный игрок или дуэт."""

    id: str
    seed: int
    member_ids: tuple[int, ...]
    member_names: tuple[str, ...] = ()
    wins: int = 0
    losses: int = 0
    status: EntrantStatus = EntrantStatus.ACTIVE
    final_place: Optional[int] = None

    @property
    def eliminated(self) -> bool:
        return self.losses >= 2

    def label(self) -> str:
        if len(self.member_ids) == 1:
            return self.member_names[0] if self.member_names else f"#{self.member_ids[0]}"
        names = " & ".join(self.member_names) if self.member_names else " & ".join(
            f"<{m}>" for m in self.member_ids
        )
        return names


@dataclass(frozen=True)
class Slot:
    """Источник слота матча.

    kind:
      seed:<n>      — entrant с посевом n (только матчи U1)
      winner:<key>  — победитель матча key
      loser:<key>   — проигравший матча key
      entrant:<id>  — конкретный entrant (GF2)
    """

    kind: str
    ref: str

    @staticmethod
    def seed(n: int) -> "Slot":
        return Slot("seed", str(n))

    @staticmethod
    def winner(key: str) -> "Slot":
        return Slot("winner", key)

    @staticmethod
    def loser(key: str) -> "Slot":
        return Slot("loser", key)

    @staticmethod
    def entrant(eid: str) -> "Slot":
        return Slot("entrant", eid)

    def __str__(self) -> str:  # pragma: no cover - debug
        return f"{self.kind}:{self.ref}"


@dataclass
class MatchSpec:
    key: str
    bracket: str          # 'W' | 'L' | 'GF'
    round_name: str       # 'Верхняя 1', 'Нижняя 3', 'Гранд-финал'
    a: Slot
    b: Slot
    gf_part: int = 0      # 1 для GF1, 2 для GF2


@dataclass
class MatchState:
    spec: MatchSpec
    status: MatchStatus = MatchStatus.WAITING
    entrant_a: Optional[str] = None
    entrant_b: Optional[str] = None
    winner_id: Optional[str] = None
    loser_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Шаблоны сеток
# ---------------------------------------------------------------------------

# Соло: посев 1–16
_SOLO_PAIRINGS = [
    (1, 16), (8, 9), (4, 13), (5, 12),
    (2, 15), (7, 10), (3, 14), (6, 11),
]


def _solo_template() -> list[MatchSpec]:
    specs: list[MatchSpec] = []
    # Верхняя сетка
    for i, (s1, s2) in enumerate(_SOLO_PAIRINGS, 1):
        specs.append(MatchSpec(f"U1-{i}", "W", "Верхняя сетка · Раунд 1",
                               Slot.seed(s1), Slot.seed(s2)))
    for i in range(1, 5):
        specs.append(MatchSpec(
            f"U2-{i}", "W", "Верхняя сетка · Раунд 2",
            Slot.winner(f"U1-{2 * i - 1}"), Slot.winner(f"U1-{2 * i}"),
        ))
    for i in range(1, 3):
        specs.append(MatchSpec(
            f"U3-{i}", "W", "Верхняя сетка · Раунд 3",
            Slot.winner(f"U2-{2 * i - 1}"), Slot.winner(f"U2-{2 * i}"),
        ))
    specs.append(MatchSpec("U4-1", "W", "Верхняя сетка · Финал",
                           Slot.winner("U3-1"), Slot.winner("U3-2")))
    # Нижняя сетка
    for i in range(1, 5):
        specs.append(MatchSpec(
            f"L1-{i}", "L", "Нижняя сетка · Раунд 1",
            Slot.loser(f"U1-{2 * i - 1}"), Slot.loser(f"U1-{2 * i}"),
        ))
    for i in range(1, 5):
        specs.append(MatchSpec(
            f"L2-{i}", "L", "Нижняя сетка · Раунд 2",
            Slot.winner(f"L1-{i}"), Slot.loser(f"U2-{i}"),
        ))
    for i in range(1, 3):
        specs.append(MatchSpec(
            f"L3-{i}", "L", "Нижняя сетка · Раунд 3",
            Slot.winner(f"L2-{2 * i - 1}"), Slot.winner(f"L2-{2 * i}"),
        ))
    specs.append(MatchSpec("L4-1", "L", "Нижняя сетка · Раунд 4",
                           Slot.loser("U3-1"), Slot.winner("L3-1")))
    specs.append(MatchSpec("L4-2", "L", "Нижняя сетка · Раунд 4",
                           Slot.loser("U3-2"), Slot.winner("L3-2")))
    specs.append(MatchSpec("L5-1", "L", "Нижняя сетка · Раунд 5",
                           Slot.winner("L4-1"), Slot.winner("L4-2")))
    specs.append(MatchSpec("L6-1", "L", "Нижняя сетка · Финал",
                           Slot.winner("L5-1"), Slot.loser("U4-1")))
    # Гранд-финал
    specs.append(MatchSpec("GF1", "GF", "Гранд-финал",
                           Slot.winner("U4-1"), Slot.winner("L6-1"), gf_part=1))
    return specs


# Дуо: посев 1–8
_DUO_PAIRINGS = [(1, 8), (4, 5), (2, 7), (3, 6)]


def _duo_template() -> list[MatchSpec]:
    specs: list[MatchSpec] = []
    for i, (s1, s2) in enumerate(_DUO_PAIRINGS, 1):
        specs.append(MatchSpec(f"U1-{i}", "W", "Верхняя сетка · Раунд 1",
                               Slot.seed(s1), Slot.seed(s2)))
    for i in range(1, 3):
        specs.append(MatchSpec(
            f"U2-{i}", "W", "Верхняя сетка · Раунд 2",
            Slot.winner(f"U1-{2 * i - 1}"), Slot.winner(f"U1-{2 * i}"),
        ))
    specs.append(MatchSpec("U3-1", "W", "Верхняя сетка · Финал",
                           Slot.winner("U2-1"), Slot.winner("U2-2")))
    for i in range(1, 3):
        specs.append(MatchSpec(
            f"L1-{i}", "L", "Нижняя сетка · Раунд 1",
            Slot.loser(f"U1-{2 * i - 1}"), Slot.loser(f"U1-{2 * i}"),
        ))
    for i in range(1, 3):
        specs.append(MatchSpec(
            f"L2-{i}", "L", "Нижняя сетка · Раунд 2",
            Slot.winner(f"L1-{i}"), Slot.loser(f"U2-{i}"),
        ))
    specs.append(MatchSpec("L3-1", "L", "Нижняя сетка · Раунд 3",
                           Slot.winner("L2-1"), Slot.winner("L2-2")))
    specs.append(MatchSpec("L4-1", "L", "Нижняя сетка · Финал",
                           Slot.loser("U3-1"), Slot.winner("L3-1")))
    specs.append(MatchSpec("GF1", "GF", "Гранд-финал",
                           Slot.winner("U3-1"), Slot.winner("L4-1"), gf_part=1))
    return specs


TEMPLATES = {
    Format.SOLO: _solo_template,
    Format.DUO: _duo_template,
}

# Ключи матчей для распределения мест.
# Каждый элемент: (диапазон мест, ключ или список ключей).
# Проигравшие матчей группы делят диапазон мест; порядок внутри
# группы официально не назначается (place_group).
_PLACEMENT_SOLO = [
    ((3, 3), "L6-1"),
    ((4, 4), "L5-1"),
    ((5, 6), ["L4-1", "L4-2"]),
    ((7, 8), ["L3-1", "L3-2"]),
    ((9, 12), ["L2-1", "L2-2", "L2-3", "L2-4"]),
    ((13, 16), ["L1-1", "L1-2", "L1-3", "L1-4"]),
]
_PLACEMENT_DUO = [
    ((3, 3), "L4-1"),
    ((4, 4), "L3-1"),
    ((5, 6), ["L2-1", "L2-2"]),
    ((7, 8), ["L1-1", "L1-2"]),
]


def entrant_count(fmt: Format) -> int:
    return 16 if fmt is Format.SOLO else 8


def _new_entrant_id() -> str:
    return uuid.uuid4().hex[:12]


class Bracket:
    """Состояние одной сетки турнира (in-memory).

    entrants: id -> Entrant
    matches: key -> MatchState
    match_order: ключи в порядке шаблона (+GF2 в конец)
    """

    def __init__(self, fmt: Format):
        self.format = fmt
        self.entrants: dict[str, Entrant] = {}
        self.matches: dict[str, MatchState] = {}
        self.match_order: list[str] = []
        for spec in TEMPLATES[fmt]():
            self.matches[spec.key] = MatchState(spec=spec)
            self.match_order.append(spec.key)

    # -- Участники --

    def add_entrant(
        self,
        member_ids: tuple[int, ...],
        member_names: tuple[str, ...] = (),
        seed: Optional[int] = None,
    ) -> Entrant:
        if seed is None:
            seed = max((e.seed for e in self.entrants.values()), default=0) + 1
        e = Entrant(
            id=_new_entrant_id(),
            seed=seed,
            member_ids=tuple(member_ids),
            member_names=tuple(member_names),
        )
        self.entrants[e.id] = e
        return e

    def entrant_by_seed(self, seed: int) -> Optional[Entrant]:
        for e in self.entrants.values():
            if e.seed == seed:
                return e
        return None

    # -- Разрешение слотов --

    def _resolve_slot(self, slot: Slot) -> Optional[str]:
        """Вернуть entrant_id для слота или None если ещё не определён."""
        if slot.kind == "seed":
            e = self.entrant_by_seed(int(slot.ref))
            return e.id if e else None
        if slot.kind == "entrant":
            return slot.ref if slot.ref in self.entrants else None
        m = self.matches.get(slot.ref)
        if m is None or m.status != MatchStatus.COMPLETED:
            return None
        return m.winner_id if slot.kind == "winner" else m.loser_id

    def refresh(self) -> list[str]:
        """Заполнить слоты всех незавершённых матчей.

        Возвращает ключи матчей, перешедших в READY.
        Идемпотентно: уже заполненные слоты не перезаписываются,
        COMPLETED/CANCELLED не трогаются.
        """
        newly_ready: list[str] = []
        for key in self.match_order:
            m = self.matches[key]
            if m.status in (MatchStatus.COMPLETED, MatchStatus.CANCELLED):
                continue
            if m.entrant_a is None:
                m.entrant_a = self._resolve_slot(m.spec.a)
            if m.entrant_b is None:
                m.entrant_b = self._resolve_slot(m.spec.b)
            if (
                m.entrant_a is not None
                and m.entrant_b is not None
                and m.status == MatchStatus.WAITING
            ):
                m.status = MatchStatus.READY
                newly_ready.append(key)
        return newly_ready

    # -- Проведение матча --

    def ready_matches(self) -> list[MatchState]:
        return [
            m for m in (self.matches[k] for k in self.match_order)
            if m.status == MatchStatus.READY
        ]

    def active_matches(self) -> list[MatchState]:
        return [
            m for m in (self.matches[k] for k in self.match_order)
            if m.status in (
                MatchStatus.READY,
                MatchStatus.IN_PROGRESS,
                MatchStatus.AWAITING_CONFIRMATION,
                MatchStatus.DISPUTED,
            )
        ]

    def get_match(self, key: str) -> Optional[MatchState]:
        return self.matches.get(key)

    def match_is_playable(self, key: str) -> bool:
        m = self.matches.get(key)
        return bool(
            m
            and m.status
            in (
                MatchStatus.READY,
                MatchStatus.IN_PROGRESS,
                MatchStatus.AWAITING_CONFIRMATION,
            )
            and m.entrant_a is not None
            and m.entrant_b is not None
        )

    def set_room_started(self, key: str) -> None:
        """READY -> IN_PROGRESS (комната создана / матч начат)."""
        m = self.matches.get(key)
        if m and m.status == MatchStatus.READY:
            m.status = MatchStatus.IN_PROGRESS

    def set_awaiting(self, key: str) -> None:
        m = self.matches.get(key)
        if m and m.status in (MatchStatus.READY, MatchStatus.IN_PROGRESS):
            m.status = MatchStatus.AWAITING_CONFIRMATION

    def set_disputed(self, key: str) -> None:
        m = self.matches.get(key)
        if m and m.status != MatchStatus.COMPLETED:
            m.status = MatchStatus.DISPUTED

    def apply_result(
        self, key: str, winner_id: str
    ) -> tuple[Optional[MatchState], Optional[MatchSpec], list[str]]:
        """Зафиксировать результат матча.

        Возвращает (завершённый MatchState, новый MatchSpec|None для GF2,
        список ключей, перешедших в READY).
        Бросает ValueError при невалидном переходе.
        """
        m = self.matches.get(key)
        if m is None:
            raise ValueError(f"match {key} not found")
        if m.status in (MatchStatus.COMPLETED, MatchStatus.CANCELLED):
            raise ValueError(f"match {key} already {m.status.value}")
        if m.entrant_a is None or m.entrant_b is None:
            raise ValueError(f"match {key} slots not filled")
        if winner_id not in (m.entrant_a, m.entrant_b):
            raise ValueError("winner must be one of the two sides")

        winner = self.entrants[winner_id]
        loser_id = m.entrant_b if winner_id == m.entrant_a else m.entrant_a
        loser = self.entrants[loser_id]

        m.status = MatchStatus.COMPLETED
        m.winner_id = winner_id
        m.loser_id = loser_id

        winner.wins += 1
        loser.losses += 1
        if loser.eliminated:
            loser.status = EntrantStatus.ELIMINATED

        # GF2: нижняя сетка выиграла GF1 → обе стороны по 1 поражению.
        new_spec: Optional[MatchSpec] = None
        if m.spec.gf_part == 1 and self._entrant_came_from_lower(winner_id):
            new_spec = MatchSpec(
                "GF2", "GF", "Гранд-финал · Решающий матч",
                Slot.entrant(m.entrant_a), Slot.entrant(m.entrant_b),
                gf_part=2,
            )
            self.matches["GF2"] = MatchState(
                spec=new_spec,
                status=MatchStatus.READY,
                entrant_a=m.entrant_a,
                entrant_b=m.entrant_b,
            )
            self.match_order.append("GF2")

        newly_ready = self.refresh()
        return m, new_spec, newly_ready

    def _entrant_came_from_lower(self, entrant_id: str) -> bool:
        """Прошёл ли entrant через нижнюю сетку (есть поражение)."""
        e = self.entrants.get(entrant_id)
        return bool(e and e.losses > 0)

    # -- Завершение и места --

    def is_finished(self) -> bool:
        """Турнир завершён, когда сыгран последний нужный GF-матч."""
        gf1 = self.matches.get("GF1")
        if gf1 is None or gf1.status != MatchStatus.COMPLETED:
            return False
        if not self._entrant_came_from_lower(gf1.winner_id):
            return True  # верхняя сетка выиграла GF1 — reset не нужен
        gf2 = self.matches.get("GF2")
        return bool(gf2 and gf2.status == MatchStatus.COMPLETED)

    def champion_id(self) -> Optional[str]:
        if not self.is_finished():
            return None
        gf1 = self.matches["GF1"]
        if not self._entrant_came_from_lower(gf1.winner_id):
            return gf1.winner_id
        return self.matches["GF2"].winner_id

    def runner_up_id(self) -> Optional[str]:
        if not self.is_finished():
            return None
        gf1 = self.matches["GF1"]
        if not self._entrant_came_from_lower(gf1.winner_id):
            return gf1.loser_id
        return self.matches["GF2"].loser_id

    def compute_placements(self) -> dict[str, tuple[Optional[int], Optional[str]]]:
        """Итоговые места: entrant_id -> (place, place_group).

        place — точное место (1–4 и одиночные), place_group —
        метка диапазона для групп («9–12»). Внутри группы порядок
        не назначается: все её участники получают place=None и
        одинаковый place_group.
        """
        n = entrant_count(self.format)
        places: dict[str, tuple[Optional[int], Optional[str]]] = {}

        champ = self.champion_id()
        runner = self.runner_up_id()
        if champ:
            places[champ] = (1, None)
        if runner:
            places[runner] = (2, None)

        groups = _PLACEMENT_SOLO if self.format is Format.SOLO else _PLACEMENT_DUO
        for (lo, hi), keys in groups:
            group_label = f"{lo}–{hi}" if lo != hi else str(lo)
            exact = lo if lo == hi else None
            key_list = keys if isinstance(keys, list) else [keys]
            for k in key_list:
                m = self.matches.get(k)
                if m and m.status == MatchStatus.COMPLETED and m.loser_id:
                    eid = m.loser_id
                    if eid not in places:
                        places[eid] = (exact, group_label)

        # Страховка: завершённый турнир не должен оставить entrant'ов
        # без места — помечаем их последним диапазоном.
        if self.is_finished():
            for e in self.entrants.values():
                if e.id not in places:
                    places[e.id] = (None, f"{n}–{n}")
        return places

    # -- Валидация для симуляций/тестов --

    def validate_invariants(self) -> list[str]:
        """Проверить инварианты сетки. Пустой список = всё ок."""
        errors: list[str] = []
        active_in_matches: dict[str, int] = {}
        for key in self.match_order:
            m = self.matches[key]
            if m.status == MatchStatus.COMPLETED:
                continue
            if m.status in (
                MatchStatus.READY,
                MatchStatus.IN_PROGRESS,
                MatchStatus.AWAITING_CONFIRMATION,
                MatchStatus.DISPUTED,
            ):
                for eid in (m.entrant_a, m.entrant_b):
                    if eid:
                        active_in_matches[eid] = active_in_matches.get(eid, 0) + 1
        for eid, cnt in active_in_matches.items():
            if cnt > 1:
                errors.append(f"entrant {eid} in {cnt} active matches")
        for e in self.entrants.values():
            if e.eliminated and e.status != EntrantStatus.ELIMINATED:
                errors.append(f"entrant {e.id} has 2 losses but active")
        return errors


def simulate(fmt: Format, rng: Optional[random.Random] = None) -> Bracket:
    """Полная симуляция турнира со случайными победителями.

    Используется тестами (10k+ прогонов). Возвращает завершённый
    Bracket — вызывающий код проверяет инварианты.
    """
    rng = rng or random.Random()
    b = Bracket(fmt)
    n = entrant_count(fmt)
    # Тестовые участники: имена T1..Tn, члены — уникальные id.
    for i in range(1, n + 1):
        if fmt is Format.SOLO:
            b.add_entrant((10_000_000 + i,), (f"T{i}",), seed=i)
        else:
            b.add_entrant(
                (10_000_000 + 2 * i - 1, 10_000_000 + 2 * i),
                (f"T{i}a", f"T{i}b"),
                seed=i,
            )
    b.refresh()
    guard = 0
    while not b.is_finished() and guard < 200:
        guard += 1
        progressed = False
        for m in list(b.active_matches()):
            if m.status == MatchStatus.DISPUTED:
                # В симуляции спор решается случайно одной из сторон.
                winner = rng.choice([m.entrant_a, m.entrant_b])
            elif m.status in (
                MatchStatus.READY,
                MatchStatus.IN_PROGRESS,
                MatchStatus.AWAITING_CONFIRMATION,
            ):
                winner = rng.choice([m.entrant_a, m.entrant_b])
            else:
                continue
            try:
                b.apply_result(m.spec.key, winner)
                progressed = True
            except ValueError:
                continue
        if not progressed:
            break
    return b
