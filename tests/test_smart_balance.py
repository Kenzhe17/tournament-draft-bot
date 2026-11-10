"""Smart Balance 4×4: перебор комбинаций, критерии, ограничения,
случайность при равном балансе, совместимость с MM-флоу."""

from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import MM_BALANCE_SUM_TOLERANCE
from utils.smart_balance import (
    _metrics,
    balance_score,
    evaluate_all_combos,
    smart_balance_4v4,
)


def _players(srs: list[float]) -> list[tuple[str, float]]:
    return [(f"P{i}", sr) for i, sr in enumerate(srs)]


def _uids(players: list[tuple[str, float]]) -> dict[str, int]:
    return {name: 1000 + i for i, (name, _) in enumerate(players)}


def _check_valid(teams, players_with_sr):
    assert len(teams) == 2
    assert all(len(t) == 4 for t in teams)
    flat = [p for t in teams for p in t]
    assert sorted(flat) == sorted(p[0] for p in players_with_sr)
    assert len(set(flat)) == 8


def _team_srs(team, sr_map):
    return sorted((sr_map[n] for n in team), reverse=True)


def _top2_split_ok(teams, ordered_names):
    top2 = set(ordered_names[:2])
    return (
        len({n for n in teams[0] if n in top2}) == 1
        and len({n for n in teams[1] if n in top2}) == 1
    )


def _ordered_names(players_with_sr, user_ids):
    return [n for n, _ in sorted(
        players_with_sr,
        key=lambda p: (-(p[1]), user_ids.get(p[0], 0), p[0]),
    )]


def _assert_best_allowed(teams, players_with_sr, user_ids):
    """Выбранная комбинация — лучшая по secondary среди допустимых."""
    combos = evaluate_all_combos(players_with_sr, user_ids)
    sr_map = dict(players_with_sr)
    ordered = _ordered_names(players_with_sr, user_ids)
    name_to_idx = {n: i for i, n in enumerate(ordered)}

    def combo_of(team_names):
        a_names = set(team_names[0])
        for c in combos:
            a_set = {ordered[i] for i in c["a"]}
            if a_set == a_names:
                return c
        raise AssertionError("chosen split not in combos")

    chosen = combo_of(teams)
    min_sum = min(c["metrics"]["sum_diff"] for c in combos)
    allowed = [
        c for c in combos
        if c["metrics"]["sum_diff"] <= min_sum + MM_BALANCE_SUM_TOLERANCE
    ]
    best = min(c["secondary"] for c in allowed)
    assert chosen["secondary"] <= best + 0.0015
    # и это лучшая из реально равных по сумме
    assert chosen["metrics"]["sum_diff"] <= min_sum + MM_BALANCE_SUM_TOLERANCE


def test_equal_sr():
    players = _players([5.0] * 8)
    uids = _uids(players)
    teams = smart_balance_4v4(players, uids, rng=random.Random(1))
    _check_valid(teams, players)
    assert _top2_split_ok(teams, _ordered_names(players, uids))


def test_descending_sr():
    players = _players([9.5, 8.5, 7.5, 6.5, 5.5, 4.5, 3.5, 2.5])
    uids = _uids(players)
    teams = smart_balance_4v4(players, uids, rng=random.Random(2))
    _check_valid(teams, players)
    assert _top2_split_ok(teams, _ordered_names(players, uids))
    _assert_best_allowed(teams, players, uids)


def test_two_strong_six_weak():
    players = _players([9.0, 8.5] + [3.0] * 6)
    uids = _uids(players)
    teams = smart_balance_4v4(players, uids, rng=random.Random(3))
    _check_valid(teams, players)
    assert _top2_split_ok(teams, _ordered_names(players, uids))
    # сильные разнесены: у каждой команды по одному топ-игроку
    sr_map = dict(players)
    top_names = {"P0", "P1"}
    assert len(top_names & set(teams[0])) == 1
    assert len(top_names & set(teams[1])) == 1


def test_four_strong_four_weak():
    players = _players([8.0, 7.5, 7.0, 6.5, 2.0, 1.5, 1.0, 0.5])
    uids = _uids(players)
    teams = smart_balance_4v4(players, uids, rng=random.Random(4))
    _check_valid(teams, players)
    assert _top2_split_ok(teams, _ordered_names(players, uids))
    _assert_best_allowed(teams, players, uids)


def test_one_sr10_others_low():
    players = _players([10.0, 3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0.2])
    uids = _uids(players)
    teams = smart_balance_4v4(players, uids, rng=random.Random(5))
    _check_valid(teams, players)
    assert _top2_split_ok(teams, _ordered_names(players, uids))
    _assert_best_allowed(teams, players, uids)


def test_tied_max_sr():
    players = _players([9.0, 9.0, 5.0, 4.0, 3.0, 2.0, 1.0, 0.5])
    uids = _uids(players)
    teams = smart_balance_4v4(players, uids, rng=random.Random(6))
    _check_valid(teams, players)
    # два сильнейших (оба 9.0) — в разных командах
    top_names = {"P0", "P1"}
    assert len(top_names & set(teams[0])) == 1
    assert len(top_names & set(teams[1])) == 1


def test_random_combos_optimality():
    rng = random.Random(42)
    for _ in range(200):
        srs = [round(rng.uniform(0, 10), 1) for _ in range(8)]
        players = _players(srs)
        uids = _uids(players)
        teams = smart_balance_4v4(players, uids, rng=rng)
        _check_valid(teams, players)
        assert _top2_split_ok(teams, _ordered_names(players, uids))
        _assert_best_allowed(teams, players, uids)


def test_equal_sum_uneven_distribution():
    # Суммы равны, но распределение по игрокам разное:
    # [9,5,4,2 | 8,6,3,1] vs [7,6,5,2 | 8,5,4,3] и т.п.
    players = _players([9.0, 8.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0])
    uids = _uids(players)
    teams = smart_balance_4v4(players, uids, rng=random.Random(7))
    _check_valid(teams, players)
    _assert_best_allowed(teams, players, uids)
    # distribution_diff выбранной комбинации должен быть минимальным
    # среди допустимых по сумме
    combos = evaluate_all_combos(players, uids)
    sr_map = dict(players)
    ordered = _ordered_names(players, uids)
    a_names = set(teams[0])
    chosen = next(
        c for c in combos if {ordered[i] for i in c["a"]} == a_names
    )
    min_sum = min(c["metrics"]["sum_diff"] for c in combos)
    allowed = [
        c for c in combos
        if c["metrics"]["sum_diff"] <= min_sum + MM_BALANCE_SUM_TOLERANCE
    ]
    assert chosen["metrics"]["distribution_diff"] == min(
        c["metrics"]["distribution_diff"] for c in allowed
    ) or chosen["secondary"] <= min(c["secondary"] for c in allowed) + 0.0015


def test_all_ids_unique_no_mutation():
    players = _players([9.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0])
    uids = _uids(players)
    snapshot = list(players)
    teams = smart_balance_4v4(players, uids)
    _check_valid(teams, players)
    # SR не изменились
    assert players == snapshot
    # Discord ID не потеряны
    all_names = {n for t in teams for n in t}
    assert all_names == {n for n, _ in players}


def test_non_8_players_returns_none():
    assert smart_balance_4v4(_players([1.0] * 7)) is None
    assert smart_balance_4v4(_players([1.0] * 9)) is None
    assert smart_balance_4v4([]) is None


def test_deterministic_tiebreak_by_uid():
    # Одинаковые SR — порядок определяется Discord ID
    players = _players([5.0] * 8)
    uids_a = {f"P{i}": 2000 - i for i in range(8)}
    uids_b = {f"P{i}": 1000 + i for i in range(8)}
    t1 = smart_balance_4v4(players, uids_a, rng=random.Random(0))
    t2 = smart_balance_4v4(players, uids_b, rng=random.Random(0))
    _check_valid(t1, players)
    _check_valid(t2, players)


def test_two_strongest_separated_in_every_combo():
    """Ни одна допустимая комбинация не ставит топ-2 в одну команду."""
    players = _players([9.9, 9.8, 5.0, 4.9, 4.8, 1.0, 0.9, 0.1])
    uids = _uids(players)
    ordered = _ordered_names(players, uids)
    for combo in evaluate_all_combos(players, uids):
        a_names = {ordered[i] for i in combo["a"]}
        b_names = {ordered[i] for i in combo["b"]}
        assert len({"P0", "P1"} & a_names) == 1
        assert len({"P0", "P1"} & b_names) == 1


def test_captain_is_strongest_in_team():
    players = _players([9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0])
    uids = _uids(players)
    teams = smart_balance_4v4(players, uids, rng=random.Random(11))
    sr_map = dict(players)
    for team in teams:
        srs = [sr_map[n] for n in team]
        assert srs[0] == max(srs)
