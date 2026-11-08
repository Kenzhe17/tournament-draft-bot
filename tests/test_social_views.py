"""Тесты V2-сообщений социальных функций."""

import types

from services.social.birthdays import (
    _birthday_label, build_birthdays_view,
)
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
    assert "**БЛИЖАЙШИЕ ДНИ РОЖДЕНИЯ**" in joined


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
    assert labels == ["Принять предложение", "Отклонить"]
    assert all(not b["disabled"] for b in rows[0]["components"])
    assert any("attachment://predlozhenie.png" in str(c) for c in comps)


def test_proposal_final_state_disables_buttons():
    view = build_proposal_view(
        _member(1, "A"), _member(2, "B"), state="accepted", guild_id=1)
    comps = _components(view)
    rows = [c for c in comps if c["type"] == 1]
    assert all(b["disabled"] for b in rows[0]["components"])
    texts = "\n".join(c.get("content", "") for c in comps if c["type"] == 10)
    assert "Предложение принято" in texts
