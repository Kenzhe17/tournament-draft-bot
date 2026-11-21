"""Regression-тесты production hotfix coin_drop (NameError, double-claim,
parallel trigger, durable drop rewards)."""
import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cogs import coin_drop


class _Resp:
    def __init__(self):
        self._done = False
        self.messages = []

    def is_done(self):
        return self._done

    async def defer(self, **kw):
        self._done = True

    async def send_message(self, msg, **kw):
        self.messages.append(msg)
        self._done = True


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, msg=None, **kw):
        self.sent.append((msg, kw))


def _fake_cog():
    """Минимальный CoinDropCog для view-тестов: mem-очередь +
    process-заглушка (реальная логика — в PG-тестах)."""
    return SimpleNamespace(
        _mem_pending=deque(),
        _process_drop_reward=AsyncMock(return_value=True),
    )


def _interaction(uid=42, gid=1, cog=None):
    cog = cog if cog is not None else _fake_cog()
    return SimpleNamespace(
        guild_id=gid, user=SimpleNamespace(id=uid, mention=f"<@{uid}>"),
        response=_Resp(), followup=_Followup(),
        client=SimpleNamespace(get_cog=lambda name: cog),
        original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        edit_original_response=AsyncMock(),
    )


def _speed_btn(view):
    return next(c for c in view.children
                if getattr(c, "custom_id", "").startswith("speed_coin_drop"))


@pytest.mark.asyncio
async def test_trigger_drop_uses_channel_guild_no_nameerror():
    """Production NameError: trigger_drop ссылался на несуществующий
    `message` — теперь берёт guild из channel."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    channel = SimpleNamespace(id=555, guild=SimpleNamespace(id=1),
                              send=AsyncMock())
    with patch("services.settings_service.settings_service") as svc:
        svc.peek.return_value = False  # ивенты выключены — выход до отправки
        await cog.trigger_drop(channel)
    svc.peek.assert_called_once_with(1, "feature.coin_drop_enabled")
    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_speed_drop_single_claim_under_race():
    """Два быстрых клика разных пользователей не должны дать
    двойную выплату."""
    view = coin_drop.SpeedDropView(300, "evt1")
    btn = _speed_btn(view)
    i1, i2 = _interaction(1), _interaction(2)
    with patch("cogs.coin_drop.grant_drop_reward",
               new=AsyncMock(return_value=True)) as grant:
        await asyncio.gather(btn.callback(i1), btn.callback(i2))
    assert grant.await_count == 1
    # Проигравший получил ephemeral-ответ «кто-то быстрее».
    assert i2.followup.sent or i2.response.messages


@pytest.mark.asyncio
async def test_speed_drop_same_user_double_click():
    """Повторный клик того же пользователя — вторая попытка
    отклоняется флагом claimed до любого обращения к PG."""
    view = coin_drop.SpeedDropView(300, "evt-same")
    btn = _speed_btn(view)
    i1, i2 = _interaction(5), _interaction(5)
    with patch("cogs.coin_drop.grant_drop_reward",
               new=AsyncMock(return_value=True)) as grant:
        await btn.callback(i1)
        await btn.callback(i2)
    assert grant.await_count == 1
    assert "быстрее" in (i2.response.messages[-1]
                         if i2.response.messages else "")


@pytest.mark.asyncio
async def test_grant_drop_reward_first_try_success():
    """PG отвечает сразу — обязательство записано, монеты
    зачислены, статус completed."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    row = {"grant_key": "speeddrop:e1", "status": "pending",
           "guild_id": 1, "user_id": 42, "amount": 300, "attempts": 1}
    with patch.object(coin_drop.drop_reward_store, "record",
                      new=AsyncMock(return_value=row)), \
         patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=AsyncMock(return_value=300)) as m, \
         patch.object(coin_drop.drop_reward_store, "complete",
                      new=AsyncMock()) as done, \
         patch.object(coin_drop.drop_reward_store, "retry",
                      new=AsyncMock()) as retry:
        ok = await coin_drop.grant_drop_reward(
            cog, 1, 42, 300, "speeddrop:e1", "speed")
    assert ok is True
    m.assert_awaited_once_with(1, 42, 300, "speeddrop:e1")
    done.assert_awaited_once_with("speeddrop:e1")
    retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_drop_reward_already_completed():
    """Повторный grant по завершённому ключу — без повторного
    начисления."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    row = {"grant_key": "speeddrop:e1", "status": "completed"}
    with patch.object(coin_drop.drop_reward_store, "record",
                      new=AsyncMock(return_value=row)), \
         patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=AsyncMock()) as m:
        ok = await coin_drop.grant_drop_reward(
            cog, 1, 42, 300, "speeddrop:e1", "speed")
    assert ok is True
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_drop_reward_pg_down_before_record():
    """PG недоступен ДО фиксации победителя: обязательство уходит
    в in-memory очередь, награда не объявляется гарантированной,
    но не теряется — worker дозапишет после восстановления PG."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    with patch.object(coin_drop.drop_reward_store, "record",
                      new=AsyncMock(side_effect=RuntimeError("pg down"))), \
         patch.object(coin_drop, "logger") as log:
        ok = await coin_drop.grant_drop_reward(
            cog, 1, 42, 300, "speeddrop:e-down", "speed")
    assert ok is False
    assert len(cog._mem_pending) == 1
    item = cog._mem_pending[0]
    assert item["grant_key"] == "speeddrop:e-down"
    assert item["user_id"] == 42 and item["amount"] == 300
    log.critical.assert_called()  # честный CRITICAL, не silent


@pytest.mark.asyncio
async def test_drain_mem_pending_recovers_after_pg_back():
    """PG восстановился: worker переносит mem-очередь в
    drop_coin_rewards и начисляет ровно один раз."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    cog._mem_pending.append({
        "grant_key": "raffledrop:e9", "kind": "raffle",
        "guild_id": 1, "user_id": 77, "amount": 500,
    })
    row = {"grant_key": "raffledrop:e9", "status": "pending",
           "guild_id": 1, "user_id": 77, "amount": 500, "attempts": 1}
    with patch.object(coin_drop.drop_reward_store, "record",
                      new=AsyncMock(return_value=row)) as rec, \
         patch.object(coin_drop.drop_reward_store, "claim_key",
                      new=AsyncMock(return_value=row)), \
         patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=AsyncMock(return_value=500)) as m, \
         patch.object(coin_drop.drop_reward_store, "complete",
                      new=AsyncMock()):
        await cog._drain_mem_pending()
    assert len(cog._mem_pending) == 0
    rec.assert_awaited_once()
    m.assert_awaited_once_with(1, 77, 500, "raffledrop:e9")


@pytest.mark.asyncio
async def test_process_drop_reward_failure_sets_backoff_retry():
    """PG лёг после записи pending: retry() возвращает строку
    в pending с backoff, двойного начисления нет."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    row = {"grant_key": "speeddrop:e3", "status": "processing",
           "guild_id": 1, "user_id": 8, "amount": 100, "attempts": 2}
    with patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=AsyncMock(side_effect=RuntimeError("pg timeout"))), \
         patch.object(coin_drop.drop_reward_store, "retry",
                      new=AsyncMock()) as retry, \
         patch.object(coin_drop.drop_reward_store, "complete",
                      new=AsyncMock()) as done:
        ok = await cog._process_drop_reward(row)
    assert ok is False
    retry.assert_awaited_once()
    args = retry.await_args.args
    assert args[0] == "speeddrop:e3"
    assert 0 < args[2] <= coin_drop._DROP_BACKOFF_CAP  # ограниченный backoff
    done.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_drop_reward_lost_complete_is_idempotent():
    """Commit начисления прошёл, но complete() потерян: награда
    уже на балансе, повторная обработка по lease-дедупликации
    не начислит второй раз (проверяется на уровне PG)."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    row = {"grant_key": "speeddrop:e4", "status": "processing",
           "guild_id": 1, "user_id": 9, "amount": 200, "attempts": 1}
    with patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=AsyncMock(return_value=200)), \
         patch.object(coin_drop.drop_reward_store, "complete",
                      new=AsyncMock(side_effect=RuntimeError("lost"))):
        ok = await cog._process_drop_reward(row)
    # Начисление свершилось — пользователю честно True;
    # complete-ошибка залогирована, lease-recovery доведёт до completed.
    assert ok is True


@pytest.mark.asyncio
async def test_speed_drop_claim_pg_failure_still_notifies_user():
    """PG недоступен в момент клика: пользователь получает ответ
    (награда в mem-очереди до восстановления), 10062 нет."""
    view = coin_drop.SpeedDropView(300, "evt-fail")
    btn = _speed_btn(view)
    cog = _fake_cog()
    i = _interaction(7, cog=cog)
    with patch.object(coin_drop.drop_reward_store, "record",
                      new=AsyncMock(side_effect=RuntimeError("pg down"))), \
         patch.object(coin_drop, "logger") as log:
        await btn.callback(i)
        await asyncio.sleep(0.01)
    # Ответ отправлен — 10062 не возникнет; обязательство в очереди.
    assert i.followup.sent or i.response.messages
    assert len(cog._mem_pending) == 1
    log.critical.assert_called()


@pytest.mark.asyncio
async def test_speed_drop_claim_discord_403_500_keeps_reward():
    """Discord отвечает 403/500 на нотификацию: награда уже
    зафиксирована и начислена — ошибка Discord её не отменяет."""
    for status in (403, 429, 500):
        view = coin_drop.SpeedDropView(300, f"evt-d{status}")
        btn = _speed_btn(view)
        i = _interaction(11)
        fake_resp = SimpleNamespace(status=status, reason="discord err")
        boom = coin_drop.discord.HTTPException(fake_resp, "boom")
        i.edit_original_response = AsyncMock(side_effect=boom)
        i.followup.send = AsyncMock(side_effect=boom)
        with patch("cogs.coin_drop.grant_drop_reward",
                   new=AsyncMock(return_value=True)) as grant:
            await btn.callback(i)  # не должно бросить исключение
        assert grant.await_count == 1


@pytest.mark.asyncio
async def test_trigger_drop_cooldown_blocks_parallel():
    """Повторный trigger в окне кулдауна не создаёт второй ивент."""
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    coin_drop.LAST_EVENT_TIME[777] = __import__("datetime").datetime.now()
    channel = SimpleNamespace(id=777, guild=SimpleNamespace(id=1),
                              send=AsyncMock())
    with patch("services.settings_service.settings_service") as svc:
        svc.peek.return_value = True
        await cog.trigger_drop(channel)
    channel.send.assert_not_called()
    coin_drop.LAST_EVENT_TIME.pop(777, None)
    coin_drop.CHANNEL_MESSAGE_COUNTERS.pop(777, None)
