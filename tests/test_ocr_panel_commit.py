"""Регрессии: панель OCR закрепляется только подтверждением маппинга.

- Открытие окна сопоставления / skeleton maps[team] НЕ резервирует
  панель: капитан может закрыть окно и выбрать другую сторону.
- «Эта панель уже закреплена» срабатывает только когда панель
  фактически занята другой командой (подтверждение или привязки).
- _open_panel_mapping на уже привязанной панели открывает
  фактическую панель, а не повторно выбранную.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from views import ocr_match_view as omv


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(omv.store, "save", lambda: None)
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


# --- helpers ---

def test_ensure_team_panel_does_not_reserve():
    """Skeleton без привязок не пишет panel_picks и не claim'ит панель."""
    t = _tournament()
    s = _session()
    t.ocr_sessions["qualifier_0"] = s

    panel = omv._ensure_team_panel(t, s, 0, panel="left")
    assert panel == "left"
    assert s["maps"]["0"]["panel"] == "left"
    # panel_picks не трогаем — выбор ещё не подтверждён
    assert s.get("panel_picks", {}) == {}
    # Для другой команды панель НЕ занята
    assert not omv._panel_claimed_by_other(s, 1, "left")


def test_unbound_skeleton_can_switch_panel():
    """Команда без привязок может переоткрыть маппинг на другой панели."""
    t = _tournament()
    s = _session()
    t.ocr_sessions["qualifier_0"] = s

    omv._ensure_team_panel(t, s, 0, panel="left")
    # Skeleton на left без привязок — _open_panel_mapping заменит на right
    own = omv._team_map(s, 0)
    assert own["panel"] == "left"
    assert not omv._map_has_claims(own)

    # Имитируем логику _open_panel_mapping: skeleton без claims → pop
    omv._match_maps(s).pop("0", None)
    assert omv._ensure_team_panel(t, s, 0, panel="right") == "right"
    assert s["maps"]["0"]["panel"] == "right"
    assert s.get("panel_picks", {}) == {}


def test_panel_claimed_after_bind():
    """После привязки строки панель считается занятой для другой команды."""
    t = _tournament()
    s = _session()
    t.ocr_sessions["qualifier_0"] = s

    omv._ensure_team_panel(t, s, 0, panel="left")
    row = s["maps"]["0"]["rows"][0]
    row.update(discord_name="P1", discord_id=113, source="self")

    assert omv._panel_claimed_by_other(s, 1, "left")
    assert not omv._panel_claimed_by_other(s, 1, "right")


def test_confirmed_map_claims_panel():
    """Подтверждённый маппинг claim'ит панель даже без строк."""
    t = _tournament()
    s = _session()
    s["maps"] = {"0": {"panel": "left", "rows": [], "confirmed": True}}
    assert omv._panel_claimed_by_other(s, 1, "left")


def test_panel_picks_legacy_not_a_claim():
    """Старый panel_picks-резерв не блокирует выбор другой командой."""
    t = _tournament()
    s = _session()
    s["panel_picks"] = {"0": "left"}  # legacy-резерв без maps
    assert not omv._panel_claimed_by_other(s, 1, "left")


# --- _open_panel_mapping end-to-end (fake interaction) ---

class _Resp:
    def __init__(self):
        self.sent = []

    async def send_message(self, *a, **kw):
        self.sent.append((a, kw))


class _Chan:
    id = 999


class _Guild:
    id = 1

    def get_channel(self, cid):
        return None


class _Ix:
    def __init__(self):
        self.guild = _Guild()
        self.channel = _Chan()
        self.response = _Resp()


@pytest.mark.asyncio
async def test_open_panel_mapping_abandoned_allows_other_panel():
    """Капитан выбрал left, закрыл окно, потом выбрал right — без ошибки."""
    t = _tournament()
    s = _session()
    t.ocr_sessions["qualifier_0"] = s
    omv.store._tournaments[(1, 2)] = t
    ix = _Ix()

    try:
        await omv._open_panel_mapping(ix, t, "qualifier", 0, 0, "left")
        assert len(ix.response.sent) == 1
        assert s["maps"]["0"]["panel"] == "left"
        assert s.get("panel_picks", {}) == {}

        # Закрыл ephemeral — состояние не закоммичено. Выбирает right:
        ix2 = _Ix()
        await omv._open_panel_mapping(ix2, t, "qualifier", 0, 0, "right")
        assert len(ix2.response.sent) == 1  # не «уже закреплена»
        assert s["maps"]["0"]["panel"] == "right"
        assert s.get("panel_picks", {}) == {}
    finally:
        omv.store._tournaments.pop((1, 2), None)


@pytest.mark.asyncio
async def test_open_panel_mapping_blocked_only_by_other_claim():
    """«Уже закреплена» — только если панель занята ДРУГОЙ командой."""
    t = _tournament()
    s = _session()
    t.ocr_sessions["qualifier_0"] = s
    omv.store._tournaments[(1, 2)] = t

    # Команда 1 заняла left привязкой
    omv._ensure_team_panel(t, s, 1, panel="left")
    s["maps"]["1"]["rows"][0].update(
        discord_name="Q1", discord_id=223, source="self"
    )

    ix = _Ix()
    try:
        # Команда 0 пытается взять left → отказ
        await omv._open_panel_mapping(ix, t, "qualifier", 0, 0, "left")
        assert len(ix.response.sent) == 1
        text = str(ix.response.sent[0])
        assert "уже закреплена" in text
        assert "0" not in s.get("maps", {})

        # ...но right доступна
        ix2 = _Ix()
        await omv._open_panel_mapping(ix2, t, "qualifier", 0, 0, "right")
        assert s["maps"]["0"]["panel"] == "right"
    finally:
        omv.store._tournaments.pop((1, 2), None)


@pytest.mark.asyncio
async def test_panel_mapping_view_uses_tournament_channel():
    """Regression: OCR-карточка живёт в канале результатов (999),
    а store ищет турнир по каналу турнира (2). PanelMappingView
    обязан получить channel_id ТУРНИРА — иначе все его callback'и
    падали с «Турнир не найден»."""
    t = _tournament()  # channel_id=2
    s = _session()
    t.ocr_sessions["qualifier_0"] = s
    omv.store._tournaments[(1, 2)] = t

    ix = _Ix()  # interaction.channel.id = 999 (канал результатов)
    try:
        await omv._open_panel_mapping(ix, t, "qualifier", 0, 0, "left")
        assert ix.response.sent, "mapping view was not sent"
        view = ix.response.sent[0][1].get("view")
        assert view is not None
        custom_ids = [
            getattr(item, "custom_id", "")
            for item in view.walk_children()
        ]
        edit_ids = [c for c in custom_ids if c.startswith("ocr_edit:")]
        assert edit_ids, "PanelMappingView buttons missing"
        # custom_id: ocr_edit:{guild}:{channel_id}:{match_type}:...
        assert all(":1:2:" in c for c in edit_ids), custom_ids
        assert not any(":999:" in c for c in custom_ids)
    finally:
        omv.store._tournaments.pop((1, 2), None)


def test_self_display_prefers_discord_nick():
    """Self-привязка показывается Discord-ником, без длинной подписи."""
    row = {"discord_name": "GameNick", "discord_display": "DiscordNick",
           "source": "self"}
    assert omv._self_display(row) == "DiscordNick"
    # Старые сессии без discord_display — fallback на discord_name
    assert omv._self_display({"discord_name": "GameNick"}) == "GameNick"


def test_panel_mapping_placeholder_shows_discord_nick():
    """В окне сопоставления self-строка помечается Discord-ником."""
    t = _tournament()
    s = _session()
    t.ocr_sessions["qualifier_0"] = s
    omv._ensure_team_panel(t, s, 0, panel="left")
    row = s["maps"]["0"]["rows"][0]
    row.update(
        discord_name="GameNick", discord_id=113,
        discord_display="DiscordNick", source="self",
    )
    view = omv.PanelMappingView(1, 2, t, "qualifier", 0, 0, "left")
    placeholders = [
        getattr(item, "placeholder", "") for item in view.children
        if hasattr(item, "placeholder")
    ]
    assert any("DiscordNick" in p for p in placeholders)
    assert not any("сам выбрал" in p for p in placeholders)
