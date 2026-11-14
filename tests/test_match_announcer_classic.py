"""Classic layout: карточка анонса матча — только название,
противостояние и составы. Без блока «ИНФОРМАЦИЯ» и кнопки
«📜 Правила»."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.tournament import Tournament
from services.match_announcer import _match_card_view


def _tournament() -> Tournament:
    t = Tournament(guild_id=1, channel_id=2)
    t.teams = [
        {"captain": "CapA", "players": ["A1", "A2", "A3", "A4"]},
        {"captain": "CapB", "players": ["B1", "B2", "B3", "B4"]},
    ]
    t.team_names = {0: "Alpha", 1: "Beta"}
    t.player_user_ids = {}
    return t


def _walk_items(view):
    for item in getattr(view, "children", []):
        yield item
        yield from _walk_items(item)


def test_match_card_has_no_info_block_or_rules_button():
    view = _match_card_view(_tournament(), 3, "Финал", "Alpha", "Beta", 0, 1)
    texts = [
        getattr(i, "content", "")
        for i in _walk_items(view)
        if getattr(i, "content", "")
    ]
    joined = "\n".join(texts)
    assert "ИНФОРМАЦИЯ" not in joined
    assert "Kazakhstan" not in joined
    assert "Правила" not in joined

    buttons = [
        i for i in _walk_items(view)
        if type(i).__name__ == "Button"
    ]
    assert buttons == []

    # Название, противостояние и составы сохранены
    assert "Финал" in joined
    assert "Alpha" in joined and "Beta" in joined
    assert "A1" in joined and "B4" in joined
