"""Длительность матча: форматирование, «Начало» из карточки матча,
поле «Длительность матча» в итоговой карточке."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cogs.ocr_upload import _format_duration, _result_card


def test_format_duration_minutes_seconds():
    assert _format_duration(625) == "10 мин. 25 сек."
    assert _format_duration(59) == "0 мин. 59 сек."


def test_format_duration_hours():
    assert _format_duration(4320) == "1 ч. 12 мин."
    assert _format_duration(3600) == "1 ч. 0 мин."
    assert _format_duration(9000) == "2 ч. 30 мин."


def test_format_duration_zero_and_negative():
    assert _format_duration(0) == "0 мин. 0 сек."
    assert _format_duration(-50) == "0 мин. 0 сек."


def _fake_tournament() -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=1,
        channel_id=2,
        mm_search_channel_id=555,
        teams=[
            {"captain": "CapA", "players": ["P1", "P2"]},
            {"captain": "CapB", "players": ["P3", "P4"]},
        ],
        team_names={0: "Team A", 1: "Team B"},
        player_user_ids={"P1": 11, "P2": 12, "P3": 13, "P4": 14},
    )


def _card_texts(view) -> str:
    parts = []
    stack = list(getattr(view, "children", []))
    while stack:
        item = stack.pop()
        content = getattr(item, "content", None)
        if content:
            parts.append(str(content))
        stack.extend(getattr(item, "children", []) or [])
    return "\n".join(parts)


def test_result_card_shows_duration():
    view = _result_card(
        _fake_tournament(), "qualifier", 0, 0, 1, match_num=3,
        duration="10 мин. 25 сек.",
    )
    text = _card_texts(view)
    assert "Длительность матча" in text
    assert "10 мин. 25 сек." in text


def test_result_card_without_duration_omits_field():
    view = _result_card(
        _fake_tournament(), "qualifier", 0, 0, 1, match_num=3,
    )
    assert "Длительность матча" not in _card_texts(view)


def test_match_started_view_uses_started_at():
    from services.match_announcer import _match_started_view

    ts = 1_700_000_000
    view = _match_started_view(
        _fake_tournament(), 1, "Матч 1", "CapA", "CapB", 0, 1,
        "qualifier", 0, with_png=False, started_at=float(ts),
    )
    assert f"<t:{ts}:t>" in _card_texts(view)
