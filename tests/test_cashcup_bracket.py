"""Детерминированные и случайные симуляции независимого движка CASH CUP."""
import copy
import random

import pytest

from services.cashcup_bracket import BracketError, create_bracket, ready_matches, record_result


def test_registration_count_and_byes():
    for n in (2, 3, 5, 7, 8, 13, 24, 32, 37, 70, 128):
        bracket = create_bracket([str(i) for i in range(n)], seed=33)
        assert set(bracket["losses"]) == {str(i) for i in range(n)}
        assert all(match["bo"] == 1 for match in ready_matches(bracket))
        main = 1 << (n.bit_length() - 1)
        expected = n-main if n > main else n//2
        assert len(ready_matches(bracket)) == expected, (n, expected)
        assert sum(map(lambda m: 2, ready_matches(bracket))) <= n


@pytest.mark.parametrize("n", range(2, 130))
def test_complete_bracket_random_winners(n):
    for seed in range(5):
        bracket = create_bracket([str(i) for i in range(n)], seed=seed)
        rng = random.Random(n * 1000 + seed)
        safety = 0
        while bracket["champion"] is None:
            matches = ready_matches(bracket)
            assert matches, "Висит сетка без активных матчей"
            ids = [pid for m in matches for pid in (m["a"], m["b"])]
            assert len(ids) == len(set(ids)), "Команда назначена в два матча одновременно"
            for match in reversed(matches):
                if match["bo"] == 3:
                    score = rng.choice([(2, 0), (2, 1), (0, 2), (1, 2)])
                else:
                    score = rng.choice([(1, 0), (0, 1)])
                record_result(bracket, match["id"], *score, evidence="fixture")
            safety += 1
            assert safety < 100, "Бесконечная генерация этапов"
        assert len(bracket["matches"]) == 2 * n - 2
        assert len([m for m in bracket["matches"] if m["bracket"] == "GF"]) == 1
        assert bracket["upper_champion"] is not None
        assert bracket["losses"][bracket["champion"]] in (0, 1)
        for pid, losses in bracket["losses"].items():
            if pid != bracket["champion"] and pid != bracket["upper_champion"]:
                assert losses == 2
        assert not ready_matches(bracket)
        assert bracket["stage"] == "completed"
        # PostgreSQL JSONB round-trip сохраняет возможность восстановления.
        assert __import__("json").loads(__import__("json").dumps(bracket)) == bracket


def test_final_is_first_to_two_without_reset():
    bracket = create_bracket(["A", "B"], seed=0)
    first = ready_matches(bracket)[0]
    record_result(bracket, first["id"], 1, 0)
    final = ready_matches(bracket)[0]
    assert final["id"] == "GF" and final["bo"] == 3
    with pytest.raises(BracketError):
        record_result(bracket, "GF", 1, 0)
    record_result(bracket, "GF", 0, 2, evidence="screenshot")
    assert bracket["champion"] == final["b"]
    assert len(bracket["matches"]) == 2
    with pytest.raises(BracketError):
        record_result(bracket, "GF", 2, 1)


def test_double_submission_rejected():
    bracket = create_bracket([str(i) for i in range(8)], seed=1)
    first = ready_matches(bracket)[0]
    record_result(bracket, first["id"], 1, 0)
    with pytest.raises(BracketError):
        record_result(bracket, first["id"], 0, 1)


def test_invalid_teams_and_scores():
    for ids in ([], ["a"], ["a", "a"]):
        with pytest.raises(BracketError):
            create_bracket(ids)
    bracket = create_bracket(["a", "b"])
    snapshot = copy.deepcopy(bracket)
    with pytest.raises(BracketError):
        record_result(bracket, "W1-1", 1, 1)
    assert bracket == snapshot


def test_grand_final_game_by_game():
    from services.cashcup_bracket import record_final_game
    b = create_bracket(["A", "B"], seed=0)
    first = ready_matches(b)[0]
    record_result(b, first["id"], 1, 0)
    m = record_final_game(b, "a", evidence="proof1")
    assert m["score"] == [1, 0] and b["champion"] is None
    m = record_final_game(b, "b", evidence="proof2")
    assert m["score"] == [1, 1] and b["champion"] is None
    m = record_final_game(b, "b", evidence="proof3")
    assert m["score"] == [1, 2] and b["champion"] == m["b"]
    with pytest.raises(BracketError):
        record_final_game(b, "a", evidence="proof4")
    assert len(m["games"]) == 3
