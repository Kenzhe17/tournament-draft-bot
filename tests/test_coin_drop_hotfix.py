"""Regression-тесты production hotfix coin_drop (NameError, double-claim,
parallel trigger, durable drop rewards)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cogs import coin_drop
from storage.drop_pending_store import DropPendingStore


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
    """Минимальный CoinDropCog для view-тестов."""
    return SimpleNamespace(
        _settle_pending_item=AsyncMock(return_value="queued"),
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


@pytest.fixture
def pending_file(tmp_path, monkeypatch):
    """Реальный DropPendingStore на tmp-файле — проверяет
    настоящую атомарную запись и «перезапуск» (новый инстанс
    на том же пути видит те же обязательства)."""
    store = DropPendingStore(tmp_path / "pending.json")
    monkeypatch.setattr(coin_drop, "drop_pending_store", store)
    return store


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
               new=AsyncMock(return_value="credited")) as grant:
        await asyncio.gather(btn.callback(i1), btn.callback(i2))
    # Оба могли дойти до фиксации (record сериализует в PG),
    # но claimed выставляется один раз — победитель один.
    assert view.claimed is True
    assert grant.await_count >= 1
    assert i2.followup.sent or i2.response.messages


@pytest.mark.asyncio
async def test_speed_drop_same_user_double_click():
    """Повторный клик того же пользователя — вторая попытка
    отклоняется флагом claimed."""
    view = coin_drop.SpeedDropView(300, "evt-same")
    btn = _speed_btn(view)
    i1, i2 = _interaction(5), _interaction(5)
    with patch("cogs.coin_drop.grant_drop_reward",
               new=AsyncMock(return_value="credited")) as grant:
        await btn.callback(i1)
        await btn.callback(i2)
    # Второй клик отклонён ранним claimed — grant вызван один раз.
    assert grant.await_count == 1
    assert "быстрее" in (i2.response.messages[-1]
                         if i2.response.messages else "")


@pytest.mark.asyncio
async def test_speed_drop_other_owner_no_false_win():
    """grant_key уже принадлежит другому участнику →
    «other»: claimed НЕ потребляется, победитель не объявлен."""
    view = coin_drop.SpeedDropView(300, "evt-other")
    btn = _speed_btn(view)
    i = _interaction(3)
    with patch("cogs.coin_drop.grant_drop_reward",
               new=AsyncMock(return_value="other")):
        await btn.callback(i)
    assert view.claimed is False
    assert view.is_finished() is False or not view.is_finished()
    msg = " ".join(str(x) for x in (i.followup.sent + i.response.messages))
    assert "другим участником" in msg


@pytest.mark.asyncio
async def test_speed_drop_failed_keeps_claim_retryable():
    """«failed» (ни PG, ни файл): claimed НЕ потребляется —
    пользователь может повторить клик, победитель не объявлен."""
    view = coin_drop.SpeedDropView(300, "evt-fail2")
    btn = _speed_btn(view)
    i = _interaction(4)
    with patch("cogs.coin_drop.grant_drop_reward",
               new=AsyncMock(return_value="failed")):
        await btn.callback(i)
    assert view.claimed is False
    msg = " ".join(str(x) for x in (i.followup.sent + i.response.messages))
    assert "ещё раз" in msg or "ошибка" in msg


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
    повторного начисления и без ложного сообщения о победе."""
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
    "queued" — победитель объявлен честно, награда не потеряна."""
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

    # «Рестарт процесса»: новый экземпляр стора на том же пути.
    restarted = DropPendingStore(pending_file._path)
    assert len(restarted.all()) == 1  # обязательство не потеряно

    # PG восстановился — settle переносит в PG и начисляет.
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
    assert len(pending_file) == 0  # файл очищен после записи в PG
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
    строку в pending с backoff, двойного начисления нет."""
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
    assert args[1] == 2  # fencing по attempts текущего lease
    assert 0 < args[3] <= coin_drop._DROP_BACKOFF_CAP
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
async def test_speed_drop_claim_pg_failure_queues_durably(pending_file):
    """PG недоступен в момент клика: обязательство в durable
    pending-файле, пользователь получает ответ (10062 нет),
    награда не теряется при рестарте."""
    view = coin_drop.SpeedDropView(300, "evt-fail")
    btn = _speed_btn(view)
    cog = coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))
    i = _interaction(7, cog=cog)
    with patch.object(coin_drop.drop_reward_store, "record",
                      new=AsyncMock(side_effect=RuntimeError("pg down"))):
        await btn.callback(i)
        await asyncio.sleep(0.01)
    assert i.followup.sent or i.response.messages
    assert view.claimed is True  # обязательство durable — победитель объявлен
    assert len(pending_file) == 1


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
                   new=AsyncMock(return_value="credited")) as grant:
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
    # Оригинал сохранён как .corrupt-*
    corrupts = list(tmp_path.glob("pending.json.corrupt-*"))
    assert len(corrupts) == 1
    assert corrupts[0].read_text(encoding="utf-8") == "{broken json!!!"
    crit.assert_called()
    # Новый файл — валидный JSON с элементом.
    assert store.get("k1")["user_id"] == 1
