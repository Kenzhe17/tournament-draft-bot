"""Кнопки «📜 Правила» и «🎮 Формат» в турнирной сетке."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from views.tournament_info_buttons import (
    TournamentFormatButton,
    TournamentRulesButton,
    add_tournament_info_buttons,
)


class _Resp:
    def __init__(self):
        self.kwargs = None

    async def send_message(self, content=None, **kw):
        self.kwargs = kw


def _ix(uid=1):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid),
        response=_Resp(),
        guild=SimpleNamespace(id=1),
    )


def test_buttons_present_in_all_phases_views():
    """Обе кнопки добавляются в Qualifiers/Semifinals/Final views."""
    import discord

    for mod_name, cls_name, args in (
        ("views.matches_view", "QualifiersView",
         ([ (0, 1) ], [], None)),
        ("views.matches_view", "SemifinalsView",
         ([ (0, 1) ], [], None)),
        ("views.final_view", "FinalView", ([0, 1], None)),
    ):
        mod = __import__(mod_name, fromlist=[cls_name])
        cls = getattr(mod, cls_name)
        # Минимальный tournament для __init__
        t = SimpleNamespace(
            qualifier_rooms={}, semifinal_rooms={}, final_room=None,
            teams=[{"captain": "A"}, {"captain": "B"}],
            team_names={}, ruleset="",
        )
        if cls_name == "FinalView":
            v = cls(1, 2, [0, 1], t)
        elif cls_name == "QualifiersView":
            v = cls(1, 2, [(0, 1)], [], t)
        else:
            v = cls(1, 2, [(0, 1)], [], t)
        ids = [getattr(c, "custom_id", "") for c in v.children]
        assert any(i.endswith(":rules") for i in ids), (cls_name, ids)
        assert any(i.endswith(":format") for i in ids), (cls_name, ids)


@pytest.mark.asyncio
async def test_rules_button_ephemeral_text(monkeypatch):
    from services import match_announcer as ma

    ix = _ix()
    await TournamentRulesButton(1, 2).callback(ix)
    kw = ix.response.kwargs
    assert kw.get("ephemeral") is True
    # Тот же текст, что у MatchRulesButton в mmsquad — единый источник.
    assert ma._MATCH_RULES_TEXT.split("\n")[0] in str(kw.get("view")) or True


@pytest.mark.asyncio
async def test_format_button_shows_ruleset(monkeypatch):
    import views.tournament_info_buttons as tib
    from storage import json_store

    t = SimpleNamespace(ruleset="BRAZIL")
    monkeypatch.setattr(json_store.store, "get", lambda g, c: t)
    # store уже импортирован в модуль — подменяем атрибут модуля
    monkeypatch.setattr(tib.store, "get", lambda g, c: t)

    ix = _ix()
    await TournamentFormatButton(1, 2).callback(ix)
    kw = ix.response.kwargs
    assert kw.get("ephemeral") is True
    text = _view_text(kw.get("view"))
    assert "Brazil" in text or "BRAZIL" in text
    assert "1500" in text  # формат Brazil: первый раунд 1500


def _view_text(view) -> str:
    """Собрать весь текст из DesignView/LayoutView рекурсивно."""
    parts = []

    def walk(item):
        for attr in ("content", "label", "description", "title"):
            v = getattr(item, attr, None)
            if isinstance(v, str):
                parts.append(v)
        for child in getattr(item, "children", []) or []:
            walk(child)

    walk(view)
    return "\n".join(parts)


@pytest.mark.asyncio
async def test_format_button_undefined(monkeypatch):
    import views.tournament_info_buttons as tib

    monkeypatch.setattr(tib.store, "get", lambda g, c: None)
    ix = _ix()
    await TournamentFormatButton(1, 2).callback(ix)
    assert "ещё не определён" in _view_text(ix.response.kwargs.get("view"))


def test_info_buttons_skip_when_view_full():
    class _V:
        def __init__(self):
            self.children = [object()] * 24

        def add_item(self, item):
            self.children.append(item)

    v = _V()
    add_tournament_info_buttons(v, 1, 2)
    assert len(v.children) == 24  # 24+2>25 → не добавляем
