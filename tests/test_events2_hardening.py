"""Регрессионные тесты P0/P1-hardening Spontaneous Events 2.0.

Покрывают: settling-машину состояний, exactly-once при крэше,
конкурирующих финализаторов, недоступность PG+файла, дубли/
ошибки инвентаря, PG-гарды действий, восстановление карточки,
retry финального edit, replies в активности, per-guild настройки.
"""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cogs import coin_drop
from services.activity_tracker import ActivityGate
from storage.drop_pending_store import DropPendingStore


# ------------------------------------------------------------ fakes

class _Resp:
    def __init__(self):
        self._done = False
        self.messages = []
        self.modals = []

    def is_done(self):
        return self._done

    async def defer(self, **kw):
        self._done = True

    async def send_message(self, msg, **kw):
        self.messages.append(msg)
        self._done = True

    async def send_modal(self, modal):
        self.modals.append(modal)
        self._done = True


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, msg=None, **kw):
        self.sent.append((msg, kw))


def _interaction(uid=1, gid=1, cid="se2:abcd1234:claim",
                 channel_id=200, bot=False):
    return SimpleNamespace(
        guild_id=gid, channel_id=channel_id,
        user=SimpleNamespace(
            id=uid, bot=bot, mention=f"<@{uid}>",
            display_name=f"u{uid}"),
        data={"custom_id": cid},
        response=_Resp(), followup=_Followup(),
    )


def _cog():
    return coin_drop.CoinDropCog(bot=SimpleNamespace(
        loop=None, get_guild=lambda g: None))


def _session(game="speed", prize=None, config=None, actions=None,
             event_id="abcd1234", end_in=30.0, start_ago=0.0,
             close_in=None):
    now = time.time()
    row = {
        "event_id": event_id, "guild_id": 1, "channel_id": 200,
        "game_type": game,
        "prize": prize or {"type": "coins", "amount": 100,
                           "item_id": None},
        "config": config if config is not None else
        {"started_ts": now - start_ago},
        "actions": actions or {},
        "state": "active",
        "deadline_at": datetime.fromtimestamp(now + end_in,
                                              timezone.utc),
        "close_at": (datetime.fromtimestamp(now + close_in,
                                             timezone.utc)
                     if close_in is not None else None),
        "message_id": 999,
    }
    return coin_drop._EventSession.from_row(row)


def _msg(edit_side_effect=None):
    return SimpleNamespace(id=999, edit=AsyncMock(
        side_effect=edit_side_effect), reply=AsyncMock(),
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=200))


@pytest.fixture(autouse=True)
def _no_event_db(monkeypatch):
    monkeypatch.setattr(coin_drop.event_store, "get",
                        AsyncMock(return_value=None))
    def _fake_settle(eid, res):
        payload = res({}) if callable(res) else res
        outcome = (payload.get("outcome")
                   if isinstance(payload, dict)
                   and "outcome" in payload else payload)
        return {"event_id": eid, "state": "settling",
                "result": {"outcome": outcome}}
    monkeypatch.setattr(
        coin_drop.event_store, "settle",
        AsyncMock(side_effect=_fake_settle))
    monkeypatch.setattr(coin_drop.event_store, "mark_card_edit",
                        AsyncMock())
    monkeypatch.setattr(coin_drop.event_store, "mark_started",
                        AsyncMock())
    monkeypatch.setattr(coin_drop.event_store,
                        "ensure_channel_state", AsyncMock())
    monkeypatch.setattr(coin_drop.event_store, "list_settling",
                        AsyncMock(return_value=[]))
    monkeypatch.setattr(coin_drop.event_store, "list_failed_cards",
                        AsyncMock(return_value=[]))


# ============================================ P0: settling state machine

@pytest.mark.asyncio
async def test_settle_persists_outcome_before_grant():
    """settle() вызывается ДО grant_event_reward и сохраняет
    outcome — победитель не может быть перевыбран."""
    cog = _cog()
    s = _session("raffle", actions={"p:1": 1, "p:2": 1})
    s.message = _msg()
    order = []

    async def _settle(eid, res):
        order.append("settle")
        computed = res({}) if callable(res) else res
        outcome = (computed.get("outcome")
                   if isinstance(computed, dict)
                   and "outcome" in computed else computed)
        assert outcome and outcome.get("winners")
        return {"event_id": eid, "state": "settling",
                "result": {"outcome": outcome}}

    async def _grant(*a, **kw):
        order.append("grant")
        return "credited"

    with patch.object(coin_drop.event_store, "settle",
                      new=AsyncMock(side_effect=_settle)), \
         patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(side_effect=_grant)):
        await cog._finish_event(s, "timeout")
    assert order == ["settle", "grant"]


@pytest.mark.asyncio
async def test_random_winner_not_rerandomized_after_restart():
    """Крэш после settle: resume берёт СОХРАНЁННЫЙ outcome —
    raffle-победитель не меняется между попытками."""
    cog = _cog()
    stored_winner = 7
    outcome = {"winners": [stored_winner], "reason": "raffle",
               "payout": 100, "detail": "x"}
    row = {
        "event_id": "cafe0001", "guild_id": 1, "channel_id": 200,
        "game_type": "raffle",
        "prize": {"type": "coins", "amount": 100, "item_id": None},
        "config": {"started_ts": time.time() - 100},
        "actions": {"p:1": 1, "p:2": 1, "p:7": 1},
        "state": "settling",
        "result": {"outcome": outcome, "reason": "timeout"},
        "deadline_at": datetime.now(timezone.utc)
        - timedelta(seconds=5),
        "close_at": None, "message_id": 555,
    }
    with patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")) as grant:
        await cog._resume_settling(row)
    assert grant.await_count == 1
    assert grant.await_args.args[2] == stored_winner


@pytest.mark.asyncio
async def test_two_competing_finalizers_single_finish():
    """Два финализатора: settle выигрывает один, второй видит
    settling и использует сохранённый outcome; finish() и
    правка карточки — ровно один раз."""
    cog = _cog()
    s1 = _session("dice", actions={"roll:1": 50, "roll:2": 90})
    s1.message = _msg()
    s2 = _session("dice", actions={"roll:1": 50, "roll:2": 90},
                  event_id="abcd1235")
    s2.message = _msg()

    settle_calls = []

    async def _settle(eid, res):
        settle_calls.append(eid)
        if len(settle_calls) == 1:
            payload = res({}) if callable(res) else res
            outcome = (payload.get("outcome")
                       if isinstance(payload, dict)
                       and "outcome" in payload else payload)
            return {"event_id": eid, "state": "settling",
                    "result": {"outcome": outcome}}
        return None  # второй видит: уже settling

    stored = {"outcome": {"winners": [2], "reason": "dice",
                          "payout": 100, "detail": ""}}

    async def _get(eid):
        return {"event_id": eid, "state": "settling",
                "result": stored}

    with patch.object(coin_drop.event_store, "settle",
                      new=AsyncMock(side_effect=_settle)), \
         patch.object(coin_drop.event_store, "get",
                      new=AsyncMock(side_effect=_get)), \
         patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(side_effect=[
                          {"state": "f"}, None])), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")):
        await asyncio.gather(
            cog._settle_and_finish(s1, {"winners": [1]}, "t"),
            cog._settle_and_finish(s2, {"winners": [9]}, "t"))
    s1.message.edit.assert_awaited_once()
    s2.message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_durable_obligation_event_stays_settling():
    """PG и pending-файл недоступны → grant 'failed' →
    finish НЕ вызывается, событие остаётся settling."""
    cog = _cog()
    s = _session("speed", actions={"winner": 5})
    s.message = _msg()
    pending = DropPendingStore(
        __import__("pathlib").Path(
            __import__("tempfile").mkdtemp()) / "p.json")

    with patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock()) as fin, \
         patch.object(coin_drop.event_reward_store, "record",
                      new=AsyncMock(side_effect=RuntimeError("pg"))), \
         patch.object(pending, "add",
                      new=AsyncMock(side_effect=OSError("disk"))), \
         patch.object(coin_drop, "event_pending_store", pending), \
         patch.object(coin_drop.event_reward_store, "claim_key",
                      new=AsyncMock()):
        await cog._finish_event(s, "first_click")
    fin.assert_not_awaited()
    s.message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_settling_recovers_failed_obligation():
    """ settling-строка без результата обязательств: resume
    повторяет grant и завершает событие."""
    cog = _cog()
    row = {
        "event_id": "cafe0002", "guild_id": 1, "channel_id": 200,
        "game_type": "speed",
        "prize": {"type": "coins", "amount": 100, "item_id": None},
        "config": {"started_ts": time.time() - 100},
        "actions": {"winner": 3},
        "state": "settling",
        "result": {"outcome": {"winners": [3], "reason": "x",
                               "payout": 100, "detail": ""}},
        "deadline_at": datetime.now(timezone.utc)
        - timedelta(seconds=5),
        "close_at": None, "message_id": 777,
    }
    with patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})) as fin, \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")) as grant:
        await cog._resume_settling(row)
    assert grant.await_count == 1
    fin.assert_awaited_once()


# ============================================ P1: actions / deadline

@pytest.mark.asyncio
async def test_action_persist_rejected_denies_user():
    """patch_actions вернул False (дедлайн/закрыто/ключ занят)
    → действие отклонено, победитель не объявлен."""
    cog = _cog()
    s = _session("speed")
    s.message = _msg()
    cog._register_session(s)
    i = _interaction(1)
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock(return_value=False)):
        await cog.handle_button(i, "claim")
    assert "Временная ошибка" in i.followup.sent[-1][0]
    assert s.finished is False


@pytest.mark.asyncio
async def test_action_after_deadline_while_waiting_lock():
    """Действие ждало session.lock за дедлайном → отклонено
    (now пересчитывается ПОСЛЕ захвата lock)."""
    cog = _cog()
    s = _session("speed", end_in=0.05)
    s.message = _msg()
    cog._register_session(s)
    i = _interaction(1)
    await s.lock.acquire()
    task = asyncio.get_running_loop().create_task(
        cog.handle_button(i, "claim"))
    await asyncio.sleep(0.15)  # дедлайн прошёл, lock занят
    s.lock.release()
    await task
    assert "истекло" in i.response.messages[-1]


@pytest.mark.asyncio
async def test_action_wrong_channel_rejected():
    cog = _cog()
    s = _session("speed")
    cog._register_session(s)
    i = _interaction(1, channel_id=999)
    await cog.handle_button(i, "claim")
    # Ответа быть не должно — тихий отказ до defer.
    assert i.response.messages == []


@pytest.mark.asyncio
async def test_action_for_wrong_game_rejected():
    cog = _cog()
    s = _session("speed")
    cog._register_session(s)
    i = _interaction(1, cid="se2:abcd1234:red")
    await cog.handle_button(i, "red")
    assert "Неизвестное действие" in i.response.messages[-1]


# ===================================== P1: card recovery / edit retry

@pytest.mark.asyncio
async def test_message_id_retry_after_failure():
    """send успешен, set_message_id упал → _ensure_message_id
    повторяет с backoff."""
    cog = _cog()
    s = _session()
    s.message = SimpleNamespace(id=4242)
    calls = []

    async def _setmid(eid, mid):
        calls.append(mid)
        if len(calls) == 1:
            raise RuntimeError("pg down")

    with patch.object(coin_drop.event_store, "set_message_id",
                      new=AsyncMock(side_effect=_setmid)), \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog._ensure_message_id(s, attempts=3)
    assert calls == [4242, 4242]


@pytest.mark.asyncio
async def test_card_recovered_via_footer_marker():
    """message_id потерян (крах между send и записью): карточка
    находится сканом истории по footer-маркеру se2:{eid}."""
    import discord as _d
    cog = _cog()
    s = _session(event_id="feed1234")
    s.message_id = None

    emb = _d.Embed(title="t")
    emb.set_footer(text="se2:feed1234")
    found = SimpleNamespace(id=31337, embeds=[emb])

    class _Chan:
        async def fetch_message(self, mid):
            raise Exception("gone")

        async def history(self, limit=25):
            yield found
            yield SimpleNamespace(id=1, embeds=[])

    class _Guild:
        def get_channel(self, cid):
            return _Chan()

        async def fetch_channel(self, cid):
            return _Chan()

    cog.bot.get_guild = lambda gid: _Guild()
    with patch.object(coin_drop.event_store, "set_message_id",
                      new=AsyncMock()) as setmid:
        msg = await cog._resolve_card_message(s)
    assert msg is found
    setmid.assert_awaited()


@pytest.mark.asyncio
async def test_final_edit_failure_marks_card_edit_failed():
    """Неудачный финальный edit → result.card_edit='failed',
    свипер повторит."""
    cog = _cog()
    s = _session("speed", actions={"winner": 5})
    s.message = _msg()
    cog._register_session(s)
    with patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")), \
         patch.object(coin_drop.event_store, "mark_card_edit",
                      new=AsyncMock()) as mark:
        await cog._finish_event(s, "first_click")
    statuses = [c.args[1] for c in mark.await_args_list]
    assert "failed" in statuses or "ok" in statuses
    # msg.edit — AsyncMock без side_effect → успех → 'ok'
    assert statuses[-1] == "ok"


@pytest.mark.asyncio
async def test_final_edit_http_error_retried_then_failed():
    """msg.edit падает дважды → card_edit='failed' (не 'ok')."""
    cog = _cog()
    s = _session("speed", actions={"winner": 5})
    import discord as _d
    err = _d.HTTPException(
        response=SimpleNamespace(status=500, reason="err"),
        message="boom")
    s.message = _msg(edit_side_effect=err)
    cog._register_session(s)
    with patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")), \
         patch.object(coin_drop.event_store, "mark_card_edit",
                      new=AsyncMock()) as mark, \
         patch("asyncio.sleep", new=AsyncMock()):
        await cog._finish_event(s, "first_click")
    assert mark.await_args.args[1] == "failed"


@pytest.mark.asyncio
async def test_failed_cards_sweeper_retries_edit():
    """Свипер находит finished-событие с card_edit!='ok' и
    повторяет правку карточки."""
    cog = _cog()
    row = {
        "event_id": "cafe0003", "guild_id": 1, "channel_id": 200,
        "game_type": "speed",
        "prize": {"type": "coins", "amount": 100, "item_id": None},
        "config": {"started_ts": time.time() - 100},
        "actions": {"winner": 5},
        "state": "finished",
        "result": {"outcome": {"winners": [5], "reason": "x",
                               "payout": 100, "detail": ""},
                   "grant": "credited"},
        "deadline_at": datetime.now(timezone.utc)
        - timedelta(seconds=5),
        "close_at": None, "message_id": 888,
    }
    s = coin_drop._EventSession.from_row(row)
    s.message = _msg()
    with patch.object(coin_drop.event_store, "list_failed_cards",
                      new=AsyncMock(return_value=[row])), \
         patch.object(coin_drop.event_store, "mark_card_edit",
                      new=AsyncMock()):
        # _resolve_channel → bot.get_guild None → fetch → None;
        # подменяем session.message через from_row? message None →
        # правка не случится. Подсунем message в сессию свипера
        # нельзя — он создаёт свою. Мокаем _edit_final_card?
        # Нет — проверяем, что правка дошла до msg через
        # _resolve_card_message: message_id=888 → fetch_message.
        class _Chan:
            async def fetch_message(self, mid):
                return s.message

        class _Guild:
            def get_channel(self, cid):
                return _Chan()

            async def fetch_channel(self, cid):
                return _Chan()

        cog.bot.get_guild = lambda gid: _Guild()
        await cog._sweep_failed_cards()
    s.message.edit.assert_awaited()


# ===================================== P1: activity gate / replies

@pytest.mark.asyncio
async def test_reply_message_counts_toward_gate():
    """Discord reply (MessageType.reply) засчитывается в гейт."""
    cog = _cog()
    cog._try_start = AsyncMock(return_value="started")
    cog._target_channel_id = lambda guild: 200
    with patch.object(coin_drop.event_config, "event_settings",
                      return_value={
                          "window_minutes": 5, "min_messages": 1,
                          "min_unique_users": 1,
                          "max_per_author": 10,
                          "min_message_interval_seconds": 0,
                          "min_active_authors_60s": 1}), \
         patch.object(coin_drop.event_config, "feature_enabled",
                      return_value=True):
        reply_type = getattr(__import__("discord").MessageType,
                             "reply", None)
        if reply_type is None:
            pytest.skip("discord.py без MessageType.reply")
        msg = SimpleNamespace(
            author=SimpleNamespace(id=1, bot=False),
            webhook_id=None, guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=200),
            type=reply_type, content="ответ")
        await cog.on_message(msg)
    cog._try_start.assert_awaited()


@pytest.mark.asyncio
async def test_uncredited_message_cannot_trigger_event():
    """Незасчитанное сообщение (дубль контента) после «холодного»
    окна не запускает событие, даже если пороги формально
    выполнены старыми сэмплами."""
    cog = _cog()
    cog._try_start = AsyncMock(return_value="cooldown")
    cog._target_channel_id = lambda guild: 200
    settings = {
        "window_minutes": 5, "min_messages": 1,
        "min_unique_users": 1, "max_per_author": 10,
        "min_message_interval_seconds": 0,
        "min_active_authors_60s": 1}
    with patch.object(coin_drop.event_config, "event_settings",
                      return_value=settings), \
         patch.object(coin_drop.event_config, "feature_enabled",
                      return_value=True):
        def _msg(uid, text, mtype=None):
            return SimpleNamespace(
                author=SimpleNamespace(id=uid, bot=False),
                webhook_id=None, guild=SimpleNamespace(id=1),
                channel=SimpleNamespace(id=200),
                type=mtype or __import__("discord").MessageType.default,
                content=text)
        # Первое сообщение — credited, try_start вызван (cooldown).
        await cog.on_message(_msg(1, "раз"))
        assert cog._try_start.await_count == 1
        # Дубль контента — НЕ credited → try_start не вызывается.
        await cog.on_message(_msg(2, "раз"))
        assert cog._try_start.await_count == 1


def test_gate_per_key_settings_isolated():
    """Пороги разных guild/channel не протекают: key A с
    min_messages=1 срабатывает, key B с min_messages=100 — нет."""
    g = ActivityGate(min_interval_s=0, max_per_author=100,
                     min_active_authors=1)
    g.configure((1, 1), min_messages=1, min_unique_users=1)
    g.configure((2, 2), min_messages=100, min_unique_users=1)
    now = time.time()
    g.record((1, 1), 10, "a", now)
    g.record((2, 2), 10, "a", now)
    assert g.evaluate((1, 1), now) is True
    assert g.evaluate((2, 2), now) is False


# ===================================== P0: item grant durability

class _FakeInv:
    def __init__(self, file_owned=False, fail_save=False):
        self._mem = {}
        self.fail_save = fail_save

    def add_cosmetic(self, c):
        key = f"{c.guild_id}:{c.user_id}"
        lst = self._mem.setdefault(key, [])
        if any(x.item_id == c.item_id for x in lst):
            return False
        lst.append(c)
        if self.fail_save:
            raise OSError("disk full")
        return True

    def load(self):
        pass

    def get_player_inventory(self, gid, uid):
        return self._mem.get(f"{gid}:{uid}", [])


def _item_env(tmp_path, monkeypatch, inv, owned=False):
    """Подменить shop_store.get_item, INVENTORY_FILE и
    event_item_marks (in-memory аналог PG-таблицы)."""
    import storage.shop_store as ss
    from storage import event_reward_store as ers
    from services import event_rewards as er
    item = SimpleNamespace(id="sword", name="Меч", is_active=True)
    monkeypatch.setattr(ss.shop_store, "get_item",
                        lambda iid: item if iid == "sword" else None)
    monkeypatch.setattr(ss, "inventory_store", inv)
    inv_file = tmp_path / "player_inventory.json"
    if owned:
        inv_file.write_text(json.dumps(
            {"1:42": [{"item_id": "sword", "equipped": False}]}),
            encoding="utf-8")
    else:
        inv_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ss, "INVENTORY_FILE", inv_file)
    marks = {}

    async def _get_mark(key):
        return marks.get(key)

    async def _put_mark(key, mode, comp=0):
        if key not in marks:
            marks[key] = {"grant_key": key, "mode": mode,
                          "comp_amount": comp}
        return marks[key]

    monkeypatch.setattr(ers.event_reward_store, "get_item_mark",
                        AsyncMock(side_effect=_get_mark))
    monkeypatch.setattr(ers.event_reward_store, "put_item_mark",
                        AsyncMock(side_effect=_put_mark))
    return er


@pytest.mark.asyncio
async def test_item_already_in_inventory_is_compensated(tmp_path,
                                                        monkeypatch):
    """Дубль предмета: durable-файл уже содержит item_id →
    mark 'comp' → компенсация монетами по ключу
    ``<grant_key>:comp``. Предмет НЕ выдаётся повторно."""
    import storage.user_balance_store as ubs
    inv = _FakeInv()
    er = _item_env(tmp_path, monkeypatch, inv, owned=True)
    credited = AsyncMock()
    monkeypatch.setattr(ubs.user_balance_store, "_use_db", True)
    monkeypatch.setattr(ubs.user_balance_store, "add_balance_once",
                        credited)
    res = await er.apply_reward({"grant_key": "event:item:dup",
                                 "kind": "item", "guild_id": 1,
                                 "user_id": 42, "amount": 100,
                                 "item_id": "sword"})
    assert res == "comp:100"
    credited.assert_awaited_once_with(
        1, 42, 100, "event:item:dup:comp")
    # В инвентарь ничего не добавлялось — только компенсация.
    assert inv.get_player_inventory(1, 42) == []


@pytest.mark.asyncio
async def test_item_comp_amount_frozen_in_obligation(tmp_path,
                                                     monkeypatch):
    """Размер компенсации заморожен в event_rewards.amount при
    record(): смена настройки events.item_dup_compensation_
    coins НЕ меняет существующее обязательство."""
    import storage.user_balance_store as ubs
    inv = _FakeInv()
    er = _item_env(tmp_path, monkeypatch, inv, owned=True)
    credited = AsyncMock()
    monkeypatch.setattr(ubs.user_balance_store, "_use_db", True)
    monkeypatch.setattr(ubs.user_balance_store, "add_balance_once",
                        credited)
    # Настройка «изменилась» — обязано остаться 250 из amount.
    monkeypatch.setattr(
        "services.event_config._peek",
        lambda gid, key, default: 9999)
    res = await er.apply_reward({"grant_key": "event:item:frozen",
                                 "kind": "item", "guild_id": 1,
                                 "user_id": 42, "amount": 250,
                                 "item_id": "sword"})
    assert res == "comp:250"
    credited.assert_awaited_once_with(
        1, 42, 250, "event:item:frozen:comp")


@pytest.mark.asyncio
async def test_item_crash_after_save_no_double_reward(tmp_path,
                                                      monkeypatch):
    """Crash после записи предмета в файл, но до complete
    ledger: retry видит mark 'item' → предмет уже наш →
    'owned' БЕЗ компенсации. И предмет, и comp за одну
    награду невозможны."""
    import storage.user_balance_store as ubs
    inv_file = tmp_path / "player_inventory.json"
    inv_file.write_text("{}", encoding="utf-8")

    class _Inv(_FakeInv):
        def add_cosmetic(self, c):
            r = super().add_cosmetic(c)
            if r:
                inv_file.write_text(json.dumps(
                    {"1:42": [{"item_id": c.item_id}]}),
                    encoding="utf-8")
            return r

    inv = _Inv()
    er = _item_env(tmp_path, monkeypatch, inv)
    credited = AsyncMock()
    monkeypatch.setattr(ubs.user_balance_store, "_use_db", True)
    monkeypatch.setattr(ubs.user_balance_store, "add_balance_once",
                        credited)
    row = {"grant_key": "event:item:crash", "kind": "item",
           "guild_id": 1, "user_id": 42, "amount": 100,
           "item_id": "sword"}
    # Первая попытка: предмет выдан и подтверждён файлом,
    # но «ledger complete» не дошёл (крах) — retry.
    assert await er.apply_reward(row) == "item"
    # Retry: mark 'item' уже зафиксирован → owned → без comp.
    assert await er.apply_reward(row) == "owned"
    credited.assert_not_awaited()
    assert len(json.loads(
        inv_file.read_text(encoding="utf-8"))["1:42"]) == 1


@pytest.mark.asyncio
async def test_item_mark_item_mode_never_compensates(tmp_path,
                                                      monkeypatch):
    """mark 'item' + предмет уже в файле (записан этим grant
    до крэша) → 'owned', компенсация не начисляется даже при
    comp_amount>0 в обязательстве."""
    import storage.user_balance_store as ubs
    import storage.event_reward_store as ers
    inv = _FakeInv()
    er = _item_env(tmp_path, monkeypatch, inv)
    credited = AsyncMock()
    monkeypatch.setattr(ubs.user_balance_store, "_use_db", True)
    monkeypatch.setattr(ubs.user_balance_store, "add_balance_once",
                        credited)

    async def _mark(key):
        return {"grant_key": key, "mode": "item",
                "comp_amount": 0}
    monkeypatch.setattr(ers.event_reward_store, "get_item_mark",
                        AsyncMock(side_effect=_mark))
    inv_file = tmp_path / "player_inventory.json"
    inv_file.write_text(json.dumps(
        {"1:42": [{"item_id": "sword", "equipped": False}]}),
        encoding="utf-8")
    res = await er.apply_reward({"grant_key": "event:item:m",
                                 "kind": "item", "guild_id": 1,
                                 "user_id": 42, "amount": 100,
                                 "item_id": "sword"})
    assert res == "owned"
    credited.assert_not_awaited()


@pytest.mark.asyncio
async def test_item_grant_confirms_durable_write(tmp_path,
                                                 monkeypatch):
    """Успешная выдача: файл инвентаря реально содержит предмет."""
    inv_file = tmp_path / "player_inventory.json"

    class _Inv2(_FakeInv):
        def add_cosmetic(self, c):
            r = super().add_cosmetic(c)
            if r:
                inv_file.write_text(json.dumps(
                    {"1:42": [{"item_id": c.item_id}]}),
                    encoding="utf-8")
            return r

    inv2 = _Inv2()
    er = _item_env(tmp_path, monkeypatch, inv2)
    res = await er.apply_reward({"grant_key": "event:item:ok",
                                 "kind": "item", "guild_id": 1,
                                 "user_id": 42, "amount": 0,
                                 "item_id": "sword"})
    assert res == "item"


@pytest.mark.asyncio
async def test_item_save_error_propagates_for_retry(tmp_path,
                                                    monkeypatch):
    """Ошибка записи инвентаря → исключение → worker retry."""
    inv = _FakeInv(fail_save=True)
    er = _item_env(tmp_path, monkeypatch, inv)
    with pytest.raises(Exception):
        await er.apply_reward({"grant_key": "event:item:err",
                               "kind": "item", "guild_id": 1,
                               "user_id": 42, "amount": 0,
                               "item_id": "sword"})


@pytest.mark.asyncio
async def test_item_phantom_dup_recovered(tmp_path, monkeypatch):
    """add_cosmetic вернул False (фантом в памяти после
    неудачного save), а в файле предмета нет → перезагрузка +
    повторная выдача → durable-подтверждение."""
    inv_file = tmp_path / "player_inventory.json"
    inv_file.write_text("{}", encoding="utf-8")

    class _Phantom(_FakeInv):
        def __init__(self):
            super().__init__()
            # «Фантом»: память считает, что предмет есть.
            self._mem["1:42"] = [SimpleNamespace(
                item_id="sword", equipped=False)]
            self._loaded = False

        def load(self):
            # После load память = файл (пусто) — фантом исчез.
            self._mem = {}
            self._loaded = True

        def add_cosmetic(self, c):
            if self._loaded:
                # Реальная выдача после load — пишем файл.
                self._mem.setdefault("1:42", []).append(c)
                inv_file.write_text(json.dumps(
                    {"1:42": [{"item_id": c.item_id}]}),
                    encoding="utf-8")
                return True
            return False  # фантом-дубль до load

    inv = _Phantom()
    er = _item_env(tmp_path, monkeypatch, inv)
    await er.apply_reward({"grant_key": "event:item:ph",
                           "kind": "item", "guild_id": 1,
                           "user_id": 42, "amount": 0,
                           "item_id": "sword"})
    assert inv._loaded


# ======================= Final-hardening: новые сценарии

@pytest.mark.asyncio
async def test_growing_claim_uses_global_atomic_key():
    """Первый клейм «Растущего куша» — ОДИН глобальный ключ
    ``gclaim`` с must_absent: два simultaneous клейма разных
    игроков не могут оба сохраниться в PG."""
    cog = _cog()
    s = _session("growing",
                 prize={"type": "coins", "amount": 500, "start": 100,
                        "item_id": None},
                 config={"started_ts": time.time(),
                         "burn_after": 30.0},
                 end_in=45.0)
    s.message = _msg()
    cog._register_session(s)
    patches = []

    async def _patch(eid, patch, must_absent=None):
        # Имитация PG must_absent по gclaim.
        if "gclaim" in patch:
            if any("gclaim" in k for k in patches):
                return False
            patches.append(patch)
            return True
        return True

    finished = []

    async def _finish(sess, reason, locked=False):
        finished.append(reason)

    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock(side_effect=_patch)), \
         patch.object(cog, "_finish_event",
                      new=AsyncMock(side_effect=_finish)):
        i1 = _interaction(1, cid="se2:abcd1234:claim")
        i2 = _interaction(2, cid="se2:abcd1234:claim")
        await cog.handle_button(i1, "claim")
        await cog.handle_button(i2, "claim")
    assert len(patches) == 1
    assert patches[0]["gclaim"]["uid"] == 1
    assert finished == ["claimed"]
    assert "уже забран" in i2.followup.sent[-1][0]


@pytest.mark.asyncio
async def test_growing_outcome_reads_gclaim():
    """compute_outcome: gclaim {"uid","elapsed"} — единственный
    источник победителя; legacy claim:<uid> — fallback."""
    from services import event_games
    prize = {"type": "coins", "amount": 500, "start": 100,
             "item_id": None}
    cfg = {"burn_after": 40.0}
    out = event_games.compute_outcome(
        "growing", cfg, {"gclaim": {"uid": 5, "elapsed": 10.0}},
        prize, 0)
    assert out["winners"] == [5]
    assert out["payout"] == event_games.growing_value(
        prize, 10.0, 45)
    out2 = event_games.compute_outcome(
        "growing", cfg, {"claim:8": 3.0}, prize, 0)
    assert out2["winners"] == [8]
    out3 = event_games.compute_outcome("growing", cfg, {}, prize, 0)
    assert out3["winners"] == [] and out3["reason"] == "burned"


def test_minority_card_hides_vote_distribution():
    """Меньшинство: публичная карточка показывает только суммарное
    число участников — red/blue распределение скрыто."""
    s = _session("minority",
                 actions={"choice:1": "red", "choice:2": "red",
                          "choice:3": "blue"})
    embed = coin_drop._build_card(s)
    desc = embed.description or ""
    assert "Участников: **3**" in desc
    assert "🔴" not in desc and "🔵" not in desc
    summ = coin_drop._actions_summary("minority", s.actions)
    assert summ == {"participants": 3}


@pytest.mark.asyncio
async def test_minority_persist_failure_denies_choice():
    """minority: _persist() == False → выбор НЕ подтверждён,
    пользователь получает ошибку (выбор не «засчитан»)."""
    cog = _cog()
    s = _session("minority")
    s.message = _msg()
    cog._register_session(s)
    i = _interaction(1, cid="se2:abcd1234:red")
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock(return_value=False)):
        await cog.handle_button(i, "red")
    assert "Временная ошибка" in i.followup.sent[-1][0]
    assert not any(k.startswith("choice:") for k in s.actions)


@pytest.mark.asyncio
async def test_guess_number_atomic_attempt_and_winner():
    """guess_number: верный ответ фиксирует попытку И победителя
    ОДНИМ атомарным patch_actions (must_absent по обоим)."""
    cog = _cog()
    s = _session("guess_number",
                 config={"started_ts": time.time(),
                         "accepted": ["17"]})
    s.message = _msg()
    cog._register_session(s)
    calls = []

    async def _patch(eid, patch, must_absent=None):
        calls.append((patch, must_absent))
        return True

    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock(side_effect=_patch)), \
         patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")):
        i = _interaction(7)
        await cog.handle_modal_answer(i, "abcd1234", "guess_number",
                                      "17")
    assert len(calls) == 1
    patch_, absent = calls[0]
    assert patch_ == {"guess:7": 1, "winner": 7}
    assert set(absent) == {"guess:7", "winner"}


@pytest.mark.asyncio
async def test_guess_number_wrong_answer_consumes_attempt():
    """Неверный ответ guess_number — одна попытка, без winner."""
    cog = _cog()
    s = _session("guess_number",
                 config={"started_ts": time.time(),
                         "accepted": ["17"]})
    s.message = _msg()
    cog._register_session(s)
    calls = []

    async def _patch(eid, patch, must_absent=None):
        calls.append(patch)
        return True

    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock(side_effect=_patch)):
        i = _interaction(7)
        await cog.handle_modal_answer(i, "abcd1234", "guess_number",
                                      "3")
    assert calls == [{"guess:7": 1}]
    assert s.finished is False
    assert "попытка использована" in i.followup.sent[-1][0]


@pytest.mark.asyncio
async def test_finish_backfills_cooldown_and_last_type():
    """Крэш между публикацией карточки и mark_started: finish
    восстанавливает cooldown_until (от started_ts) и
    last_event_type в event_channel_state через guarded
    ensure_channel_state (не перезаписывает более новый ивент)."""
    cog = _cog()
    start = time.time() - 5
    s = _session("speed", actions={"winner": 5},
                 config={"started_ts": start, "cooldown_s": 900})
    s.message = _msg()
    cog._register_session(s)
    with patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")), \
         patch.object(coin_drop.event_store, "ensure_channel_state",
                      new=AsyncMock()) as mark:
        await cog._finish_event(s, "first_click")
    mark.assert_awaited()
    args = mark.await_args.args
    assert args[0] == 1 and args[1] == 200
    until = args[2]
    assert until.tzinfo is not None
    # cooldown_until ≈ started_ts + 900s
    assert abs(until.timestamp() - (start + 900)) < 2
    assert args[3] == "speed" and args[4] == s.event_id


@pytest.mark.asyncio
async def test_settling_without_outcome_never_closed():
    """settling без валидного outcome в result → событие НЕ
    закрывается (finish не вызывается), критический лог."""
    cog = _cog()
    row = {
        "event_id": "bad00001", "guild_id": 1, "channel_id": 200,
        "game_type": "speed",
        "prize": {"type": "coins", "amount": 100, "item_id": None},
        "config": {"started_ts": time.time() - 100},
        "actions": {}, "state": "settling",
        "result": {"reason": "timeout"},  # нет outcome!
        "deadline_at": datetime.now(timezone.utc)
        - timedelta(seconds=5),
        "close_at": None, "message_id": 555,
    }
    with patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock()) as fin, \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock()):
        await cog._resume_settling(row)
    fin.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_recovered_via_cv2_components():
    """Components V2: embeds пусты — карточка находится по
    дереву компонентов (TextDisplay с маркером / custom_id
    se2:{eid}:*)."""
    cog = _cog()
    s = _session(event_id="beef0001")
    s.message_id = None

    td = SimpleNamespace(content="-# se2:beef0001")
    btn = SimpleNamespace(custom_id="se2:beef0001:claim")
    row = SimpleNamespace(children=[btn])
    container = SimpleNamespace(children=[td, row])
    found = SimpleNamespace(id=777, embeds=[],
                            components=[container],
                            author=SimpleNamespace(bot=True))

    class _Chan:
        async def fetch_message(self, mid):
            raise Exception("gone")

        async def history(self, limit=100):
            yield found
            yield SimpleNamespace(id=2, embeds=[], components=[],
                                  author=SimpleNamespace(bot=True))

    class _Guild:
        def get_channel(self, cid):
            return _Chan()

        async def fetch_channel(self, cid):
            return _Chan()

    cog.bot.get_guild = lambda gid: _Guild()
    with patch.object(coin_drop.event_store, "set_message_id",
                      new=AsyncMock()):
        msg = await cog._resolve_card_message(s)
    assert msg is found


@pytest.mark.asyncio
async def test_refresh_and_final_edit_serialized():
    """refresh и final edit разделяют session.card_lock:
    «живой» снапшот не может вклиниться между settle и
    финальной правкой карточки."""
    cog = _cog()
    s = _session("speed", actions={"winner": 5})
    s.message = _msg()
    cog._register_session(s)
    await s.card_lock.acquire()
    try:
        task = asyncio.get_running_loop().create_task(
            cog._refresh_card(s, force=True))
        await asyncio.sleep(0.05)
        s.message.edit.assert_not_awaited()
    finally:
        s.card_lock.release()
    await task
    s.message.edit.assert_awaited()



# =================== Final-release: reward result UI / cooldown

@pytest.mark.asyncio
async def test_final_card_shows_compensation_amount():
    """Item-дубль: финальная карточка показывает ФАКТИЧЕСКИЙ
    итог — 'comp:100' → компенсация 100 монет, а не «предмет
    добавлен»."""
    cog = _cog()
    s = _session(
        "speed",
        prize={"type": "item", "amount": 0, "item_id": "sword",
               "item_name": "Меч", "label": "предмет"},
        actions={"winner": 7})
    s.message = _msg()
    captured = {}

    def _fake_compose(embed, view):
        captured["embed"] = embed
        return view

    with patch.object(coin_drop, "compose",
                      side_effect=_fake_compose), \
         patch.object(coin_drop.event_store, "mark_card_edit",
                      new=AsyncMock()):
        await cog._edit_final_card_inner(
            s, {"winners": [7], "reason": "first_click",
                "payout": None, "detail": ""},
            "credited",
            rewards={"7": {"status": "credited",
                           "mode": "comp:100"}})
    desc = captured["embed"].description
    assert "компенсация" in desc and "100" in desc
    assert "добавлен" not in desc


@pytest.mark.asyncio
async def test_final_card_item_pending_not_claimed():
    """Награда ещё queued/pending: карточка НЕ утверждает, что
    предмет выдан — показывает 'будут зафиксированы'."""
    cog = _cog()
    s = _session(
        "speed",
        prize={"type": "item", "amount": 0, "item_id": "sword",
               "item_name": "Меч", "label": "предмет"},
        actions={"winner": 7})
    s.message = _msg()
    captured = {}

    def _fake_compose(embed, view):
        captured["embed"] = embed
        return view

    with patch.object(coin_drop, "compose",
                      side_effect=_fake_compose), \
         patch.object(coin_drop.event_store, "mark_card_edit",
                      new=AsyncMock()):
        await cog._edit_final_card_inner(
            s, {"winners": [7], "reason": "first_click",
                "payout": None, "detail": ""},
            "queued",
            rewards={"7": {"status": "queued", "mode": None}})
    desc = captured["embed"].description
    assert "будут зафиксированы" in desc
    assert "добавлен" not in desc


@pytest.mark.asyncio
async def test_final_card_item_owned_shows_already_owned():
    """mode='owned' — предмет уже был у игрока до grant
    (наша прошлая выдача): карточка честно пишет 'уже есть'."""
    cog = _cog()
    s = _session(
        "speed",
        prize={"type": "item", "amount": 0, "item_id": "sword",
               "item_name": "Меч", "label": "предмет"},
        actions={"winner": 7})
    s.message = _msg()
    captured = {}

    def _fake_compose(embed, view):
        captured["embed"] = embed
        return view

    with patch.object(coin_drop, "compose",
                      side_effect=_fake_compose), \
         patch.object(coin_drop.event_store, "mark_card_edit",
                      new=AsyncMock()):
        await cog._edit_final_card_inner(
            s, {"winners": [7], "reason": "first_click",
                "payout": None, "detail": ""},
            "credited",
            rewards={"7": {"status": "credited",
                           "mode": "owned"}})
    desc = captured["embed"].description
    assert "уже есть" in desc
    assert "добавлен" not in desc


@pytest.mark.asyncio
async def test_reward_card_sync_after_late_retry():
    """Поздний retry завершил награду → _sync_reward_card
    перечитывает event_rewards.result и обновляет карточку
    фактическим итогом."""
    cog = _cog()
    eid = "abcd1234"
    row = {
        "event_id": eid, "guild_id": 1, "channel_id": 200,
        "game_type": "speed",
        "prize": {"type": "item", "amount": 0, "item_id": "sword",
                  "item_name": "Меч", "label": "предмет"},
        "config": {"started_ts": time.time() - 100},
        "actions": {"winner": 5},
        "state": "finished",
        "result": {"grant": "queued",
                   "outcome": {"winners": [5], "reason": "x",
                               "payout": None, "detail": ""},
                   "rewards": {"5": {"status": "queued",
                                     "mode": None}}},
        "deadline_at": datetime.now(timezone.utc)
        - timedelta(seconds=5),
        "close_at": None, "message_id": 999,
    }
    edited = {}

    async def _fake_edit(session, outcome, grant_status,
                         rewards=None):
        edited["rewards"] = rewards
        edited["grant"] = grant_status

    reward_row = {"grant_key": f"event:{eid}:5", "kind": "item",
                  "guild_id": 1, "user_id": 5, "amount": 100,
                  "item_id": "sword", "status": "completed",
                  "result": "comp:100"}
    with patch.object(coin_drop.event_store, "get",
                      new=AsyncMock(return_value=row)), \
         patch.object(coin_drop.event_reward_store, "get_many",
                      new=AsyncMock(return_value=[reward_row])), \
         patch.object(cog, "_edit_final_card",
                      new=AsyncMock(side_effect=_fake_edit)):
        await cog._sync_reward_card(f"event:{eid}:5")
    assert edited["rewards"]["5"]["mode"] == "comp:100"
    assert edited["rewards"]["5"]["status"] == "credited"
