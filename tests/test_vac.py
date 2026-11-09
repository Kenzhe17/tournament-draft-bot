"""Тесты VAC: парсер длительности и сервисная логика (in-memory store)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import vac_service


# ------------------------------------------------------------ duration parser

@pytest.mark.parametrize("text,expected", [
    ("30m", timedelta(minutes=30)),
    ("2h", timedelta(hours=2)),
    ("1d", timedelta(days=1)),
    ("7d", timedelta(days=7)),
    ("14d", timedelta(days=14)),
    ("30d", timedelta(days=30)),
    ("1d12h", timedelta(days=1, hours=12)),
    ("1d12h30m", timedelta(days=1, hours=12, minutes=30)),
    (" 7D ", timedelta(days=7)),
])
def test_parse_duration_valid(text, expected):
    assert vac_service.parse_duration(text) == expected


@pytest.mark.parametrize("text", [
    "", "abc", "0m", "0d", "-1h", "1w", "1.5h", "30", "m", "1d2x",
    "999d", "400d", "1d25h99m99x",
])
def test_parse_duration_invalid(text):
    assert vac_service.parse_duration(text) is None


def test_parse_duration_max_boundary():
    assert vac_service.parse_duration("365d") == timedelta(days=365)
    assert vac_service.parse_duration("366d") is None


# ------------------------------------------------------- in-memory VAC store

class FakeVacStore:
    """Имитация vac_store поверх списка записей."""

    def __init__(self):
        self.rows = []
        self._next_id = 1

    async def _archive_expired(self, guild_id):
        now = datetime.now(timezone.utc)
        n = 0
        for r in self.rows:
            if (r["guild_id"] == guild_id and r["revoked_at"] is None
                    and r["expired_at"] is None and r["expires_at"] <= now):
                r["expired_at"] = now
                n += 1
        return n

    async def get_active(self, guild_id, user_id):
        await self._archive_expired(guild_id)
        now = datetime.now(timezone.utc)
        for r in reversed(self.rows):
            if (r["guild_id"] == guild_id and r["user_id"] == user_id
                    and r["revoked_at"] is None and r["expired_at"] is None
                    and r["expires_at"] > now):
                return dict(r)
        return None

    async def issue(self, guild_id, user_id, issued_by, expires_at):
        await self._archive_expired(guild_id)
        # уникальность: нет активной/неархивированной записи
        now = datetime.now(timezone.utc)
        for r in self.rows:
            if (r["guild_id"] == guild_id and r["user_id"] == user_id
                    and r["revoked_at"] is None and r["expired_at"] is None
                    and r["expires_at"] > now):
                return None
        row = {
            "id": self._next_id, "guild_id": guild_id, "user_id": user_id,
            "issued_by": issued_by,
            "issued_at": datetime.now(timezone.utc),
            "expires_at": expires_at, "revoked_at": None,
            "revoked_by": None, "expired_at": None,
        }
        self._next_id += 1
        self.rows.append(row)
        return dict(row)

    async def revoke(self, guild_id, user_id, revoked_by):
        now = datetime.now(timezone.utc)
        for r in reversed(self.rows):
            if (r["guild_id"] == guild_id and r["user_id"] == user_id
                    and r["revoked_at"] is None and r["expired_at"] is None
                    and r["expires_at"] > now):
                r["revoked_at"] = now
                r["revoked_by"] = revoked_by
                return dict(r)
        return None

    async def list_active(self, guild_id):
        await self._archive_expired(guild_id)
        now = datetime.now(timezone.utc)
        return [
            dict(r) for r in self.rows
            if (r["guild_id"] == guild_id and r["revoked_at"] is None
                and r["expired_at"] is None and r["expires_at"] > now)
        ]


@pytest.fixture
def fake_store(monkeypatch):
    store = FakeVacStore()
    monkeypatch.setattr(vac_service, "vac_store", store)
    return store


@pytest.mark.asyncio
async def test_issue_and_active(fake_store):
    row, err = await vac_service.issue_vac(1, 42, 7, timedelta(minutes=30))
    assert err is None and row is not None
    assert await vac_service.is_vac_active(1, 42)


@pytest.mark.asyncio
async def test_duplicate_vac_rejected(fake_store):
    await vac_service.issue_vac(1, 42, 7, timedelta(days=7))
    row, err = await vac_service.issue_vac(1, 42, 8, timedelta(days=1))
    assert row is None and err == "already_active"
    # вторая запись не создана
    assert len(fake_store.rows) == 1


@pytest.mark.asyncio
async def test_unvac(fake_store):
    await vac_service.issue_vac(1, 42, 7, timedelta(hours=2))
    row = await vac_service.revoke_vac(1, 42, 9)
    assert row is not None
    assert not await vac_service.is_vac_active(1, 42)
    # повторный unvac — None (идемпотентно)
    assert await vac_service.revoke_vac(1, 42, 9) is None
    # история сохранена
    assert fake_store.rows[0]["revoked_by"] == 9


@pytest.mark.asyncio
async def test_expiry_allows_new_vac(fake_store):
    # «истёкший» VAC: expires_at в прошлом
    await fake_store.issue(1, 42, 7, datetime.now(timezone.utc) - timedelta(minutes=1))
    assert not await vac_service.is_vac_active(1, 42)
    # истёкшая запись заархивирована → новый VAC выдаётся
    row, err = await vac_service.issue_vac(1, 42, 7, timedelta(days=1))
    assert err is None and row is not None


@pytest.mark.asyncio
async def test_guild_isolation(fake_store):
    await vac_service.issue_vac(1, 42, 7, timedelta(days=1))
    assert await vac_service.is_vac_active(2, 42) is False
    assert await vac_service.is_vac_active(1, 43) is False


@pytest.mark.asyncio
async def test_vacs_list_only_active(fake_store):
    await vac_service.issue_vac(1, 1, 7, timedelta(days=1))
    await fake_store.issue(1, 2, 7, datetime.now(timezone.utc) - timedelta(minutes=5))
    await vac_service.issue_vac(1, 3, 7, timedelta(hours=1))
    await vac_service.revoke_vac(1, 3, 7)
    rows = await vac_service.list_active_vacs(1)
    assert [r["user_id"] for r in rows] == [1]


@pytest.mark.asyncio
async def test_concurrent_issue(fake_store):
    import asyncio
    results = await asyncio.gather(
        vac_service.issue_vac(1, 42, 7, timedelta(days=1)),
        vac_service.issue_vac(1, 42, 8, timedelta(days=2)),
    )
    successes = [r for r, e in results if r is not None]
    assert len(successes) == 1
    assert len(fake_store.rows) == 1
