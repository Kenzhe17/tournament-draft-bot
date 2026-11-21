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
    with patch("cogs.coin_drop.add_balance", new=AsyncMock()) as add:
        await asyncio.gather(btn.callback(i1), btn.callback(i2))
    assert add.await_count == 1
    # Проигравший получил ephemeral-ответ «кто-то быстрее».
    assert i2.followup.sent or i2.response.messages


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
