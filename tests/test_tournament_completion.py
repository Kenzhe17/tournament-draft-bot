"""Завершение турнира: итоговая карточка, архивация, гонки.

Покрывает:
- stale-guard в update_tournament_message (запоздавшее обновление
  старого турнира не трогает карточку нового);
- изоляция _EDIT_CACHE по message_id (не по каналу);
- release_completed_tournament: архив → итоговая карточка →
  освобождение слота; при ошибке архива слот сохраняется;
- restore_final_card: восстановление отображения из архива без
  пересчёта ELO/истории.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_spec = importlib.util.spec_from_file_location(
    "root_bot", Path(__file__).resolve().parent.parent / "bot.py"
)
bot_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot_module)

from models.tournament import Tournament, TournamentPhase
from services import tournament_lifecycle as tl


# ---------------------------------------------------------------------------
# Фейки
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, mid: int):
        self.id = mid
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self


class _FakeChannel:
    def __init__(self, cid: int = 2):
        self.id = cid
        self.messages: dict[int, _FakeMessage] = {}

    async def fetch_message(self, mid: int):
        if mid not in self.messages:
            raise RuntimeError("not found")
        return self.messages[mid]


class _FakeTournament:
    def __init__(self, tid="t1", mid=42):
        self.id = tid
        self.guild_id = 1
        self.channel_id = 2
        self.message_id = mid
        self.phase = "SETUP"
        self.betting_open = True
        self.betting_phase = "qualifiers"
        self.betting_results = {}

    def to_dict(self):
        return {"id": self.id, "phase": self.phase}

    def is_betting_open(self):
        return True


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
def _clean(monkeypatch):
    bot_module._EDIT_CACHE.clear()
    bot_module._EDIT_LOCKS.clear()
    bot_module._TOURNAMENT_FP_CACHE.clear()
    # Анонсы матчей не нужны в этих тестах — глушим, чтобы не
    # уходить в реальные store/DB запросы.
    try:
        import services.match_announcer as ma
        monkeypatch.setattr(
            ma, "announce_phase_matches",
            lambda *a, **kw: _noop())
    except Exception:
        pass
    yield
    bot_module._EDIT_CACHE.clear()
    bot_module._EDIT_LOCKS.clear()
    bot_module._TOURNAMENT_FP_CACHE.clear()


async def _noop():
    return None


# ---------------------------------------------------------------------------
# update_tournament_message: stale-guard и кэш по message_id
# ---------------------------------------------------------------------------


class TestStaleUpdateGuard:
    @pytest.mark.asyncio
    async def test_stale_tournament_update_skipped(self, monkeypatch):
        """Турнир A завершён и заархивирован, в канале живёт B —
        запоздалый update(A) не должен ничего редактировать."""
        import storage.json_store as js

        new_t = _FakeTournament(tid="new", mid=100)
        fake_store = SimpleNamespace(get=lambda g, c: new_t)
        monkeypatch.setattr(js, "store", fake_store)

        counter = {"builds": 0}
        bot = _make_bot(monkeypatch, counter)
        channel = _FakeChannel()
        msg_old = _FakeMessage(42)
        channel.messages[42] = msg_old
        guild = SimpleNamespace(id=1, get_channel=lambda cid: channel)

        stale = _FakeTournament(tid="old", mid=42)
        ok = await bot.update_tournament_message(guild, stale)

        assert ok is True                      # не ошибка — просто skip
        assert counter["builds"] == 0          # embed даже не строился
        assert msg_old.edits == []             # чужая карточка цела

    @pytest.mark.asyncio
    async def test_two_tournaments_same_channel_isolated(self, monkeypatch):
        """Старый и новый турниры в одном канале: каждый edit идёт
        в СВОЁ сообщение (кэш по message_id, не по каналу)."""
        import storage.json_store as js

        holder = {"current": None}
        fake_store = SimpleNamespace(
            get=lambda g, c: holder["current"])
        monkeypatch.setattr(js, "store", fake_store)

        counter = {"builds": 0}
        bot = _make_bot(monkeypatch, counter)
        channel = _FakeChannel()
        msg_a = _FakeMessage(42)
        msg_b = _FakeMessage(100)
        channel.messages[42] = msg_a
        channel.messages[100] = msg_b
        guild = SimpleNamespace(id=1, get_channel=lambda cid: channel)

        t_a = _FakeTournament(tid="old", mid=42)
        holder["current"] = t_a
        await bot.update_tournament_message(guild, t_a)

        t_b = _FakeTournament(tid="new", mid=100)
        holder["current"] = t_b   # слот канала перешёл новому
        await bot.update_tournament_message(guild, t_b)

        assert len(msg_a.edits) == 1
        assert len(msg_b.edits) == 1
        # Ключи кэша разные — по message_id.
        keys = list(bot_module._EDIT_CACHE.keys())
        assert any(k.endswith(":42") for k in keys)
        assert any(k.endswith(":100") for k in keys)

    @pytest.mark.asyncio
    async def test_update_allowed_when_store_has_same_id(
            self, monkeypatch):
        """Обычный путь: store.get возвращает тот же турнир —
        обновление проходит."""
        import storage.json_store as js

        t = _FakeTournament(tid="t1", mid=42)
        fake_store = SimpleNamespace(get=lambda g, c: t)
        monkeypatch.setattr(js, "store", fake_store)

        counter = {"builds": 0}
        bot = _make_bot(monkeypatch, counter)
        channel = _FakeChannel()
        channel.messages[42] = _FakeMessage(42)
        guild = SimpleNamespace(id=1, get_channel=lambda cid: channel)

        ok = await bot.update_tournament_message(guild, t)
        assert ok is True
        assert counter["builds"] == 1


# ---------------------------------------------------------------------------
# release_completed_tournament
# ---------------------------------------------------------------------------


def _real_tournament() -> Tournament:
    t = Tournament(guild_id=1, channel_id=2)
    t.id = "t_done"
    t.message_id = 555
    t.phase = TournamentPhase.COMPLETE
    t.winner_team_index = 0
    return t


class TestReleaseCompleted:
    @pytest.mark.asyncio
    async def test_archive_ok_releases_slot(self, monkeypatch):
        # archive() — sync-функция стора.
        monkeypatch.setattr(tl.tournament_archive, "archive",
                            lambda t: True)
        deleted = []
        monkeypatch.setattr(
            tl.store, "delete",
            lambda g, c: deleted.append((g, c)))

        guild = SimpleNamespace(id=1)
        channel = _FakeChannel()
        channel.messages[555] = _FakeMessage(555)
        guild.get_channel = lambda cid: channel

        async def _fake_card(message, tournament, g):
            message.edits.append({"view": "winner-card"})
            return True
        monkeypatch.setattr(
            tl, "render_final_card_to_message", _fake_card)

        t = _real_tournament()
        ok = await tl.release_completed_tournament(guild, t,
                                                   channel=channel)
        assert ok is True
        assert deleted == [(1, 2)]
        assert len(channel.messages[555].edits) == 1

    @pytest.mark.asyncio
    async def test_archive_failure_keeps_slot(self, monkeypatch):
        """Архив не записался → запись НЕ удаляется, False."""
        monkeypatch.setattr(tl.tournament_archive, "archive",
                            lambda t: False)
        deleted = []
        monkeypatch.setattr(
            tl.store, "delete",
            lambda g, c: deleted.append((g, c)))

        guild = SimpleNamespace(id=1)
        t = _real_tournament()
        ok = await tl.release_completed_tournament(guild, t)
        assert ok is False
        assert deleted == []               # слот сохранён

    @pytest.mark.asyncio
    async def test_card_edit_failure_still_releases(self, monkeypatch):
        """Архив ОК, но Discord edit упал → слот освобождается
        (карточку можно восстановить из архива позже)."""
        monkeypatch.setattr(tl.tournament_archive, "archive",
                            lambda t: True)
        deleted = []
        monkeypatch.setattr(
            tl.store, "delete",
            lambda g, c: deleted.append((g, c)))

        guild = SimpleNamespace(id=1)
        channel = _FakeChannel()           # fetch_message → not found
        guild.get_channel = lambda cid: channel

        t = _real_tournament()
        ok = await tl.release_completed_tournament(guild, t,
                                                   channel=channel)
        assert ok is True
        assert deleted == [(1, 2)]


# ---------------------------------------------------------------------------
# restore_final_card
# ---------------------------------------------------------------------------


class TestRestoreFinalCard:
    @pytest.mark.asyncio
    async def test_restore_edits_message_only(self, monkeypatch):
        t = _real_tournament()
        data = t.to_dict()
        entry = {
            "tournament_id": "t_done",
            "guild_id": 1,
            "channel_id": 2,
            "data": data,
        }
        monkeypatch.setattr(tl.tournament_archive, "get",
                            lambda tid: entry)

        msg = _FakeMessage(555)
        channel = _FakeChannel()
        channel.messages[555] = msg
        guild = SimpleNamespace(id=1)
        guild.get_channel = lambda cid: channel
        client = SimpleNamespace(get_guild=lambda gid: guild)

        async def _fake_card(message, tournament, g):
            assert tournament.id == "t_done"
            assert tournament.phase == TournamentPhase.COMPLETE
            message.edits.append({"view": "winner"})
            return True
        monkeypatch.setattr(
            tl, "render_final_card_to_message", _fake_card)

        ok = await tl.restore_final_card("t_done", client=client)
        assert ok is True
        assert len(msg.edits) == 1

    @pytest.mark.asyncio
    async def test_restore_missing_archive(self, monkeypatch):
        monkeypatch.setattr(tl.tournament_archive, "get",
                            lambda tid: None)
        ok = await tl.restore_final_card("nope",
                                         client=SimpleNamespace())
        assert ok is False
