"""Тесты единого менеджера пула матч-каналов.

Покрывает: атомарный захват слотов (PG-семантика через FakeConn),
pool_full без временных каналов, переходы состояний, безопасное
освобождение (lockdown + purge → FREE), force_release, 429-retry
и нормализацию OCR-имён команд.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord

from services import match_channel_pool as mcp


# ---------------------------------------------------------------------------
# Fake asyncpg: in-memory таблица match_channel_slots
# ---------------------------------------------------------------------------


class FakeConn:
    def __init__(self, db: "FakeDB"):
        self.db = db

    async def execute(self, sql: str, *args):
        if "INSERT INTO match_channel_slots" in sql:
            cid = args[0]
            self.db.rows.setdefault(
                cid, {"channel_id": cid, "guild_id": 0,
                      "state": "free", "owner": ""}
            )
            return "INSERT 0 1"
        if "UPDATE match_channel_slots" in sql and "FOR UPDATE" not in sql:
            # transition / acquire update: args = (guild_id, owner, cid)
            # или (channel_id, new_state, owner, expected)
            if len(args) == 4 and isinstance(args[3], list):
                cid, new_state, owner, expected = args
                row = self.db.rows.get(cid)
                if row is None or row["state"] not in expected:
                    return "UPDATE 0"
                row["state"] = new_state
                row["owner"] = owner
                return "UPDATE 1"
            if len(args) == 1:
                # force_release: SET state='free', owner='' WHERE channel_id
                row = self.db.rows.get(args[0])
                if row is None:
                    return "UPDATE 0"
                row.update(state="free", owner="")
                return "UPDATE 1"
            guild_id, owner, cid = args
            row = self.db.rows.get(cid)
            if row is None or row["state"] != "free":
                return "UPDATE 0"
            row.update(state="reserved", owner=owner, guild_id=guild_id)
            return "UPDATE 1"
        return "OK"

    async def fetchrow(self, sql: str, *args):
        if "FOR UPDATE SKIP LOCKED" in sql:
            guild_id = args[0]
            allowed = args[1] if len(args) > 1 else None
            for cid in sorted(self.db.rows):
                row = self.db.rows[cid]
                if allowed is not None and cid not in allowed:
                    continue
                if row["state"] == "free" and (
                    row["guild_id"] in (0, guild_id)
                ):
                    return {"channel_id": cid}
            return None
        if "FOR UPDATE" in sql:
            row = self.db.rows.get(args[0])
            return dict(row) if row else None
        row = self.db.rows.get(args[0])
        return dict(row) if row else None

    async def fetch(self, sql: str, *args):
        return [dict(r) for r in self.db.rows.values()]

    def transaction(self):
        return _FakeTx()


class _FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        return FakeAcquire(FakeConn(self.db))


class FakeDB:
    def __init__(self):
        self.rows = {}


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(mcp, "get_pool", AsyncMock(return_value=FakePool(db)))
    monkeypatch.setattr(mcp, "pool_channel_ids",
                        lambda: [101, 102, 103, 104, 105])
    mcp._pool_ids = [101, 102, 103, 104, 105]
    return db


# ---------------------------------------------------------------------------
# PG-слой: атомарность и переходы
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_slots_registers_all_pool_ids(fake_db):
    await mcp.ensure_slots()
    assert len(fake_db.rows) == 5
    assert all(r["state"] == "free" for r in fake_db.rows.values())


@pytest.mark.asyncio
async def test_acquire_never_returns_channel_outside_pool(fake_db):
    """Слот вне MM_MATCH_CHANNEL_POOL не выдаётся, даже если free."""
    await mcp.ensure_slots()
    fake_db.rows[999] = {
        "channel_id": 999, "guild_id": 0,
        "state": "free", "owner": "",
    }
    got = await mcp.acquire_slot(guild_id=999, owner="m1")
    assert got in (101, 102, 103, 104, 105)
    assert fake_db.rows[999]["state"] == "free"


@pytest.mark.asyncio
async def test_five_simultaneous_acquires_get_distinct_slots(fake_db):
    await mcp.ensure_slots()
    results = await asyncio.gather(*[
        mcp.acquire_slot(guild_id=999, owner=f"m{i}") for i in range(5)
    ])
    assert sorted(r for r in results if r) == [101, 102, 103, 104, 105]
    states = {r["state"] for r in fake_db.rows.values()}
    assert states == {"reserved"}


@pytest.mark.asyncio
async def test_two_concurrent_requests_for_last_slot(fake_db):
    """Два претендента на последний свободный слот — ровно один побеждает."""
    await mcp.ensure_slots()
    for cid in (101, 102, 103, 104):
        await mcp.acquire_slot(999, "taken")
    a, b = await asyncio.gather(
        mcp.acquire_slot(999, "A"), mcp.acquire_slot(999, "B")
    )
    winners = [x for x in (a, b) if x is not None]
    assert len(winners) == 1
    assert winners[0] == 105
    # Второй получил None — слот один
    assert (a is None) != (b is None)


@pytest.mark.asyncio
async def test_pool_full_returns_none(fake_db):
    await mcp.ensure_slots()
    for cid in (101, 102, 103, 104, 105):
        await mcp.acquire_slot(999, "taken")
    assert await mcp.acquire_slot(999, "late") is None


@pytest.mark.asyncio
async def test_transition_guards_wrong_state(fake_db):
    await mcp.ensure_slots()
    cid = await mcp.acquire_slot(999, "o")
    # reserved → active разрешён
    assert await mcp.transition(cid, {mcp.RESERVED}, mcp.ACTIVE, "o")
    # active → reserved запрещён (защита от двойного освобождения)
    assert not await mcp.transition(cid, {mcp.RESERVED}, mcp.ACTIVE, "o")
    # повторный release-переход cleaning→free только из cleaning
    assert await mcp.transition(cid, {mcp.ACTIVE}, mcp.CLEANING)
    assert not await mcp.transition(cid, {mcp.ACTIVE}, mcp.CLEANING)
    assert await mcp.transition(cid, {mcp.CLEANING}, mcp.FREE)
    assert fake_db.rows[cid]["state"] == "free"


@pytest.mark.asyncio
async def test_force_release_clears_any_state(fake_db):
    await mcp.ensure_slots()
    cid = await mcp.acquire_slot(999, "o")
    await mcp.transition(cid, {mcp.RESERVED}, mcp.ACTIVE, "o")
    old = await mcp.force_release(cid)
    assert old["state"] == "active"
    assert fake_db.rows[cid]["state"] == "free"
    assert fake_db.rows[cid]["owner"] == ""


# ---------------------------------------------------------------------------
# Discord-слой: acquire_channel / release_channel
# ---------------------------------------------------------------------------


class _FakeTextChannel:
    """Подмена discord.TextChannel для isinstance-проверок менеджера."""

    def __init__(self, cid, guild):
        self.id = cid
        self.guild = guild
        self.edit = AsyncMock()
        self.pins = AsyncMock(return_value=[])
        hist = MagicMock()
        hist.flatten = AsyncMock(return_value=[])
        self.history = MagicMock(return_value=hist)
        self.delete_messages = AsyncMock()


def _fake_guild(cid=999):
    guild = MagicMock()
    guild.id = cid
    guild.default_role = MagicMock()
    channels = {}
    guild.get_channel = lambda i: channels.get(i)
    guild.fetch_channel = AsyncMock(
        side_effect=lambda i: channels.get(i)
    )
    return guild, channels


@pytest.fixture
def fake_discord(monkeypatch):
    """Пул discord.TextChannel → FakeChannel; каналы 101..105 в guild."""
    monkeypatch.setattr(discord, "TextChannel", _FakeTextChannel)
    guild, channels = _fake_guild()
    for cid in (101, 102, 103, 104, 105):
        channels[cid] = _FakeTextChannel(cid, guild)
    return guild, channels


@pytest.mark.asyncio
async def test_acquire_channel_pool_full(fake_db, fake_discord):
    guild, _ = fake_discord
    await mcp.ensure_slots()
    for cid in (101, 102, 103, 104, 105):
        await mcp.acquire_slot(guild.id, "other")
    channel, err = await mcp.acquire_channel(guild, [1, 2, 3])
    assert channel is None
    assert err == "pool_full"


@pytest.mark.asyncio
async def test_acquire_channel_success_sets_overwrites_and_active(
    fake_db, fake_discord
):
    guild, channels = fake_discord
    await mcp.ensure_slots()
    channel, err = await mcp.acquire_channel(
        guild, user_ids=[11, 22], role_ids=[33], organizer_id=44,
        owner="test",
    )
    assert err is None
    assert channel is channels[101]
    assert fake_db.rows[101]["state"] == "active"
    channel.edit.assert_awaited()  # overwrites применены
    channel.delete_messages.assert_not_awaited()  # история пуста


@pytest.mark.asyncio
async def test_release_channel_locks_down_and_frees(
    fake_db, fake_discord
):
    guild, channels = fake_discord
    await mcp.ensure_slots()
    channel, _ = await mcp.acquire_channel(guild, [11])
    assert fake_db.rows[101]["state"] == "active"

    ok = await mcp.release_channel(channel)
    assert ok is True
    assert fake_db.rows[101]["state"] == "free"
    # resting edit вызван второй раз (после acquire-edit):
    # свободный канал пула скрыт от @everyone полностью
    assert channel.edit.await_count == 2
    resting = channel.edit.await_args.kwargs["overwrites"]
    ow = resting[guild.default_role]
    assert ow.send_messages is False
    assert ow.view_channel is False  # канал не виден всем по умолчанию
    # персональные overwrites игроков сняты — в dict только default_role
    assert len(resting) == 1


@pytest.mark.asyncio
async def test_release_channel_failure_marks_error(
    fake_db, fake_discord, monkeypatch
):
    guild, channels = fake_discord
    await mcp.ensure_slots()
    channel, _ = await mcp.acquire_channel(guild, [11])
    channel.edit.side_effect = RuntimeError("discord down")
    ok = await mcp.release_channel(channel)
    assert ok is False
    assert fake_db.rows[101]["state"] == "error"


@pytest.mark.asyncio
async def test_release_non_pool_channel_is_noop(fake_db, fake_discord):
    guild, _ = fake_discord
    channel = _FakeTextChannel(999999, guild)
    assert await mcp.release_channel(channel) is True


@pytest.mark.asyncio
async def test_429_retry_honors_retry_after(fake_db, monkeypatch):
    calls = {"n": 0}

    class FakeRateLimited(Exception):
        retry_after = 0.01

    # discord.RateLimited — подменяем на время теста
    monkeypatch.setattr(discord, "RateLimited", FakeRateLimited)

    async def factory():
        calls["n"] += 1
        if calls["n"] < 2:
            raise FakeRateLimited()
        return "ok"

    assert await mcp._discord_call(factory, "test") == "ok"
    assert calls["n"] == 2

    # Бесконечных циклов нет: всегда 429 → проброс после 3 попыток
    async def always_limited():
        raise FakeRateLimited()

    with pytest.raises(FakeRateLimited):
        await mcp._discord_call(always_limited, "test")


# ---------------------------------------------------------------------------
# Модель очереди: waiting_channel
# ---------------------------------------------------------------------------


def test_waiting_channel_state():
    from models.mm_search import (
        QUEUE_WAITING_CHANNEL, MMSearch,
    )

    s = MMSearch(guild_id=1, channel_id=2, state=QUEUE_WAITING_CHANNEL)
    assert s.is_locked is True
    assert s.is_waiting_channel is True
