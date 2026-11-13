"""Голосование за правила (BRAZIL/KAZAKHSTAN) в /tournament create.

Покрытие: фаза RULES_VOTING, только участники, один голос,
нет досрочного завершения, таймер 20с, ничья/пусто → KAZAKHSTAN,
больше голосов → победитель, продолжение формирования команд,
restart-restore.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.rules_vote as rv_mod
from models.tournament import TournamentPhase


def _tournament(n=16, **kw):
    names = [f"P{i}" for i in range(1, n + 1)]
    t = SimpleNamespace(
        id="t1",
        guild_id=1,
        channel_id=2,
        phase=TournamentPhase.SETUP,
        ruleset="",
        rules_vote={},
        players_pool=names,
        player_user_ids={name: i + 1 for i, name in enumerate(names)},
        formation_mode=None,
        size=None,
        teams=[],
    )
    for k, v in kw.items():
        setattr(t, k, v)
    return t


@pytest.fixture
def env(monkeypatch):
    t = _tournament()
    monkeypatch.setattr(rv_mod.store, "get", lambda g, c: t)
    monkeypatch.setattr(rv_mod.store, "set", lambda x: None)
    rv_mod._LOCKS.clear()
    rv_mod._TIMERS.clear()
    for t in list(rv_mod._UI_TASKS.values()):
        t.cancel()
    rv_mod._UI_TASKS.clear()
    return SimpleNamespace(tournament=t)


class _Resp:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kw):
        self.messages.append(str(content))


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


def _cid(fmt="brazil", gid=1, cid=2):
    return f"rv:{gid}:{cid}:{fmt}"


def _rv(env):
    return env.tournament.rules_vote


class _Channel:
    id = 2

    def __init__(self):
        self.sent = []

    async def send(self, view=None, **kw):
        self.sent.append(view)
        return SimpleNamespace(id=777)


class TestStart:
    @pytest.mark.asyncio
    async def test_start_sets_phase_and_state(self, env):
        ch = _Channel()
        ok = await rv_mod.start_rules_vote(
            _guild(), ch, env.tournament, bot=None,
        )
        assert ok is True
        assert env.tournament.phase == TournamentPhase.RULES_VOTING
        rv = _rv(env)
        assert rv["vote_status"] == "open"
        assert rv["vote_deadline"] > time.time()
        assert rv["vote_message_id"] == 777
        assert ch.sent  # CV2-карточка отправлена

    @pytest.mark.asyncio
    async def test_idempotent_no_second_message(self, env):
        ch = _Channel()
        await rv_mod.start_rules_vote(_guild(), ch, env.tournament)
        await rv_mod.start_rules_vote(_guild(), ch, env.tournament)
        assert len(ch.sent) == 1


class TestVoteClick:
    @pytest.mark.asyncio
    async def test_participant_vote(self, env, monkeypatch):
        env.tournament.rules_vote = {
            "vote_message_id": 5, "vote_channel_id": 2,
            "vote_deadline": time.time() + 20, "votes": {},
            "vote_status": "open",
        }
        monkeypatch.setattr(
            "services.rules_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        ix = _Ix(env, 3, _cid("kazakhstan"))
        await rv_mod.handle_rules_vote_click(ix, "kazakhstan")
        assert _rv(env)["votes"] == {"3": "kazakhstan"}
        assert ix.response.messages == ["✅ Голос учтён."]

    @pytest.mark.asyncio
    async def test_non_participant_denied(self, env):
        env.tournament.rules_vote = {
            "vote_message_id": 0, "vote_channel_id": 2,
            "vote_deadline": time.time() + 20, "votes": {},
            "vote_status": "open",
        }
        ix = _Ix(env, 999, _cid())
        await rv_mod.handle_rules_vote_click(ix, "brazil")
        assert "не являетесь участником" in ix.response.messages[0]
        assert _rv(env)["votes"] == {}

    @pytest.mark.asyncio
    async def test_double_vote_denied(self, env, monkeypatch):
        env.tournament.rules_vote = {
            "vote_message_id": 0, "vote_channel_id": 2,
            "vote_deadline": time.time() + 20,
            "votes": {"3": "brazil"}, "vote_status": "open",
        }
        monkeypatch.setattr(
            "services.rules_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        ix = _Ix(env, 3, _cid("kazakhstan"))
        await rv_mod.handle_rules_vote_click(ix, "kazakhstan")
        assert "уже проголосовали" in ix.response.messages[0]
        assert _rv(env)["votes"]["3"] == "brazil"

    @pytest.mark.asyncio
    async def test_no_early_finalize_when_all_voted(
        self, env, monkeypatch
    ):
        """Все 16 проголосовали — голосование НЕ завершается досрочно."""
        env.tournament.rules_vote = {
            "vote_message_id": 0, "vote_channel_id": 2,
            "vote_deadline": time.time() + 20, "votes": {},
            "vote_status": "open",
        }
        monkeypatch.setattr(
            "services.rules_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        finalized = []

        async def fake_fin(g, t):
            finalized.append(t)
            return True

        monkeypatch.setattr(rv_mod, "finalize_rules_vote", fake_fin)
        for uid in range(1, 17):
            ix = _Ix(env, uid, _cid("brazil"))
            await rv_mod.handle_rules_vote_click(ix, "brazil")
        assert len(_rv(env)["votes"]) == 16
        assert finalized == []  # таймер ещё не истёк
        assert _rv(env)["vote_status"] == "open"

    @pytest.mark.asyncio
    async def test_expired_vote_finalizes(self, env, monkeypatch):
        env.tournament.rules_vote = {
            "vote_message_id": 0, "vote_channel_id": 2,
            "vote_deadline": time.time() - 1, "votes": {},
            "vote_status": "open",
        }
        finalized = []

        async def fake_fin(g, t):
            finalized.append(t)
            _rv(env)["vote_status"] = "done"
            return True

        monkeypatch.setattr(rv_mod, "finalize_rules_vote", fake_fin)
        ix = _Ix(env, 1, _cid())
        await rv_mod.handle_rules_vote_click(ix, "brazil")
        assert finalized == [env.tournament]
        assert "завершено" in ix.response.messages[0]


class TestFinalize:
    @pytest.mark.asyncio
    async def test_brazil_wins(self, env, monkeypatch):
        _setup_open(env, monkeypatch)
        _rv(env)["votes"] = {str(i): "brazil" for i in range(1, 9)}
        _rv(env)["votes"].update(
            {str(i): "kazakhstan" for i in range(9, 17)}
        )
        # 8 vs 8 → ничья → KAZAKHSTAN; сделаем 9:7
        _rv(env)["votes"]["9"] = "brazil"
        launched = []

        async def fake_launch(g, ch, t, b):
            launched.append(t)

        monkeypatch.setattr(
            "views.setup_view.launch_tournament_formation", fake_launch
        )
        ok = await rv_mod.finalize_rules_vote(_guild(), env.tournament)
        assert ok is True
        # brazil=9 (1..8 + "9"), kazakhstan=7 (10..16) → BRAZIL
        assert env.tournament.ruleset == "BRAZIL"
        assert launched == [env.tournament]

    @pytest.mark.asyncio
    async def test_kazakhstan_wins(self, env, monkeypatch):
        _setup_open(env, monkeypatch)
        _rv(env)["votes"] = {str(i): "kazakhstan" for i in range(1, 17)}
        launched = []

        async def fake_launch(g, ch, t, b):
            launched.append(t)

        monkeypatch.setattr(
            "views.setup_view.launch_tournament_formation", fake_launch
        )
        await rv_mod.finalize_rules_vote(_guild(), env.tournament)
        assert env.tournament.ruleset == "KAZAKHSTAN"
        assert _rv(env)["vote_status"] == "done"
        assert _rv(env)["selected_ruleset"] == "KAZAKHSTAN"
        assert launched

    @pytest.mark.asyncio
    async def test_tie_goes_kazakhstan(self, env, monkeypatch):
        _setup_open(env, monkeypatch)
        _rv(env)["votes"] = {str(i): "brazil" for i in range(1, 9)}
        _rv(env)["votes"].update(
            {str(i): "kazakhstan" for i in range(9, 17)}
        )
        monkeypatch.setattr(
            "views.setup_view.launch_tournament_formation", _noop_async()
        )
        await rv_mod.finalize_rules_vote(_guild(), env.tournament)
        assert env.tournament.ruleset == "KAZAKHSTAN"
        assert _rv(env)["tie"] is True

    @pytest.mark.asyncio
    async def test_zero_votes_kazakhstan(self, env, monkeypatch):
        _setup_open(env, monkeypatch)
        monkeypatch.setattr(
            "views.setup_view.launch_tournament_formation", _noop_async()
        )
        await rv_mod.finalize_rules_vote(_guild(), env.tournament)
        assert env.tournament.ruleset == "KAZAKHSTAN"
        assert _rv(env)["tie"] is False

    @pytest.mark.asyncio
    async def test_idempotent_finalize(self, env, monkeypatch):
        _setup_open(env, monkeypatch)
        monkeypatch.setattr(
            "views.setup_view.launch_tournament_formation", _noop_async()
        )
        assert await rv_mod.finalize_rules_vote(
            SimpleNamespace(id=1), env.tournament
        ) is True
        assert await rv_mod.finalize_rules_vote(
            SimpleNamespace(id=1), env.tournament
        ) is False

    @pytest.mark.asyncio
    async def test_brazil_strict_majority(self, env, monkeypatch):
        _setup_open(env, monkeypatch)
        _rv(env)["votes"] = {str(i): "brazil" for i in range(1, 13)}
        _rv(env)["votes"].update(
            {str(i): "kazakhstan" for i in range(13, 17)}
        )
        monkeypatch.setattr(
            "views.setup_view.launch_tournament_formation", _noop_async()
        )
        await rv_mod.finalize_rules_vote(_guild(), env.tournament)
        assert env.tournament.ruleset == "BRAZIL"


def _guild():
    class _G:
        id = 1

        def get_channel(self, cid):
            return _Channel()

        async def fetch_channel(self, cid):
            return _Channel()

    return _G()


def _setup_open(env, monkeypatch):
    env.tournament.phase = TournamentPhase.RULES_VOTING
    env.tournament.rules_vote = {
        "vote_message_id": 0, "vote_channel_id": 2,
        "vote_deadline": time.time() + 20, "votes": {},
        "vote_status": "open",
    }
    monkeypatch.setattr(
        "services.rules_vote._mirror_to_pg",
        lambda *a, **k: _noop(),
    )


async def _noop(*a, **k):
    return None


def _noop_async():
    async def _f(*a, **k):
        return None
    return _f


class TestRestore:
    @pytest.mark.asyncio
    async def test_expired_vote_finalized_on_restore(
        self, env, monkeypatch
    ):
        env.tournament.rules_vote = {
            "vote_message_id": 0, "vote_channel_id": 2,
            "vote_deadline": time.time() - 5, "votes": {},
            "vote_status": "open",
        }
        monkeypatch.setattr(
            rv_mod.store, "all", lambda: [env.tournament]
        )
        monkeypatch.setattr(
            "services.rules_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        monkeypatch.setattr(
            "views.setup_view.launch_tournament_formation", _noop_async()
        )

        class _Bot:
            def add_view(self, v):
                pass

            def get_guild(self, gid):
                return _guild()

        await rv_mod.restore_rules_votes(_Bot())
        assert _rv(env)["vote_status"] == "done"
        assert env.tournament.ruleset == "KAZAKHSTAN"

    @pytest.mark.asyncio
    async def test_done_but_phase_stuck_continues(
        self, env, monkeypatch
    ):
        env.tournament.phase = TournamentPhase.RULES_VOTING
        env.tournament.ruleset = "BRAZIL"
        env.tournament.rules_vote = {
            "vote_message_id": 0, "vote_channel_id": 2,
            "vote_deadline": time.time() - 5, "votes": {},
            "vote_status": "done", "selected_ruleset": "BRAZIL",
        }
        monkeypatch.setattr(
            rv_mod.store, "all", lambda: [env.tournament]
        )
        launched = []

        async def fake_launch(g, ch, t, b):
            launched.append(t)

        monkeypatch.setattr(
            "views.setup_view.launch_tournament_formation", fake_launch
        )

        class _Bot:
            def add_view(self, v):
                pass

            def get_guild(self, gid):
                return _guild()

        await rv_mod.restore_rules_votes(_Bot())
        assert launched == [env.tournament]

    @pytest.mark.asyncio
    async def test_orphan_phase_reset_to_setup(self, env, monkeypatch):
        """RULES_VOTING без rules_vote → возврат в SETUP."""
        env.tournament.phase = TournamentPhase.RULES_VOTING
        env.tournament.rules_vote = {}
        monkeypatch.setattr(
            rv_mod.store, "all", lambda: [env.tournament]
        )

        class _Bot:
            def add_view(self, v):
                pass

            def get_guild(self, gid):
                return None

        await rv_mod.restore_rules_votes(_Bot())
        assert env.tournament.phase == TournamentPhase.SETUP


class TestRulesetLabel:
    def test_labels(self):
        assert "Brazil" in rv_mod.ruleset_label("BRAZIL")
        assert "Kazakhstan" in rv_mod.ruleset_label("KAZAKHSTAN")
        assert rv_mod.ruleset_label("") == "—"


class TestDeadlockRegression:
    """Регрессия: finalize внутри удерживаемого Lock → deadlock."""

    @pytest.mark.asyncio
    async def test_expired_click_no_deadlock(self, env, monkeypatch):
        env.tournament.rules_vote = {
            "vote_message_id": 0, "vote_channel_id": 2,
            "vote_deadline": time.time() - 1, "votes": {},
            "vote_status": "open",
        }
        monkeypatch.setattr(
            "services.rules_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        launched = []

        async def fake_launch(g, ch, t, b):
            launched.append(t)

        monkeypatch.setattr(
            "views.setup_view.launch_tournament_formation", fake_launch
        )
        ix = _Ix(env, 1, _cid())
        await asyncio.wait_for(
            rv_mod.handle_rules_vote_click(ix, "brazil"), timeout=5
        )
        assert _rv(env)["vote_status"] == "done"
        assert "завершено" in ix.response.messages[0]

    @pytest.mark.asyncio
    async def test_concurrent_clicks_serialized(self, env, monkeypatch):
        """Параллельные голоса двух игроков — оба приняты,
        состояние консистентно, без deadlock."""
        env.tournament.rules_vote = {
            "vote_message_id": 0, "vote_channel_id": 2,
            "vote_deadline": time.time() + 20, "votes": {},
            "vote_status": "open",
        }
        monkeypatch.setattr(
            "services.rules_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        monkeypatch.setattr(rv_mod, "_UI_DEBOUNCE_S", 0.01)
        ixs = [_Ix(env, 1, _cid("brazil")), _Ix(env, 2, _cid("kazakhstan"))]
        await asyncio.wait_for(
            asyncio.gather(
                rv_mod.handle_rules_vote_click(ixs[0], "brazil"),
                rv_mod.handle_rules_vote_click(ixs[1], "kazakhstan"),
            ),
            timeout=5,
        )
        votes = _rv(env)["votes"]
        assert votes == {"1": "brazil", "2": "kazakhstan"}

    @pytest.mark.asyncio
    async def test_timer_survives_transient_error(
        self, env, monkeypatch
    ):
        env.tournament.rules_vote = {
            "vote_message_id": 0, "vote_channel_id": 2,
            "vote_deadline": time.time() + 0.2, "votes": {},
            "vote_status": "open",
        }
        calls = {"n": 0}
        real_get = rv_mod.store.get

        def flaky_get(g, c):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return real_get(g, c)

        monkeypatch.setattr(rv_mod.store, "get", flaky_get)
        monkeypatch.setattr(rv_mod, "_TIMER_RETRY_S", 0.05)
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        launched = []

        async def fake_launch(g, ch, t, b):
            launched.append(t)

        monkeypatch.setattr(
            "views.setup_view.launch_tournament_formation", fake_launch
        )
        rv_mod._schedule_timer(1, 2)
        await asyncio.sleep(1.2)
        assert _rv(env)["vote_status"] == "done"
        rv_mod._TIMERS.clear()


class TestFormationIdempotent:
    """_continue_tournament_start — строго однократный запуск
    формирования команд: параллельные finalize/watchdog/restore
    не должны дать двойное распределение игроков."""

    def _ready(self, env):
        env.tournament.phase = TournamentPhase.RULES_VOTING
        env.tournament.rules_vote = {"vote_status": "done"}

    @pytest.mark.asyncio
    async def test_concurrent_continue_launches_once(
        self, env, monkeypatch
    ):
        import views.setup_view as sv

        self._ready(env)
        calls = []
        gate = asyncio.Event()

        async def fake_launch(g, c, t, b):
            calls.append(1)
            await gate.wait()

        monkeypatch.setattr(sv, "launch_tournament_formation", fake_launch)
        guild = SimpleNamespace(id=1, get_channel=lambda cid: _Channel())
        t1 = asyncio.create_task(
            rv_mod._continue_tournament_start(guild, env.tournament)
        )
        t2 = asyncio.create_task(
            rv_mod._continue_tournament_start(guild, env.tournament)
        )
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.gather(t1, t2)
        assert calls == [1]
        assert _rv(env)["formation_started"] is True

    @pytest.mark.asyncio
    async def test_finalize_and_watchdog_no_double(
        self, env, monkeypatch
    ):
        """finalize_rules_vote + параллельный _continue — один launch."""
        import views.setup_view as sv

        self._ready(env)
        env.tournament.rules_vote = {
            "vote_status": "open",
            "vote_deadline": time.time() - 1,
            "votes": {},
        }
        calls = []
        gate = asyncio.Event()

        async def fake_launch(g, c, t, b):
            calls.append(1)
            await gate.wait()

        monkeypatch.setattr(sv, "launch_tournament_formation", fake_launch)
        guild = SimpleNamespace(id=1, get_channel=lambda cid: _Channel())
        t1 = asyncio.create_task(
            rv_mod.finalize_rules_vote(guild, env.tournament)
        )
        t2 = asyncio.create_task(
            rv_mod._continue_tournament_start(guild, env.tournament)
        )
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.gather(t1, t2)
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_failed_launch_resets_flag_for_retry(
        self, env, monkeypatch
    ):
        import views.setup_view as sv

        self._ready(env)
        calls = []

        async def boom(g, c, t, b):
            calls.append("boom")
            raise RuntimeError("discord down")

        monkeypatch.setattr(sv, "launch_tournament_formation", boom)
        guild = SimpleNamespace(id=1, get_channel=lambda cid: _Channel())
        await rv_mod._continue_tournament_start(guild, env.tournament)
        # флаг сброшен — повтор возможен (watchdog/restore)
        assert _rv(env)["formation_started"] is False

        async def ok_launch(g, c, t, b):
            calls.append("ok")

        monkeypatch.setattr(sv, "launch_tournament_formation", ok_launch)
        await rv_mod._continue_tournament_start(guild, env.tournament)
        assert calls == ["boom", "ok"]
        assert _rv(env)["formation_started"] is True

    @pytest.mark.asyncio
    async def test_phase_changed_no_launch(self, env, monkeypatch):
        import views.setup_view as sv

        self._ready(env)
        env.tournament.phase = TournamentPhase.DRAFT
        calls = []

        async def fake_launch(*a):
            calls.append(1)

        monkeypatch.setattr(sv, "launch_tournament_formation", fake_launch)
        await rv_mod._continue_tournament_start(None, env.tournament)
        assert calls == []


class TestSyncVoteUi:
    """ui_synced=True только после реального msg.edit."""

    def _rv(self):
        return {
            "vote_status": "done",
            "vote_channel_id": 2,
            "vote_message_id": 555,
            "ui_sync_attempts": 0,
        }

    @pytest.mark.asyncio
    async def test_missing_refs_not_synced(self, env):
        rv = self._rv()
        rv["vote_channel_id"] = 0
        rv["vote_message_id"] = 0
        guild = SimpleNamespace(id=1, get_channel=lambda cid: None)
        ok = await rv_mod._sync_vote_ui(guild, env.tournament, rv)
        assert ok is False
        assert rv["ui_synced"] is False
        assert "ref missing" in rv.get("ui_sync_last_error", "")

    @pytest.mark.asyncio
    async def test_guild_unavailable_not_synced(self, env, monkeypatch):
        monkeypatch.setattr(rv_mod, "_bot_guild", lambda gid: None)
        rv = self._rv()
        ok = await rv_mod._sync_vote_ui(None, env.tournament, rv)
        assert ok is False
        assert rv["ui_synced"] is False
        assert "guild" in rv.get("ui_sync_last_error", "")

    @pytest.mark.asyncio
    async def test_success_marks_synced(self, env, monkeypatch):
        edited = []

        class _Msg:
            async def edit(self, **kw):
                edited.append(1)

        class _Vch:
            async def fetch_message(self, mid):
                return _Msg()

        guild = SimpleNamespace(id=1, get_channel=lambda cid: _Vch())
        monkeypatch.setattr(
            rv_mod, "build_rules_vote_view", lambda t, r: object()
        )
        rv = self._rv()
        ok = await rv_mod._sync_vote_ui(guild, env.tournament, rv)
        assert ok is True
        assert rv["ui_synced"] is True
        assert edited == [1]

    @pytest.mark.asyncio
    async def test_edit_failure_not_synced(self, env, monkeypatch):
        class _Msg:
            async def edit(self, **kw):
                raise RuntimeError("discord 500")

        class _Vch:
            async def fetch_message(self, mid):
                return _Msg()

        guild = SimpleNamespace(id=1, get_channel=lambda cid: _Vch())
        monkeypatch.setattr(
            rv_mod, "build_rules_vote_view", lambda t, r: object()
        )
        rv = self._rv()
        ok = await rv_mod._sync_vote_ui(guild, env.tournament, rv)
        assert ok is False
        assert rv["ui_synced"] is False
        assert rv["ui_sync_attempts"] == 1

    @pytest.mark.asyncio
    async def test_deleted_message_gives_up(self, env, monkeypatch):
        import discord

        class _Vch:
            async def fetch_message(self, mid):
                raise discord.NotFound(
                    SimpleNamespace(status=404, reason="NF"), "gone"
                )

        guild = SimpleNamespace(id=1, get_channel=lambda cid: _Vch())
        monkeypatch.setattr(
            rv_mod, "build_rules_vote_view", lambda t, r: object()
        )
        rv = self._rv()
        ok = await rv_mod._sync_vote_ui(guild, env.tournament, rv)
        assert ok is False
        assert rv["ui_synced"] is False
        # attempts в потолок — watchdog не повторяет вечно
        assert rv["ui_sync_attempts"] >= rv_mod._UI_SYNC_MAX_ATTEMPTS
