"""Тесты V2-сообщений социальных функций."""

import types

from services.social.birthdays import (
    _birthday_label, build_birthdays_view,
)
from services.social.birthday_congrats import build_congrats_view
from services.social.marriages import build_marriages_view
from services.social.proposal import build_proposal_view


def _member(uid: int, name: str):
    return types.SimpleNamespace(
        id=uid, mention=f"<@{uid}>", display_name=name,
        guild=types.SimpleNamespace(id=1),
    )


def _components(view) -> list:
    return view.to_components()[0]["components"]


def test_birthdays_empty():
    view = build_birthdays_view([])
    texts = [c.get("content", "") for c in _components(view) if c["type"] == 10]
    assert any("нет сохранённых дней рождения" in t for t in texts)


def test_birthdays_today_and_upcoming():
    entries = [
        {"user_id": 1, "name": "A", "birthday": "09.11.2001",
         "days": 0, "mention": "<@1>"},
        {"user_id": 2, "name": "B", "birthday": "12.11.2000",
         "days": 3, "mention": "<@2>"},
        {"user_id": 3, "name": "LeftUser", "birthday": "24.12.1999",
         "days": 45, "mention": None},
    ]
    view = build_birthdays_view(entries)
    texts = [c.get("content", "") for c in _components(view) if c["type"] == 10]
    joined = "\n".join(texts)
    assert "**СЕГОДНЯ**" in joined
    assert "<@1> — 9 ноября · сегодня" in joined
    assert "<@2> — 12 ноября · 3 дн." in joined
    assert "LeftUser — 24 декабря · 45 дн." in joined  # без mention для вышедшего
    assert "**БЛИЖАЙШИЕ**" in joined


def test_birthdays_label_no_year():
    assert _birthday_label("09.11.2001", 5) == "9 ноября · 5 дн."
    assert _birthday_label("29.02.2000", 0) == "29 февраля · сегодня"
    assert _birthday_label("29.02.2000", 1) == "29 февраля · завтра"


def test_proposal_pending_has_buttons():
    view = build_proposal_view(_member(1, "A"), _member(2, "B"), guild_id=1)
    comps = _components(view)
    rows = [c for c in comps if c["type"] == 1]
    assert len(rows) == 1
    labels = [b["label"] for b in rows[0]["components"]]
    assert labels == ["Принять", "Отклонить"]
    assert all(not b["disabled"] for b in rows[0]["components"])
    assert any("attachment://proposal.png" in str(c) for c in comps)


def test_proposal_final_state_disables_buttons():
    view = build_proposal_view(
        _member(1, "A"), _member(2, "B"), state="accepted", guild_id=1)
    comps = _components(view)
    rows = [c for c in comps if c["type"] == 1]
    assert all(b["disabled"] for b in rows[0]["components"])
    texts = "\n".join(c.get("content", "") for c in comps if c["type"] == 10)
    assert "Предложение принято" in texts


def test_congrats_view():
    view = build_congrats_view(_member(7, "Именинник"))
    comps = _components(view)
    texts = "\n".join(c.get("content", "") for c in comps if c["type"] == 10)
    assert "С днём рождения, <@7>!" in texts
    assert any("attachment://birthday_congrats.png" in str(c) for c in comps)


def test_marriages_empty():
    view = build_marriages_view([], invoker_id=1, guild_id=1)
    texts = "\n".join(c.get("content", "") for c in _components(view) if c["type"] == 10)
    assert "нет действующих союзов" in texts
    assert any("attachment://marriages.png" in str(c) for c in _components(view))


def test_marriages_list_and_pagination():
    from datetime import datetime, timezone
    entries = [
        {"user_a": i, "user_b": i + 100,
         "married_at": datetime(2026, 11, 9, tzinfo=timezone.utc)}
        for i in range(10)
    ]
    view = build_marriages_view(entries, invoker_id=1, guild_id=1)
    comps = _components(view)
    texts = "\n".join(c.get("content", "") for c in comps if c["type"] == 10)
    assert "Всего действующих браков: 10" in texts
    assert "стр. 1/2" in texts
    assert "01 · <@0> & <@100>" in texts
    # 8 на страницу — 9-й и 10-й на второй
    assert "<@8>" not in texts
    rows = [c for c in comps if c["type"] == 1]
    assert len(rows) == 1
    labels = [b["label"] for b in rows[0]["components"]]
    assert labels == ["Назад", "Далее"]
    disabled = [b["disabled"] for b in rows[0]["components"]]
    assert disabled == [True, False]
