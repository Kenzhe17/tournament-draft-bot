"""Тесты очереди «БЕЗ РУИН» (no_ruin): SR-гейты, изоляция очередей,
совместное существование normal/no_ruin, SR-свип перед матчем.

Запуск: pytest tests/test_mm_no_ruin.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from models.mm_search import (
    MMFormat,
    MMSearch,
    QUEUE_TYPE_NO_RUIN,
    QUEUE_TYPE_NORMAL,
    NO_RUIN_MIN_SR,
)
from storage.mm_search_store import MMSearchStore


# ---------------------------------------------------------------- helpers

class _FakeResponse:
    def __init__(self):
        self.sent: list[tuple] = []
        self.edited = 0

    async def send_message(self, *a, **kw):
        self.sent.append((a, kw))

    async def edit_message(self, *a, **kw):
        self.edited += 1

    async def defer(self, *a, **kw):
        pass

    def is_done(self):
        return False


class _FakeFollowup:
    async def send(self, *a, **kw):
        pass


class _FakeInteraction:
    def __init__(self, user_id: int, guild_id: int = 1, channel_id: int = 10):
        self.user = SimpleNamespace(
            id=user_id,
            display_name=f"U{user_id}",
            primary_guild=SimpleNamespace(id=config.GUILD_ID),
        )
        self.guild = SimpleNamespace(id=guild_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.message = SimpleNamespace(id=999, edit=AsyncMock())


class _DictStore:
    """Полный аналог MMSearchStore в памяти для тестов cog."""

    def __init__(self):
        self.data: dict[tuple, MMSearch] = {}

    def get(self, guild_id, channel_id, queue_type=QUEUE_TYPE_NORMAL):
        return self.data.get((guild_id, channel_id, queue_type))

    def get_by_channel(self, guild_id, channel_id):
        return {
            qt: s for (g, c, qt), s in self.data.items()
            if g == guild_id and c == channel_id
        }

    def set(self, s: MMSearch):
        qt = s.queue_type or QUEUE_TYPE_NORMAL
        s.queue_type = qt
        self.data[(s.guild_id, s.channel_id, qt)] = s

    def delete(self, guild_id, channel_id, queue_type=QUEUE_TYPE_NORMAL):
        self.data.pop((guild_id, channel_id, queue_type), None)

    def get_by_guild(self, guild_id):
        return [s for (g, _c, _q), s in self.data.items() if g == guild_id]

    def all(self):
        return list(self.data.values())


class _FakeRCStore:
    def __init__(self):
        self.active = None

    async def get_active_by_search(self, guild_id, channel_id, queue_type=None):
        if self.active is None:
            return None
        if queue_type is not None and (
            getattr(self.active, "queue_type", "normal") or "normal"
        ) != queue_type:
            return None
        return self.active

    async def get(self, match_id):
        return self.active


def _stats(sr: float):
    return SimpleNamespace(skill_rating=sr)


def _make_cog(monkeypatch, store: _DictStore, srs: dict[int, float]):
    import cogs.mm as mm_mod
    import services.vac_service as vac_mod

    class _StatsStore:
        async def get(self, guild_id, user_id):
            return _stats(srs.get(user_id, 0.0))

    async def _no_vac(guild_id, user_id):
        return None

    monkeypatch.setattr(mm_mod, "mm_search_store", store)
    monkeypatch.setattr(mm_mod, "ready_check_store", _FakeRCStore())
    monkeypatch.setattr(mm_mod, "player_stats_store", _StatsStore())
    monkeypatch.setattr(vac_mod, "get_active_vac", _no_vac)
    bot = SimpleNamespace(mm_cog=None)
    return mm_mod.MMCog(bot)


def _nr_search(n: int = 0, locked: bool = False) -> MMSearch:
    s = MMSearch(
        guild_id=1, channel_id=10, format=MMFormat.SQUAD,
        queue_type=QUEUE_TYPE_NO_RUIN,
    )
    s.message_id = 999
    for i in range(n):
        s.add_player(f"NR{i}", 2000 + i)
    if locked:
        s.state = "locked"
        s.match_num = 7
    return s


# ---------------------------------------------------------------- SR bounds

def test_sr_range_boundaries():
    cog_sr = __import__("cogs.mm", fromlist=["MMCog"]).MMCog._sr_in_no_ruin_range
    assert not cog_sr(5.49)   # ниже минимума — запрещено
    assert cog_sr(5.50)       # минимум — разрешено
    assert cog_sr(9.0)        # внутри диапазона — разрешено
    assert not cog_sr(10.01)  # выше максимума — запрещено


@pytest.mark.asyncio
async def test_join_no_ruin_denied_below_min_sr(monkeypatch):
    store = _DictStore()
    store.set(_nr_search())
    cog = _make_cog(monkeypatch, store, {3000: 5.49})
    inter = _FakeInteraction(3000)

    await cog.handle_queue_join(inter, 1, 10, QUEUE_TYPE_NO_RUIN)

    texts = [a[0] for a, _k in inter.response.sent]
    assert any("5.5 SR" in t and "5.5" in t for t in texts)
    assert any("5.4" in t or "5.5" in t for t in texts)
    assert len(store.get(1, 10, QUEUE_TYPE_NO_RUIN).players) == 0


@pytest.mark.asyncio
async def test_join_no_ruin_denied_above_max_sr(monkeypatch):
    store = _DictStore()
    store.set(_nr_search())
    cog = _make_cog(monkeypatch, store, {3000: 10.5})
    inter = _FakeInteraction(3000)

    await cog.handle_queue_join(inter, 1, 10, QUEUE_TYPE_NO_RUIN)

    texts = [a[0] for a, _k in inter.response.sent]
    assert any("Максимальный SR" in t for t in texts)
    assert len(store.get(1, 10, QUEUE_TYPE_NO_RUIN).players) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("sr", [5.5, 9.0])
async def test_join_no_ruin_allowed_in_range(monkeypatch, sr):
    store = _DictStore()
    store.set(_nr_search())
    cog = _make_cog(monkeypatch, store, {3000: sr})
    inter = _FakeInteraction(3000)

    await cog.handle_queue_join(inter, 1, 10, QUEUE_TYPE_NO_RUIN)

    assert "U3000" in store.get(1, 10, QUEUE_TYPE_NO_RUIN).players


@pytest.mark.asyncio
async def test_normal_queue_has_no_sr_gate(monkeypatch):
    """Обычка не проверяет SR — игрок с любым рейтингом входит."""
    store = _DictStore()
    normal = MMSearch(guild_id=1, channel_id=10, format=MMFormat.SQUAD)
    normal.message_id = 999
    store.set(normal)
    cog = _make_cog(monkeypatch, store, {3000: 1.0})
    inter = _FakeInteraction(3000)

    await cog.handle_queue_join(inter, 1, 10, QUEUE_TYPE_NORMAL)

    assert "U3000" in store.get(1, 10, QUEUE_TYPE_NORMAL).players


# ---------------------------------------------------------------- isolation

@pytest.mark.asyncio
async def test_cannot_be_in_both_queues_switch_is_atomic(monkeypatch):
    """Игрок в Обычке жмёт «БЕЗ РУИН» — вылетает из Обычки атомарно."""
    store = _DictStore()
    normal = MMSearch(guild_id=1, channel_id=10, format=MMFormat.SQUAD)
    normal.add_player("P0", 1000)
    store.set(normal)
    nr = _nr_search()
    store.set(nr)
    cog = _make_cog(monkeypatch, store, {1000: 7.0, 3000: 7.0})

    inter = _FakeInteraction(1000)
    await cog.handle_queue_join(inter, 1, 10, QUEUE_TYPE_NO_RUIN)

    normal_after = store.get(1, 10, QUEUE_TYPE_NORMAL)
    nr_after = store.get(1, 10, QUEUE_TYPE_NO_RUIN)
    assert "P0" not in normal_after.players
    assert "U1000" in nr_after.players
    # Пользователь ровно в одной очереди
    assert sum(
        1000 in s.player_user_ids.values() for s in store.all()
    ) == 1


@pytest.mark.asyncio
async def test_repeat_join_same_queue_is_idempotent(monkeypatch):
    store = _DictStore()
    nr = _nr_search()
    store.set(nr)
    cog = _make_cog(monkeypatch, store, {3000: 7.0})
    inter = _FakeInteraction(3000)

    await cog.handle_queue_join(inter, 1, 10, QUEUE_TYPE_NO_RUIN)
    await cog.handle_queue_join(inter, 1, 10, QUEUE_TYPE_NO_RUIN)

    assert store.get(1, 10, QUEUE_TYPE_NO_RUIN).players.count("U3000") == 1


@pytest.mark.asyncio
async def test_two_queues_same_channel_coexist(monkeypatch):
    """Обе очереди живут в одном канале независимо."""
    store = _DictStore()
    normal = MMSearch(guild_id=1, channel_id=10, format=MMFormat.SQUAD)
    normal.add_player("N0", 1000)
    store.set(normal)
    nr = _nr_search()
    nr.add_player("R0", 2000)
    store.set(nr)

    searches = store.get_by_channel(1, 10)
    assert set(searches) == {QUEUE_TYPE_NORMAL, QUEUE_TYPE_NO_RUIN}
    assert searches[QUEUE_TYPE_NORMAL].players == ["N0"]
    assert searches[QUEUE_TYPE_NO_RUIN].players == ["R0"]


@pytest.mark.asyncio
async def test_no_ruin_search_created_lazily_on_join(monkeypatch):
    """Первый вход в БЕЗ РУИН создаёт очередь без /mm squad."""
    store = _DictStore()
    cog = _make_cog(monkeypatch, store, {3000: 7.0})
    inter = _FakeInteraction(3000)

    await cog.handle_queue_join(inter, 1, 10, QUEUE_TYPE_NO_RUIN)

    nr = store.get(1, 10, QUEUE_TYPE_NO_RUIN)
    assert nr is not None
    assert nr.queue_type == QUEUE_TYPE_NO_RUIN
    assert nr.message_id == 999  # взят с кнопки панели
    assert "U3000" in nr.players


# ---------------------------------------------------------------- store/RC

def test_store_distinguishes_queue_types(tmp_path, monkeypatch):
    import storage.mm_search_store as mms_mod

    monkeypatch.setattr(mms_mod, "MM_SEARCH_FILE", tmp_path / "mm.json")
    monkeypatch.setattr(mms_mod, "DATABASE_URL", "")
    store = MMSearchStore()

    normal = MMSearch(guild_id=1, channel_id=10)
    normal.add_player("A", 1)
    nr = MMSearch(guild_id=1, channel_id=10, queue_type=QUEUE_TYPE_NO_RUIN)
    nr.add_player("B", 2)
    store.set(normal)
    store.set(nr)

    assert store.get(1, 10).players == ["A"]
    assert store.get(1, 10, QUEUE_TYPE_NO_RUIN).players == ["B"]
    assert len(store.get_by_channel(1, 10)) == 2

    # JSON roundtrip
    store2 = MMSearchStore()
    assert store2.get(1, 10, QUEUE_TYPE_NO_RUIN).players == ["B"]


@pytest.mark.asyncio
async def test_ready_check_store_allows_parallel_modes():
    """Два активных RC в одном канале: normal + no_ruin — легально."""
    from models.ready_check import ReadyCheck
    from storage.ready_check_store import ReadyCheckStore

    store = ReadyCheckStore()
    store._loaded = True  # без PG/JSON
    rc_n = ReadyCheck(match_id="rc_n", guild_id=1, search_channel_id=10,
                      tournament_channel_id=0,
                      queue_type=QUEUE_TYPE_NORMAL)
    rc_r = ReadyCheck(match_id="rc_r", guild_id=1, search_channel_id=10,
                      tournament_channel_id=0,
                      queue_type=QUEUE_TYPE_NO_RUIN)

    assert await store.create(rc_n)
    assert await store.create(rc_r)  # не конфликтует — другой queue_type

    assert (await store.get_active_by_search(1, 10, QUEUE_TYPE_NORMAL)
            ).match_id == "rc_n"
    assert (await store.get_active_by_search(1, 10, QUEUE_TYPE_NO_RUIN)
            ).match_id == "rc_r"


# ---------------------------------------------------------------- SR sweep

@pytest.mark.asyncio
async def test_begin_match_sweeps_low_sr_from_no_ruin(monkeypatch):
    """Игрок с упавшим SR исключается до формирования матча."""
    import cogs.mm as mm_mod

    store = _DictStore()
    nr = _nr_search(n=8)
    # UID 2000..2007; у 2003 SR упал до 4.0
    srs = {2000 + i: 7.0 for i in range(8)}
    srs[2003] = 4.0
    store.set(nr)
    cog = _make_cog(monkeypatch, store, srs)
    cog._update_queue_panel = AsyncMock()
    monkeypatch.setattr(
        cog, "_create_match_channel",
        AsyncMock(return_value=(SimpleNamespace(id=1), None)),
    )
    start_rc = AsyncMock(return_value=SimpleNamespace(match_id="rc_x"))
    monkeypatch.setattr(cog, "_start_ready_check", start_rc)
    monkeypatch.setattr(
        mm_mod, "match_number_store",
        SimpleNamespace(get_or_assign=AsyncMock(return_value=1)),
    )
    chan = SimpleNamespace(send=AsyncMock(), id=10)
    cog.bot.get_channel = lambda cid: chan

    nr.state = "locked"
    inter = _FakeInteraction(1000)
    rc, err = await cog._begin_match_impl(nr, inter)

    # 7 игроков осталось < 8 → матч не стартовал, очередь открыта
    assert rc is None
    assert nr.state == "open"
    assert "NR3" not in nr.players
    assert len(nr.players) == 7
    start_rc.assert_not_called()
