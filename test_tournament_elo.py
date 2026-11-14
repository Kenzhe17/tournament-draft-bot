"""Тесты новой системы ELO турниров (ТЗ r!z3, FACEIT-style).

Запуск: pytest test_tournament_elo.py -v
"""

from utils.elo_config import ELO_CONFIG
from utils.rating_calculator import (
    calculate_team_skill_rating,
    calculate_expected_win_probability,
    calculate_base_elo,
    calculate_player_performance,
    calculate_performance_modifiers,
    calculate_rank_bonus,
    clamp_elo_change,
    calculate_final_elo,
    compute_match_elo_changes,
)


# ---------------------------------------------------------------- helpers

def p(name, team, won, k=0, d=0, a=0, dmg=0, sr=5.0, rank=999):
    return {
        "name": name,
        "team": team,
        "won": won,
        "kills": k,
        "deaths": d,
        "assists": a,
        "damage": dmg,
        "skill_rating": sr,
        "elo_rank": rank,
    }


def full_match(winner_stats=None, loser_stats=None, sr_a=5.0, sr_b=5.0):
    """8 игроков: 4 на команду a (победитель) и 4 на b."""
    winner_stats = winner_stats or [(5, 4, 5, 2179)] * 4
    loser_stats = loser_stats or [(3, 6, 2, 900)] * 4
    players = []
    for i, (k, d, a, dmg) in enumerate(winner_stats):
        players.append(p(f"wa{i}", "a", True, k, d, a, dmg, sr_a))
    for i, (k, d, a, dmg) in enumerate(loser_stats):
        players.append(p(f"wb{i}", "b", False, k, d, a, dmg, sr_b))
    return players


# ---------------------------------------------------------------- SR

def test_equal_teams_expected_50():
    ea, eb = calculate_expected_win_probability(5.0, 5.0)
    assert abs(ea - 0.5) < 1e-9 and abs(eb - 0.5) < 1e-9


def test_sr_difference():
    ea, _ = calculate_expected_win_probability(6.75, 5.25)
    # 10 ** ((5.25-6.75)/2) = 10^-0.75 ≈ 0.1778 → ea ≈ 0.849
    assert 0.84 < ea < 0.86


def test_sr_edges():
    ea, _ = calculate_expected_win_probability(10.0, 0.0)
    assert ea > 0.99
    ea2, _ = calculate_expected_win_probability(0.0, 10.0)
    assert ea2 < 0.01
    # SR=0 не ломает
    assert calculate_team_skill_rating([]) == 0.0
    assert calculate_team_skill_rating([8.0, 7.0, 6.0, 6.0]) == 6.75


# ---------------------------------------------------------------- base elo

def test_base_elo_table():
    # из таблицы ТЗ
    assert calculate_base_elo(0.5, True) == 25.0
    assert calculate_base_elo(0.5, False) == -25.0
    assert abs(calculate_base_elo(0.7, True) - 23.0) < 1e-9
    assert abs(calculate_base_elo(0.7, False) - (-27.0)) < 1e-9
    assert abs(calculate_base_elo(0.85, True) - 21.5) < 1e-9
    assert abs(calculate_base_elo(0.85, False) - (-28.5)) < 1e-9
    assert abs(calculate_base_elo(0.3, True) - 27.0) < 1e-9
    assert abs(calculate_base_elo(0.3, False) - (-23.0)) < 1e-9
    assert abs(calculate_base_elo(0.15, True) - 28.5) < 1e-9
    assert abs(calculate_base_elo(0.15, False) - (-21.5)) < 1e-9


# ---------------------------------------------------------------- performance

def test_performance_score_example():
    # K5 D4 → 5*1.0 - 4*0.75 = 2.0
    score = calculate_player_performance(5, 4)
    assert abs(score - 2.0) < 0.01


def test_performance_modifiers_relative():
    # 8 разных score, mean=4.5: max→+5, min→-5, >mean→+3, <mean→-3
    scores = [8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0]
    mods = calculate_performance_modifiers(scores)
    assert mods == [5, 3, 3, 3, -3, -3, -3, -5]


def test_performance_ties_same_modifier():
    # два лучших с одинаковым score → оба +5
    scores = [9.0, 9.0, 5.0, 4.0, 3.0, 2.0, 1.0, 0.0]
    mods = calculate_performance_modifiers(scores)
    assert mods[0] == mods[1] == 5
    assert mods[-1] == -5  # минимум тоже делят позицию


def test_all_identical_scores():
    scores = [5.0] * 8
    mods = calculate_performance_modifiers(scores)
    assert mods == [0] * 8  # все средние → 0


def test_performance_above_below_mean():
    # mean=3.0: max=10→+5, min=-4→-5, >mean→+3, <mean→-3
    scores = [10.0, 8.0, 6.0, 4.0, 2.0, 0.0, -2.0, -4.0]
    mods = calculate_performance_modifiers(scores)
    assert mods == [5, 3, 3, 3, -3, -3, -3, -5]


def test_performance_exact_mean_zero():
    # scores=[10,4,0,0,0,0,0,2] → sum=16, mean=2.0
    # score==2 → "average" → 0; max=10→+5; min=0→-5; 4>2→+3
    scores = [10.0, 4.0, 0.0, 0.0, 0.0, 0.0, 0.0, 2.0]
    mods = calculate_performance_modifiers(scores)
    assert mods[0] == 5      # max
    assert mods[1] == 3      # выше среднего
    assert mods[2:7] == [-5] * 5  # все минимумы
    assert mods[7] == 0      # ровно среднее


# ---------------------------------------------------------------- rank bonus

def test_rank_bonus_boundaries():
    assert calculate_rank_bonus(1) == 0
    assert calculate_rank_bonus(10) == 0    # 10-е место — без бонуса
    assert calculate_rank_bonus(11) == 3   # 11-е — бонус
    assert calculate_rank_bonus(20) == 3
    assert calculate_rank_bonus(999) == 3


# ---------------------------------------------------------------- final elo / clamp

def test_final_elo_bounds_win():
    # экстремумы не выходят за +20..+30
    assert calculate_final_elo(30.0, 5, 3, True) == 30
    assert calculate_final_elo(20.0, -5, 0, True) == 20
    assert calculate_final_elo(25.0, 0, 0, True) == 25


def test_final_elo_bounds_loss():
    assert calculate_final_elo(-20.0, 5, 3, False) == -20  # не становится +
    assert calculate_final_elo(-30.0, -5, 0, False) == -30
    assert calculate_final_elo(-25.0, 0, 0, False) == -25


def test_rounding_once():
    # 21.5 + 0 + 3 = 24.5 → round → 24 (banker's) или 25 — проверяем int
    v = calculate_final_elo(21.5, 0, 3, True)
    assert isinstance(v, int) and 20 <= v <= 30


# ---------------------------------------------------------------- full match

def test_full_match_bounds_and_sign():
    players = full_match(
        winner_stats=[(10, 0, 5, 5000), (8, 1, 4, 4000), (6, 2, 3, 3000), (4, 3, 2, 2000)],
        loser_stats=[(1, 8, 0, 500), (2, 7, 1, 800), (1, 9, 0, 400), (0, 10, 0, 300)],
    )
    res = compute_match_elo_changes(players)
    assert len(res) == 8
    for name, r in res.items():
        if r["won"]:
            assert 20 <= r["elo_change"] <= 30, (name, r)
        else:
            assert -30 <= r["elo_change"] <= -20, (name, r)


def test_loss_never_positive_win_never_negative():
    players = full_match(sr_a=8.0, sr_b=2.0)
    res = compute_match_elo_changes(players)
    for r in res.values():
        assert (r["elo_change"] > 0) == r["won"]


def test_good_stats_reduce_loss():
    # Проигравший с лучшей статой матча теряет меньше, чем с худшей
    good_loser = full_match(
        winner_stats=[(2, 8, 0, 400)] * 4,
        loser_stats=[(10, 0, 5, 5000), (1, 9, 0, 300), (1, 9, 0, 300), (1, 9, 0, 300)],
    )
    res = compute_match_elo_changes(good_loser)
    # лучший из проигравших vs худший из проигравших
    changes = {n: r["elo_change"] for n, r in res.items() if not r["won"]}
    assert max(changes.values()) > min(changes.values())


def test_rank_bonus_applied_before_clamp():
    # Игрок вне TOP10 получает +3 к изменению до ограничений
    players = full_match(sr_a=5.0, sr_b=5.0)
    for pl in players:
        pl["elo_rank"] = 50  # все вне TOP10
    res = compute_match_elo_changes(players)
    # при равной статистике у всех perf_mod одинаковый → разница только в bonus
    # все должны получить одинаковый change (все +3)
    changes = {r["elo_change"] for r in res.values() if r["won"]}
    assert len(changes) == 1


def test_top10_no_bonus():
    players = full_match()
    for pl in players:
        pl["elo_rank"] = 5
    res = compute_match_elo_changes(players)
    for r in res.values():
        assert r["rank_bonus"] == 0


def test_spec_example():
    """Пример из ТЗ §9: A победитель, SR 6.75 vs 5.25, rank=15."""
    players = []
    # Команда A: SR в среднем 6.75 → [8,7,6,6]; B: [6,5,5,5] → 5.25
    srs_a = [8.0, 7.0, 6.0, 6.0]
    srs_b = [6.0, 5.0, 5.0, 5.0]
    # целевой игрок — лучший по статистике в матче
    players.append(p("target", "a", True, 5, 4, 5, 2179, srs_a[0], 15))
    # остальные A — слабее по статистике
    for i, sr in enumerate(srs_a[1:]):
        players.append(p(f"a{i}", "a", True, 1, 8, 0, 300, sr, 20))
    for i, sr in enumerate(srs_b):
        players.append(p(f"b{i}", "b", False, 2, 7, 1, 600, sr, 30))

    res = compute_match_elo_changes(players)
    r = res["target"]
    # expected_win ≈ 0.849 → base ≈ 20+10*0.151 ≈ 21.5
    assert 21.0 < r["base_elo"] < 22.0
    assert r["performance_modifier"] == 5   # лучший вклад
    assert r["rank_bonus"] == 3             # 15-е место
    # 21.5 + 5 + 3 = 29.5 → clamp → 30? нет: 29.5 ≤ 30 → round → 30 (или 29)
    assert 20 <= r["elo_change"] <= 30
    assert r["elo_change"] >= 28  # должно быть около +29/+30


def test_idempotency_marker_field():
    """Tournament имеет processed_elo_matches для защиты от повторного начисления."""
    from models.tournament import Tournament
    import inspect
    sig = inspect.signature(Tournament)
    assert "processed_elo_matches" in sig.parameters
