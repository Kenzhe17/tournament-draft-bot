"""Автоматическая симуляция TEST-сессии CASH CUP до финала.

Чистая функция над state dict (тот же формат, что cashcup_store):
teams + bracket (services/cashcup_bracket). Детерминирована по seed —
результат воспроизводим. Не выполняет никаких Discord/БД операций:
запись результата — ответственность вызывающего кода (store.mutate).
"""
from __future__ import annotations

import random
from typing import Any

from services.cashcup_bracket import (
    BracketError,
    create_bracket,
    ready_matches,
    record_final_game,
    record_result,
)

_MAX_STEPS = 8 * 64 + 64


def _allowed_scores(bo: int) -> list[tuple[int, int]]:
    if bo == 3:
        return [(2, 0), (2, 1), (1, 2), (0, 2)]
    return [(1, 0), (0, 1)]


def _places(bracket: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Итоговые места из результатов сетки (не назначаются случайно)."""
    gf = next((m for m in bracket["matches"] if m["bracket"] == "GF"), None)
    champion = bracket.get("champion")
    runner_up = None
    third = None
    if gf and gf.get("winner"):
        runner_up = gf["loser"]
    l_done = [m for m in bracket["matches"]
              if m["bracket"] == "L" and m.get("status") == "completed"]
    if l_done:
        # Последний завершённый матч нижней сетки — финалистов L,
        # его проигравший выбывает третьим (второе поражение).
        third = l_done[-1].get("loser")
    wb = bracket.get("upper_champion")
    # Победитель losers bracket — финалист GF, проигравший GF.
    # Если GF ещё не сыгран, LB-финалист — выживший нижней сетки.
    lower_alive = [t for t in (bracket.get("lower") or [])
                   if t and t != champion]
    return {
        "champion": champion,
        "runner_up": runner_up,
        "third": third,
        "wb_winner": wb,
        "lb_winner": (runner_up or (lower_alive[0] if lower_alive else None)),
        "gf_score": (gf or {}).get("score"),
        "gf_games": (gf or {}).get("games") or [],
    }


def simulate_to_final(state: dict[str, Any], *,
                      seed: int | None = None) -> dict[str, Any]:
    """Проигрывает всю сетку тестовой сессии случайными результатами.

    state['bracket'] создаётся при необходимости (старт из snapshot).
    Возвращает сводку: places, историю матчей, число серий, seed.
    """
    teams = state.get("teams") or []
    if len(teams) < 2:
        raise BracketError("В тестовой сессии меньше двух команд")
    effective_seed = seed if seed is not None else state.get("seed")
    rng = random.Random(effective_seed)

    bracket = state.get("bracket")
    started = bracket is not None
    if bracket is None:
        bracket = create_bracket([t["id"] for t in teams],
                                 seed=effective_seed)
        state["bracket"] = bracket
    if state.get("status") == "snapshot":
        state["status"] = "running"
    if state.get("status") not in ("running", "paused"):
        raise BracketError("Симуляция возможна только для running/paused "
                           "тестовой сессии")

    history: list[dict[str, Any]] = []
    steps = 0
    while bracket["champion"] is None:
        steps += 1
        if steps > _MAX_STEPS:
            raise BracketError("Симуляция зависла: превышен лимит шагов")
        # Авто-разбор споров в симуляции: фиксируем и играем дальше.
        active = set(bracket["active_keys"])
        for m in bracket["matches"]:
            if m["id"] in active and m.get("status") == "disputed":
                m["status"] = "ready"
                m.setdefault("dispute", {})["auto_resolved_by_sim"] = True
        ready = ready_matches(bracket)
        if not ready:
            raise BracketError("Симуляция зависла: нет активных матчей")
        for m in list(ready):
            if m["bracket"] == "GF":
                # BO3: до двух побед одной из сторон.
                side = rng.choice(("a", "b"))
                record_final_game(bracket, side, evidence="TEST:sim:gf1")
                history.append({"id": m["id"], "bracket": "GF",
                                "score": list(m["score"]),
                                "winner": m.get("winner")})
                if bracket["champion"] is None:
                    side2 = "a" if side == "b" and rng.random() < 0.5 else \
                        rng.choice(("a", "b"))
                    # Вторая игра: случайный победитель, но не гарантируем
                    # одинаковую сторону — BO3 честный.
                    side2 = rng.choice(("a", "b"))
                    record_final_game(bracket, side2, evidence="TEST:sim:gf2")
                    history.append({"id": m["id"], "bracket": "GF",
                                    "score": list(m["score"]),
                                    "winner": bracket.get("champion")})
                continue
            score = rng.choice(_allowed_scores(m["bo"]))
            record_result(bracket, m["id"], score[0], score[1],
                          evidence="TEST:sim")
            history.append({"id": m["id"], "bracket": m["bracket"],
                            "score": list(score),
                            "winner": m.get("winner")})

    if bracket["champion"] is not None:
        state["status"] = "completed"
    places = _places(bracket, state)
    played = sum(1 for m in bracket["matches"]
                 if m.get("status") == "completed")
    return {
        "seed": effective_seed,
        "started_bracket": not started,
        "matches_total": len(bracket["matches"]),
        "matches_completed": played,
        "history": history,
        "places": places,
    }
