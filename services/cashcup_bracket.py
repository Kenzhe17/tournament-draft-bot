"""Независимый движок CASH CUP: произвольное число команд, Double Elimination.

W и L — BO1, единственный гранд-финал — BO3, без bracket reset.
BYE не считается матчем или поражением. Весь граф воспроизводим из JSON.
"""
from __future__ import annotations

import random
from typing import Any


class BracketError(ValueError):
    pass


def create_bracket(team_ids: list[str], *, seed: int | None = None) -> dict[str, Any]:
    if len(team_ids) < 2 or len(set(team_ids)) != len(team_ids):
        raise BracketError("Необходимо минимум две уникальные команды")
    order = team_ids[:]
    random.Random(seed).shuffle(order)
    bracket = {
        "stage": "upper", "round": 1, "upper": order, "lower": [],
        "incoming": [], "carry": [], "active_keys": [], "matches": [],
        "losses": {tid: 0 for tid in order}, "champion": None,
        "upper_champion": None, "seed": seed, "upper_first": True,
        "dq": [],
    }
    _schedule(bracket)
    return bracket


def _schedule(b: dict[str, Any]) -> None:
    """Открывает следующий этап без фиктивных игр, пока не появится матч."""
    while not b["active_keys"] and b["champion"] is None:
        stage = b["stage"]
        if stage == "upper":
            if len(b["upper"]) < 2:
                b["upper_champion"] = b["upper"][0] if b["upper"] else None
                b["stage"] = "lower"
                continue
            entries = b["upper"]
            if b["upper_first"]:
                # Правильные BYE: только стартовый отборочный раунд.
                # Например 13 команд => 5 матчей + 3 BYE => сетка на 8.
                base = 1 << (len(entries).bit_length() - 1)
                qualifying_matches = len(entries) - base
                count = 2 * qualifying_matches if qualifying_matches else len(entries)
                b["carry"] = entries[count:]
                b["upper_first"] = False
                _make_matches(b, "W", entries[:count], 1)
            else:
                b["carry"] = entries[-1:] if len(entries) % 2 else []
                _make_matches(b, "W", entries[:len(entries) - len(b["carry"])], 1)
        elif stage == "lower":
            entries = b["lower"] + b["incoming"]
            b["incoming"] = []
            if len(entries) < 2:
                b["lower"] = entries
                if len(b["upper"]) == 1:
                    if len(entries) != 1:
                        raise BracketError("Нижняя сетка не определила финалиста")
                    b["stage"] = "grand"
                else:
                    b["round"] += 1
                    b["stage"] = "upper"
                continue
            b["carry"] = entries[-1:] if len(entries) % 2 else []
            _make_matches(b, "L", entries[:len(entries) - len(b["carry"])], 1)
        elif stage == "grand":
            dq = set(b.get("dq", ()))
            present = [x for x in (b["upper"][0] if b["upper"] else None,
                                   b["lower"][0] if b["lower"] else None)
                       if x and x not in dq]
            if len(present) == 1:
                # Один финалист дисквалифицирован до GF — второй объявляется чемпионом.
                b["champion"] = present[0]
                b["active_keys"] = []
                b["stage"] = "completed"
                continue
            if len(present) != 2:
                raise BracketError("Финалисты не определены")
            _make_matches(b, "GF", present, 3)
        else:
            raise BracketError(f"Неизвестная стадия {stage}")


def _make_matches(b: dict[str, Any], prefix: str, entries: list[str], bo: int) -> None:
    for i in range(0, len(entries), 2):
        mid = f"{prefix}{b['round']}-{i//2+1}" if prefix != "GF" else "GF"
        match = {
            "id": mid, "bracket": prefix, "a": entries[i], "b": entries[i+1],
            "bo": bo, "score": [0, 0], "status": "ready", "winner": None,
            "loser": None, "evidence": "", "room_id": "", "room_password": "",
            "checkins": [], "games": [],
        }
        b["matches"].append(match)
        b["active_keys"].append(mid)


def ready_matches(b: dict[str, Any]) -> list[dict[str, Any]]:
    active = set(b["active_keys"])
    return [m for m in b["matches"]
            if m["id"] in active and m["status"] == "ready"]


def record_result(
    b: dict[str, Any], match_id: str, score_a: int, score_b: int, *,
    evidence: str = "",
) -> dict[str, Any]:
    if b["champion"] is not None:
        raise BracketError("Турнир завершён")
    m = next((m for m in ready_matches(b) if m["id"] == match_id), None)
    if m is None:
        raise BracketError("Матч не найден либо результат уже подтверждён")
    wins = 2 if m["bo"] == 3 else 1
    allowed = ((2, 0), (2, 1), (0, 2), (1, 2)) if wins == 2 else ((1, 0), (0, 1))
    if (score_a, score_b) not in allowed:
        raise BracketError(f"Недопустимый счёт BO{m['bo']}")
    winner = m["a"] if score_a > score_b else m["b"]
    loser = m["b"] if winner == m["a"] else m["a"]
    m.update(score=[score_a, score_b], winner=winner, loser=loser,
             status="completed", evidence=evidence)
    b["losses"][loser] += 1
    if m["bracket"] == "L" and b["losses"][loser] != 2:
        raise BracketError("Во второй сетке выбывают только после второго поражения")
    if m["bracket"] == "W" and b["losses"][loser] != 1:
        raise BracketError("В верхней сетке допускаются команды с 0 поражений")
    if m["bracket"] == "GF":
        b["champion"] = winner
        b["active_keys"] = []
        b["stage"] = "completed"
        _auto_forfeit(b)
        return m
    _advance(b, m)
    _auto_forfeit(b)
    return m


def _advance(b: dict[str, Any], m: dict[str, Any]) -> None:
    """Переход к следующему этапу после завершения раунда.

    Блокируется любым незавершённым матчем активного этапа: ready И disputed.
    ready_matches() не подходит — он исключает disputed, и спорный матч
    ошибочно считался бы отсутствующим, теряя участников раунда."""
    pending = [x for x in b["matches"]
               if x["id"] in b["active_keys"]
               and x["status"] in ("ready", "disputed")]
    if pending:
        return
    current = [x for x in b["matches"] if x["id"] in b["active_keys"]]
    winners = [x["winner"] for x in current if x["winner"]] + b["carry"]
    losers = ([x["loser"] for x in current if x["loser"]]
              + [y for x in current for y in x.get("extra_losers", [])])
    if m["bracket"] == "W":
        b["upper"] = winners
        if len(winners) == 1:
            b["upper_champion"] = winners[0]
        b["incoming"] = losers
        b["stage"] = "lower"
    else:
        b["lower"] = winners
        b["round"] += 1
        b["stage"] = "upper"
    b["active_keys"] = []
    b["carry"] = []
    _schedule(b)


def _void_match(b: dict[str, Any], m: dict[str, Any]) -> None:
    """Оба участника дисквалифицированы: поражение обоим, слот победителя — BYE."""
    for tid in (m["a"], m["b"]):
        b["losses"][tid] += 1
    m.update(score=[0, 0], winner=None, loser=None, status="void",
             evidence="technical:both_disqualified",
             extra_losers=[m["a"], m["b"]])
    _advance(b, m)


def _auto_forfeit(b: dict[str, Any]) -> None:
    """Автоматически завершает матчи с дисквалифицированными командами."""
    if b["champion"] is not None:
        return
    dq = set(b.get("dq", ()))
    if not dq:
        return
    for m in ready_matches(b):
        sides = [s for s in (m["a"], m["b"]) if s in dq]
        if not sides:
            continue
        if len(sides) == 2:
            _void_match(b, m)
        elif m["bracket"] == "GF":
            # Учитываем уже сыгранные игры: победителю достаточно 2 побед.
            loser = sides[0]
            score = list(m["score"])
            score[0 if loser == m["b"] else 1] = 2
            record_result(b, m["id"], score[0], score[1],
                          evidence="technical:forfeit")
        else:
            loser = sides[0]
            score = (0, 1) if loser == m["a"] else (1, 0)
            record_result(b, m["id"], *score, evidence="technical:forfeit")
        return _auto_forfeit(b)


def schedule_steps(team_ids: list[str], *, seed: int | None = None,
                   max_steps: int | None = None) -> list[int]:
    """Симуляция полной сетки: возвращает число матчей на каждом шаге.

    Каждый шаг = раунд готовых матчей, идущих параллельно (ограничение —
    только комнаты). Детерминированно: все готовые матчи завершаются за шаг.
    Используется для прогноза расписания, не для результатов.
    """
    b = create_bracket(team_ids, seed=seed)
    steps: list[int] = []
    limit = max_steps or (8 * len(team_ids) + 16)
    while b["champion"] is None:
        ready = ready_matches(b)
        if not ready:
            raise BracketError("Симуляция зависла: нет активных матчей")
        steps.append(len(ready))
        if len(steps) > limit:
            raise BracketError("Симуляция превысила лимит шагов")
        for m in list(ready):
            score = (2, 0) if m["bo"] == 3 else (1, 0)
            record_result(b, m["id"], *score, evidence="schedule-sim")
    return steps


def disqualify(b: dict[str, Any], team_id: str, *, evidence: str = "") -> str:
    """Дисквалификация: команда проигрывает все оставшиеся матчи."""
    if team_id not in b["losses"]:
        raise BracketError("Команда не участвует в турнире")
    if team_id in b.get("dq", ()):
        raise BracketError("Команда уже дисквалифицирована")
    if b["champion"] is not None:
        raise BracketError("Турнир завершён")
    b.setdefault("dq", []).append(team_id)
    _auto_forfeit(b)
    return team_id


def record_final_game(b: dict[str, Any], winner_side: str, *, evidence: str) -> dict[str, Any]:
    """Записать одну игру GF. Победа №2 завершает BO3 без reset."""
    m = next((m for m in ready_matches(b) if m["bracket"] == "GF"), None)
    if m is None:
        raise BracketError("Гранд-финал не готов или завершён")
    if winner_side not in ("a", "b"):
        raise BracketError("Победитель игры должен быть a или b")
    if len(m["games"]) >= 3:
        raise BracketError("В серии не может быть больше трёх игр")
    idx = 0 if winner_side == "a" else 1
    score = list(m["score"])
    score[idx] += 1
    m["games"].append({"winner": m[winner_side], "evidence": evidence})
    if score[idx] == 2:
        record_result(b, "GF", score[0], score[1], evidence=evidence)
    else:
        m["score"] = score
    return m
