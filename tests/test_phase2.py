"""Phase 2: P1.2 (timer fingerprint skip), P5 (pets filter), P8 (invite refresh cadence)."""

import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "root_bot", Path(__file__).resolve().parent.parent / "bot.py"
)
bot_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot_module)


class _FakeTournament:
    """Минимальный турнир с to_dict/is_betting_open для fingerprint."""

    def __init__(self, tid="t1"):
        self.id = tid
        self.guild_id = 1
        self.channel_id = 2
        self.message_id = 42
        self.phase = "SETUP"
        self.state = {"picks": []}
        self.betting_open = True
        self.betting_phase = "qualifiers"
        self.betting_results = {}
        self._betting_open_now = True

    def to_dict(self):
        return {"id": self.id, "phase": self.phase, "state": self.state}

    def is_betting_open(self):
        return self._betting_open_now


class _FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self


class _FakeChannel:
    def __init__(self):
        self.id = 2
        self.fetches = 0
        self.message = _FakeMessage()

    async def fetch_message(self, mid):
        self.fetches += 1
        return self.message


def _make_bot(monkeypatch, counter):
    bot = object.__new__(bot_module.TournamentBot)
    bot._registered_view_keys = set()
    bot._register_view = lambda view: None
    bot.build_view_for_tournament = lambda t: SimpleNamespace(children=[])

    async def _embed(t, guild):
        counter["builds"] += 1
        return SimpleNamespace(to_dict=lambda: {"description": "v"})

    monkeypatch.setattr(bot_module, "build_embed_for_phase", _embed)
    monkeypatch.setattr(
        bot_module, "compose", lambda e, v: SimpleNamespace(children=[])
    )
    return bot


@pytest.fixture(autouse=True)
def _clean():
    bot_module._EDIT_CACHE.clear()
    bot_module._EDIT_LOCKS.clear()
    bot_module._TOURNAMENT_FP_CACHE.clear()
    yield
    bot_module._EDIT_CACHE.clear()
    bot_module._EDIT_LOCKS.clear()
    bot_module._TOURNAMENT_FP_CACHE.clear()


class TestTimerFingerprint:
    @pytest.mark.asyncio
    async def test_timer_ticks_skip_embed_build(self, monkeypatch):
        """3 таймерных тика без изменений → 1 build_embed, 1 edit."""
        import services.match_announcer as ma

        announces = []

        async def _announce(t, g, b):
            announces.append(1)

        monkeypatch.setattr(ma, "announce_phase_matches", _announce)

        counter = {"builds": 0}
        bot = _make_bot(monkeypatch, counter)
        guild = SimpleNamespace(id=1, get_channel=lambda cid: _CHAN)
        channel = _FakeChannel()
        guild.get_channel = lambda cid: channel
        t = _FakeTournament()

        for _ in range(3):
            await bot.update_tournament_message(guild, t, from_timer=True)

        assert counter["builds"] == 1
        assert len(channel.message.edits) == 1
        # announce отработал на каждом тике (идемпотентен внутри).
        assert len(announces) == 3

    @pytest.mark.asyncio
    async def test_state_change_rebuilds(self, monkeypatch):
        import services.match_announcer as ma

        async def _announce(t, g, b):
            return None

        monkeypatch.setattr(ma, "announce_phase_matches", _announce)
        counter = {"builds": 0}
        bot = _make_bot(monkeypatch, counter)
        channel = _FakeChannel()
        guild = SimpleNamespace(id=1, get_channel=lambda cid: channel)
        t = _FakeTournament()

        await bot.update_tournament_message(guild, t, from_timer=True)
        t.state["picks"] = ["player1"]
        await bot.update_tournament_message(guild, t, from_timer=True)
        assert counter["builds"] == 2

    @pytest.mark.asyncio
    async def test_betting_close_transition_rebuilds(self, monkeypatch):
        """is_betting_open() False → fingerprint меняется → rebuild."""
        import services.match_announcer as ma

        async def _announce(t, g, b):
            return None

        monkeypatch.setattr(ma, "announce_phase_matches", _announce)
        counter = {"builds": 0}
        bot = _make_bot(monkeypatch, counter)
        channel = _FakeChannel()
        guild = SimpleNamespace(id=1, get_channel=lambda cid: channel)
        t = _FakeTournament()

        await bot.update_tournament_message(guild, t, from_timer=True)
        t._betting_open_now = False  # окно ставок закрылось по времени
        await bot.update_tournament_message(guild, t, from_timer=True)
        assert counter["builds"] == 2

    @pytest.mark.asyncio
    async def test_non_timer_call_always_rebuilds(self, monkeypatch):
        """Внешние вызовы (кнопки/модалы) не используют fp-кэш."""
        import services.match_announcer as ma

        async def _announce(t, g, b):
            return None

        monkeypatch.setattr(ma, "announce_phase_matches", _announce)
        counter = {"builds": 0}
        bot = _make_bot(monkeypatch, counter)
        channel = _FakeChannel()
        guild = SimpleNamespace(id=1, get_channel=lambda cid: channel)
        t = _FakeTournament()

        await bot.update_tournament_message(guild, t)  # не from_timer
        await bot.update_tournament_message(guild, t)
        assert counter["builds"] == 2

    @pytest.mark.asyncio
    async def test_ttl_forces_rebuild(self, monkeypatch):
        """После TTL кэш протухает — страховка от PG-дрейфа."""
        import services.match_announcer as ma

        async def _announce(t, g, b):
            return None

        monkeypatch.setattr(ma, "announce_phase_matches", _announce)
        monkeypatch.setattr(bot_module, "_TIMER_REBUILD_TTL", 0.0)
        counter = {"builds": 0}
        bot = _make_bot(monkeypatch, counter)
        channel = _FakeChannel()
        guild = SimpleNamespace(id=1, get_channel=lambda cid: channel)
        t = _FakeTournament()

        await bot.update_tournament_message(guild, t, from_timer=True)
        await bot.update_tournament_message(guild, t, from_timer=True)
        assert counter["builds"] == 2

    def test_fingerprint_stable_and_sensitive(self):
        t1 = _FakeTournament()
        t2 = _FakeTournament()
        fp1 = bot_module._tournament_display_fingerprint(t1)
        fp2 = bot_module._tournament_display_fingerprint(t2)
        assert fp1 == fp2  # одинаковое состояние → одинаковая сигнатура

        t2.state["picks"] = ["x"]
        assert bot_module._tournament_display_fingerprint(t2) != fp1

        t3 = _FakeTournament()
        t3._betting_open_now = False
        assert bot_module._tournament_display_fingerprint(t3) != fp1


class TestPetsFilter:
    def test_filters_by_owner_membership(self):
        rows = [
            {"pet_id": 1, "owner_1_id": 100, "owner_2_id": None},
            {"pet_id": 2, "owner_1_id": 200, "owner_2_id": 300},
            {"pet_id": 3, "owner_1_id": 400, "owner_2_id": 500},
        ]
        out = list(bot_module._pets_for_members(rows, {100, 300}))
        assert [r["pet_id"] for r in out] == [1, 2]

    def test_empty_members_no_rows(self):
        rows = [{"pet_id": 1, "owner_1_id": 1, "owner_2_id": 2}]
        assert list(bot_module._pets_for_members(rows, set())) == []


class TestInviteRefreshCadence:
    def test_constants(self):
        import cogs.reputation as rep

        assert rep._SWEEP_INTERVAL == 60
        assert rep._INVITE_REFRESH_EVERY == 5
