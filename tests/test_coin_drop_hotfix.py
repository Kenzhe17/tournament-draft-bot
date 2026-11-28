"""Regression-тесты coin_drop: durable drop rewards (legacy
путь drop_coin_rewards), pending-файл, протокол фиксации.

View-тесты старых SpeedDropView/RaffleDropView удалены вместе с
классами — события 2.0 покрыты tests/test_events2_*.py.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cogs import coin_drop
from storage.drop_pending_store import DropPendingStore


def _fake_cog():
    """Минимальный CoinDropCog для grant-протокола."""
    return SimpleNamespace(
        _settle_pending_item=AsyncMock(return_value="queued"),
        _process_drop_reward=AsyncMock(return_value=True),
        _settle_event_pending=AsyncMock(return_value="queued"),
        _process_event_reward=AsyncMock(return_value=True),
    )


@pytest.fixture
def pending_file(tmp_path, monkeypatch):
    """Реальный DropPendingStore на tmp-файле."""
    store = DropPendingStore(tmp_path / "pending.json")
    monkeypatch.setattr(coin_drop, "drop_pending_store", store)
    return store


@pytest.mark.asyncio
async def test_grant_drop_reward_first_try_success():
    """PG отвечает сразу — обязательство записано, claim взят,
    монеты зачислены, статус completed."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    row = {"grant_key": "speeddrop:e1", "status": "pending",
           "guild_id": 1, "user_id": 42, "amount": 300, "attempts": 1}
    with patch.object(coin_drop.drop_reward_store, "record",
                      new=AsyncMock(return_value=row)), \
         patch.object(coin_drop.drop_reward_store, "claim_key",
                      new=AsyncMock(return_value=row)), \
         patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=AsyncMock(return_value=300)) as m, \
         patch.object(coin_drop.drop_reward_store, "complete",
                      new=AsyncMock(return_value=True)) as done, \
         patch.object(coin_drop.drop_reward_store, "retry",
                      new=AsyncMock()):
        res = await coin_drop.grant_drop_reward(
            cog, 1, 42, 300, "speeddrop:e1", "speed")
    assert res == "credited"
    m.assert_awaited_once_with(1, 42, 300, "speeddrop:e1")
    done.assert_awaited_once()


@pytest.mark.asyncio
async def test_grant_drop_reward_other_user_rejected():
    """Повторный grant_key с ДРУГИМ user_id → "other", без
    повторного начисления."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    row = {"grant_key": "speeddrop:e1", "status": "pending",
           "user_id": 42, "guild_id": 1, "amount": 300}
    with patch.object(coin_drop.drop_reward_store, "record",
                      new=AsyncMock(return_value=row)), \
         patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=AsyncMock()) as m:
        res = await coin_drop.grant_drop_reward(
            cog, 1, 99, 300, "speeddrop:e1", "speed")
    assert res == "other"
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_drop_reward_already_completed():
    """Повторный grant по завершённому ключу — "credited"
    без повторного начисления."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    row = {"grant_key": "speeddrop:e1", "status": "completed",
           "user_id": 42, "guild_id": 1, "amount": 300}
    with patch.object(coin_drop.drop_reward_store, "record",
                      new=AsyncMock(return_value=row)), \
         patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=AsyncMock()) as m:
        res = await coin_drop.grant_drop_reward(
            cog, 1, 42, 300, "speeddrop:e1", "speed")
    assert res == "credited"
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_drop_reward_pg_down_uses_pending_file(pending_file):
    """PG недоступен ДО фиксации: обязательство уходит в
    durable pending-файл (переживает рестарт), возвращается
    "queued"."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    with patch.object(coin_drop.drop_reward_store, "record",
                      new=AsyncMock(side_effect=RuntimeError("pg down"))):
        res = await coin_drop.grant_drop_reward(
            cog, 1, 42, 300, "speeddrop:e-down", "speed")
    assert res == "queued"
    assert len(pending_file) == 1
    item = pending_file.all()[0]
    assert item["grant_key"] == "speeddrop:e-down"
    assert item["user_id"] == 42 and item["amount"] == 300


@pytest.mark.asyncio
async def test_pending_file_survives_restart(pending_file):
    """«Перезапуск»: новый инстанс store на том же файле видит
    обязательство; worker переносит его в PG и начисляет."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    with patch.object(coin_drop.drop_reward_store, "record",
                      new=AsyncMock(side_effect=RuntimeError("pg down"))):
        await coin_drop.grant_drop_reward(
            cog, 1, 77, 500, "raffledrop:e9", "raffle")
    assert len(pending_file) == 1

    restarted = DropPendingStore(pending_file._path)
    assert len(restarted.all()) == 1

    row = {"grant_key": "raffledrop:e9", "status": "pending",
           "guild_id": 1, "user_id": 77, "amount": 500, "attempts": 1}
    with patch.object(coin_drop.drop_reward_store, "record",
                      new=AsyncMock(return_value=row)), \
         patch.object(coin_drop.drop_reward_store, "claim_key",
                      new=AsyncMock(return_value=row)), \
         patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=AsyncMock(return_value=500)) as m, \
         patch.object(coin_drop.drop_reward_store, "complete",
                      new=AsyncMock(return_value=True)):
        res = await cog._settle_pending_item(restarted.all()[0])
    assert res == "credited"
    assert len(pending_file) == 0
    m.assert_awaited_once_with(1, 77, 500, "raffledrop:e9")


@pytest.mark.asyncio
async def test_grant_total_failure_returns_failed():
    """Ни PG, ни pending-файл не принимают обязательство →
    "failed": победителя объявлять нельзя."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    bad = DropPendingStore.__new__(DropPendingStore)
    bad._path = None
    bad._items = {}
    bad._loaded = True
    bad._lock = __import__("threading").Lock()
    bad.add = lambda item: (_ for _ in ()).throw(OSError("disk full"))
    with patch.object(coin_drop.drop_reward_store, "record",
                      new=AsyncMock(side_effect=RuntimeError("pg down"))), \
         patch.object(coin_drop, "drop_pending_store", bad):
        res = await coin_drop.grant_drop_reward(
            cog, 1, 42, 300, "speeddrop:e-total", "speed")
    assert res == "failed"


@pytest.mark.asyncio
async def test_process_drop_reward_failure_sets_backoff_retry():
    """PG лёг после записи pending: fenced retry() возвращает
    строку в pending с backoff."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    row = {"grant_key": "speeddrop:e3", "status": "processing",
           "guild_id": 1, "user_id": 8, "amount": 100, "attempts": 2}
    with patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=AsyncMock(side_effect=RuntimeError("pg timeout"))), \
         patch.object(coin_drop.drop_reward_store, "retry",
                      new=AsyncMock(return_value=True)) as retry, \
         patch.object(coin_drop.drop_reward_store, "complete",
                      new=AsyncMock()) as done:
        ok = await cog._process_drop_reward(row)
    assert ok is False
    retry.assert_awaited_once()
    args = retry.await_args.args
    assert args[0] == "speeddrop:e3"
    assert args[1] == 2
    assert 0 < args[3] <= coin_drop._DROP_BACKOFF_CAP
    done.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_drop_reward_lost_complete_is_idempotent():
    """Commit начисления прошёл, но complete() потерян: награда
    уже на балансе, повторная обработка по lease-дедупликации
    не начислит второй раз."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    row = {"grant_key": "speeddrop:e4", "status": "processing",
           "guild_id": 1, "user_id": 9, "amount": 200, "attempts": 1}
    with patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=AsyncMock(return_value=200)), \
         patch.object(coin_drop.drop_reward_store, "complete",
                      new=AsyncMock(side_effect=RuntimeError("lost"))):
        ok = await cog._process_drop_reward(row)
    assert ok is True


@pytest.mark.asyncio
async def test_trigger_drop_disabled_flag_blocks():
    """feature.coin_drop_enabled=False → trigger_drop не создаёт
    событие (ручной вызов тоже уважает feature flag)."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    channel = SimpleNamespace(id=555, guild=SimpleNamespace(id=1))
    with patch("services.event_config.feature_enabled",
               return_value=False):
        status = await cog.trigger_drop(channel)
    assert status == "disabled"


@pytest.mark.asyncio
async def test_trigger_drop_active_guard_blocks_forcedrop():
    """forcedrop НЕ обходит guard активного события."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    fake_session = SimpleNamespace(finished=False)
    cog._by_channel[(1, 555)] = "evt12345"
    cog._sessions["evt12345"] = fake_session
    channel = SimpleNamespace(id=555, guild=SimpleNamespace(id=1))
    with patch("services.event_config.feature_enabled",
               return_value=True), \
         patch("services.event_config.event_settings",
               return_value={}):
        status = await cog.trigger_drop(channel)
    assert status == "active"


@pytest.mark.asyncio
async def test_corrupt_pending_file_preserved_not_overwritten(tmp_path):
    """Битый pending JSON не затирается пустым состоянием:
    оригинал сохраняется как .corrupt-*, CRITICAL в лог."""
    import logging
    path = tmp_path / "pending.json"
    path.write_text("{broken json!!!", encoding="utf-8")
    store = DropPendingStore(path)
    with patch.object(logging.getLogger("storage.drop_pending_store"),
                      "critical") as crit:
        item = store.add({"grant_key": "k1", "kind": "speed",
                          "guild_id": 1, "user_id": 1, "amount": 10})
    assert item["grant_key"] == "k1"
    corrupts = list(tmp_path.glob("pending.json.corrupt-*"))
    assert len(corrupts) == 1
    assert corrupts[0].read_text(encoding="utf-8") == "{broken json!!!"
    crit.assert_called()
    assert store.get("k1")["user_id"] == 1
