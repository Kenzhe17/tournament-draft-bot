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
    fv._TIMERS.clear()
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


def _cid(env, fmt="brazil", gid=1, cid=2):
    s = fv.get_vote_state(env.tournament, "final", 0) or {}
    return f"fv:{gid}:{cid}:{s.get('vote_id', 'vX')}:{fmt}"


def _cid_raw(gid, cid, vote_id, fmt="brazil"):
    return f"fv:{gid}:{cid}:{vote_id}:{fmt}"


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
        ix = _Ix(env, 1, _cid(env, "kazakhstan"))
        ix.guild.get_channel = lambda cid: _Ch()
        await fv.handle_vote_click(ix, "kazakhstan")
        s = _state(env)
        assert s["votes"] == {"1": "kazakhstan"}
        assert ix.response.messages == ["✅ Голос учтён."]
        assert edits  # сообщение обновлено

    @pytest.mark.asyncio
    async def test_non_participant_denied(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        ix = _Ix(env, 999, _cid(env))
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
        ix = _Ix(env, 1, _cid(env, "kazakhstan"))
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
        ix = _Ix(env, 1, _cid(env))
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
        assert all(b.custom_id.startswith("fv:1:2:") and ":final:" not in b.custom_id for b in btns)

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


# ---------------------------------------------------------------------------
# Поколения голосований (vote_id), конкурентность, таймеры, restore/cancel
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402


def _prep(env, monkeypatch, votes=None):
    fv.prepare_format_vote(env.tournament, "final", 0, 2)
    s = _state(env)
    s["vote_message_id"] = 0  # без Discord edit
    if votes:
        s["votes"] = dict(votes)
    monkeypatch.setattr(
        "services.format_vote._mirror_to_pg", lambda *a, **k: _noop()
    )
    monkeypatch.setattr(
        "services.match_announcer.update_match_card",
        lambda *a, **k: _noop(),
    )
    return s


class TestVoteGenerations:
    @pytest.mark.asyncio
    async def test_eight_concurrent_votes_finalize_no_deadlock(
        self, env, monkeypatch
    ):
        s = _prep(env, monkeypatch)
        fmts = ["brazil", "kazakhstan"]
        ixs = [
            _Ix(env, i, _cid(env, fmts[i % 2])) for i in range(1, 9)
        ]
        await asyncio.wait_for(
            asyncio.gather(*[
                fv.handle_vote_click(ix, fmts[i % 2])
                for i, ix in enumerate(ixs)
            ]),
            timeout=10,
        )
        assert s["vote_status"] == "done"
        assert s["selected_format"] in ("brazil", "kazakhstan")
        assert len(s["votes"]) == 8  # 9-й голос не принимается

    @pytest.mark.asyncio
    async def test_stale_custom_id_rejected_after_channel_reuse(
        self, env, monkeypatch
    ):
        """Старое голосование не влияет на новый матч в том же канале."""
        s1 = _prep(env, monkeypatch)
        old_vote_id = s1["vote_id"]
        old_match_id = s1["match_id"]

        # Новый матч в ТОМ ЖЕ канале (pool reuse): новый турнир,
        # store.get отдаёт его — старый fv недоступен.
        t2 = _tournament(id="t2")
        monkeypatch.setattr(fv.store, "get", lambda g, c: t2)
        env2 = SimpleNamespace(tournament=t2)
        fv.prepare_format_vote(t2, "final", 0, 2)
        s2 = fv.get_vote_state(t2, "final", 0)
        assert s2["vote_id"] != old_vote_id

        ix = _Ix(env2, 1, _cid_raw(1, 2, old_vote_id, "brazil"))
        await fv.handle_vote_click(ix, "brazil")
        assert s2["votes"] == {}
        assert "не найдено" in ix.response.messages[0].lower()

        # Актуальная кнопка нового поколения работает.
        ix2 = _Ix(env2, 2, _cid_raw(1, 2, s2["vote_id"], "kazakhstan"))
        await fv.handle_vote_click(ix2, "kazakhstan")
        assert s2["votes"] == {"2": "kazakhstan"}

    @pytest.mark.asyncio
    async def test_legacy_custom_id_rejected(self, env):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        ix = _Ix(env, 1, "fv:1:2:final:0:brazil")  # формат до vote_id
        await fv.handle_vote_click(ix, "brazil")
        assert _state(env)["votes"] == {}
        assert "устарело" in ix.response.messages[0]

    @pytest.mark.asyncio
    async def test_two_matches_different_channels_independent(
        self, env, monkeypatch
    ):
        t1 = env.tournament                       # channel_id=2
        t2 = _tournament(id="t2", channel_id=3)
        t2.match_channels = {
            "3": {
                "match_type": "final", "match_index": 0,
                "card_message_id": 200, "card_state": "active",
            }
        }
        t2.teams = [
            {"players": [f"Q{i}" for i in range(1, 5)]},
            {"players": [f"Q{i}" for i in range(5, 9)]},
        ]
        t2.player_user_ids = {f"Q{i}": 100 + i for i in range(1, 9)}
        monkeypatch.setattr(
            fv.store, "get",
            lambda g, c: t1 if c == 2 else (t2 if c == 3 else None),
        )
        fv.prepare_format_vote(t1, "final", 0, 2)
        fv.prepare_format_vote(t2, "final", 0, 3)
        s1 = fv.get_vote_state(t1, "final", 0)
        s2 = fv.get_vote_state(t2, "final", 0)
        assert s1["vote_id"] != s2["vote_id"]

        # Клик по голосованию канала 3 пишет только в t2.
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg", lambda *a, **k: _noop()
        )
        s1["vote_message_id"] = 0
        s2["vote_message_id"] = 0
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        ix = _Ix(env2 := SimpleNamespace(tournament=t2), 101,
                 _cid_raw(1, 3, s2["vote_id"], "brazil"))
        ix.guild = SimpleNamespace(id=1)
        await fv.handle_vote_click(ix, "brazil")
        assert s2["votes"] == {"101": "brazil"}
        assert s1["votes"] == {}

    @pytest.mark.asyncio
    async def test_five_votes_finalize_by_timer(self, env, monkeypatch):
        s = _prep(
            env, monkeypatch,
            votes={str(i): "brazil" for i in range(1, 6)},
        )
        s["vote_deadline"] = time.time() + 0.3
        fv._schedule_timer(1, 2, "final", 0, fv=s)
        key = f"vote:{s['vote_id']}"
        assert key in fv._TIMERS
        await asyncio.sleep(1.6)
        assert s["vote_status"] == "done"
        assert s["selected_format"] == "brazil"
        assert key not in fv._TIMERS  # таймер/lock почищены

    @pytest.mark.asyncio
    async def test_timers_of_two_votes_independent(
        self, env, monkeypatch
    ):
        s = _prep(env, monkeypatch)
        t2 = _tournament(id="t2", channel_id=3)
        t2.match_channels = {
            "3": {"match_type": "final", "match_index": 0}
        }
        monkeypatch.setattr(fv.store, "get", lambda g, c: env.tournament)
        fv.prepare_format_vote(t2, "final", 0, 3)
        s2 = fv.get_vote_state(t2, "final", 0)
        fv._schedule_timer(1, 2, "final", 0, fv=s)
        fv._schedule_timer(1, 3, "final", 0, fv=s2)
        assert f"vote:{s['vote_id']}" in fv._TIMERS
        assert f"vote:{s2['vote_id']}" in fv._TIMERS
        for task in list(fv._TIMERS.values()):
            task.cancel()
        fv._TIMERS.clear()

    @pytest.mark.asyncio
    async def test_restore_registers_per_vote_id(self, env, monkeypatch):
        s = _prep(env, monkeypatch)
        s["vote_deadline"] = time.time() + 60
        added = []

        class _Bot:
            def add_view(self, v):
                added.append(v)

            def get_guild(self, gid):
                return None

        monkeypatch.setattr(fv.store, "all", lambda: [env.tournament])
        await fv.restore_format_votes(_Bot())
        assert len(added) == 1
        cids = [b.custom_id for b in added[0].children]
        assert all(s["vote_id"] in c for c in cids)
        assert f"vote:{s['vote_id']}" in fv._TIMERS
        for task in list(fv._TIMERS.values()):
            task.cancel()
        fv._TIMERS.clear()

    @pytest.mark.asyncio
    async def test_cancel_cleans_timer_and_lock(self, env, monkeypatch):
        s = _prep(env, monkeypatch)
        key = f"vote:{s['vote_id']}"
        fv._TIMERS[key] = asyncio.create_task(asyncio.sleep(60))
        fv._LOCKS[key] = asyncio.Lock()
        fv.cancel_open_format_votes(env.tournament)
        assert s["vote_status"] == "cancelled"
        assert key not in fv._TIMERS
        assert key not in fv._LOCKS
