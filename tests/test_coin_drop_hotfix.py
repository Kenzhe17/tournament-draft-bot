"""Regression-тесты production hotfix coin_drop (NameError, double-claim,
parallel trigger)."""
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


def _interaction(uid=42, gid=1):
    return SimpleNamespace(
        guild_id=gid, user=SimpleNamespace(id=uid, mention=f"<@{uid}>"),
        response=_Resp(), followup=_Followup(),
        original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        edit_original_response=AsyncMock(),
    )


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
    """Два быстрых клика не должны дать двойную выплату."""
    view = coin_drop.SpeedDropView(300, "evt1")
    btn = next(c for c in view.children
               if getattr(c, "custom_id", "").startswith("speed_coin_drop"))
    i1, i2 = _interaction(1), _interaction(2)
    with patch("cogs.coin_drop.grant_drop_reward",
               new=AsyncMock(return_value=True)) as grant:
        await asyncio.gather(btn.callback(i1), btn.callback(i2))
    assert grant.await_count == 1
    # Проигравший получил ephemeral-ответ «кто-то быстрее».
    assert i2.followup.sent or i2.response.messages


@pytest.mark.asyncio
async def test_grant_drop_reward_first_try_success():
    """PG отвечает сразу — грант подтверждён, retry не запускается."""
    with patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=AsyncMock(return_value=300)) as m:
        ok = await coin_drop.grant_drop_reward(1, 42, 300, "speeddrop:e1")
    assert ok is True
    m.assert_awaited_once_with(1, 42, 300, "speeddrop:e1")


@pytest.mark.asyncio
async def test_grant_drop_reward_pg_failure_schedules_retry():
    """PG-ошибка при первом вызове: награда не теряется —
    фоновый retry дозачисляет по тому же grant_key."""
    calls = []

    async def flaky(*a):
        calls.append(a)
        if len(calls) == 1:
            raise RuntimeError("pg timeout")
        return 300

    with patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=flaky), \
         patch.object(coin_drop, "_DROP_GRANT_RETRY_DELAYS", (0.0,)):
        ok = await coin_drop.grant_drop_reward(1, 42, 300, "speeddrop:e2")
        assert ok is False
        await asyncio.sleep(0.05)  # дождаться retry-task
    assert len(calls) == 2
    # Один и тот же ключ — повторное начисление исключено на уровне PG.
    assert calls[0][3] == calls[1][3] == "speeddrop:e2"


@pytest.mark.asyncio
async def test_speed_drop_claim_pg_failure_still_notifies_user():
    """add_balance_once падает: пользователь получает ответ
    (награда дозачислится retry), исключения наружу нет."""
    view = coin_drop.SpeedDropView(300, "evt-fail")
    btn = next(c for c in view.children
               if getattr(c, "custom_id", "").startswith("speed_coin_drop"))
    i = _interaction(7)
    with patch.object(coin_drop.user_balance_store, "add_balance_once",
                      new=AsyncMock(side_effect=RuntimeError("pg down"))), \
         patch.object(coin_drop, "_DROP_GRANT_RETRY_DELAYS", ()), \
         patch.object(coin_drop, "logger") as log:
        await btn.callback(i)
        await asyncio.sleep(0.01)  # дать завершиться retry-task
    # Ответ отправлен — 10062 не возникнет; потери награды нет
    # (retry-цепочка запущена, при полном отказе — ERROR в лог).
    assert i.followup.sent or i.response.messages
    log.error.assert_called()  # все retry исчерпаны -> честный ERROR


@pytest.mark.asyncio
async def test_speed_drop_claim_discord_response_loss_keeps_reward():
    """PG-запись прошла, но Discord-ответ потерян:
    пользователь не получает ошибку, монеты не дублируются —
    повторный вызов grant с тем же ключом идёт в PG-дедупликацию."""
    view = coin_drop.SpeedDropView(300, "evt-lost")
    btn = next(c for c in view.children
               if getattr(c, "custom_id", "").startswith("speed_coin_drop"))
    i = _interaction(9)
    i.edit_original_response = AsyncMock(
        side_effect=coin_drop.discord.HTTPException(
            SimpleNamespace(status=500, reason="err"), "boom"))
    i.followup.send = AsyncMock(
        side_effect=coin_drop.discord.HTTPException(
            SimpleNamespace(status=500, reason="err"), "boom"))
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


import asyncio  # noqa: E402  (нужен для gather выше)
