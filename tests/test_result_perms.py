"""Права на подтверждение/исправление результатов обычных
турниров (/tournament create): can_manage_tournament_results +
гейты RoomsConfirmView."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.permissions import can_manage_tournament_results


def _user(uid, admin=False, roles=()):
    return SimpleNamespace(
        id=uid,
        guild_permissions=SimpleNamespace(administrator=admin),
        roles=list(roles),
    )


def _guild():
    return SimpleNamespace(id=1, roles=[], get_role=lambda rid: None)


def _tournament(organizer_id=0, tid="t1"):
    return SimpleNamespace(
        id=tid, guild_id=1, channel_id=2, organizer_id=organizer_id,
    )


class _OrgStore:
    def __init__(self, orgs=None):
        self._orgs = orgs or {}

    def get(self, gid, cid):
        return self._orgs.get((gid, cid))


@pytest.fixture
def org_store(monkeypatch):
    import storage.organizer_store as osm

    store = _OrgStore()
    monkeypatch.setattr(osm.organizer_store, "get", store.get)
    # can_manage импортирует organizer_store лениво из модуля —
    # патчим атрибут на самом классе-модуле.
    import utils.permissions as up

    monkeypatch.setattr(
        "storage.organizer_store.organizer_store.get", store.get
    )
    return store


class TestCanManage:
    def test_server_admin(self):
        assert can_manage_tournament_results(
            _user(9, admin=True), _guild(), _tournament()
        )

    def test_tournament_creator(self):
        assert can_manage_tournament_results(
            _user(42), _guild(), _tournament(organizer_id=42)
        )
        assert not can_manage_tournament_results(
            _user(43), _guild(), _tournament(organizer_id=42)
        )

    def test_channel_organizer_of_this_tournament(self, org_store):
        orgs = SimpleNamespace(
            is_organizer=lambda uid: uid == 77,
            tournament_id="t1",
        )
        org_store._orgs[(1, 2)] = orgs
        assert can_manage_tournament_results(
            _user(77), _guild(), _tournament(tid="t1")
        )

    def test_channel_organizer_other_tournament_denied(self, org_store):
        orgs = SimpleNamespace(
            is_organizer=lambda uid: uid == 77,
            tournament_id="other",
        )
        org_store._orgs[(1, 2)] = orgs
        assert not can_manage_tournament_results(
            _user(77), _guild(), _tournament(tid="t1")
        )

    def test_plain_captain_denied(self):
        # Капитан без организаторских прав — False.
        assert not can_manage_tournament_results(
            _user(5), _guild(), _tournament(organizer_id=42)
        )

    def test_none_user_denied(self):
        assert not can_manage_tournament_results(
            None, _guild(), _tournament()
        )


# ---------- гейты RoomsConfirmView ----------

class _Resp:
    def __init__(self, ix):
        self._ix = ix

    async def send_message(self, content=None, **kw):
        self._ix.messages.append(str(content))

    async def defer(self, **kw):
        pass

    def is_done(self):
        return False


class _Ix:
    def __init__(self, uid, admin=False):
        self.user = SimpleNamespace(
            id=uid,
            display_name=f"u{uid}",
            name=f"u{uid}",
            guild_permissions=SimpleNamespace(administrator=admin),
            roles=[],
        )
        self.guild = SimpleNamespace(id=1, roles=[], get_role=lambda r: None)
        self.channel = SimpleNamespace(id=2)
        self.response = _Resp(self)
        self.messages = []


class TestRoomsConfirmGates:
    @pytest.fixture(autouse=True)
    def _patch_store(self, monkeypatch):
        import views.ocr_match_view as omv

        self.tournament = SimpleNamespace(
            id="t1", guild_id=1, channel_id=2, organizer_id=42,
            teams=[
                {"players": ["A1"], "captain": "A1"},
                {"players": ["B1"], "captain": "B1"},
            ],
            team_names={}, player_user_ids={"A1": 1, "B1": 2},
            qualifier_matches=[(0, 1)], semifinal_matches=[],
            final_teams=[0, 1], ocr_sessions={},
            processed_elo_matches=[], phase=None,
        )
        self.tournament.ocr_sessions["qualifier_0"] = {
            "mode": "rooms", "status": "awaiting_mapping",
            "rooms": [], "gen": 1,
        }
        monkeypatch.setattr(omv.store, "get", lambda g, c: self.tournament)

    @pytest.mark.asyncio
    async def test_confirm_denied_for_captain(self):
        import views.ocr_match_view as omv

        # Капитан команды 0 — display_name должен совпадать с
        # clean_nickname капитана "A1".
        ix = _Ix(1)
        ix.user.display_name = "A1"
        view = omv.RoomsConfirmView(1, 2, self.tournament, "qualifier", 0)
        await view._on_confirm(ix)
        assert any("организатор" in m for m in ix.messages)

    @pytest.mark.asyncio
    async def test_confirm_allowed_for_creator(self, monkeypatch):
        import views.ocr_match_view as omv

        ix = _Ix(42)  # organizer_id
        view = omv.RoomsConfirmView(1, 2, self.tournament, "qualifier", 0)
        # Дальше сработают проверки готовности сессии — нас
        # интересует только прохождение гейта прав: сообщение
        # НЕ должно быть отказом по правам.
        await view._on_confirm(ix)
        assert not any("организатор" in m and "может" in m
                       for m in ix.messages)

    @pytest.mark.asyncio
    async def test_edit_denied_for_stranger(self):
        import views.ocr_match_view as omv

        ix = _Ix(999)
        view = omv.RoomsConfirmView(1, 2, self.tournament, "qualifier", 0)
        await view._on_edit(ix)
        assert any("Нет прав" in m for m in ix.messages)

    @pytest.mark.asyncio
    async def test_edit_allowed_for_admin(self):
        import views.ocr_match_view as omv

        ix = _Ix(9, admin=True)
        view = omv.RoomsConfirmView(1, 2, self.tournament, "qualifier", 0)
        # edit шлёт ephemeral view — проверяем, что отказа нет
        await view._on_edit(ix)
        assert not any("Нет прав" in m for m in ix.messages)
