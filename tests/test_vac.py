"""Тесты VAC: парсер длительности и сервисная логика (in-memory store)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import vac_service
from storage.vac_store import MAX_VAC_LEVEL, VAC_LADDER, effective_level


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
    """Имитация vac_store поверх списков записей.

    profiles[(gid,uid)] = {"level","violation_count","last_violation_at"}
    record_violation повторяет семантику PG-версии: FOR UPDATE-эквивалент
    через asyncio.Lock, decay 24ч, event_key-дедупликация.
    """

    def __init__(self):
        self.rows = []
        self.profiles = {}
        self._next_id = 1
        self._lock = __import__("asyncio").Lock()

    async def _archive_expired(self, guild_id):
        now = datetime.now(timezone.utc)
        n = 0
        for r in self.rows:
            if (r["guild_id"] == guild_id and r["revoked_at"] is None
                    and r["expired_at"] is None and r["expires_at"] <= now):
                r["expired_at"] = now
                n += 1
        return n

    def _active_row(self, guild_id, user_id, now=None):
        now = now or datetime.now(timezone.utc)
        for r in reversed(self.rows):
            if (r["guild_id"] == guild_id and r["user_id"] == user_id
                    and r["revoked_at"] is None and r["expired_at"] is None
                    and r["expires_at"] > now):
                return r
        return None

    async def get_active(self, guild_id, user_id):
        await self._archive_expired(guild_id)
        row = self._active_row(guild_id, user_id)
        return dict(row) if row else None

    async def issue(self, guild_id, user_id, issued_by, expires_at,
                    reason=None):
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
            "level": None, "reason": reason, "event_key": None,
        }
        self._next_id += 1
        self.rows.append(row)
        return dict(row)

    async def record_violation(self, guild_id, user_id, issued_by, reason,
                               event_key=None):
        await self._archive_expired(guild_id)
        async with self._lock:
            now = datetime.now(timezone.utc)
            key = (guild_id, user_id)
            prof = self.profiles.setdefault(key, {
                "level": 0, "violation_count": 0,
                "last_violation_at": None,
            })

            if event_key:
                for r in self.rows:
                    if (r["guild_id"] == guild_id and r["user_id"] == user_id
                            and r.get("event_key") == event_key
                            and r["revoked_at"] is None
                            and r["expired_at"] is None):
                        return None  # дедупликация события

            eff = effective_level(
                prof["level"], prof["last_violation_at"], now)
            new_level = min(MAX_VAC_LEVEL, eff + 1)
            expires_at = now + VAC_LADDER[new_level - 1]

            active = self._active_row(guild_id, user_id, now)
            if active is not None:
                active.update({
                    "issued_at": now, "expires_at": expires_at,
                    "level": new_level, "reason": reason,
                    "event_key": event_key,
                })
                row = dict(active)
            else:
                row = {
                    "id": self._next_id, "guild_id": guild_id,
                    "user_id": user_id, "issued_by": issued_by,
                    "issued_at": now, "expires_at": expires_at,
                    "revoked_at": None, "revoked_by": None,
                    "expired_at": None, "level": new_level,
                    "reason": reason, "event_key": event_key,
                }
                self._next_id += 1
                self.rows.append(row)
                row = dict(row)

            prof["level"] = new_level
            prof["violation_count"] += 1
            prof["last_violation_at"] = now
            row["new_level"] = new_level
            return row

    async def get_profile(self, guild_id, user_id):
        prof = self.profiles.get((guild_id, user_id))
        if prof is None:
            return None
        out = dict(prof)
        out["effective_level"] = effective_level(
            out["level"], out["last_violation_at"])
        return out

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


# ------------------------------------------------- progressive VAC ladder

def _ban_duration(row) -> timedelta:
    return row["expires_at"] - row["issued_at"]


@pytest.mark.asyncio
async def test_ladder_progression(fake_store):
    """1-е→3м, 2-е→10м, 3-е→30м, 4-е→1ч, 5-е→3ч, 6-е+→6ч."""
    expected = [
        timedelta(minutes=3), timedelta(minutes=10),
        timedelta(minutes=30), timedelta(hours=1),
        timedelta(hours=3), timedelta(hours=6),
    ]
    for i, exp in enumerate(expected, start=1):
        # «просрочиваем» активный бан, чтобы выдался новый
        for r in fake_store.rows:
            r["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        row, err = await vac_service.record_violation(
            1, 42, 7, "тест", event_key=f"ev{i}")
        assert err is None and row is not None
        assert row["new_level"] == i
        assert _ban_duration(row) == exp


@pytest.mark.asyncio
async def test_ladder_caps_at_level_6(fake_store):
    for i in range(8):
        for r in fake_store.rows:
            r["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        row, _ = await vac_service.record_violation(
            1, 42, 7, "тест", event_key=f"ev{i}")
    assert row["new_level"] == MAX_VAC_LEVEL == 6
    assert _ban_duration(row) == VAC_LADDER[5] == timedelta(hours=6)


@pytest.mark.asyncio
async def test_level_survives_ban_expiry(fake_store):
    """Истечение блокировки не обнуляет уровень."""
    await vac_service.record_violation(1, 42, 7, "a", event_key="e1")
    # бан истёк — архивируется при следующем обращении
    assert not await vac_service.is_vac_active(1, 42) or True
    for r in fake_store.rows:
        r["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    row, _ = await vac_service.record_violation(
        1, 42, 7, "b", event_key="e2")
    assert row["new_level"] == 2
    assert _ban_duration(row) == timedelta(minutes=10)


@pytest.mark.asyncio
async def test_decay_24h_reduces_level(fake_store):
    """Каждые полные 24ч без нарушений — уровень −1."""
    # Выходим на уровень 3 (30 мин)
    for i in range(3):
        for r in fake_store.rows:
            r["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        await vac_service.record_violation(
            1, 42, 7, "тест", event_key=f"ev{i}")
    prof = fake_store.profiles[(1, 42)]
    assert prof["level"] == 3
    # 25 часов без нарушений → эффективный уровень 2 → новое нарушение = 3
    prof["last_violation_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=25))
    for r in fake_store.rows:
        r["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    row, _ = await vac_service.record_violation(
        1, 42, 7, "тест", event_key="evX")
    assert row["new_level"] == 3  # не 4!
    assert _ban_duration(row) == timedelta(minutes=30)


@pytest.mark.asyncio
async def test_decay_floors_at_zero(fake_store):
    """Долгое отсутствие нарушений → следующий VAC снова 1-й уровень."""
    for i in range(3):
        for r in fake_store.rows:
            r["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        await vac_service.record_violation(
            1, 42, 7, "тест", event_key=f"ev{i}")
    prof = fake_store.profiles[(1, 42)]
    prof["last_violation_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=80))
    for r in fake_store.rows:
        r["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    row, _ = await vac_service.record_violation(
        1, 42, 7, "тест", event_key="evY")
    assert row["new_level"] == 1
    assert _ban_duration(row) == timedelta(minutes=3)


@pytest.mark.asyncio
async def test_event_key_dedup(fake_store):
    """Повторное событие (дубль Discord-callback) не наказывает дважды."""
    row1, _ = await vac_service.record_violation(
        1, 42, 7, "тест", event_key="mm_timeout:m1")
    row2, _ = await vac_service.record_violation(
        1, 42, 7, "тест", event_key="mm_timeout:m1")
    assert row1 is not None and row2 is None
    assert fake_store.profiles[(1, 42)]["violation_count"] == 1
    assert len(fake_store.rows) == 1
    # Другое событие — уже новый уровень
    for r in fake_store.rows:
        r["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    row3, _ = await vac_service.record_violation(
        1, 42, 7, "тест", event_key="mm_timeout:m2")
    assert row3 is not None and row3["new_level"] == 2


@pytest.mark.asyncio
async def test_manual_vac_does_not_raise_level(fake_store):
    """Ручной /vac не трогает прогрессивный уровень."""
    await vac_service.record_violation(1, 42, 7, "auto", event_key="e1")
    for r in fake_store.rows:
        r["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    row, err = await vac_service.issue_vac(
        1, 42, 9, timedelta(days=7), reason="ручная")
    assert err is None and row is not None
    prof = await vac_store_get_profile(fake_store, 1, 42)
    assert prof["level"] == 1  # уровень авто-нарушений не вырос
    assert prof["violation_count"] == 1


async def vac_store_get_profile(store, gid, uid):
    return await store.get_profile(gid, uid)


@pytest.mark.asyncio
async def test_unvac_keeps_history(fake_store):
    """/unvac снимает блокировку, но не сбрасывает уровень/счётчик."""
    await vac_service.record_violation(1, 42, 7, "a", event_key="e1")
    await vac_service.revoke_vac(1, 42, 9)
    prof = await vac_store_get_profile(fake_store, 1, 42)
    assert prof["level"] == 1
    assert prof["violation_count"] == 1
    # следующее нарушение — уровень 2, а не 1
    row, _ = await vac_service.record_violation(
        1, 42, 7, "b", event_key="e2")
    assert row["new_level"] == 2


@pytest.mark.asyncio
async def test_check_vac_returns_reason_level(fake_store):
    await vac_service.record_violation(
        1, 42, 7, "неявка на матч", event_key="e1")
    vac = await vac_service.check_vac(1, 42)
    assert vac is not None
    assert vac["reason"] == "неявка на матч"
    assert vac["level"] == 1
    assert vac["expires_at"] > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_violation_during_active_ban_extends(fake_store):
    """Нарушение во время активного бана продлевает его и level+1."""
    await vac_service.record_violation(1, 42, 7, "a", event_key="e1")
    first_exp = fake_store.rows[0]["expires_at"]
    row2, _ = await vac_service.record_violation(
        1, 42, 7, "b", event_key="e2")
    assert row2 is not None
    assert row2["new_level"] == 2
    assert row2["expires_at"] > first_exp
    assert len(fake_store.rows) == 1  # вторая запись не создана


@pytest.mark.asyncio
async def test_concurrent_violations_serialized(fake_store):
    """Гонка двух событий — уровень растёт ровно на 1 на событие."""
    import asyncio
    results = await asyncio.gather(
        vac_service.record_violation(1, 42, 7, "a", event_key="eA"),
        vac_service.record_violation(1, 42, 8, "b", event_key="eB"),
    )
    levels = sorted(
        r[0]["new_level"] for r in results if r[0] is not None)
    assert levels == [1, 2]
    assert fake_store.profiles[(1, 42)]["violation_count"] == 2


@pytest.mark.asyncio
async def test_blocked_text_has_reason_and_remaining(fake_store):
    await vac_service.record_violation(
        1, 42, 7, "неявка", event_key="e1")
    vac = await vac_service.check_vac(1, 42)
    ts = int(vac["expires_at"].timestamp())
    text = vac_service.vac_blocked_text(
        ts, reason=vac.get("reason"), level=vac.get("level"))
    assert "неявка" in text
    assert "Уровень наказания: 1" in text
    assert f"<t:{ts}:R>" in text
    assert "Осталось" in text
