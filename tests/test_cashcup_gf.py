"""Grand Final CASH CUP: WB/LB победитель, BO3 без reset,
идемпотентность результата, счёт поражений — для всех
поддерживаемых размеров сетки (8/16/18/20/32)."""
from __future__ import annotations

import pytest

from services.cashcup_bracket import (
    BracketError,
    create_bracket,
    ready_matches,
    record_final_game,
    record_result,
)

SIZES = (8, 16, 18, 20, 32)


def _play_to_gf(n: int, seed: int):
    """Доиграть сетку до состояния «активен только GF»."""
    b = create_bracket([f"t{i}" for i in range(n)], seed=seed)
    for _ in range(500):
        ready = ready_matches(b)
        assert ready, f"deadlock n={n} seed={seed}"
        if all(m["bracket"] == "GF" for m in ready):
            return b
        for m in list(ready):
            record_result(b, m["id"], 1, 0, evidence="test")
    raise AssertionError("GF not reached")


def _play_gf(b, winner_side_for_target: str, target: str) -> None:
    """BO3: target выигрывает 2 игры из 3 (с одним поражением)."""
    gf = next(m for m in b["matches"] if m["bracket"] == "GF")
    side = "a" if gf["a"] == target else "b"
    other = "b" if side == "a" else "a"
    record_final_game(b, side, evidence="g1")
    record_final_game(b, other, evidence="g2")
    record_final_game(b, side, evidence="g3")


@pytest.mark.parametrize("n", SIZES)
@pytest.mark.parametrize("winner_path", ("wb", "lb"))
def test_gf_winner_and_idempotency(n, winner_path):
    b = _play_to_gf(n, seed=3)
    wb = b["upper"][0]
    lb = b["lower"][0]
    target = wb if winner_path == "wb" else lb
    _play_gf(b, winner_path, target)

    gf = next(m for m in b["matches"] if m["bracket"] == "GF")
    assert b["champion"] == target
    assert gf["winner"] == target
    assert gf["loser"] != target
    # BO3 без reset: WB-чемпион без поражений, LB-чемпион — 1.
    assert b["losses"][target] == (0 if winner_path == "wb" else 1)
    assert all(v <= 2 for v in b["losses"].values())
    # Идемпотентность: после завершения результат/GF не принимаются.
    for fn in (lambda: record_result(b, "GF", 2, 0),
               lambda: record_final_game(b, "a", evidence="x")):
        with pytest.raises(BracketError):
            fn()


@pytest.mark.parametrize("n", SIZES)
def test_gf_sweep_2_0(n):
    """Победа 2:0 в GF тоже валидна и идемпотентна."""
    b = _play_to_gf(n, seed=5)
    target = b["upper"][0]
    gf = next(m for m in b["matches"] if m["bracket"] == "GF")
    side = "a" if gf["a"] == target else "b"
    record_final_game(b, side, evidence="g1")
    record_final_game(b, side, evidence="g2")
    assert b["champion"] == target
    assert tuple(gf["score"]) == ((2, 0) if side == "a" else (0, 2))
    with pytest.raises(BracketError):
        record_final_game(b, side, evidence="g3")


def test_gf_requires_both_finalists():
    """GF не создаётся без двух финалистов — движок не фальсифицирует."""
    b = create_bracket([f"s{i}" for i in range(4)], seed=1)
    # Сыграть WB-раунд: победители идут в lower, GF ждёт LB-финалиста.
    ready = ready_matches(b)
    assert all(m["bracket"] == "W" for m in ready)
    for m in ready:
        record_result(b, m["id"], 1, 0, evidence="t")
    # Ни одного GF-матча, пока lower не определит второго финалиста.
    assert not any(m["bracket"] == "GF"
                   for m in ready_matches(b))
