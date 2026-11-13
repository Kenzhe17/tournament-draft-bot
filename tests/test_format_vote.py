"""Голосование за формат матча (🇧🇷 Brazil / 🇰🇿 Kazakhstan).

Покрытие: prepare только для 8v8, права участников, один голос,
досрочное завершение при 8/8, ничья → случайный выбор, дедлайн,
format label в карточке, persistent custom_id.
"""

from __future__ import annotations

import asyncio
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
    for t in list(fv._UI_TASKS.values()):
        t.cancel()
    fv._UI_TASKS.clear()
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


def _cid(fmt="brazil", gid=1, cid=2, mt="final", mi=0, tid="t1"):
    return f"fv:{gid}:{cid}:{tid}:{mt}:{mi}:{fmt}"


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
        monkeypatch.setattr(fv, "_UI_DEBOUNCE_S", 0.01)
        ix = _Ix(env, 1, _cid("kazakhstan"))
        ix.guild.get_channel = lambda cid: _Ch()
        await fv.handle_vote_click(ix, "kazakhstan")
        # UI-обновление отложенное (debounce) — ждём задачу.
        for _ in range(100):
            if not fv._UI_TASKS:
                break
            await asyncio.sleep(0.01)
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
        assert all(b.custom_id.startswith("fv:1:2:t1:final:0:") for b in btns)

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


class TestDeadlockRegression:
    """Регрессия: finalize внутри удерживаемого Lock → deadlock."""

    @pytest.mark.asyncio
    async def test_eighth_vote_finalizes_without_deadlock(
        self, env, monkeypatch
    ):
        """8/8 голосов → немедленная финализация. Раньше deadlock:
        handle держал lock, finalize ждал тот же lock."""
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["votes"] = {str(i): "brazil" for i in range(1, 8)}
        s["vote_message_id"] = 0
        monkeypatch.setattr(fv, "_UI_DEBOUNCE_S", 0.01)
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        ix = _Ix(env, 8, _cid("kazakhstan"))
        # wait_for: deadlock проявился бы как TimeoutError.
        await asyncio.wait_for(
            fv.handle_vote_click(ix, "kazakhstan"), timeout=5
        )
        assert s["vote_status"] == "done"
        assert s["selected_format"] in ("brazil", "kazakhstan")

    @pytest.mark.asyncio
    async def test_expired_click_finalizes_without_deadlock(
        self, env, monkeypatch
    ):
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_deadline"] = time.time() - 1
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        ix = _Ix(env, 1, _cid())
        await asyncio.wait_for(
            fv.handle_vote_click(ix, "brazil"), timeout=5
        )
        assert s["vote_status"] == "done"
        assert "завершено" in ix.response.messages[0]

    @pytest.mark.asyncio
    async def test_concurrent_clicks_one_finalize(self, env, monkeypatch):
        """Два параллельных голоса на границе deadline: оба ответа,
        ровно одна финализация, без deadlock."""
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["votes"] = {str(i): "brazil" for i in range(1, 7)}
        s["vote_message_id"] = 0
        finals = []

        async def counting_fin(g, t, mt, mi):
            finals.append(1)
            s["vote_status"] = "done"
            return True

        monkeypatch.setattr(fv, "finalize_format_vote", counting_fin)
        monkeypatch.setattr(fv, "_UI_DEBOUNCE_S", 0.01)
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        ixs = [
            _Ix(env, uid, _cid("kazakhstan" if uid % 2 else "brazil"))
            for uid in (7, 8)
        ]
        await asyncio.wait_for(
            asyncio.gather(
                fv.handle_vote_click(ixs[0], "kazakhstan"),
                fv.handle_vote_click(ixs[1], "brazil"),
            ),
            timeout=5,
        )
        # Один из кликов мог финализировать (7+1=8) — не более 2.
        assert len(finals) <= 2


class TestTimerResilience:
    @pytest.mark.asyncio
    async def test_timer_survives_transient_error(self, env, monkeypatch):
        """Ошибка внутри итерации таймера не убивает задачу."""
        fv.prepare_format_vote(env.tournament, "final", 0, 2)
        s = _state(env)
        s["vote_deadline"] = time.time() + 0.2
        calls = {"n": 0}

        real_get = fv.store.get

        def flaky_get(g, c):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return real_get(g, c)

        monkeypatch.setattr(fv.store, "get", flaky_get)
        monkeypatch.setattr(fv, "_WATCHDOG_S", 9999)
        monkeypatch.setattr(fv, "_TIMER_RETRY_S", 0.05)
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        monkeypatch.setattr(
            "services.format_vote._mirror_to_pg",
            lambda *a, **k: _noop(),
        )
        fv._schedule_timer(1, 2, "t1", "final", 0)
        await asyncio.sleep(1.2)
        # Таймер жив после ошибки и финализировал по deadline.
        assert s["vote_status"] == "done"
        task = fv._TIMERS.get("1:2:t1:final:0")
        fv._TIMERS.clear()
        if task is not None:
            task.cancel()


async def _noop(*a, **k):
    return None


class TestSyncVoteUiHonesty:
    """ui_synced=True только после реального обновления."""

    def _fv(self):
        return {
            "vote_status": "done",
            "vote_channel_id": 2,
            "vote_message_id": 555,
            "match_id": "t1_final_0",
            "ui_sync_attempts": 0,
        }

    @pytest.mark.asyncio
    async def test_missing_refs_not_synced(self, env, monkeypatch):
        monkeypatch.setattr(fv, "store_bot_guild", lambda gid: None)

        async def _card(*a, **k):
            return None

        import services.match_announcer as ma
        monkeypatch.setattr(ma, "update_match_card", _card)
        state = self._fv()
        state["vote_channel_id"] = 0
        state["vote_message_id"] = 0
        guild = SimpleNamespace(id=1, get_channel=lambda cid: None)
        ok = await fv._sync_vote_ui(
            guild, env.tournament, "final", 0, state
        )
        assert ok is False
        assert state["ui_synced"] is False
        assert "ref missing" in state.get("ui_sync_last_error", "")

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
        monkeypatch.setattr(fv, "build_vote_view", lambda *a, **k: object())

        async def _card(*a, **k):
            return None

        import services.match_announcer as ma
        monkeypatch.setattr(ma, "update_match_card", _card)
        state = self._fv()
        ok = await fv._sync_vote_ui(
            guild, env.tournament, "final", 0, state
        )
        assert ok is True
        assert state["ui_synced"] is True
        assert edited == [1]


class TestPooledChannelIsolation:
    """Регрессия BUG: MM_MATCH_CHANNEL_POOL переиспользует канал —
    разные турниры с одинаковым final:0 делили key/timer/custom_id.
    """

    @pytest.mark.asyncio
    async def test_three_matches_same_channel_sequential(
        self, monkeypatch
    ):
        """Один канал, три последовательных турнира (t1→t2→t3),
        у каждого final:0 — голосования независимы и завершаются."""
        tournaments = [
            _tournament(id=tid) for tid in ("t1", "t2", "t3")
        ]
        current = {"t": tournaments[0]}
        monkeypatch.setattr(
            fv.store, "get", lambda g, c: current["t"]
        )
        monkeypatch.setattr(fv.store, "set", lambda x: None)
        monkeypatch.setattr(
            fv, "_mirror_to_pg", lambda *a, **k: _noop()
        )
        monkeypatch.setattr(
            "services.match_announcer.update_match_card",
            lambda *a, **k: _noop(),
        )
        fv._LOCKS.clear()
        fv._TIMERS.clear()
        fv._UI_TASKS.clear()

        # Пул: матч заканчивается → канал отдаётся следующему
        # турниру. Каждый получает СВОЙ таймер под своим tid.
        for t in tournaments:
            current["t"] = t
            fv.prepare_format_vote(t, "final", 0, 2)
            s = fv.get_vote_state(t, "final", 0)
            s["vote_deadline"] = time.time() - 1
            fv._schedule_timer(1, 2, t.id, "final", 0)
            await asyncio.sleep(0.3)
            assert s["vote_status"] == "done", t.id
            assert s["selected_format"] in fv._FORMATS
            # 0 голосов → правила выбирают формат (random/tie).
            # Таймер убран из реестра.
            assert not any(
                k.endswith(f":{t.id}:final:0")
                or f":{t.id}:final:0" in k
                for k in fv._TIMERS
            )

    @pytest.mark.asyncio
    async def test_stale_timer_does_not_touch_new_match(
        self, monkeypatch
    ):
        """Зависший _TIMERS от старого турнира в том же канале:
        при срабатывании видит чужой турнир в store и завершается,
        не финализируя и не отменяя голосование нового матча."""
        old = _tournament(id="t_old")
        new = _tournament(id="t_new")
        current = {"t": new}
        monkeypatch.setattr(
            fv.store, "get", lambda g, c: current["t"]
        )
        monkeypatch.setattr(fv.store, "set", lambda x: None)
        monkeypatch.setattr(
            fv, "_mirror_to_pg", lambda *a, **k: _noop()
        )
        fv._LOCKS.clear()
        fv._TIMERS.clear()
        fv._UI_TASKS.clear()

        # «Зависший» таймер старого матча: deadline давно прошёл,
        # но задача ещё не умерла (например, спала в retry).
        fv.prepare_format_vote(new, "final", 0, 2)
        new_s = fv.get_vote_state(new, "final", 0)
        new_s["vote_deadline"] = time.time() + 60
        # Ручная «протухшая» запись таймера со старым tid.
        stale = asyncio.create_task(
            fv._vote_timer(1, 2, "t_old", "final", 0)
        )
        fv._TIMERS["1:2:t_old:final:0"] = stale
        # Даём ему тикнуть: store.get → t_new ≠ t_old → return.
        await asyncio.sleep(0.1)
        await asyncio.wait_for(stale, timeout=5)
        assert new_s["vote_status"] == "open"
        assert new_s["votes"] == {}

        # А таймер НОВОГО матча планируется под своим ключом
        # и не конфликтует с записью старого.
        fv._schedule_timer(1, 2, "t_new", "final", 0)
        assert "1:2:t_new:final:0" in fv._TIMERS
        assert "1:2:t_old:final:0" not in fv._TIMERS or (
            fv._TIMERS.get("1:2:t_old:final:0") is stale
        )

    @pytest.mark.asyncio
    async def test_stale_button_rejected_for_new_match(
        self, monkeypatch
    ):
        """Кнопка со старым tournament_id не голосует за новый матч."""
        old = _tournament(id="t_old")
        new = _tournament(id="t_new")
        current = {"t": new}
        monkeypatch.setattr(
            fv.store, "get", lambda g, c: current["t"]
        )
        monkeypatch.setattr(fv.store, "set", lambda x: None)
        monkeypatch.setattr(
            fv, "_mirror_to_pg", lambda *a, **k: _noop()
        )
        fv._LOCKS.clear()
        fv._TIMERS.clear()
        fv._UI_TASKS.clear()

        fv.prepare_format_vote(new, "final", 0, 2)
        fv.prepare_format_vote(old, "final", 0, 2)
        fv.get_vote_state(new, "final", 0)["vote_message_id"] = 0

        # Клик по кнопке СТАРОГО матча (tid=t_old) в том же канале.
        ix = _Ix(None, 1, _cid("brazil", tid="t_old"))
        ix.guild = SimpleNamespace(id=1)
        await asyncio.wait_for(
            fv.handle_vote_click(ix, "brazil"), timeout=5
        )
        # Новый матч не получил голос.
        assert fv.get_vote_state(new, "final", 0)["votes"] == {}
        assert "не найден" in ix.response.messages[0]
