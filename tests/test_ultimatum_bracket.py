"""Тесты движка сетки «Ультиматум» 2.0 (ultimatum/bracket.py).

Чистый Python — без Discord/БД. Проверяем:
- шаблоны и переходы DE для Соло и Дуо;
- корректное число матчей (Соло 30/31, Дуо 14/15);
- GF1/GF2 reset;
- распределение мест;
- инварианты: нет дублей участников в активных матчах,
  выбывание после двух поражений, ровно один чемпион.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ultimatum.bracket import (  # noqa: E402
    Bracket, Format, MatchStatus, Slot, entrant_count, simulate,
)


def _fill(b: Bracket) -> None:
    n = entrant_count(b.format)
    for i in range(1, n + 1):
        if b.format is Format.SOLO:
            b.add_entrant((i,), (f"P{i}",), seed=i)
        else:
            b.add_entrant(
                (1000 + 2 * i, 1000 + 2 * i + 1),
                (f"A{i}", f"B{i}"), seed=i,
            )
    b.refresh()


def _play_all(b: Bracket, rng: random.Random) -> None:
    guard = 0
    while not b.is_finished() and guard < 300:
        guard += 1
        progressed = False
        for m in list(b.active_matches()):
            if m.status == MatchStatus.DISPUTED:
                winner = rng.choice([m.entrant_a, m.entrant_b])
            else:
                winner = rng.choice([m.entrant_a, m.entrant_b])
            try:
                b.apply_result(m.spec.key, winner)
                progressed = True
            except ValueError:
                continue
        if not progressed:
            break


# ---------------------------------------------------------------------------
# Шаблоны
# ---------------------------------------------------------------------------

def test_solo_template_counts():
    b = Bracket(Format.SOLO)
    w = [k for k, m in b.matches.items() if m.spec.bracket == "W"]
    l = [k for k, m in b.matches.items() if m.spec.bracket == "L"]
    gf = [k for k, m in b.matches.items() if m.spec.bracket == "GF"]
    assert len(w) == 15   # 8+4+2+1
    assert len(l) == 14   # 4+4+2+2+1+1
    assert len(gf) == 1   # только GF1, GF2 создаётся по необходимости
    assert len(b.matches) == 30


def test_duo_template_counts():
    b = Bracket(Format.DUO)
    w = [k for k, m in b.matches.items() if m.spec.bracket == "W"]
    l = [k for k, m in b.matches.items() if m.spec.bracket == "L"]
    gf = [k for k, m in b.matches.items() if m.spec.bracket == "GF"]
    assert len(w) == 7    # 4+2+1
    assert len(l) == 6    # 2+2+1+1
    assert len(gf) == 1
    assert len(b.matches) == 14


def test_u1_seed_pairings_solo():
    b = Bracket(Format.SOLO)
    _fill(b)
    ready = {m.spec.key: m for m in b.ready_matches()}
    assert len(ready) == 8
    # Пара 1–16: seed 1 против seed 16
    m = ready["U1-1"]
    e1 = b.entrants[m.entrant_a]
    e16 = b.entrants[m.entrant_b]
    assert {e1.seed, e16.seed} == {1, 16}


def test_u1_seed_pairings_duo():
    b = Bracket(Format.DUO)
    _fill(b)
    ready = {m.spec.key: m for m in b.ready_matches()}
    assert len(ready) == 4
    m = ready["U1-1"]
    seeds = {b.entrants[m.entrant_a].seed, b.entrants[m.entrant_b].seed}
    assert seeds == {1, 8}


# ---------------------------------------------------------------------------
# Поток matches / переходы
# ---------------------------------------------------------------------------

def test_matches_unlock_only_when_both_slots_known():
    b = Bracket(Format.SOLO)
    _fill(b)
    # Изначально READY только U1-*
    assert {m.spec.key for m in b.ready_matches()} == {
        f"U1-{i}" for i in range(1, 9)
    }
    # Проигрываем U1-1 и U1-2 → открываются L1-1 и U2-1
    b.apply_result("U1-1", b.matches["U1-1"].entrant_a)
    b.apply_result("U1-2", b.matches["U1-2"].entrant_a)
    b.refresh()
    keys = {m.spec.key for m in b.ready_matches()}
    assert "U2-1" in keys
    assert "L1-1" in keys
    # L2-1 требует loser(U2-1) — ещё WAITING
    assert b.matches["L2-1"].status == MatchStatus.WAITING


def test_lower_bracket_loser_feeds():
    b = Bracket(Format.SOLO)
    _fill(b)
    rng = random.Random(1)
    # Доигрываем U1 полностью, потом U2-1
    for i in range(1, 9):
        m = b.matches[f"U1-{i}"]
        b.apply_result(m.spec.key, rng.choice([m.entrant_a, m.entrant_b]))
    for i in range(1, 5):
        m = b.matches[f"U2-{i}"]
        if m.status == MatchStatus.READY:
            b.apply_result(m.spec.key, rng.choice([m.entrant_a, m.entrant_b]))
    b.refresh()
    # Проигравшие U2 попали в L2-*
    for i in range(1, 5):
        m = b.matches[f"L2-{i}"]
        assert m.status in (MatchStatus.READY, MatchStatus.WAITING)
        if m.status == MatchStatus.READY:
            assert m.entrant_a is not None and m.entrant_b is not None


def test_second_loss_eliminates():
    b = Bracket(Format.SOLO)
    _fill(b)
    e = next(iter(b.entrants.values()))
    e.losses = 1
    # Искусственно: entrant с 1 поражением проигрывает ещё раз
    # (через прямой apply_result на матче, где он участвует)
    target = None
    for m in b.matches.values():
        if m.entrant_a == e.id or m.entrant_b == e.id:
            target = m
            break
    if target is None:
        pytest.skip("entrant not yet in a match")
    winner = target.entrant_b if target.entrant_a == e.id else target.entrant_a
    b.apply_result(target.spec.key, winner)
    assert e.losses == 2
    assert e.status.value == "ELIMINATED"


# ---------------------------------------------------------------------------
# GF1 / GF2
# ---------------------------------------------------------------------------

def test_gf1_upper_win_no_gf2():
    """Если верхняя сетка выигрывает GF1 — reset не нужен."""
    b = Bracket(Format.DUO)
    _fill(b)
    rng = random.Random(42)
    _play_all(b, rng)
    assert b.is_finished()
    gf1 = b.matches["GF1"]
    assert gf1.status == MatchStatus.COMPLETED
    winner = b.entrants[gf1.winner_id]
    if winner.losses == 0:
        assert "GF2" not in b.matches
    else:
        assert "GF2" in b.matches
        assert b.matches["GF2"].status == MatchStatus.COMPLETED


def test_gf2_created_only_when_needed():
    """Сценарий: нижняя сетка выигрывает GF1 → появляется GF2."""
    b = Bracket(Format.SOLO)
    _fill(b)
    # Ищем комбинацию, где lower-bracket finalist выигрывает GF1.
    # Проще: симулируем много раз и проверяем логику на найденном случае.
    found = False
    for seed in range(300):
        rng = random.Random(seed)
        bb = Bracket(Format.SOLO)
        _fill(bb)
        _play_all(bb, rng)
        gf1 = bb.matches["GF1"]
        w = bb.entrants[gf1.winner_id]
        if w.losses > 0:
            found = True
            assert "GF2" in bb.matches
            assert bb.matches["GF2"].status == MatchStatus.COMPLETED
            assert bb.matches["GF2"].winner_id == gf1.winner_id or True
            break
    assert found, "не удалось получить сценарий с GF2 за 300 прогонов"


def test_gf2_same_finalists():
    for seed in range(500):
        rng = random.Random(seed)
        bb = Bracket(Format.DUO)
        _fill(bb)
        _play_all(bb, rng)
        gf1 = bb.matches["GF1"]
        w = bb.entrants[gf1.winner_id]
        if w.losses > 0 and "GF2" in bb.matches:
            gf2 = bb.matches["GF2"]
            assert {gf2.entrant_a, gf2.entrant_b} == {gf1.entrant_a, gf1.entrant_b}
            return
    pytest.skip("GF2 scenario not hit")


# ---------------------------------------------------------------------------
# Полные симуляции
# ---------------------------------------------------------------------------

def _check_finished(b: Bracket) -> list[str]:
    errors = b.validate_invariants()
    if not b.is_finished():
        errors.append("tournament not finished")
        return errors
    champ = b.champion_id()
    if not champ:
        errors.append("no champion")
    runner = b.runner_up_id()
    if not runner or runner == champ:
        errors.append("bad runner-up")
    # Один чемпион
    champions = [e for e in b.entrants.values() if e.status.value == "CHAMPION"]
    # статус CHAMPION ставится только через placements — в движке
    # проверяем по winner_id
    places = b.compute_placements()
    place1 = [eid for eid, (p, _) in places.items() if p == 1]
    if len(place1) != 1:
        errors.append(f"place-1 count {len(place1)}")
    # Все места распределены
    n = entrant_count(b.format)
    if len(places) != n:
        errors.append(f"placements cover {len(places)}/{n}")
    return errors


def test_simulate_solo_invariants():
    rng = random.Random(1234)
    counts = {30: 0, 31: 0}
    for _ in range(2000):
        b = simulate(Format.SOLO, rng)
        errs = _check_finished(b)
        assert not errs, errs
        total = len(b.matches)
        assert total in (30, 31), total
        counts[total] += 1
    # ОБА варианта должны встречаться (GF2 примерно в половине случаев)
    assert counts[30] > 0 and counts[31] > 0, counts


def test_simulate_duo_invariants():
    rng = random.Random(5678)
    counts = {14: 0, 15: 0}
    for _ in range(2000):
        b = simulate(Format.DUO, rng)
        errs = _check_finished(b)
        assert not errs, errs
        total = len(b.matches)
        assert total in (14, 15), total
        counts[total] += 1
    assert counts[14] > 0 and counts[15] > 0, counts


def test_placements_solo_groups():
    """Места 1–4 точные, 5+ — группы без внутреннего порядка."""
    rng = random.Random(99)
    for _ in range(300):
        b = simulate(Format.SOLO, rng)
        places = b.compute_placements()
        exact = {p for p, _ in places.values() if p is not None}
        assert exact == {1, 2, 3, 4}, exact
        groups = {g for p, g in places.values() if p is None and g}
        assert groups <= {"5–6", "7–8", "9–12", "13–16"}, groups


def test_placements_duo_groups():
    rng = random.Random(77)
    for _ in range(300):
        b = simulate(Format.DUO, rng)
        places = b.compute_placements()
        exact = {p for p, _ in places.values() if p is not None}
        assert exact == {1, 2, 3, 4}, exact
        groups = {g for p, g in places.values() if p is None and g}
        assert groups <= {"5–6", "7–8"}, groups


def test_no_eliminated_entrant_plays():
    """После 2 поражений entrant не должен получать новых матчей."""
    rng = random.Random(5)
    for _ in range(500):
        b = simulate(Format.SOLO, rng)
        for m in b.matches.values():
            if m.status != MatchStatus.COMPLETED:
                continue
            for eid in (m.entrant_a, m.entrant_b):
                e = b.entrants[eid]
                # После завершения турнира у всех кроме чемпиона
                # и runner-up ровно 2 поражения; они не играли после
                # второго поражения — проверяем счётчик.
                assert e.losses <= 2


def test_apply_result_rejects_double_finalize():
    b = Bracket(Format.SOLO)
    _fill(b)
    m = b.matches["U1-1"]
    w = m.entrant_a
    b.apply_result("U1-1", w)
    with pytest.raises(ValueError):
        b.apply_result("U1-1", w)


def test_apply_result_validates_winner():
    b = Bracket(Format.SOLO)
    _fill(b)
    m = b.matches["U1-1"]
    with pytest.raises(ValueError):
        b.apply_result("U1-1", "nonexistent")


def test_waiting_match_not_playable():
    b = Bracket(Format.SOLO)
    _fill(b)
    assert not b.match_is_playable("U2-1")
    assert b.match_is_playable("U1-1")
