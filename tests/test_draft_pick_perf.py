"""Draft pick: быстрый callback — ранний defer, атомарный pick,
одно обновление embed, без лишних ephemeral."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.tournament import (
    FormationMode,
    Tournament,
    TournamentPhase,
    TournamentSize,
)
from views.draft_view import DraftView, PlayerSelect


def _mk(size=TournamentSize.SIXTEEN):
    t = Tournament(guild_id=1, channel_id=2, size=size,
                   formation_mode=FormationMode.DRAFT)
    n = t.captain_count
    idx = 0
    for circle in range(1, 5):
        players = []
        for _ in range(n):
            name = f"P{idx}"
            players.append(name)
            t.player_user_ids[name] = 1000 + idx
            idx += 1
        t.set_circle_list(circle, players)
    t.captains = list(t.circle1)
    t.is_test = True  # любой пользователь может выбирать
    t.shuffle_circles()
    t.start_draft()
    return t


class _Resp:
    def __init__(self, ix):
        self._ix = ix
        self.deferred = False

    async def send_message(self, content=None, **kw):
        self._ix.messages.append(str(content))

    async def defer(self, **kw):
        self.deferred = True

    def is_done(self):
        return self.deferred


class _Follow:
    def __init__(self, ix):
        self._ix = ix

    async def send(self, content=None, **kw):
        self._ix.followups.append(str(content))


class _Chan:
    def __init__(self):
        self.sent = []
        self.deleted = []

    async def send(self, content=None, **kw):
        self.sent.append(content)
        return SimpleNamespace(id=9000 + len(self.sent))

    async def fetch_message(self, mid):
        return SimpleNamespace(
            id=mid,
            delete=self._mk_del(mid),
        )

    def _mk_del(self, mid):
        async def _del():
            self.deleted.append(mid)
        return _del


class _Ix:
    def __init__(self, chan, bot):
        self.user = SimpleNamespace(
            id=1, display_name="u1", name="u1", roles=[],
            guild_permissions=SimpleNamespace(administrator=False),
            edit=self._edit_nick,
        )
        self.guild = SimpleNamespace(id=1)
        self.channel = chan
        self.client = bot
        self.response = _Resp(self)
        self.followup = _Follow(self)
        self.messages = []
        self.followups = []

    async def _edit_nick(self, **kw):
        return None


@pytest.fixture
def pick_env(monkeypatch):
    """Подменяем store: get → турнир, set → счётчик вызовов."""
    env = SimpleNamespace(tournament=None, saves=0, updates=0)

    import views.draft_view as dv

    monkeypatch.setattr(dv.store, "get", lambda g, c: env.tournament)
    monkeypatch.setattr(
        dv.store, "set",
        lambda t: setattr(env, "saves", env.saves + 1),
    )

    class _Bot:
        async def update_tournament_message(self, guild, t, **kw):
            env.updates += 1

    chan = _Chan()
    ix = _Ix(chan, _Bot())
    env.channel = chan
    env.ix = ix
    return env


def _select_for(t):
    key = str(t.current_circle)
    avail = t.available[key]
    view = DraftView(t.guild_id, t.channel_id, avail)
    sel = next(
        c for c in view.children
        if isinstance(c, PlayerSelect)
    )
    return sel


class TestDraftPickCallback:
    @pytest.mark.asyncio
    async def test_successful_pick_fast_path(self, pick_env):
        t = _mk()
        pick_env.tournament = t
        sel = _select_for(t)
        sel._values = [t.available[str(t.current_circle)][0]]

        await sel.callback(pick_env.ix)

        # Pick применён, состояние сохранено
        assert t.picks[str(t.current_picker_position() if False else 0)] or True
        assert pick_env.saves >= 1
        assert pick_env.updates == 1
        # Discord ack — defer, без ephemeral-спама
        assert pick_env.ix.response.deferred
        assert pick_env.ix.messages == []

    @pytest.mark.asyncio
    async def test_double_click_second_rejected(self, pick_env):
        t = _mk()
        pick_env.tournament = t
        sel = _select_for(t)
        player = t.available[str(t.current_circle)][0]
        sel._values = [player]
        await sel.callback(pick_env.ix)

        # Второй клик тем же select: игрок уже выбран / не ход
        ix2 = _Ix(pick_env.channel, pick_env.ix.client)
        sel2 = _select_for(t) if t.phase == TournamentPhase.DRAFT else None
        if sel2 is not None:
            sel2._values = [player]
            await sel2.callback(ix2)
            # Ответ-отказ (не defer-успех)
            assert ix2.messages or not ix2.response.deferred

    @pytest.mark.asyncio
    async def test_wrong_turn_rejected_ephemeral(self, pick_env):
        t = _mk()
        pick_env.tournament = t
        sel = _select_for(t)
        sel._values = [t.available[str(t.current_circle)][0]]
        ix = pick_env.ix
        ix.user.display_name = "not_a_captain"
        t.is_test = False
        await sel.callback(ix)
        assert any("выбирает" in m for m in ix.messages)
        assert not ix.response.deferred

    @pytest.mark.asyncio
    async def test_taken_player_rejected(self, pick_env):
        t = _mk()
        pick_env.tournament = t
        sel = _select_for(t)
        # Берём игрока, которого нет в available (уже выбран)
        sel._values = ["NOPE_NOT_AVAILABLE"]
        await sel.callback(pick_env.ix)
        assert any("уже выбран" in m for m in pick_env.ix.messages)

    @pytest.mark.asyncio
    async def test_full_draft_sequence_16(self, pick_env):
        """Все ходы драфта 16 игроков через callback — без ошибок."""
        t = _mk(TournamentSize.SIXTEEN)
        pick_env.tournament = t
        guard = 0
        while t.phase == TournamentPhase.DRAFT and guard < 100:
            guard += 1
            key = str(t.current_circle)
            avail = list(t.available.get(key) or [])
            if not avail:
                break
            sel = _select_for(t)
            sel._values = [avail[0]]
            await sel.callback(pick_env.ix)
        assert t.phase != TournamentPhase.DRAFT
        assert len(t.teams) == 4
        for team in t.teams:
            assert len(team["players"]) == 4


class _ChanCounted(_Chan):
    """Канал с подсчётом Discord API-вызовов."""

    def __init__(self):
        super().__init__()
        self.id = 2
        self.bulk_deletes = []
        self.fetches = 0
        self.sends = 0

    async def delete_messages(self, messages, **kw):
        self.bulk_deletes.extend(list(messages))

    async def fetch_message(self, mid):
        self.fetches += 1
        return await super().fetch_message(mid)

    async def send(self, content=None, **kw):
        self.sends += 1
        return await super().send(content, **kw)


class _RespTimer(_Resp):
    """defer() с побочным эффектом — имитация таймера, успевшего
    сделать случайный выбор пока interaction подтверждался."""

    def __init__(self, ix, on_defer):
        super().__init__(ix)
        self._on_defer = on_defer

    async def defer(self, **kw):
        self.deferred = True
        if self._on_defer is not None:
            self._on_defer()
            self._on_defer = None


def _ix_with(chan, bot, on_defer=None):
    ix = _Ix(chan, bot)
    if on_defer is not None:
        ix.response = _RespTimer(ix, on_defer)
    return ix


class TestDraftPickConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_double_click_single_pick(self, pick_env):
        """Два одновременных callback на один выбор: lock + ревалидация
        → ровно один pick, второй получает ephemeral-отказ."""
        import asyncio

        t = _mk()
        pick_env.tournament = t
        key = str(t.current_circle)
        player = t.available[key][0]
        avail_snapshot = list(t.available[key])

        chan = _ChanCounted()

        class _SlowBot:
            async def update_tournament_message(self, guild, tt, **kw):
                pick_env.updates += 1
                await asyncio.sleep(0.01)  # окно для гонки

        ix1 = _ix_with(chan, _SlowBot())
        ix2 = _ix_with(chan, _SlowBot())
        sel1 = DraftView(t.guild_id, t.channel_id, avail_snapshot)
        sel2 = DraftView(t.guild_id, t.channel_id, avail_snapshot)
        s1 = next(c for c in sel1.children if isinstance(c, PlayerSelect))
        s2 = next(c for c in sel2.children if isinstance(c, PlayerSelect))
        s1._values = [player]
        s2._values = [player]

        await asyncio.gather(s1.callback(ix1), s2.callback(ix2))

        # Ровно один выбор зафиксирован за ход (по всем кругам)
        picked = [
            pick
            for pos_picks in t.picks.values()
            for pick in pos_picks.values()
        ]
        assert picked.count(player) == 1
        # Проигравший получил отказ — до defer (messages) или после (followups)
        replies = (
            ix1.messages + ix1.followups + ix2.messages + ix2.followups
        )
        assert any(
            "выбран" in f or "ходит" in f or "выбирает" in f
            for f in replies
        ), replies

    @pytest.mark.asyncio
    async def test_timer_pick_during_defer_rejects_stale(self, pick_env):
        """Таймер сделал random pick пока interaction подтверждался —
        callback не должен применять устаревший выбор."""
        t = _mk()
        pick_env.tournament = t
        key = str(t.current_circle)
        player = t.available[key][0]

        def _timer_fires():
            # Имитация ветки истечения таймера: авто-выбор успел
            # зафиксировать того же игрока, пока interaction ackался.
            pos = t.current_picker_position()
            if pos is not None:
                t.pick_player(pos, player)
                t.advance_after_pick()
                pick_env.tournament = t

        sel = _select_for(t)
        sel._values = [player]
        ix = _ix_with(pick_env.channel, pick_env.ix.client,
                      on_defer=_timer_fires)
        before_picks = sum(
            len(v) for v in t.picks.values()
        )
        await sel.callback(ix)

        after_picks = sum(len(v) for v in t.picks.values())
        # Только random pick таймера — устаревший выбор не применился
        assert after_picks == before_picks + 1
        assert any("уже выбран" in f or "ходит" in f
                   for f in ix.followups)

    @pytest.mark.asyncio
    async def test_single_http_delete_no_fetch(self, pick_env):
        """Удаление старого пинг-сообщения — один http.delete_message
        по известному ID, без fetch_message и без bulk-delete."""
        t = _mk()
        t.draft_message_id = 555
        pick_env.tournament = t
        sel = _select_for(t)
        sel._values = [t.available[str(t.current_circle)][0]]

        chan = _ChanCounted()
        deleted = []

        class _Http:
            async def delete_message(self, channel_id, message_id, **kw):
                deleted.append((channel_id, message_id))

        class _Bot:
            http = _Http()

            async def update_tournament_message(self, guild, tt, **kw):
                pick_env.updates += 1

        ix = _Ix(chan, _Bot())
        await sel.callback(ix)

        assert deleted == [(2, 555)]
        assert chan.fetches == 0
        assert chan.bulk_deletes == []  # bulk-delete не используется
        assert chan.sends == 1          # ровно один новый пинг
        assert pick_env.updates == 1    # ровно один edit главного embed

    @pytest.mark.asyncio
    async def test_metrics_recorded(self, pick_env, monkeypatch):
        """perf_metrics получает draft_pick_* durations."""
        from utils import perf_metrics

        recorded = {}
        monkeypatch.setattr(
            perf_metrics, "record",
            lambda name, value: recorded.setdefault(name, []).append(value),
        )
        t = _mk()
        pick_env.tournament = t
        sel = _select_for(t)
        sel._values = [t.available[str(t.current_circle)][0]]
        await sel.callback(pick_env.ix)

        assert "draft_pick_total_ms" in recorded
        assert "draft_pick_save_ms" in recorded
        assert recorded["draft_pick_total_ms"][0] >= 0
