"""Тесты Ranked Queue: состояния, блокировка, канал матча, leave/join.

Запуск: pytest tests/test_mm_queue.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.mm_search import (
    MMFormat,
    MMSearch,
    QUEUE_LOCKED_READY_CHECK,
    QUEUE_OPEN,
)
from storage import mm_search_store as mms_mod
from storage.mm_search_store import MMSearchStore


# ---------------------------------------------------------------- helpers

def _store(tmp_path, monkeypatch) -> MMSearchStore:
    monkeypatch.setattr(mms_mod, "MM_SEARCH_FILE", tmp_path / "mm.json")
    monkeypatch.setattr(mms_mod, "DATABASE_URL", "")
    return MMSearchStore()


def _search(n: int = 8, locked: bool = False) -> MMSearch:
    s = MMSearch(guild_id=1, channel_id=10, format=MMFormat.SQUAD)
    for i in range(n):
        s.add_player(f"P{i}", 1000 + i)
    if locked:
        s.state = QUEUE_LOCKED_READY_CHECK
        s.match_num = 42
    return s


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


class _FakeFollowup:
    async def send(self, *a, **kw):
        pass


class _FakeInteraction:
    def __init__(self, user_id: int, guild_id: int = 1, channel_id: int = 10):
        self.user = SimpleNamespace(id=user_id, display_name=f"U{user_id}")
        self.user.primary_guild = None
        self.guild = SimpleNamespace(id=guild_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.message = SimpleNamespace(edit=AsyncMock())


def _cog(monkeypatch, search: MMSearch | None):
    import cogs.mm as mm_mod

    class _Store:
        def __init__(self, s):
            self.s = s

        def get(self, guild_id, channel_id):
            return self.s

        def set(self, s):
            pass

        def get_by_guild(self, guild_id):
            return [self.s] if self.s else []

        def all(self):
            return [self.s] if self.s else []

    monkeypatch.setattr(mm_mod, "mm_search_store", _Store(search))
    bot = SimpleNamespace(mm_cog=None)
    cog = mm_mod.MMCog(bot)
    return cog


# ---------------------------------------------------------------- model

def test_state_roundtrip_and_legacy_default():
    s = _search(locked=True)
    s2 = MMSearch.from_dict(s.to_dict())
    assert s2.is_locked and s2.match_num == 42

    legacy = s.to_dict()
    legacy.pop("state")
    legacy.pop("match_num")
    s3 = MMSearch.from_dict(legacy)
    assert s3.state == QUEUE_OPEN and s3.match_num == 0 and not s3.is_locked


def test_clear_resets_state():
    s = _search(locked=True)
    s.clear()
    assert s.state == QUEUE_OPEN and s.match_num == 0


# ---------------------------------------------------------------- store

def test_store_persists_queue_state(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    s = _search(locked=True)
    store.set(s)

    store2 = MMSearchStore()
    got = store2.get(1, 10)
    assert got is not None
    assert got.is_locked and got.match_num == 42
    assert len(got.players) == 8


# ---------------------------------------------------------------- handlers

@pytest.mark.asyncio
async def test_leave_rejected_when_locked(tmp_path, monkeypatch):
    s = _search(locked=True)
    cog = _cog(monkeypatch, s)
    inter = _FakeInteraction(1000)

    await cog.handle_queue_leave(inter, 1, 10)

    assert "проверка готовности" in inter.response.sent[0][0][0].lower()
    assert len(s.players) == 8  # никого не выгнали


@pytest.mark.asyncio
async def test_join_rejected_when_locked(tmp_path, monkeypatch):
    s = _search(locked=True)
    cog = _cog(monkeypatch, s)
    inter = _FakeInteraction(2000)

    await cog.handle_queue_join(inter, 1, 10)

    assert "закрыт" in inter.response.sent[0][0][0].lower()
    assert len(s.players) == 8


@pytest.mark.asyncio
async def test_leave_open_queue_removes_player(tmp_path, monkeypatch):
    s = _search(n=3)
    cog = _cog(monkeypatch, s)
    inter = _FakeInteraction(1001)

    await cog.handle_queue_leave(inter, 1, 10)

    assert inter.response.edited == 1
    assert "P1" not in s.players
    assert len(s.players) == 2


# ---------------------------------------------------------------- begin_match

@pytest.mark.asyncio
async def test_begin_match_channel_failure_reverts_to_open(monkeypatch):
    import cogs.mm as mm_mod

    s = _search()
    cog = _cog(monkeypatch, s)
    monkeypatch.setattr(cog, "_update_queue_panel", AsyncMock())
    monkeypatch.setattr(
        cog, "_create_match_channel", AsyncMock(return_value=(None, "err"))
    )
    start_rc = AsyncMock()
    monkeypatch.setattr(cog, "_start_ready_check", start_rc)

    mns = SimpleNamespace(get_or_assign=AsyncMock(return_value=42))
    monkeypatch.setattr(mm_mod, "match_number_store", mns)

    inter = _FakeInteraction(1000)
    rc, err = await cog._begin_match(s, inter)

    assert rc is None and err
    assert s.state == QUEUE_OPEN and s.match_num == 0
    start_rc.assert_not_called()


@pytest.mark.asyncio
async def test_begin_match_success_locks_and_passes_channel(monkeypatch):
    import cogs.mm as mm_mod

    s = _search()
    cog = _cog(monkeypatch, s)
    monkeypatch.setattr(cog, "_update_queue_panel", AsyncMock())
    fake_channel = SimpleNamespace(id=555)
    monkeypatch.setattr(
        cog, "_create_match_channel", AsyncMock(return_value=(fake_channel, None))
    )
    fake_rc = SimpleNamespace(match_id="rc_x")
    start_rc = AsyncMock(return_value=fake_rc)
    monkeypatch.setattr(cog, "_start_ready_check", start_rc)

    mns = SimpleNamespace(get_or_assign=AsyncMock(return_value=42))
    monkeypatch.setattr(mm_mod, "match_number_store", mns)

    inter = _FakeInteraction(1000)
    rc, err = await cog._begin_match(s, inter)

    assert rc is fake_rc and err is None
    assert s.state == QUEUE_LOCKED_READY_CHECK and s.match_num == 42
    kwargs = start_rc.call_args.kwargs
    assert kwargs["match_num"] == 42
    assert kwargs["match_channel_id"] == 555
