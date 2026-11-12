"""Формирование команд для Tournaments 2.0.

balanced_sr: сортировка по SR 0–10 → 4 равные группы →
первая группа — капитаны → каждая команда получает по
одному игроку из каждой группы (змейка) → локальная
оптимизация сумм SR (минимум разницы max-min, затем
дисперсия). Детерминировано при фиксированном seed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

NEUTRAL_SR = 5.0


@dataclass
class SRPlayer:
    user_id: int
    name: str = ""
    sr: float = NEUTRAL_SR


def team_count_for(players: int) -> int:
    """16 игроков → 4 команды; 32 → 8 команд (по 4 игрока)."""
    if players == 16:
        return 4
    if players == 32:
        return 8
    raise ValueError("balanced_sr requires 16 or 32 players")


def _groups(players: list[SRPlayer]) -> list[list[SRPlayer]]:
    """Отсортировать по SR и разбить на 4 равные группы
    (от сильных к слабым)."""
    ordered = sorted(players, key=lambda p: (-p.sr, p.user_id))
    size = len(ordered) // 4
    return [ordered[i * size:(i + 1) * size] for i in range(4)]


def _team_sum(team: list[SRPlayer]) -> float:
    return sum(p.sr for p in team)


def _spread(teams: list[list[SRPlayer]]) -> tuple[float, float]:
    """(max-min разница сумм, дисперсия) — меньше = лучше."""
    sums = [_team_sum(t) for t in teams]
    spread = max(sums) - min(sums)
    mean = sum(sums) / len(sums)
    var = sum((s - mean) ** 2 for s in sums) / len(sums)
    return spread, var


def _initial_assignment(
    groups: list[list[SRPlayer]], n_teams: int
) -> list[list[SRPlayer]]:
    """Каждая команда получает по одному игроку из каждой
    группы: змейка по убыванию SR."""
    teams: list[list[SRPlayer]] = [[] for _ in range(n_teams)]
    for g_idx, group in enumerate(groups):
        # group size == n_teams (по одному игроку на команду)
        order = (
            range(n_teams) if g_idx % 2 == 0
            else range(n_teams - 1, -1, -1)
        )
        for rank, team_i in enumerate(order):
            teams[team_i].append(group[rank])
    return teams


def _local_optimize(
    teams: list[list[SRPlayer]], n_teams: int,
    rng: random.Random, iterations: int = 6000
) -> list[list[SRPlayer]]:
    """Детерминированный hill-climbing: случайный обмен
    игроков между командами, принимаем только улучшение."""
    cur = [list(t) for t in teams]
    cur_score = _spread(cur)
    best = [list(t) for t in cur]
    best_score = cur_score
    for _ in range(iterations):
        t1, t2 = rng.sample(range(n_teams), 2)
        i1 = rng.randrange(len(cur[t1]))
        # игроки из одной «группы» (позиция в команде) —
        # обмен только внутри одного ранга сохраняет
        # представление «по одному из каждой группы»
        i2 = i1
        cur[t1][i1], cur[t2][i2] = cur[t2][i2], cur[t1][i1]
        score = _spread(cur)
        if score < cur_score or (
            score == cur_score and rng.random() < 0.05
        ):
            cur_score = score
            if score < best_score:
                best = [list(t) for t in cur]
                best_score = score
        else:
            cur[t1][i1], cur[t2][i2] = cur[t2][i2], cur[t1][i1]
    return best


def form_teams_balanced_sr(
    players: list[SRPlayer], seed: int | None = None
) -> tuple[list[list[SRPlayer]], list[float]]:
    """Вернуть (команды, суммы SR), отсортированные по сумме
    убыв. (seed 1 = сильнейшая команда). Детерминировано.

    players: ровно 16 (→4 команды) или 32 (→8 команд).
    """
    n_teams = team_count_for(len(players))
    if seed is None:
        seed = hash(tuple(sorted(p.user_id for p in players))) & 0xFFFFFFFF
    rng = random.Random(seed)
    groups = _groups(players)
    teams = _initial_assignment(groups, n_teams)
    teams = _local_optimize(teams, n_teams, rng)
    sums = [_team_sum(t) for t in teams]
    order = sorted(range(n_teams), key=lambda i: (-sums[i], i))
    ordered = [teams[i] for i in order]
    return ordered, [sums[i] for i in order]
