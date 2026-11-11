"""UI-полировка OCR/карточки матча.

- Кнопки «Отправить результат» / «Комната» / «📜 Правила» — одна строка.
- Повторный _refresh_public_message без изменения состояния не
  редактирует сообщение (PNG не перерисовывается бесконечно).
- Подписи источника привязки — «Игрок сам выбрал себя», без 'self'.
- SelfMapView — минимальный текст (без K/D/A-воды и 'self').
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from views import ocr_match_view as omv
from services.match_announcer import _match_started_view


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(omv.store, "save", lambda: None)
    # sharded-режим: set() пишет shard напрямую, минуя save()
    monkeypatch.setattr(omv.store, "_write_shard", lambda t: None)
    monkeypatch.setattr(omv.store, "_delete_shard", lambda k: None)
    omv._REFRESH_SIGS.clear()
    yield
    omv._REFRESH_SIGS.clear()


def _tournament():
    return SimpleNamespace(
        teams=[
            {"captain": "CapA", "players": ["CapA", "P1", "P2", "P3"]},
            {"captain": "CapB", "players": ["CapB", "Q1", "Q2", "Q3"]},
        ],
        player_user_ids={
            "CapA": 111, "P1": 113, "P2": 114, "P3": 115,
            "CapB": 222, "Q1": 223, "Q2": 224, "Q3": 225,
        },
        qualifier_matches=[(0, 1)],
        semifinal_matches=[],
        final_teams=(0, 1),
        team_names={},
        guild_id=1,
        channel_id=2,
        match_mode="normal",
        ocr_sessions={},
    )


def _session():
    return {
        "mode": "classic",
        "match_type": "qualifier",
        "match_index": 0,
        "teams": [0, 1],
        "uploaded_by": 111,
        "banner": "win",
        "winner_side": "left",
        "maps": {},
        "panel_picks": {},
        "panels": {
            "left": {"name": "A", "rows": [
                {"name": f"L{i}", "kills": 1, "deaths": 0, "assists": 0,
                 "dmg": 100}
                for i in range(4)
            ]},
            "right": {"name": "B", "rows": [
                {"name": f"R{i}", "kills": 1, "deaths": 0, "assists": 0,
                 "dmg": 100}
                for i in range(4)
            ]},
        },
        "status": "awaiting_mapping",
        "results_channel_id": 777,
        "message_id": 555,
    }


# --- Карточка матча: все кнопки в одной строке ---

def _action_rows(view):
    import discord

    rows = []
    for child in view.container.children:
        if isinstance(child, discord.ui.ActionRow):
            rows.append(child)
    return rows


def test_match_card_buttons_single_row():
    t = _tournament()
    view = _match_started_view(
        t, 1, "QF", "CapA", "CapB", 0, 1,
        "qualifier", 0, with_png=False, state="active",
    )
    rows = _action_rows(view)
    assert len(rows) == 1
    labels = [getattr(i, "label", "") for i in rows[0].children]
    assert any("Отправить результат" in l for l in labels)
    assert any("vs" in l for l in labels)  # комната
    assert any("Правила" in l for l in labels)


def test_match_card_processing_keeps_room_rules_same_row():
    t = _tournament()
    view = _match_started_view(
        t, 1, "QF", "CapA", "CapB", 0, 1,
        "qualifier", 0, with_png=False, state="processing",
    )
    rows = _action_rows(view)
    assert len(rows) == 1
    labels = [getattr(i, "label", "") for i in rows[0].children]
    assert not any("Отправить результат" in l for l in labels)
    assert any("Правила" in l for l in labels)


def test_match_card_done_has_no_buttons():
    t = _tournament()
    view = _match_started_view(
        t, 1, "QF", "CapA", "CapB", 0, 1,
        "qualifier", 0, with_png=False, state="done",
        winner_name="CapA", result_url="https://x",
    )
    assert _action_rows(view) == []


# --- Идемпотентный refresh публичного OCR-сообщения ---

class _FakeMsg:
    def __init__(self):
        self.edits = 0
        self.flags = None  # не-V2 → smart_view вернёт view как есть

    async def edit(self, **kw):
        self.edits += 1


class _FakeChannel:
    def __init__(self, msg):
        self.msg = msg

    async def fetch_message(self, mid):
        assert mid == 555
        return self.msg


class _FakeGuild:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, cid):
        return self._channel if cid == 777 else None

    async def fetch_channel(self, cid):
        return self._channel


class _FakeResponse:
    async def send_message(self, *a, **kw):
        pass


class _FakeInteraction:
    def __init__(self, channel):
        self.guild = _FakeGuild(channel)
        self.channel = SimpleNamespace(id=999)
        self.response = _FakeResponse()


@pytest.mark.asyncio
async def test_refresh_public_message_idempotent():
    """Повторный refresh без изменения состояния — 0 лишних edit."""
    t = _tournament()
    t.ocr_sessions["qualifier_0"] = _session()
    msg = _FakeMsg()
    ix = _FakeInteraction(_FakeChannel(msg))

    await omv._refresh_public_message(ix, t, "qualifier", 0)
    assert msg.edits == 1

    # То же состояние — сигнатура совпала, edit не выполняется
    await omv._refresh_public_message(ix, t, "qualifier", 0)
    await omv._refresh_public_message(ix, t, "qualifier", 0)
    assert msg.edits == 1

    # Изменение маппинга — edit снова выполняется ровно один раз
    s = t.ocr_sessions["qualifier_0"]
    omv._ensure_team_panel(t, s, 0)
    s["maps"]["0"]["rows"][0].update(
        discord_name="P1", discord_id=113, source="self"
    )
    await omv._refresh_public_message(ix, t, "qualifier", 0)
    assert msg.edits == 2
    await omv._refresh_public_message(ix, t, "qualifier", 0)
    assert msg.edits == 2


@pytest.mark.asyncio
async def test_self_bind_single_public_edit():
    """self-bind → ровно один edit публичного сообщения."""
    t = _tournament()
    t.ocr_sessions["qualifier_0"] = _session()
    omv.store._tournaments[(1, 2)] = t
    msg = _FakeMsg()
    ix = _FakeInteraction(_FakeChannel(msg))
    ix.user = SimpleNamespace(id=113, display_name="P1")

    class _Resp(_FakeResponse):
        def __init__(self):
            self.messages = []

        async def send_message(self, *a, **kw):
            self.messages.append((a, kw))

    ix.response = _Resp()
    try:
        await omv._self_bind_row(ix, 1, 2, "qualifier", 0, 0)
        assert msg.edits == 1
    finally:
        omv.store._tournaments.pop((1, 2), None)


# --- Подпись 'self' → 'Игрок сам выбрал себя' ---

def test_source_label_no_self_word():
    assert omv._source_label("self") == "Игрок сам выбрал себя"
    assert omv._source_label("captain") == "капитан"
    assert "self" not in omv._source_label("self")


def test_self_map_view_minimal_text():
    """Окно выбора своего ника — без 'self' и лишних пояснений."""
    t = _tournament()
    s = _session()
    t.ocr_sessions["qualifier_0"] = s
    omv._ensure_team_panel(t, s, 0)
    view = omv.SelfMapView(1, 2, t, "qualifier", 0, 0, 113, "P1")
    sel = view.select
    assert sel.placeholder == "Выберите свой игровой ник"
    for opt in sel.options:
        assert "self" not in (opt.label + (opt.description or "")).lower()
        assert "можно сменить" not in (opt.description or "")
        # K/D/A оставляем — нужен для различения строк
        assert "K/D/A" in (opt.description or "")
