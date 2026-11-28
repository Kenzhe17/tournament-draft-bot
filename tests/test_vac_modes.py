"""Тесты применения прогрессивного VAC в режимах r!z3.

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
