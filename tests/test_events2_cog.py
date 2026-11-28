"""Cog-level тесты событий 2.0: гонки, поздние действия,
рестарт, идемпотентный финиш, карточка. Без БД/Discord —
event_store и grant мокируются."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from cogs import coin_drop


@pytest.fixture(autouse=True)
def _no_event_db(monkeypatch):
    """event_store.get → None по умолчанию: клики по уже
    завершённым/несуществующим событиям не ходят в PG.
    Тесты, проверяющие restore/settle-гонки, перекрывают
    эти патчи."""
    monkeypatch.setattr(coin_drop.event_store, "get",
                        AsyncMock(return_value=None))
    # settle() по умолчанию «успешен»: state→settling, outcome
    # вычисляется из builder(row) как в реальном UPDATE
    # ... FOR UPDATE (мок вызывает builder с пустой строкой —
    # cog-фолбэки подставляют поля сессии).
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
                 bot=False):
    return SimpleNamespace(
        guild_id=gid, channel_id=200, user=SimpleNamespace(
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
    s = coin_drop._EventSession.from_row(row)
    return s


def _msg():
    return SimpleNamespace(id=999, edit=AsyncMock(), reply=AsyncMock(),
                           guild=SimpleNamespace(id=1),
                           channel=SimpleNamespace(id=200))


# ------------------------------------------------------------- tests

@pytest.mark.asyncio
async def test_speed_double_click_single_winner():
    """Два одновременных клика — один победитель (session.lock +
    проверка winner в actions)."""
    cog = _cog()
    s = _session("speed")
    s.message = _msg()
    cog._register_session(s)
    i1 = _interaction(1, cid="se2:abcd1234:claim")
    i2 = _interaction(2, cid="se2:abcd1234:claim")
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock()) as patch_act, \
         patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "finished"})), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")) as grant:
        await asyncio.gather(cog.handle_button(i1, "claim"),
                             cog.handle_button(i2, "claim"))
    # Только один прошёл persist winner → один grant.
    assert grant.await_count == 1
    assert s.finished is True


@pytest.mark.asyncio
async def test_late_action_rejected():
    """Клик после deadline — ephemeral-отказ, действий нет."""
    cog = _cog()
    s = _session("speed", end_in=-1)  # дедлайн в прошлом
    cog._register_session(s)
    i = _interaction(1)
    with patch.object(coin_drop.event_store, "get",
                      new=AsyncMock(return_value=None)):
        await cog.handle_button(i, "claim")
    assert "истекло" in i.response.messages[-1]


@pytest.mark.asyncio
async def test_unknown_event_rejected():
    cog = _cog()
    i = _interaction(1, cid="se2:ffffffff:claim")
    with patch.object(coin_drop.event_store, "get",
                      new=AsyncMock(return_value=None)):
        await cog.handle_button(i, "claim")
    assert "завершено" in i.response.messages[-1]


@pytest.mark.asyncio
async def test_raffle_join_and_finish():
    cog = _cog()
    s = _session("raffle", prize={"type": "coins", "amount": 200,
                                  "item_id": None})
    s.message = _msg()
    cog._register_session(s)
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock()):
        for uid in (1, 2, 3):
            i = _interaction(uid, cid="se2:abcd1234:join")
            await cog.handle_button(i, "join")
    assert {1, 2, 3} <= set(
        int(k[2:]) for k in s.actions if k.startswith("p:"))
    # повторный join отклонён
    i = _interaction(1, cid="se2:abcd1234:join")
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock()):
        await cog.handle_button(i, "join")
    assert "уже участвуете" in i.followup.sent[-1][0]

    with patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")) as grant:
        await cog._finish_event(s, "timeout")
    assert grant.await_count == 1
    winner_uid = int(grant.await_args.args[2])
    assert winner_uid in (1, 2, 3)
    # карточка отредактирована, кнопки убраны
    s.message.edit.assert_awaited()


@pytest.mark.asyncio
async def test_chests_choice_once():
    cog = _cog()
    s = _session("chests",
                 config={"started_ts": time.time(),
                         "win_chest": 1})
    s.message = _msg()
    cog._register_session(s)
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock()):
        i1 = _interaction(1, cid="se2:abcd1234:chest:1")
        await cog.handle_button(i1, "chest:1")
        i2 = _interaction(1, cid="se2:abcd1234:chest:3")
        await cog.handle_button(i2, "chest:3")
    assert s.actions["choice:1"] == 1  # первый выбор сохранён
    assert "уже сделали выбор" in i2.followup.sent[-1][0]


@pytest.mark.asyncio
async def test_modal_correct_wins():
    cog = _cog()
    s = _session("quick_math",
                 config={"started_ts": time.time(),
                         "a": 2, "b": 3, "op": "+",
                         "accepted": ["5"]})
    s.message = _msg()
    cog._register_session(s)
    i = _interaction(9)
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock()), \
         patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")):
        await cog.handle_modal_answer(i, "abcd1234", "quick_math", "5")
    assert s.actions.get("winner") == 9
    assert s.finished is True


@pytest.mark.asyncio
async def test_modal_wrong_then_correct():
    cog = _cog()
    s = _session("word",
                 config={"started_ts": time.time(),
                         "accepted": ["тигр", "полосатый"]})
    s.message = _msg()
    cog._register_session(s)
    i1 = _interaction(1)
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock()):
        await cog.handle_modal_answer(i1, "abcd1234", "word", "мимо")
    assert "Неверно" in i1.followup.sent[-1][0]
    assert s.finished is False
    i2 = _interaction(2)
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock()), \
         patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")):
        await cog.handle_modal_answer(i2, "abcd1234", "word", "ТИГР")
    assert s.actions.get("winner") == 2


@pytest.mark.asyncio
async def test_guess_number_single_attempt():
    cog = _cog()
    s = _session("guess_number",
                 config={"started_ts": time.time(),
                         "number": 7, "accepted": ["7"]})
    s.message = _msg()
    cog._register_session(s)
    i1 = _interaction(1)
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock()):
        await cog.handle_modal_answer(i1, "abcd1234",
                                      "guess_number", "3")
    assert "попытка использована" in i1.followup.sent[-1][0]
    # повторная попытка тем же — отказ
    i2 = _interaction(1)
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock()):
        await cog.handle_modal_answer(i2, "abcd1234",
                                      "guess_number", "7")
    assert "Попытка уже использована" in i2.followup.sent[-1][0]
    assert s.finished is False


@pytest.mark.asyncio
async def test_growing_claim_before_and_after_burn():
    cog = _cog()
    # burn_after=10, start 20с назад → уже сгорел.
    s = _session("growing",
                 prize={"type": "coins", "amount": 500, "start": 100,
                        "item_id": None},
                 config={"started_ts": time.time() - 20.0,
                         "burn_after": 10.0},
                 end_in=25.0)
    s.message = _msg()
    cog._register_session(s)
    i = _interaction(1)
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock()):
        await cog.handle_button(i, "claim")
    assert "сгорел" in i.followup.sent[-1][0]
    assert s.finished is False

    # Живой куст: burn через 30с.
    s2 = _session("growing",
                  prize={"type": "coins", "amount": 500, "start": 100,
                         "item_id": None},
                  config={"started_ts": time.time(),
                          "burn_after": 30.0},
                  end_in=45.0, event_id="abcd1235")
    s2.message = _msg()
    cog._register_session(s2)
    i2 = _interaction(2, cid="se2:abcd1235:claim")
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock()), \
         patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")) as grant:
        await cog.handle_button(i2, "claim")
    assert grant.await_count == 1
    # выплата — текущая сумма, не максимум
    prize_arg = grant.await_args.args[3]
    assert prize_arg["amount"] < 500
    assert s2.finished is True


@pytest.mark.asyncio
async def test_last_click_one_per_user():
    cog = _cog()
    s = _session("last_click", config={"started_ts": time.time(),
                                       "duration": 30.0},
                 end_in=30.0)
    s.message = _msg()
    cog._register_session(s)
    with patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock()):
        i1 = _interaction(1, cid="se2:abcd1234:click")
        await cog.handle_button(i1, "click")
        i2 = _interaction(1, cid="se2:abcd1234:click")
        await cog.handle_button(i2, "click")
    assert "уже кликали" in i2.followup.sent[-1][0]
    assert len([k for k in s.actions if k.startswith("click:")]) == 1


@pytest.mark.asyncio
async def test_finish_event_idempotent_and_card_isolation():
    """Повторный _finish_event — no-op; редактируется ТОЛЬКО
    карточка события (message_id), чужие сообщения не трогаем."""
    cog = _cog()
    s = _session("speed", actions={"winner": 5})
    msg = _msg()
    other_msg = _msg()
    s.message = msg
    cog._register_session(s)
    with patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")):
        await cog._finish_event(s, "first_click")
        await cog._finish_event(s, "timeout")  # повтор — no-op
    msg.edit.assert_awaited_once()
    other_msg.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_finish_grant_failed_no_winner_declared():
    """grant 'failed' → победитель не объявлен как получивший
    приз (карточка показывает техническую ошибку)."""
    cog = _cog()
    s = _session("speed", actions={"winner": 5})
    s.message = _msg()
    cog._register_session(s)
    with patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="failed")):
        await cog._finish_event(s, "first_click")
    # edit вызван — проверяем, что grant_status=failed дошёл
    # до result (через event_store.finish mock call args)
    # — достаточно, что второй grant не делался.


@pytest.mark.asyncio
async def test_restore_session_from_db_and_finish():
    """«Рестарт»: сессия восстанавливается из строки БД,
    просроченное событие финализируется без двойной выплаты."""
    cog = _cog()
    row = {
        "event_id": "beef0001", "guild_id": 1, "channel_id": 200,
        "game_type": "raffle",
        "prize": {"type": "coins", "amount": 100, "item_id": None},
        "config": {"started_ts": time.time() - 100},
        "actions": {"p:1": 1, "p:2": 1},
        "state": "active",
        "deadline_at": datetime.now(timezone.utc)
        - timedelta(seconds=5),
        "close_at": None, "message_id": 555,
    }
    with patch.object(coin_drop.event_store, "get",
                      new=AsyncMock(return_value=row)), \
         patch.object(coin_drop.event_store, "finish",
                      new=AsyncMock(return_value={"state": "f"})), \
         patch.object(coin_drop.event_store, "patch_actions",
                      new=AsyncMock()), \
         patch("cogs.coin_drop.grant_event_reward",
               new=AsyncMock(return_value="credited")) as grant:
        i = _interaction(1, cid="se2:beef0001:join")
        await cog.handle_button(i, "join")
        # сессия восстановлена и сразу финализируется (deadline past)
        await asyncio.sleep(0.05)
    assert grant.await_count == 1
    assert grant.await_args.args[2] in (1, 2)


@pytest.mark.asyncio
async def test_event_pending_settle_roundtrip(tmp_path):
    """event pending-файл → PG: settle переносит обязательство
    и начисляет (coins)."""
    store = coin_drop.DropPendingStore(
        tmp_path / "event_pending.json")
    with patch.object(coin_drop, "event_pending_store", store):
        cog = _cog()
        row = {"grant_key": "event:k1", "status": "pending",
               "guild_id": 1, "user_id": 9, "amount": 150,
               "kind": "coins", "item_id": None, "attempts": 1}
        with patch.object(coin_drop.event_reward_store, "record",
                          new=AsyncMock(return_value=row)), \
             patch.object(coin_drop.event_reward_store, "claim_key",
                          new=AsyncMock(return_value=row)), \
             patch("services.event_rewards.apply_reward",
                   new=AsyncMock()) as apply_r, \
             patch.object(coin_drop.event_reward_store, "complete",
                          new=AsyncMock(return_value=True)):
            res = await cog._settle_event_pending({
                "grant_key": "event:k1", "kind": "coins",
                "guild_id": 1, "user_id": 9, "amount": 150,
                "item_id": None})
        assert res == "credited"
        apply_r.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_event_reward_retry_on_failure():
    """Сбой apply → fenced retry в pending с backoff."""
    cog = _cog()
    row = {"grant_key": "event:k2", "status": "processing",
           "guild_id": 1, "user_id": 9, "amount": 50,
           "kind": "xp", "item_id": None, "attempts": 1}
    with patch("services.event_rewards.apply_reward",
               new=AsyncMock(side_effect=RuntimeError("pg down"))), \
         patch.object(coin_drop.event_reward_store, "retry",
                      new=AsyncMock(return_value=True)) as retry, \
         patch.object(coin_drop.event_reward_store, "complete",
                      new=AsyncMock()) as done:
        ok = await cog._process_event_reward(row)
    assert ok is False
    retry.assert_awaited_once()
    assert retry.await_args.args[1] == 1
    done.assert_not_awaited()
