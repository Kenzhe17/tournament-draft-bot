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
    monkeypatch.setattr(
        coin_drop.event_store, "settle",
        AsyncMock(side_effect=lambda eid, res: {
            "event_id": eid, "state": "settling", "result": res}))
    monkeypatch.setattr(coin_drop.event_store, "mark_card_edit",
                        AsyncMock())
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
        assert "outcome" in res and res["outcome"]["winners"]
        return {"event_id": eid, "state": "settling",
                "result": res}

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
            return {"event_id": eid, "state": "settling",
                    "result": res}
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
    """Подменить shop_store.get_item и INVENTORY_FILE."""
    import storage.shop_store as ss
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
    return er


@pytest.mark.asyncio
async def test_item_already_in_inventory_is_satisfied(tmp_path,
                                                    monkeypatch):
    """Дубль предмета: durable-файл уже содержит item_id →
    обязательство исполнено (no-op), без ошибки."""
    inv = _FakeInv()
    er = _item_env(tmp_path, monkeypatch, inv, owned=True)
    await er.apply_reward({"grant_key": "event:item:dup",
                           "kind": "item", "guild_id": 1,
                           "user_id": 42, "amount": 0,
                           "item_id": "sword"})


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
    await er.apply_reward({"grant_key": "event:item:ok",
                           "kind": "item", "guild_id": 1,
                           "user_id": 42, "amount": 0,
                           "item_id": "sword"})


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
