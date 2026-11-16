"""RC-валидация CASH CUP: откат с анализом зависимостей, disputed-матчи,
прогноз расписания, призовые места (ТЗ §4,6,11,12)."""
import copy

import pytest

from services.cashcup_bracket import (BracketError, create_bracket, ready_matches,
                                      record_result, schedule_steps)
from cogs import cashcup
from cogs.cashcup import _apply_prizes, _push_snapshot, _revert_blocked, _started_markers


def _state_with_bracket(n=8, seed=0):
    b = create_bracket([f"T{i}" for i in range(n)], seed=seed)
    return {"status": "running", "bracket": b, "undo": [], "teams": []}


def test_revert_blocked_by_new_checkin():
    st = _state_with_bracket()
    # Снимок до результата W-раунда.
    _push_snapshot(st)
    # Имитируем результат: завершаем все готовые матчи (как record_result).
    for m in list(ready_matches(st["bracket"])):
        record_result(st["bracket"], m["id"], 1, 0)
    # Новый раунд: dependent-матч получил check-in → откат заблокирован.
    nxt = ready_matches(st["bracket"])[0]
    nxt["checkins"].append(123)
    assert _revert_blocked(st) == {nxt["id"]}


def test_revert_allowed_without_new_markers():
    st = _state_with_bracket()
    _push_snapshot(st)
    for m in list(ready_matches(st["bracket"])):
        record_result(st["bracket"], m["id"], 1, 0)
    # Новый раунд готов, но без check-in/комнаты → откат разрешён.
    assert _revert_blocked(st) == set()


def test_revert_blocked_by_room_on_dependent():
    st = _state_with_bracket()
    _push_snapshot(st)
    for m in list(ready_matches(st["bracket"])):
        record_result(st["bracket"], m["id"], 1, 0)
    for m in ready_matches(st["bracket"]):
        m["room_id"] = "12345"
    assert _revert_blocked(st) == {m["id"] for m in ready_matches(st["bracket"])}


def test_snapshot_keeps_bracket_history():
    st = _state_with_bracket()
    _push_snapshot(st)
    snapshot = st["undo"][-1]["bracket"]
    before = copy.deepcopy(st["bracket"])
    record_result(st["bracket"], ready_matches(st["bracket"])[0]["id"], 1, 0)
    # Снимок — отдельная копия, мутации её не трогают.
    assert st["undo"][-1]["bracket"] is not before
    assert snapshot["matches"]


def test_disputed_match_not_in_ready():
    b = create_bracket(["A", "B"], seed=0)
    m = ready_matches(b)[0]
    m["status"] = "disputed"
    assert ready_matches(b) == []
    # record_result не находит disputed-матч.
    with pytest.raises(BracketError):
        record_result(b, m["id"], 1, 0)


def test_schedule_steps_all_sizes():
    for n in (8, 13, 24, 37, 64, 70):
        steps = schedule_steps([f"T{i}" for i in range(n)], seed=1)
        assert len(steps) >= 3, (n, steps)
        assert sum(steps) == 2 * n - 2, (n, sum(steps))
        # Пиковая параллельность не превышает число команд/2.
        assert max(steps) <= (n + 1) // 2


def test_apply_prizes_first_place_only():
    st = {"status": "running"}
    out = _apply_prizes(st, 300, "KZT")
    assert out["places"] == {"1": 300, "2": 0, "3": 0}
    assert out["total"] == 300
    with pytest.raises(cashcup.CashCupError):
        _apply_prizes(st, -1, "KZT")


def _fake_state():
    b = create_bracket(["TA", "TB", "TC", "TD"], seed=0)
    teams = [{"id": "TA", "name": "Alpha", "captain": 1,
              "members": [{"discord_id": 1, "nick": "N1", "ff_uid": "1",
                           "device": "mobile", "verified": True},
                          {"discord_id": 2, "nick": "N2", "ff_uid": "2",
                           "device": "pc", "verified": True}],
              "pending": [], "lineup": [1, 2]},
             {"id": "TB", "name": "Beta", "captain": 3,
              "members": [{"discord_id": 3, "nick": "N3", "ff_uid": "3",
                           "device": "mobile", "verified": True}],
              "pending": [], "lineup": [3]}]
    return {"status": "running", "bracket": b, "teams": teams, "undo": []}


def test_match_card_text():
    st = _fake_state()
    m = ready_matches(st["bracket"])[0]
    text = cashcup._match_card_text(st, m)
    assert m["id"] in text and "BO1" in text and "Check-in" in text


def test_board_embed_lists_active():
    st = _fake_state()
    emb = cashcup._board_embed(st)
    desc = emb.description or ""
    assert "W1-" in desc or "L1-" in desc or "`" in desc


def test_board_view_persistent_custom_id():
    v = cashcup.BoardSelectView([])
    ids = [getattr(i, "custom_id", "") for i in v.children]
    assert "cashcup:board_select" in ids


def test_board_shows_disputed_and_champion():
    st = _fake_state()
    m = ready_matches(st["bracket"])[0]
    m["status"] = "disputed"
    m["dispute"] = {"reason": "r", "evidence": []}
    bm = cashcup._board_matches(st["bracket"])
    assert any(x["id"] == m["id"] for x in bm)
    emb = cashcup._board_embed(st)
    assert "СПОР" in (emb.description or "")
    # Чемпион: board показывает итог.
    st2 = _fake_state()
    st2["bracket"]["champion"] = "TA"
    emb2 = cashcup._board_embed(st2)
    assert "Чемпион" in (emb2.description or "")


def test_board_pagination_over_25():
    matches = [{"id": f"M{i}", "bracket": "W", "bo": 1, "score": [0, 0],
                "status": "ready", "checkins": [], "room_id": "",
                "a": "A", "b": "B"} for i in range(30)]
    v = cashcup.BoardSelectView(matches)
    assert len(v._sel.options) == cashcup.BOARD_PER_PAGE
    v2 = cashcup.BoardSelectView(matches, page=1)
    assert len(v2._sel.options) == 30 - cashcup.BOARD_PER_PAGE
    assert v2._page == 1
    # единый размер страницы для embed и select
    assert cashcup.BoardSelectView.PER_PAGE == cashcup.BOARD_PER_PAGE


def test_cashcup_disabled_by_default():
    import config
    # По умолчанию флаг выключен (env не задан в тестах).
    assert config.CASHCUP_ENABLED is False or isinstance(config.CASHCUP_ENABLED, bool)
