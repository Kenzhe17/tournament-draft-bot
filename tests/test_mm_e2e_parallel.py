"""E2E: два параллельных MM-матча и очередь при занятых каналах.

Без Discord API: guild/channel/stats/number-store подменены fake-объектами,
но весь путь ``MMCog._begin_match`` реальный — баланс SR, слот пула,
создание турнира, CV2-карточка, WAITING_FOR_CHANNEL, повторный запуск.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cogs.mm as mm  # noqa: E402
from models.mm_search import MMFormat, MMSearch, QUEUE_OPEN  # noqa: E402
from services import match_channel_pool as mcp  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, mid: int):
        self.id = mid


class _FakeChannel:
    def __init__(self, cid: int):
        self.id = cid
        self.sent: list = []

    async def send(self, **kwargs):
        msg = _FakeMessage(5000 + self.id + len(self.sent))
        self.sent.append(msg)
        return msg


class _FakePool:
    """Имитация ровно двух слотов пула."""

    def __init__(self, channels: list[_FakeChannel]):
        self.free = list(channels)
        self.busy: list[_FakeChannel] = []

    async def acquire_channel(self, guild, user_ids, role_ids=None,
                              organizer_id=0, owner=""):
        if not self.free:
            return None, "pool_full"
        ch = self.free.pop(0)
        self.busy.append(ch)
        return ch, None

    async def release_channel(self, channel):
        if channel in self.busy:
            self.busy.remove(channel)
            self.free.append(channel)
        return True


class _FakeStats:
    async def get(self, guild_id, user_id):
        return None


class _FakeNumbers:
    def __init__(self):
        self._n = 0

    async def get_or_assign(self, key):
        self._n += 1
        return self._n

    async def assign(self, key, num):
        return None

    async def release(self, key):
        return None


class _FakeTournamentStore:
    def __init__(self):
        self.by_key: dict = {}

    def set(self, t):
        self.by_key[(t.guild_id, t.channel_id)] = t

    def get(self, gid, cid):
        return self.by_key.get((gid, cid))

    def get_by_guild(self, gid):
        return [t for (g, _), t in self.by_key.items() if g == gid]


class _FakeSearchStore:
    def __init__(self):
        self.items: dict = {}

    def get(self, gid, cid):
        return self.items.get((gid, cid))

    def set(self, s):
        self.items[(s.guild_id, s.channel_id)] = s

    def all(self):
        return list(self.items.values())

    def delete(self, gid, cid):
        self.items.pop((gid, cid), None)


def _search(gid: int, cid: int, tag: str) -> MMSearch:
    s = MMSearch(guild_id=gid, channel_id=cid, format=MMFormat.SQUAD)
    for i in range(8):
        name = f"{tag}P{i}"
        s.players.append(name)
        s.player_user_ids[name] = 1000 + cid * 10 + i
    return s


@pytest.fixture
def e2e(monkeypatch):
    channels = [_FakeChannel(501), _FakeChannel(502)]
    pool = _FakePool(channels)
    guild = SimpleNamespace(
        id=1, me=SimpleNamespace(id=0),
        get_channel=lambda cid: None,
    )
    cog = mm.MMCog.__new__(mm.MMCog)
    cog.bot = SimpleNamespace(guilds=[guild])
    cog._search_timers = {}
    cog._begin_inflight = {}
    cog._begin_fail_at = {}

    searches = _FakeSearchStore()
    tournaments = _FakeTournamentStore()

    monkeypatch.setattr(mm, "mm_search_store", searches)
    monkeypatch.setattr(mm, "store", tournaments)
    monkeypatch.setattr(mm, "player_stats_store", _FakeStats())
    monkeypatch.setattr(mm, "match_number_store", _FakeNumbers())
    monkeypatch.setattr(mcp, "acquire_channel", pool.acquire_channel)

    async def _noop_panel(self, search):
        return None

    async def _noop_timer(self, gid, cid):
        return None

    monkeypatch.setattr(mm.MMCog, "_update_queue_panel", _noop_panel)
    monkeypatch.setattr(mm.MMCog, "_start_search_timer", _noop_timer)

    return SimpleNamespace(
        cog=cog, guild=guild, pool=pool,
        channels=channels, searches=searches, tournaments=tournaments,
    )


def _shim(env, search):
    return mm._SystemInteraction(env.guild, search.channel_id)


# ---------------------------------------------------------------------------
# Сценарии
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_parallel_matches_two_channels(e2e):
    """Два filled-поиска → два матча на разных каналах пула."""
    s1 = _search(1, 10, "A")
    s2 = _search(1, 11, "B")

    t1, err1 = await e2e.cog._begin_match(s1, _shim(e2e, s1))
    t2, err2 = await e2e.cog._begin_match(s2, _shim(e2e, s2))

    assert err1 is None and err2 is None
    assert t1 is not None and t2 is not None
    assert t1.channel_id != t2.channel_id
    assert {t1.channel_id, t2.channel_id} == {501, 502}
    # CV2-карточка отправлена ровно один раз в каждый канал
    for ch in e2e.channels:
        assert len(ch.sent) == 1
    # Состояние матча — active, результат ожидается
    meta = t1.match_channels[str(t1.channel_id)]
    assert meta["state"] == "active"


@pytest.mark.asyncio
async def test_third_match_waits_then_starts_after_release(e2e):
    """Третий матч ждёт WAITING_FOR_CHANNEL, стартует после освобождения."""
    s1 = _search(1, 10, "A")
    s2 = _search(1, 11, "B")
    s3 = _search(1, 12, "C")

    await e2e.cog._begin_match(s1, _shim(e2e, s1))
    await e2e.cog._begin_match(s2, _shim(e2e, s2))

    t3, err3 = await e2e.cog._begin_match(s3, _shim(e2e, s3))
    assert t3 is None
    assert "заняты" in (err3 or "")
    assert s3.is_waiting_channel

    # Освобождаем один слот → sweeper-путь: повторный _begin_match
    freed = e2e.pool.busy[0]
    await e2e.pool.release_channel(freed)

    t3, err3 = await e2e.cog._begin_match(s3, _shim(e2e, s3))
    assert err3 is None and t3 is not None
    assert t3.channel_id == freed.id
    assert not s3.is_waiting_channel


def _refill(search: MMSearch, tag: str) -> None:
    """Имитация повторного набора 8 игроков в той же очереди."""
    search.clear()
    for i in range(8):
        name = f"{tag}P{i}"
        search.players.append(name)
        search.player_user_ids[name] = 2000 + i


@pytest.mark.asyncio
async def test_parallel_matches_from_one_queue(e2e):
    """Одна очередь: матч1 → повторные 8 → матч2 (параллельно) →
    третьи 8 → WAITING_CHANNEL (оба канала заняты)."""
    s1 = _search(1, 10, "A")

    t1, err1 = await e2e.cog._begin_match(s1, _shim(e2e, s1))
    assert err1 is None and t1 is not None

    # Первый матч активен — НЕ блокирует новый запуск того же поиска
    _refill(s1, "B")
    t2, err2 = await e2e.cog._begin_match(s1, _shim(e2e, s1))
    assert err2 is None and t2 is not None
    assert t2.id != t1.id
    assert t2.channel_id != t1.channel_id
    assert {t1.channel_id, t2.channel_id} == {501, 502}

    # Третьи 8 игроков — оба канала заняты → WAITING_CHANNEL
    _refill(s1, "C")
    t3, err3 = await e2e.cog._begin_match(s1, _shim(e2e, s1))
    assert t3 is None
    assert "заняты" in (err3 or "")
    assert s1.is_waiting_channel
    assert s1.match_num > 0  # поколение запуска сохранено


@pytest.mark.asyncio
async def test_same_generation_idempotent(e2e):
    """Повтор _begin_match для того же match_num возвращает
    существующий турнир, а не создаёт дубль."""
    s1 = _search(1, 10, "A")
    t1, _ = await e2e.cog._begin_match(s1, _shim(e2e, s1))
    assert t1 is not None

    # Турнир generation = match_num из meta; search очищен →
    # эмулируем состояние «pending retry того же поколения»:
    meta = t1.match_channels[str(t1.channel_id)]
    s1.match_num = meta["match_num"]
    s1.state = "waiting_channel"
    s1.players = ["X"] * 8  # is_full для sweeper-проверок

    t1b, err = await e2e.cog._begin_match(s1, _shim(e2e, s1))
    assert t1b is t1
    assert err is None
    # Дубля нет: карточка отправлена один раз
    assert sum(len(c.sent) for c in e2e.channels) == 1


def test_pending_mm_tournament_generation(e2e):
    """_pending_mm_tournament: gen 0 → None, чужой match_num → None,
    свой match_num → турнир."""
    s = MMSearch(guild_id=1, channel_id=10, format=MMFormat.SQUAD)
    assert e2e.cog._pending_mm_tournament(s) is None

    s.match_num = 77
    assert e2e.cog._pending_mm_tournament(s) is None
