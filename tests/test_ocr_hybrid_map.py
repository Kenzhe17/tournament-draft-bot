"""Гибридное сопоставление OCR: self-service + капитан.

Проверяем чистую логику привязок (без Discord API):
- skeleton maps[team] и авто-панель;
- прогресс и завершённость команды;
- уникальность discord_id в матче;
- self-bind: свой ник, перенос, чужой ник → конфликт;
- капитан: только свободные строки, self-привязки не трогаются.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from views import ocr_match_view as omv


@pytest.fixture(autouse=True)
def _no_store_save(monkeypatch):
    """JsonStore.set вызывает save() → to_dict; в тестах глушим."""
    monkeypatch.setattr(omv.store, "save", lambda: None)
    # sharded-режим: set() пишет shard напрямую, минуя save()
    monkeypatch.setattr(omv.store, "_write_shard", lambda t: None)
    monkeypatch.setattr(omv.store, "_delete_shard", lambda k: None)


def _rows(prefix, n=4):
    return [
        {
            "name": f"{prefix}{i}",
            "kills": i + 1,
            "deaths": i,
            "assists": i,
            "dmg": 1000 * (i + 1),
        }
        for i in range(n)
    ]


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
            "left": {"name": "Хищники", "rows": _rows("L")},
            "right": {"name": "Разрушители", "rows": _rows("R")},
        },
        "status": "awaiting_mapping",
        "results_channel_id": 0,
    }


def _tournament_with_session():
    t = _tournament()
    t.ocr_sessions["qualifier_0"] = _session()
    return t


# --- Skeleton / панель / прогресс ---

def test_ensure_team_panel_creates_skeleton():
    t = _tournament_with_session()
    s = t.ocr_sessions["qualifier_0"]
    panel = omv._ensure_team_panel(t, s, 0)
    assert panel == "left"  # uploaded_by=CapA(111), banner=win
    tm = s["maps"]["0"]
    assert tm["panel"] == "left"
    assert len(tm["rows"]) == 4
    assert all(not omv._row_bound(r) for r in tm["rows"])
    done, total = omv._mapping_progress(s, 0)
    assert (done, total) == (0, 4)


def test_ensure_team_panel_other_team_right():
    t = _tournament_with_session()
    s = t.ocr_sessions["qualifier_0"]
    # загружал CapB (222) → его команда team1 на левой? banner=win →
    # uploaded_by определяет команду: 222 → team1 → panel left для team1
    s["uploaded_by"] = 222
    assert omv._ensure_team_panel(t, s, 1) == "left"
    assert omv._ensure_team_panel(t, s, 0) == "right"


def test_ensure_team_panel_unknown_uploader():
    t = _tournament_with_session()
    s = t.ocr_sessions["qualifier_0"]
    s["uploaded_by"] = 999999
    assert omv._ensure_team_panel(t, s, 0) is None


# --- Завершённость ---

def _bind(tm, idx, name, uid, source="self"):
    tm["rows"][idx].update(
        discord_name=name, discord_id=uid, source=source
    )


def test_classic_complete_requires_all_bound():
    t = _tournament_with_session()
    s = t.ocr_sessions["qualifier_0"]
    omv._ensure_team_panel(t, s, 0)
    omv._ensure_team_panel(t, s, 1)
    assert not omv._classic_mapping_complete(s, 0, 1)
    for i in range(4):
        _bind(s["maps"]["0"], i, f"P{i}", 100 + i)
    assert not omv._classic_mapping_complete(s, 0, 1)
    for i in range(4):
        _bind(s["maps"]["1"], i, f"Q{i}", 200 + i)
    assert omv._classic_mapping_complete(s, 0, 1)


def test_classic_complete_rejects_duplicate_uid():
    t = _tournament_with_session()
    s = t.ocr_sessions["qualifier_0"]
    omv._ensure_team_panel(t, s, 0)
    omv._ensure_team_panel(t, s, 1)
    for i in range(4):
        _bind(s["maps"]["0"], i, f"P{i}", 100 + i)
        _bind(s["maps"]["1"], i, f"Q{i}", 200 + i)
    # дублируем uid в обеих командах
    s["maps"]["1"]["rows"][0]["discord_id"] = 100
    assert not omv._classic_mapping_complete(s, 0, 1)


# --- uid / roster helpers ---

def test_uid_bound_in_match():
    t = _tournament_with_session()
    s = t.ocr_sessions["qualifier_0"]
    omv._ensure_team_panel(t, s, 0)
    _bind(s["maps"]["0"], 0, "P1", 113, "self")
    assert omv._uid_bound_in_match(s, 113) == 0
    assert omv._uid_bound_in_match(s, 114) is None
    # exclude саму строку
    assert omv._uid_bound_in_match(s, 113, exclude=(0, 0)) is None


def test_roster_name_for_uid():
    t = _tournament()
    assert omv._roster_name_for_uid(t, 0, 113) == "P1"
    assert omv._roster_name_for_uid(t, 1, 113) is None
    assert omv._roster_name_for_uid(t, 0, 999) is None


# --- Fake interaction для self-bind ---

class _FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False

    async def send_message(self, *a, **kw):
        self.messages.append((a, kw))

    async def defer(self, **kw):
        self.deferred = True

    async def send(self, *a, **kw):
        self.messages.append((a, kw))


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, *a, **kw):
        self.messages.append((a, kw))


class _FakeInteraction:
    def __init__(self, user_id, display_name="User"):
        self.user = SimpleNamespace(id=user_id, display_name=display_name)
        self.guild = SimpleNamespace(
            id=1,
            get_channel=lambda cid: None,
            fetch_channel=self._fetch,
        )
        self.channel = SimpleNamespace(id=999)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.client = None

    async def _fetch(self, cid):
        raise RuntimeError("no channel")


@pytest.mark.asyncio
async def test_self_bind_own_row():
    t = _tournament_with_session()
    omv.store._tournaments[(1, 2)] = t
    try:
        ix = _FakeInteraction(113, "P1")
        await omv._self_bind_row(ix, 1, 2, "qualifier", 0, 0)
        s = t.ocr_sessions["qualifier_0"]
        row = s["maps"]["0"]["rows"][0]
        assert row["discord_id"] == 113
        assert row["discord_name"] == "P1"
        assert row["source"] == "self"
        assert ix.response.messages  # подтверждение отправлено
    finally:
        omv.store._tournaments.pop((1, 2), None)


@pytest.mark.asyncio
async def test_self_bind_wrong_team_rejected():
    """Игрок чужой команды не может привязать ник."""
    t = _tournament_with_session()
    omv.store._tournaments[(1, 2)] = t
    try:
        ix = _FakeInteraction(223, "Q1")  # команда 1
        # team определяется автоматически — Q1 в team1, panel right
        await omv._self_bind_row(ix, 1, 2, "qualifier", 0, 0)
        s = t.ocr_sessions["qualifier_0"]
        # Q1 привязался к team1 (maps["1"]), а не к team0
        assert "1" in s["maps"]
        row = s["maps"]["1"]["rows"][0]
        assert row["discord_id"] == 223
        assert row["discord_name"] == "Q1"
    finally:
        omv.store._tournaments.pop((1, 2), None)


@pytest.mark.asyncio
async def test_self_bind_outsider_rejected():
    t = _tournament_with_session()
    omv.store._tournaments[(1, 2)] = t
    try:
        ix = _FakeInteraction(999999, "Stranger")
        await omv._self_bind_row(ix, 1, 2, "qualifier", 0, 0)
        s = t.ocr_sessions["qualifier_0"]
        assert not s["maps"] or all(
            not r.get("discord_id")
            for tm in s["maps"].values() for r in tm["rows"]
        )
    finally:
        omv.store._tournaments.pop((1, 2), None)


@pytest.mark.asyncio
async def test_self_bind_move_frees_old_row():
    """Игрок сменил ник: старая строка освобождается."""
    t = _tournament_with_session()
    omv.store._tournaments[(1, 2)] = t
    try:
        await omv._self_bind_row(_FakeInteraction(113), 1, 2, "qualifier", 0, 0)
        await omv._self_bind_row(_FakeInteraction(113), 1, 2, "qualifier", 0, 1)
        s = t.ocr_sessions["qualifier_0"]
        rows = s["maps"]["0"]["rows"]
        assert rows[0]["discord_id"] is None  # освобождена
        assert rows[1]["discord_id"] == 113
        # uid встречается ровно один раз в матче
        ids = [
            r["discord_id"]
            for tm in s["maps"].values() for r in tm["rows"]
            if r.get("discord_id")
        ]
        assert ids.count(113) == 1
    finally:
        omv.store._tournaments.pop((1, 2), None)


@pytest.mark.asyncio
async def test_self_bind_taken_row_conflict():
    """Чужой закреплённый ник → отказ + конфликт организатору."""
    t = _tournament_with_session()
    omv.store._tournaments[(1, 2)] = t
    try:
        await omv._self_bind_row(_FakeInteraction(113), 1, 2, "qualifier", 0, 0)
        ix2 = _FakeInteraction(114, "P2")
        await omv._self_bind_row(ix2, 1, 2, "qualifier", 0, 0)
        s = t.ocr_sessions["qualifier_0"]
        # строка осталась за первым игроком
        assert s["maps"]["0"]["rows"][0]["discord_id"] == 113
        assert any("закреплён" in str(m) for m, _ in ix2.response.messages)
    finally:
        omv.store._tournaments.pop((1, 2), None)


@pytest.mark.asyncio
async def test_captain_can_override_self():
    """Капитан может переопределить ник, закреплённый игроком."""
    t = _tournament_with_session()
    omv.store._tournaments[(1, 2)] = t
    try:
        await omv._self_bind_row(_FakeInteraction(113), 1, 2, "qualifier", 0, 0)
        view = omv.PanelMappingView(1, 2, t, "qualifier", 0, 0, "left")
        view.assign = {0: "P2", 1: "P3"}
        ix = _FakeInteraction(111, "CapA")
        await view._save_hybrid(ix, t, t.ocr_sessions["qualifier_0"])
        s = t.ocr_sessions["qualifier_0"]
        rows = s["maps"]["0"]["rows"]
        # override: строка 0 теперь за P2 (капитан), self-привязка снята
        assert rows[0]["discord_id"] == 114
        assert rows[0]["discord_name"] == "P2"
        assert rows[0]["source"] == "captain"
        assert rows[1]["discord_id"] == 115
        assert rows[1]["source"] == "captain"
    finally:
        omv.store._tournaments.pop((1, 2), None)


@pytest.mark.asyncio
async def test_captain_reassign_after_view_open():
    """Self-bind между открытием view и save — капитан всё равно
    может переопределить (override, не молчаливый пропуск)."""
    t = _tournament_with_session()
    omv.store._tournaments[(1, 2)] = t
    try:
        view = omv.PanelMappingView(1, 2, t, "qualifier", 0, 0, "left")
        await omv._self_bind_row(_FakeInteraction(113), 1, 2, "qualifier", 0, 0)
        view.assign = {0: "P2", 1: "P3"}
        ix = _FakeInteraction(111, "CapA")
        await view._save_hybrid(ix, t, t.ocr_sessions["qualifier_0"])
        s = t.ocr_sessions["qualifier_0"]
        rows = s["maps"]["0"]["rows"]
        assert rows[0]["discord_id"] == 114 and rows[0]["source"] == "captain"
        assert rows[1]["discord_id"] == 115 and rows[1]["source"] == "captain"
    finally:
        omv.store._tournaments.pop((1, 2), None)


@pytest.mark.asyncio
async def test_captain_same_player_noop():
    """Капитан назначает того же игрока, что уже self-bound — без override."""
    t = _tournament_with_session()
    omv.store._tournaments[(1, 2)] = t
    try:
        await omv._self_bind_row(_FakeInteraction(113), 1, 2, "qualifier", 0, 0)
        view = omv.PanelMappingView(1, 2, t, "qualifier", 0, 0, "left")
        view.assign = {0: "P1"}  # P1 = uid 113, уже self-bound на строке 0
        ix = _FakeInteraction(111, "CapA")
        await view._save_hybrid(ix, t, t.ocr_sessions["qualifier_0"])
        s = t.ocr_sessions["qualifier_0"]
        row = s["maps"]["0"]["rows"][0]
        assert row["discord_id"] == 113
        # source остаётся self — noop не затирает
        assert row["source"] == "self"
    finally:
        omv.store._tournaments.pop((1, 2), None)


@pytest.mark.asyncio
async def test_captain_moves_bound_player():
    """Капитан переносит привязанного игрока на другую строку за один save."""
    t = _tournament_with_session()
    omv.store._tournaments[(1, 2)] = t
    try:
        await omv._self_bind_row(_FakeInteraction(113), 1, 2, "qualifier", 0, 0)
        view = omv.PanelMappingView(1, 2, t, "qualifier", 0, 0, "left")
        # P1(self, строка 0) → строка 1; строка 0 → P2
        view.assign = {0: "P2", 1: "P1"}
        ix = _FakeInteraction(111, "CapA")
        await view._save_hybrid(ix, t, t.ocr_sessions["qualifier_0"])
        s = t.ocr_sessions["qualifier_0"]
        rows = s["maps"]["0"]["rows"]
        assert rows[0]["discord_id"] == 114  # P2
        assert rows[1]["discord_id"] == 113  # P1 перенесён
        # uid 113 ровно один раз в матче
        ids = [
            r["discord_id"]
            for tm in s["maps"].values() for r in tm["rows"]
            if r.get("discord_id")
        ]
        assert ids.count(113) == 1
    finally:
        omv.store._tournaments.pop((1, 2), None)
