"""Unit-тесты движка CASH CUP TEST: сетка 18 команд, симуляция,
места, детерминизм, BO3, споры, технические результаты.

Не требуют Discord/PostgreSQL — чистая логика services/cashcup_sim
и services/cashcup_bracket.
"""
from __future__ import annotations

import collections

import pytest

from services.cashcup_bracket import (
    BracketError,
    create_bracket,
    disqualify,
    ready_matches,
    record_final_game,
    record_result,
)
from services.cashcup_sim import simulate_to_final


def _teams(n: int = 18) -> list[dict]:
    return [{"id": f"team{i:02d}", "name": f"Team {i}",
             "captain": 1000 + i, "members": [], "pending": [],
             "lineup": []} for i in range(n)]


def _state(n: int = 18, seed: int = 7) -> dict:
    return {"status": "snapshot", "test": True, "guild_id": 1,
            "teams": _teams(n), "seed": seed, "bracket": None,
            "base": {"teams": _teams(n)}}


EXPECTED_18 = {
    ("W", "W1"): 2, ("L", "L1"): 1,
    ("W", "W2"): 8, ("L", "L2"): 4,
    ("W", "W3"): 4, ("L", "L3"): 4,
    ("W", "W4"): 2, ("L", "L4"): 3,
    ("W", "W5"): 1, ("L", "L5"): 2,
    ("L", "L6"): 1, ("L", "L7"): 1,
    ("GF", "GF"): 1,
}


def _round_of(m: dict) -> str:
    if m["bracket"] == "GF":
        return "GF"
    return m["id"].split("-")[0]  # W1, L2, ...


def test_bracket_18_match_table():
    """Полная сетка 18 команд даёт ровно таблицу ТЗ: 34 серии."""
    state = _state(18)
    summary = simulate_to_final(state, seed=7)
    b = state["bracket"]
    counts = collections.Counter(
        (m["bracket"], _round_of(m)) for m in b["matches"])
    assert dict(counts) == EXPECTED_18
    assert len(b["matches"]) == 34
    assert summary["matches_total"] == 34
    assert summary["matches_completed"] == 34
    # Все команды сыграли: каждый участник минимум один матч.
    played = {tid for m in b["matches"] for tid in (m["a"], m["b"])
              if tid}
    assert played == {t["id"] for t in state["teams"]}
    # Проигравшие W идут в L, второе поражение выбывает.
    losses = b["losses"]
    assert all(v <= 2 for v in losses.values())
    assert losses[summary["places"]["champion"]] <= 1


def test_simulation_deterministic():
    s1, s2 = _state(18, seed=3), _state(18, seed=3)
    r1 = simulate_to_final(s1)
    r2 = simulate_to_final(s2)
    assert r1["places"]["champion"] == r2["places"]["champion"]
    assert r1["places"]["runner_up"] == r2["places"]["runner_up"]
    assert [h["score"] for h in r1["history"]] == \
           [h["score"] for h in r2["history"]]


def test_places_from_results_not_random():
    state = _state(18)
    summary = simulate_to_final(state, seed=11)
    p = summary["places"]
    b = state["bracket"]
    gf = next(m for m in b["matches"] if m["bracket"] == "GF")
    assert p["champion"] == b["champion"] == gf["winner"]
    assert p["runner_up"] == gf["loser"]
    # Чемпион и второе место — финалисты GF.
    assert {p["champion"], p["runner_up"]} == {gf["a"], gf["b"]}
    assert p["wb_winner"] == b.get("upper_champion")
    # Третье место — проигравший последнего матча losers bracket.
    l_done = [m for m in b["matches"]
              if m["bracket"] == "L" and m["status"] == "completed"]
    assert p["third"] == l_done[-1]["loser"]
    assert p["third"] not in (p["champion"], p["runner_up"])


def test_gf_bo3_scores_valid():
    state = _state(18)
    simulate_to_final(state, seed=5)
    gf = next(m for m in state["bracket"]["matches"]
              if m["bracket"] == "GF")
    sa, sb = gf["score"]
    assert (sa, sb) in ((2, 0), (2, 1), (1, 2), (0, 2))
    assert len(gf["games"]) in (2, 3)
    # Лимит 3 игр на серию GF — проверяем на движке напрямую.
    b2 = create_bracket([f"x{i}" for i in range(4)], seed=1)
    while not any(m["bracket"] == "GF" and m["status"] == "ready"
                  for m in b2["matches"]):
        for m in list(ready_matches(b2)):
            if m["bracket"] == "GF":
                continue
            record_result(b2, m["id"], 1, 0, evidence="t")
    record_final_game(b2, "a", evidence="t")
    record_final_game(b2, "b", evidence="t")
    record_final_game(b2, "a", evidence="t")  # 2:1 — серия завершена
    with pytest.raises(BracketError):
        record_final_game(b2, "b", evidence="t")


def test_manual_flow_checkin_room_result():
    """Ручной путь: check-in → комната → результат → следующий раунд."""
    state = _state(4, seed=1)
    b = create_bracket([t["id"] for t in state["teams"]], seed=1)
    state["bracket"] = b
    state["status"] = "running"
    m = ready_matches(b)[0]
    # Оба капитана check-in (виртуально).
    caps = []
    for tid in (m["a"], m["b"]):
        t = next(x for x in state["teams"] if x["id"] == tid)
        caps.append(t["captain"])
    m["checkins"] = caps
    m["room_id"] = "TEST-x"
    record_result(b, m["id"], 1, 0, evidence="TEST")
    assert m["status"] == "completed"
    assert m["winner"] in (m["a"], m["b"])


def test_second_loss_eliminates():
    """Команда с двумя поражениями выбывает из сетки."""
    ids = [f"t{i}" for i in range(4)]
    b = create_bracket(ids, seed=2)
    # Проигрываем первого два W-матча: проигравшие идут в L.
    for m in list(ready_matches(b)):
        record_result(b, m["id"], 1, 0, evidence="t")
    losers = [m["loser"] for m in b["matches"] if m["bracket"] == "W"]
    assert len(losers) == 2
    # В L они встречаются; проигравший получает 2 поражения и выбывает.
    l_match = next(m for m in ready_matches(b) if m["bracket"] == "L")
    record_result(b, l_match["id"], 1, 0, evidence="t")
    eliminated = [tid for tid, n in b["losses"].items() if n == 2]
    assert len(eliminated) == 1
    assert eliminated[0] == l_match["loser"]


def test_dispute_blocks_then_resolves():
    state = _state(4, seed=4)
    b = create_bracket([t["id"] for t in state["teams"]], seed=4)
    state["bracket"] = b
    m = ready_matches(b)[0]
    m["status"] = "disputed"
    m["dispute"] = {"reason": "test"}
    # ready_matches не видит disputed → simulate завис бы; двигаем вручную.
    assert all(x["id"] != m["id"] for x in ready_matches(b))
    m["status"] = "ready"
    record_result(b, m["id"], 1, 0, evidence="TEST:resolve")
    assert m["status"] == "completed"


def test_dq_forfeit_flow():
    state = _state(4, seed=9)
    b = create_bracket([t["id"] for t in state["teams"]], seed=9)
    victim = b["matches"][0]["a"]
    disqualify(b, victim, evidence="TEST:dq")
    # DQ-команда проигрывает все оставшиеся матчи автоматически.
    while b["champion"] is None:
        ready = ready_matches(b)
        assert ready, "сетка зависла после DQ"
        for m in list(ready):
            if m["bracket"] == "GF":
                record_final_game(b, "a", evidence="t")
                if b["champion"] is None:
                    record_final_game(b, "a", evidence="t")
            else:
                record_result(b, m["id"], 1, 0, evidence="t")
    assert b["champion"] != victim
    assert b["losses"][victim] >= 2


def test_snapshot_does_not_mutate_base():
    """Симуляция не трогает base-снимок (кнопка «Заново»)."""
    state = _state(6, seed=8)
    base_before = str(state["base"])
    simulate_to_final(state, seed=8)
    assert str(state["base"]) == base_before
    assert state["status"] == "completed"


def test_too_few_teams_rejected():
    state = _state(1)
    with pytest.raises(BracketError):
        simulate_to_final(state)
