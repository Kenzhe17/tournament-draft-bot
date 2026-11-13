"""Голосование за формат матча (🇧🇷 Brazil / 🇰🇿 Kazakhstan).

Покрытие: prepare только для 8v8, права участников, один голос,
досрочное завершение при 8/8, ничья → случайный выбор, дедлайн,
format label в карточке, persistent custom_id.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.format_vote as fv


def _tournament(**kw):
    names = [f"P{i}" for i in range(1, 9)]
    t = SimpleNamespace(
        id="t1",
        guild_id=1,
        channel_id=2,
        mm_search_channel_id=555,
        teams=[
            {"players": names[0:4], "captain": "P1"},
            {"players": names[4:8], "captain": "P5"},
        ],
        player_user_ids={n: i + 1 for i, n in enumerate(names)},
        match_channels={
            "2": {
                "match_type": "final",
                "match_index": 0,
                "card_message_id": 100,
                "card_state": "active",
            }
        },
    )
    for k, v in kw.items():
        setattr(t, k, v)
    return t


@pytest.fixture
def env(monkeypatch):
    t = _tournament()
    monkeypatch.setattr(fv.store, "get", lambda g, c: t)
    monkeypatch.setattr(fv.store, "set", lambda x: None)
    fv._LOCKS.clear()
    return SimpleNamespace(tournament=t)


class _Resp:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kw):
        self.messages.append(str(content))

    async def defer(self, **kw):
        pass


class _Followup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kw):
        self.messages.append(str(content))


class _Ix:
    def __init__(self, env, user_id, custom_id=None):
        self.guild = SimpleNamespace(id=1)
        self.user = SimpleNamespace(id=user_id)
        self.data = {"custom_id": custom_id or ""}
        self.response = _Resp()
        self.followup = _Followup()
        self.channel = SimpleNamespace(id=2)


def _cid(fmt="brazil", gid=1, cid=2, mt="final", mi=0):
    return f"fv:{gid}:{cid}:{mt}:{mi}:{fmt}"


def _state(env):
    return fv.get_vote_state(env.tournament, "final", 0)


class TestPrepare:
    def test_creates_state_for_8v8(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        assert s is not None
        assert s["vote_status"] == "open"
        assert s["vote_deadline"] > time.time()
        assert s["votes"] == {}
        assert s["selected_format"] is None

    def test_idempotent(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        assert len(env.tournament.match_channels) == 1

    def test_skips_non_8v8(self, env):
        env.tournament.teams[0]["players"] = env.tournament.teams[0]["players"][:2]
        env.tournament.teams[1]["players"] = env.tournament.teams[1]["players"][:2]
        assert fv.prepare_format_vote(
            env.tournament, "final", 0, 2
        ) is None
        assert _state(env) is None

    def test_format_label_open(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        assert fv.match_format_label(env.tournament, "final", 0) == "Голосование"


class TestVoteClick:
    @pytest.mark.asyncio
    async def test_participant_vote_recorded(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        _state(env)["vote_message_id"] = 5
        edits = []

        class _Msg:
            async def edit(self, view=None, **kw):
                edits.append(view)

        class _Ch:
            id = 2

            async def fetch_message(self, mid):
                return _Msg()

        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        ix = _Ix(env, 1, _cid("kazakhstan"))
        ix.guild.get_channel = lambda cid: _Ch()
        await fv.handle_vote_click(ix, "kazakhstan")
        s = _state(env)
        assert s["votes"] == {"1": "kazakhstan"}
        assert ix.response.messages == ["✅ Голос учтён."]
        assert edits  # сообщение обновлено

    @pytest.mark.asyncio
    async def test_non_participant_denied(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        ix = _Ix(env, 999, _cid())
        await fv.handle_vote_click(ix, "brazil")
        assert "не являетесь участником" in ix.response.messages[0]
        assert _state(env)["votes"] == {}

    @pytest.mark.asyncio
    async def test_double_vote_denied(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        s = _state(env)
        s["votes"]["1"] = "brazil"
        s["vote_message_id"] = 0  # edit пропускается
        ix = _Ix(env, 1, _cid("kazakhstan"))
        await fv.handle_vote_click(ix, "kazakhstan")
        assert "уже проголосовали" in ix.response.messages[0]
        assert s["votes"]["1"] == "brazil"  # голос не изменён

    @pytest.mark.asyncio
    async def test_expired_vote_finalizes(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_deadline"] = time.time() - 1
        finalized = []

        async def fake_fin(guild, t, mt, mi):
            finalized.append((mt, mi))
            s["vote_status"] = "done"
            s["selected_format"] = "brazil"
            return True

        monkeypatch.setattr(fv, "finalize_format_vote", fake_fin)
        ix = _Ix(env, 1, _cid())
        await fv.handle_vote_click(ix, "brazil")
        assert finalized == [("final", 0)]
        assert "завершено" in ix.response.messages[0]


class TestFinalize:
    @pytest.mark.asyncio
    async def test_winner_and_card_update(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["votes"] = {str(i): "brazil" for i in range(1, 6)}
        s["votes"].update({str(i): "kazakhstan" for i in range(6, 9)})
        s["vote_message_id"] = 0  # без Discord edit
        card_updates = []

        async def fake_card(guild, t, mt, mi, state="active", **kw):
            card_updates.append(state)

        monkeypatch.setattr(
            "services.match_announcer.update_match_card", fake_card
        )
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        ok = await fv.finalize_format_vote(None, env.tournament, "final", 0)
        assert ok is True
        assert s["selected_format"] == "brazil"
        assert s["vote_status"] == "done"
        assert s.get("tie") is False
        assert fv.match_format_label(env.tournament, "final", 0) == "🇧🇷 Brazil"
        assert card_updates == ["active"]

    @pytest.mark.asyncio
    async def test_tie_random_with_reason(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["votes"] = {str(i): "brazil" for i in range(1, 5)}
        s["votes"].update({str(i): "kazakhstan" for i in range(5, 9)})
        s["vote_message_id"] = 0
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        await fv.finalize_format_vote(None, env.tournament, "final", 0)
        assert s["selected_format"] in ("brazil", "kazakhstan")
        assert s["tie"] is True

    @pytest.mark.asyncio
    async def test_zero_votes_random(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_message_id"] = 0
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        await fv.finalize_format_vote(None, env.tournament, "final", 0)
        assert s["selected_format"] in ("brazil", "kazakhstan")
        assert s.get("tie") is False  # 0 голосов — без «равенства»

    @pytest.mark.asyncio
    async def test_idempotent_second_finalize(self, env, monkeypatch):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_message_id"] = 0
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        assert await fv.finalize_format_vote(
            None, env.tournament, "final", 0
        ) is True
        assert await fv.finalize_format_vote(
            None, env.tournament, "final", 0
        ) is False


class TestViewAndRestore:
    def test_open_view_has_two_enabled_buttons(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        view = fv.build_vote_view(env.tournament, "final", 0, s)
        btns = [
            c for c in view.walk_children()
            if isinstance(c, __import__("discord").ui.Button)
        ]
        assert len(btns) == 2
        assert all(not b.disabled for b in btns)
        assert all(b.custom_id.startswith("fv:1:2:final:0:") for b in btns)

    def test_done_view_buttons_disabled(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_status"] = "done"
        s["selected_format"] = "kazakhstan"
        view = fv.build_vote_view(env.tournament, "final", 0, s)
        btns = [
            c for c in view.walk_children()
            if isinstance(c, __import__("discord").ui.Button)
        ]
        assert all(b.disabled for b in btns)

    def test_cancel_marks_votes(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        fv.cancel_open_format_votes(env.tournament)
        assert _state(env)["vote_status"] == "cancelled"
        assert fv.match_format_label(env.tournament, "final", 0) == ""


async def _noop(*a, **k):
    return None
