"""Smart Balance — балансировка 8 игроков на 2 команды 4×4 по Skill Rating.

Полный перебор всех допустимых разбиений (20 комбинаций: игрок №1
фиксирован в команде A, игрок №2 обязан попасть в B). Для каждой
комбинации считается многокритериальная оценка:

- разница суммарного SR;
- разница сумм двух сильнейших игроков каждой команды;
- сумма попарных разниц отсортированных по убыванию составов;
- разница внутреннего разброса (max − min) команд.

Двухступенчатая оптимизация: сначала отбираются комбинации с разницей
суммарного SR не более минимально достижимой + MM_BALANCE_SUM_TOLERANCE,
затем среди них выбирается лучшая по остальным критериям. При равных
оценках (в пределах MM_BALANCE_EPSILON) выбор случайный.

Единственное жёсткое ограничение — два сильнейших игрока в разных
командах. Остальные правила — мягкие, через весовую оценку.
"""

from __future__ import annotations

import itertools
import random
from typing import Callable, Sequence

from config import (
    MM_BALANCE_EPSILON,
    MM_BALANCE_SUM_TOLERANCE,
    MM_BALANCE_WEIGHTS,
)

PlayersWithSR = Sequence[tuple[str, float]]


def _sort_key(
    player: tuple[str, float],
    user_ids: dict[str, int] | None,
) -> tuple[float, int, str]:
    """SR по убыванию, при равенстве — стабильный порядок по Discord ID."""
    name, sr = player
    uid = (user_ids or {}).get(name, 0) or 0
    return (-sr, uid, name)


def _metrics(srs_a: list[float], srs_b: list[float]) -> dict[str, float]:
    """Четыре критерия дисбаланса для пары отсортированных составов."""
    sum_diff = abs(sum(srs_a) - sum(srs_b))
    top_diff = abs(sum(srs_a[:2]) - sum(srs_b[:2]))
    dist_diff = sum(abs(a - b) for a, b in zip(srs_a, srs_b))
    spread_diff = abs(
        (srs_a[0] - srs_a[-1]) - (srs_b[0] - srs_b[-1])
    )
    return {
        "sum_diff": sum_diff,
        "top_diff": top_diff,
        "distribution_diff": dist_diff,
        "spread_diff": spread_diff,
    }


def balance_score(metrics: dict[str, float]) -> float:
    """Итоговая взвешенная оценка дисбаланса (меньше — лучше)."""
    w = MM_BALANCE_WEIGHTS
    return (
        w["sum"] * metrics["sum_diff"] / 40.0
        + w["top"] * metrics["top_diff"] / 20.0
        + w["dist"] * metrics["distribution_diff"] / 40.0
        + w["spread"] * metrics["spread_diff"] / 10.0
    )


def _secondary_score(metrics: dict[str, float]) -> float:
    """Оценка по критериям кроме суммарного SR (веса нормализованы)."""
    w = MM_BALANCE_WEIGHTS
    norm = w["top"] + w["dist"] + w["spread"] or 1.0
    return (
        w["top"] * metrics["top_diff"] / 20.0
        + w["dist"] * metrics["distribution_diff"] / 40.0
        + w["spread"] * metrics["spread_diff"] / 10.0
    ) / norm


def smart_balance_4v4(
    players_with_sr: PlayersWithSR,
    user_ids: dict[str, int] | None = None,
    *,
    rng: random.Random | None = None,
) -> list[list[str]] | None:
    """Разбить ровно 8 игроков на 2 команды по 4 (имена игроков).

    Args:
        players_with_sr: список (имя, SR); SR в диапазоне 0–10.
        user_ids: имя → Discord ID для стабильного тай-брейка.
        rng: генератор случайных чисел для выбора при равном балансе.

    Returns:
        [team_a, team_b] — по 4 имени, сильнейший игрок первым
        (капитан), или None если игроков не 8.
    """
    if len(players_with_sr) != 8:
        return None

    ordered = sorted(players_with_sr, key=lambda p: _sort_key(p, user_ids))
    names = [p[0] for p in ordered]
    srs = [float(p[1]) for p in ordered]

    # Игрок 0 фиксирован в команде A; игрок 1 обязан быть в B —
    # два сильнейших всегда в разных командах. Остальные 3 места A
    # выбираем из игроков 2..7: C(6,3) = 20 комбинаций.
    combos = []
    for rest in itertools.combinations(range(2, 8), 3):
        idx_a = frozenset({0, *rest})
        idx_b = frozenset(range(8)) - idx_a
        srs_a = sorted((srs[i] for i in idx_a), reverse=True)
        srs_b = sorted((srs[i] for i in idx_b), reverse=True)
        m = _metrics(srs_a, srs_b)
        combos.append({
            "a": idx_a,
            "b": idx_b,
            "metrics": m,
            "score": balance_score(m),
            "secondary": _secondary_score(m),
        })

    # Этап 1: ограничить разницу суммарного SR.
    min_sum = min(c["metrics"]["sum_diff"] for c in combos)
    allowed = [
        c for c in combos
        if c["metrics"]["sum_diff"] <= min_sum + MM_BALANCE_SUM_TOLERANCE
    ]

    # Этап 2: лучший вариант по остальным критериям.
    best_secondary = min(c["secondary"] for c in allowed)
    tied = [
        c for c in allowed
        if c["secondary"] <= best_secondary + MM_BALANCE_EPSILON
    ]
    chosen = (rng or random).choice(tied)

    def _team_members(indices: frozenset[int]) -> list[str]:
        # Сильнейший — первым (капитан), дальше по убыванию SR.
        return sorted(
            (names[i] for i in indices),
            key=lambda n: _sort_key((n, dict(players_with_sr)[n]), user_ids),
        )

    return [_team_members(chosen["a"]), _team_members(chosen["b"])]


def evaluate_all_combos(
    players_with_sr: PlayersWithSR,
    user_ids: dict[str, int] | None = None,
) -> list[dict]:
    """Все допустимые комбинации с метриками — для тестов/симуляций."""
    if len(players_with_sr) != 8:
        return []
    ordered = sorted(players_with_sr, key=lambda p: _sort_key(p, user_ids))
    names = [p[0] for p in ordered]
    srs = [float(p[1]) for p in ordered]
    out = []
    for rest in itertools.combinations(range(2, 8), 3):
        idx_a = frozenset({0, *rest})
        idx_b = frozenset(range(8)) - idx_a
        srs_a = sorted((srs[i] for i in idx_a), reverse=True)
        srs_b = sorted((srs[i] for i in idx_b), reverse=True)
        m = _metrics(srs_a, srs_b)
        out.append({
            "a": idx_a,
            "b": idx_b,
            "metrics": m,
            "score": balance_score(m),
            "secondary": _secondary_score(m),
        })
    return out
