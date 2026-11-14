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


@pytest.mark.asyncio
async def test_begin_match_idempotent_no_duplicate(e2e):
    """Повторный вызов для того же поиска не создаёт второй матч."""
    s1 = _search(1, 10, "A")
    t1, _ = await e2e.cog._begin_match(s1, _shim(e2e, s1))
    t1b, err = await e2e.cog._begin_match(s1, _shim(e2e, s1))
    assert t1b is t1 or t1b.id == t1.id
    assert len(e2e.channels[0].sent) + len(e2e.channels[1].sent) == 1
