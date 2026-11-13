"""Тесты применения прогрессивного VAC в режимах r!z3.

- MM Ready Check: авто-нарушения идут через record_violation
  (лестница, а не фиксированные 10 минут) + event_key-дедупликация.
- Ultimatum: _vac_check покрывает solo и duo (оба игрока),
  текст содержит причину, окончание и оставшееся время.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import vac_service
from tests.test_vac import FakeVacStore


@pytest.fixture
def fake_store(monkeypatch):
    store = FakeVacStore()
    monkeypatch.setattr(vac_service, "vac_store", store)
    return store


# ------------------------------------------------------------- MM auto-VAC

@pytest.mark.asyncio
async def test_mm_vac_uses_progressive_ladder(fake_store):
    """MM no-show: первый → 3 мин, второй → 10 мин (не фикс. 10 мин)."""
    from cogs.mm import MMCog

    bot = SimpleNamespace(user=SimpleNamespace(id=1))
    cog = MMCog(bot)

    await cog._issue_mm_vacs(1, [42], "не подтвердил готовность",
                             event_key="mm_timeout:m1")
    row = fake_store.rows[-1]
    assert row["level"] == 1
    assert row["expires_at"] - row["issued_at"] == timedelta(minutes=3)

    # Следующий матч — уровень 2 (10 мин)
    for r in fake_store.rows:
        r["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    await cog._issue_mm_vacs(1, [42], "отказ от матча",
                             event_key="mm_decline:m2")
    row = fake_store.rows[-1]
    assert row["level"] == 2
    assert row["expires_at"] - row["issued_at"] == timedelta(minutes=10)


@pytest.mark.asyncio
async def test_mm_vac_event_key_no_double_punish(fake_store):
    """Повторный вызов с тем же event_key не создаёт второй бан."""
    from cogs.mm import MMCog

    cog = MMCog(SimpleNamespace(user=SimpleNamespace(id=1)))
    await cog._issue_mm_vacs(1, [42, 43], "таймаут",
                             event_key="mm_timeout:m9")
    await cog._issue_mm_vacs(1, [42, 43], "таймаут",
                             event_key="mm_timeout:m9")
    assert fake_store.profiles[(1, 42)]["violation_count"] == 1
    assert fake_store.profiles[(1, 43)]["violation_count"] == 1
    assert len(fake_store.rows) == 2


# -------------------------------------------------------- Ultimatum _vac_check

try:
    from cogs.ultimatum import _vac_check
except ImportError:  # старое дерево без Ultimatum 2.0
    _vac_check = None


def _require_vac_check():
    if _vac_check is None:
        pytest.skip("_vac_check отсутствует в этой версии cogs/ultimatum")
    return _vac_check


@pytest.mark.asyncio
async def test_ult_vac_check_solo(fake_store):
    vac_check = _require_vac_check()

    await vac_service.record_violation(1, 42, 7, "неявка", event_key="e1")
    vac = await vac_service.check_vac(1, 42)
    ts = int(vac["expires_at"].timestamp())

    text = await vac_check(1, 42)
    assert text is not None
    assert "неявка" in text
    assert f"<t:{ts}:R>" in text
    # чистый пользователь — None
    assert await vac_check(1, 999) is None


@pytest.mark.asyncio
async def test_ult_vac_check_duo_both_players(fake_store):
    """Duo: VAC любого из двух блокирует, в тексте — кто именно."""
    vac_check = _require_vac_check()

    await vac_service.record_violation(1, 42, 7, "срыв", event_key="e1")
    # второй чист — блокирует первый, упоминание есть
    text = await vac_check(1, 42, 555)
    assert text is not None and "<@42>" in text
    # оба чисты — None
    assert await vac_check(1, 555, 556) is None


# ------------------------------------------------------------- service API

@pytest.mark.asyncio
async def test_check_vac_fail_open_on_store_error(monkeypatch):
    """Сбой PG не должен ломать регистрацию — check_vac → None."""
    class Boom:
        async def get_active(self, *a):
            raise RuntimeError("pg down")
    monkeypatch.setattr(vac_service, "vac_store", Boom())
    assert await vac_service.check_vac(1, 42) is None
    assert await vac_service.is_vac_active(1, 42) is False
