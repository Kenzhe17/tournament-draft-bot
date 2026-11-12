"""Balanced SR — автоматическая комплектация команд по Skill Rating (Tournaments 2.0).

Правила:
- Игроки сортируются по SR по убыванию и делятся на 4 группы силы
  (по num_teams игроков в каждой).
- Каждая команда получает ровно одного игрока из каждой группы.
- Цель — минимизировать разницу суммарного SR между командами.
- Никакого посева по SR в сетке: порядок команд перемешивается случайно.

Чистые функции без Discord-зависимостей — покрываются unit-тестами.
"""

from __future__ import annotations

import random
from typing import Sequence

PlayersWithSR = Sequence[tuple[str, float]]


def duplicate_user_ids(user_ids: dict[str, int]) -> list[int]:
    """Discord ID (>0), встречающиеся у двух и более имён игроков."""
    seen: set[int] = set()
    dups: list[int] = []
    for uid in user_ids.values():
        if not uid or uid <= 0:
            continue
        if uid in seen and uid not in dups:
            dups.append(uid)
        seen.add(uid)
    return sorted(dups)


def _sort_key(player: tuple[str, float], user_ids: dict[str, int] | None):
    name, sr = player
    uid = (user_ids or {}).get(name, 0) or 0
    return (-sr, uid, name)


def _spread(totals: list[float]) -> float:
    return max(totals) - min(totals)


def balanced_sr_teams(
    players_with_sr: PlayersWithSR,
    user_ids: dict[str, int] | None = None,
    *,
    rng: random.Random | None = None,
) -> list[list[str]] | None:
    """Распределить игроков на команды по 4 человека (Balanced SR).

    Args:
        players_with_sr: список (имя, SR); SR в диапазоне 0–10.
        user_ids: имя → Discord ID для стабильного тай-брейка.
        rng: генератор случайных чисел для выбора при равных суммах.

    Returns:
        Список команд; в каждой 4 имени, сильнейший — первым (капитан).
        Порядок команд случайный (без посева по SR). None если
        количество игроков не делится на 4 или меньше 8.
    """
    n = len(players_with_sr)
    if n < 8 or n % 4 != 0:
        return None
    num_teams = n // 4
    if num_teams < 2:
        return None

    ordered = sorted(players_with_sr, key=lambda p: _sort_key(p, user_ids))
    srs = {name: float(sr) for name, sr in players_with_sr}

    # 4 группы силы: 0 — сильнейшие num_teams, 3 — слабейшие
    groups = [
        ordered[i * num_teams:(i + 1) * num_teams]
        for i in range(4)
    ]

    rnd = rng or random.Random()
    totals = [0.0] * num_teams
    # assignment[g][team_idx] = имя игрока из группы g в команде team_idx
    assignment: list[list[str | None]] = [
        [None] * num_teams for _ in range(4)
    ]

    # Жадная инициализация: группы от сильной к слабой, внутри группы
    # по убыванию SR; игрок достаётся команде с наименьшей текущей суммой.
    for g in range(4):
        members = sorted(groups[g], key=lambda p: (-p[1], p[0]))
        team_order = sorted(range(num_teams), key=lambda t: totals[t])
        for k, (name, sr) in enumerate(members):
            # k-й сильнейший группы → k-я команда с наименьшей суммой
            team_idx = team_order[k]
            assignment[g][team_idx] = name
            totals[team_idx] += float(sr)

    # Локальный поиск: обмен игроками одной и той же группы между
    # командами, если это уменьшает разброс суммарного SR.
    for _ in range(400):
        improved = False
        for g in range(4):
            for i in range(num_teams):
                for j in range(i + 1, num_teams):
                    ni, nj = assignment[g][i], assignment[g][j]
                    if ni is None or nj is None:
                        continue
                    si, sj = srs[ni], srs[nj]
                    if abs(si - sj) < 1e-12:
                        continue
                    new_totals = list(totals)
                    new_totals[i] += sj - si
                    new_totals[j] += si - sj
                    if _spread(new_totals) < _spread(totals) - 1e-9:
                        assignment[g][i], assignment[g][j] = nj, ni
                        totals = new_totals
                        improved = True
        if not improved:
            break

    teams: list[list[str]] = []
    for t in range(num_teams):
        members = [assignment[g][t] for g in range(4)]
        members = [m for m in members if m is not None]
        # Сильнейший — первым (капитан)
        members.sort(key=lambda n: _sort_key((n, srs[n]), user_ids))
        teams.append(members)

    # Без посева по SR: порядок команд случаен
    rnd.shuffle(teams)
    return teams
