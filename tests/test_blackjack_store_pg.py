"""PG-интеграция BLACKJACK 2.0: атомарный дебет, CAS-ходы,
идемпотентный settlement, refund, тайм-аут, конкурентные списания.

Реальная временная PostgreSQL (pgserver) — production-БД не трогаем.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

import storage.db as dbmod
from games.blackjack_engine import OUT_PUSH, OUT_WIN, Round
from storage import blackjack_store as bj
from storage.user_balance_store import user_balance_store

G, U = 777, 4242

DDL = """
CREATE TABLE IF NOT EXISTS user_balance (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    balance INTEGER DEFAULT 100,
    PRIMARY KEY (guild_id, user_id)
);
"""


def _game(player_total=20, dealer_total=18):
    """Детерминированная раздача без natural: фаза player."""
    def hand(total):
        if total == 20:
            return [("10", "♠"), ("9", "♥"), ("A", "♦")]  # 20 soft? 10+9+A=20
        if total == 18:
            return [("10", "♣"), ("8", "♠")]
        return [("5", "♠"), ("6", "♥")]
    return Round(
        deck=[("2", "♣"), ("3", "♦")],
        player=hand(player_total),
        dealer=hand(dealer_total),
    )


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_bj")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=1, max_size=4)
    prev_url = dbmod.DATABASE_URL
    prev_pool = dbmod._pool
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    store = bj.blackjack_store
    store._schema_ready = False
    async with pool.acquire() as conn:
        await conn.execute(DDL)
        await store._ensure_schema(conn)
        await conn.execute("DELETE FROM blackjack_ledger")
        await conn.execute("DELETE FROM blackjack_rounds")
        await conn.execute("DELETE FROM user_balance")
    try:
        yield pool
    finally:
        dbmod._pool = prev_pool
        dbmod.DATABASE_URL = prev_url
        store._schema_ready = False
        await pool.close()


async def _set_balance(amount: int) -> None:
    pool = await dbmod.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_balance (guild_id, user_id, balance) "
            "VALUES ($1,$2,$3) ON CONFLICT (guild_id,user_id) "
            "DO UPDATE SET balance=$3",
            G, U, amount)


async def _balance() -> int:
    return await user_balance_store.get_balance(G, U)


@pytest.mark.asyncio
async def test_create_round_debits_atomically(pg_pool):
    await _set_balance(1000)
    rid, _created = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    assert rid
    assert await _balance() == 500
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.stake == 500 and stored.status == "active"
    # ledger stake записан
    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM blackjack_ledger WHERE round_id=$1 AND op='stake'",
            rid)
    assert n == 1


@pytest.mark.asyncio
async def test_insufficient_funds_no_round(pg_pool):
    await _set_balance(100)
    with pytest.raises(bj.InsufficientFunds):
        await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    assert await _balance() == 100
    assert await bj.blackjack_store.get_active(G, U) is None


@pytest.mark.asyncio
async def test_one_active_game_per_user(pg_pool):
    await _set_balance(5000)
    rid1, _ = await bj.blackjack_store.create_round(G, U, 55, 100, _game())
    with pytest.raises(bj.ActiveGameExists):
        await bj.blackjack_store.create_round(G, U, 55, 100, _game())
    # другой пользователь — можно
    rid2, _ = await bj.blackjack_store.create_round(G, U + 1, 55, 100, _game())
    assert rid1 and rid2


@pytest.mark.asyncio
async def test_concurrent_debits_no_negative(pg_pool):
    """create_round(600) vs subtract_balance(600) при балансе 1000 —
    ровно одна операция проходит, баланс не уходит в минус."""
    await _set_balance(1000)
    results = await asyncio.gather(
        bj.blackjack_store.create_round(G, U, 55, 600, _game()),
        _try_subtract(600),
        return_exceptions=True,
    )
    bal = await _balance()
    assert bal >= 0
    # сумма успешных списаний ≤ 1000
    spent = 0
    for r in results:
        if isinstance(r, tuple) or r is True:
            # (round_id, created) — create_round; True — subtract_balance
            spent += 600
    assert spent <= 1000
    assert bal == 1000 - spent


async def _try_subtract(amount: int) -> bool:
    try:
        await user_balance_store.subtract_balance(G, U, amount)
        return True
    except ValueError:
        return False


@pytest.mark.asyncio
async def test_apply_action_hit_then_stand_settles_once(pg_pool):
    await _set_balance(2000)
    rid, _created = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    bal_after_deal = await _balance()
    assert bal_after_deal == 1500

    # два конкурентных хода — ровно один успешен
    r1, r2 = await asyncio.gather(
        bj.blackjack_store.apply_action(rid, "hit"),
        bj.blackjack_store.apply_action(rid, "stand"),
        return_exceptions=True,
    )
    oks = [r for r in (r1, r2) if not isinstance(r, Exception)]
    assert len(oks) == 1
    updated = oks[0]
    assert updated.game.phase == "done"  # hit→? stand→done; hit может дать done при 21

    # повторный settle/ход — RoundNotActive, баланс не меняется
    bal_settled = await _balance()
    with pytest.raises(bj.RoundNotActive):
        await bj.blackjack_store.apply_action(rid, "hit")
    assert await _balance() == bal_settled

    # ledger payout ровно одна запись
    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM blackjack_ledger WHERE round_id=$1 AND op='payout'",
            rid)
    assert n == 1


@pytest.mark.asyncio
async def test_settlement_payout_amounts(pg_pool):
    # баланс держим < DECAY_THRESHOLD(10000), иначе get_balance сам
    # применит дневной decay при чтении
    await _set_balance(9000)
    # win: игрок 20 vs дилер 18 → stand: дилер 18 стоит → win → gross 1000
    rid, _created = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    updated = await bj.blackjack_store.apply_action(rid, "stand")
    assert updated.outcome == OUT_WIN
    assert updated.game.reason in ("higher", "dealer_bust")
    expected = 9000 - 500 + 2 * 500
    assert await _balance() == expected


@pytest.mark.asyncio
async def test_push_returns_stake(pg_pool):
    await _set_balance(1000)
    # deal_deck: player=[deck[-1],deck[-2]], dealer=[deck[-3],deck[-4]]
    game = Round.deal_deck([
        ("2", "♠"),
        ("A", "♣"), ("K", "♦"),   # dealer: BJ
        ("A", "♠"), ("Q", "♥"),   # player: BJ
    ])
    assert game.phase == "done" and game.outcome == OUT_PUSH
    rid, _created = await bj.blackjack_store.create_round(G, U, 55, 500, game)
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.status == "settled"
    assert stored.outcome == OUT_PUSH
    # push → gross=stake → баланс вернулся к 1000
    assert await _balance() == 1000


@pytest.mark.asyncio
async def test_refund_only_before_message(pg_pool):
    await _set_balance(1000)
    rid, _created = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    assert await _balance() == 500
    # до доставки (message_id NULL) — refund ок
    assert await bj.blackjack_store.refund_undelivered(rid) is True
    assert await _balance() == 1000
    # повтор — False (idempotent)
    assert await bj.blackjack_store.refund_undelivered(rid) is False
    assert await _balance() == 1000


@pytest.mark.asyncio
async def test_no_refund_after_message_delivered(pg_pool):
    await _set_balance(1000)
    rid, _created = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    await bj.blackjack_store.mark_message(rid, 999)
    assert await bj.blackjack_store.refund_undelivered(rid) is False
    assert await _balance() == 500


@pytest.mark.asyncio
async def test_expire_due_auto_stands(pg_pool):
    await _set_balance(2000)
    rid, _created = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    # доставка подтверждена — иначе expire_due ждёт reconcile/refund
    await bj.blackjack_store.mark_message(rid, 555)
    # deadline в прошлое
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE blackjack_rounds SET deadline=$2 WHERE round_id=$1",
            rid, datetime.now(UTC) - timedelta(seconds=5))
    settled = await bj.blackjack_store.expire_due()
    assert any(s.round_id == rid for s in settled)
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.status == "settled" and stored.game.phase == "done"
    # повторный expire — пусто
    assert await bj.blackjack_store.expire_due() == []


@pytest.mark.asyncio
async def test_recovery_lists_active(pg_pool):
    await _set_balance(2000)
    rid, _created = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    active = await bj.blackjack_store.list_active()
    assert any(s.round_id == rid for s in active)


# ------------------------------------------------------------------
# Hardening: version CAS, deadline, op_id, cap, delivery states
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_version_rejected(pg_pool):
    """expected_version ≠ текущей → StaleVersion, состояние не меняется."""
    await _set_balance(2000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    stored = await bj.blackjack_store.get_round(rid)
    ver = stored.version
    with pytest.raises(bj.StaleVersion):
        await bj.blackjack_store.apply_action(rid, "hit",
                                              expected_version=ver + 99)
    # состояние не изменилось
    again = await bj.blackjack_store.get_round(rid)
    assert again.version == ver and again.game.phase == "player"
    # правильная версия проходит
    ok = await bj.blackjack_store.apply_action(rid, "hit",
                                               expected_version=ver)
    assert ok.version == ver + 1


@pytest.mark.asyncio
async def test_concurrent_same_version_only_one_wins(pg_pool):
    """Два клика с одной версией кнопки → ровно один apply_action."""
    await _set_balance(2000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    stored = await bj.blackjack_store.get_round(rid)
    ver = stored.version
    r1, r2 = await asyncio.gather(
        bj.blackjack_store.apply_action(rid, "hit", expected_version=ver),
        bj.blackjack_store.apply_action(rid, "stand", expected_version=ver),
        return_exceptions=True,
    )
    oks = [r for r in (r1, r2) if not isinstance(r, Exception)]
    errs = [r for r in (r1, r2) if isinstance(r, Exception)]
    assert len(oks) == 1
    # второй — либо StaleVersion, либо RoundNotActive (если первый завершил)
    assert all(isinstance(e, (bj.StaleVersion, bj.RoundNotActive))
               for e in errs)


@pytest.mark.asyncio
async def test_expired_deadline_hit_becomes_auto_stand(pg_pool):
    """Ход после истечения deadline → детерминированный auto-Stand
    прямо в транзакции, даже если sweeper ещё не сработал."""
    await _set_balance(2000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE blackjack_rounds SET deadline=$2 WHERE round_id=$1",
            rid, datetime.now(UTC) - timedelta(seconds=1))
    updated = await bj.blackjack_store.apply_action(rid, "hit")
    assert updated.game.phase == "done"
    assert updated.status == "settled"


@pytest.mark.asyncio
async def test_auto_stand_before_deadline_rejected(pg_pool):
    """sweeper не может завершить раздачу досрочно."""
    await _set_balance(2000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    with pytest.raises(bj.RoundNotActive):
        await bj.blackjack_store.apply_action(rid, "auto_stand")
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.status == "active"


@pytest.mark.asyncio
async def test_op_id_idempotent_replay_no_double_debit(pg_pool):
    """Повторное подтверждение с тем же op_id → тот же round_id,
    без повторного списания — включая мгновенный natural."""
    await _set_balance(2000)
    game = Round.deal_deck([
        ("2", "♠"),
        ("A", "♣"), ("K", "♦"),   # dealer natural
        ("A", "♠"), ("Q", "♥"),   # player natural → push
    ])
    assert game.phase == "done"
    op = "op-" + "x" * 20
    rid1, created1 = await bj.blackjack_store.create_round(
        G, U, 55, 500, game, op_id=op)
    assert created1 is True
    bal1 = await _balance()  # 2000-500+500(push)=2000
    assert bal1 == 2000
    # повтор с тем же op_id — natural уже settled, partial index
    # не защищает → только op_id спасает от дабл-списания
    rid2, created2 = await bj.blackjack_store.create_round(
        G, U, 55, 500, game, op_id=op)
    assert rid2 == rid1 and created2 is False
    assert await _balance() == bal1 == 2000


@pytest.mark.asyncio
async def test_op_id_sequential_replay_active_round(pg_pool):
    await _set_balance(2000)
    op = "op-" + "y" * 20
    rid1, c1 = await bj.blackjack_store.create_round(
        G, U, 55, 500, _game(), op_id=op)
    assert c1
    rid2, c2 = await bj.blackjack_store.create_round(
        G, U, 55, 500, _game(), op_id=op)
    assert (rid2, c2) == (rid1, False)
    assert await _balance() == 1500  # списано один раз


@pytest.mark.asyncio
async def test_payout_not_capped_below_promised(pg_pool, monkeypatch):
    """Выплата не «съедается» max_balance: ставка уже списана,
    ledger и UI обещают gross — credit обязан исполнить gross."""
    from storage import user_balance_store as ubs
    monkeypatch.setattr(ubs.decay_tracker, "should_apply_decay",
                        lambda *a, **k: False)
    await _set_balance(99500)  # у cap=100000
    rid, _ = await bj.blackjack_store.create_round(
        G, U, 55, 1000, _game())  # win → gross 2000
    await bj.blackjack_store.apply_action(rid, "stand")
    # 99500 - 1000 + 2000 = 100500 > cap — выплачено полностью
    assert await _balance() == 100500
    async with pg_pool.acquire() as conn:
        amt = await conn.fetchval(
            "SELECT amount FROM blackjack_ledger "
            "WHERE round_id=$1 AND op='payout'", rid)
    assert amt == 2000


@pytest.mark.asyncio
async def test_refund_blocked_in_unknown_state(pg_pool):
    """'unknown'/'sending' → refund запрещён: доставка неоднозначна."""
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    await bj.blackjack_store.mark_delivery(rid, "unknown")
    assert await bj.blackjack_store.refund_undelivered(rid) is False
    assert await _balance() == 500
    # 'failed' (доказанная недоставка) → refund разрешён
    await bj.blackjack_store.mark_delivery(rid, "failed")
    assert await bj.blackjack_store.refund_undelivered(rid) is True
    assert await _balance() == 1000


@pytest.mark.asyncio
async def test_mark_delivery_never_regresses_delivered(pg_pool):
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    await bj.blackjack_store.mark_message(rid, 555)
    await bj.blackjack_store.mark_delivery(rid, "unknown")
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "delivered"


# ------------------------------------------------------------------
# Reconcile доставки (fake Discord client)
# ------------------------------------------------------------------

class _FakeHistory:
    def __init__(self, messages):
        self._messages = messages

    def __aiter__(self):
        async def gen():
            for m in self._messages:
                yield m
        return gen()


class _FakeChannel:
    def __init__(self, messages):
        self._messages = messages

    def history(self, **_kw):
        return _FakeHistory(self._messages)


class _FakeComponents:
    def __init__(self, content):
        self._content = content

    def to_dict(self):
        return {"type": 10, "content": self._content}


class _FakeMessage:
    def __init__(self, mid, content):
        self.id = mid
        self.components = [_FakeComponents(content)]


class _FakeClient:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, _cid):
        return self._channel


@pytest.mark.asyncio
async def test_reconcile_finds_marker_and_confirms_delivery(pg_pool):
    """'unknown' + сообщение с маркером в истории → delivered."""
    from games import blackjack as bjgame
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    await bj.blackjack_store.mark_delivery(rid, "unknown")
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE blackjack_rounds SET created_at=$2 WHERE round_id=$1",
            rid, datetime.now(UTC) - timedelta(minutes=10))
    msg = _FakeMessage(4321, f"карта\n-# ref {rid}")
    client = _FakeClient(_FakeChannel([msg]))
    handled = await bjgame.reconcile_undelivered(client)
    assert handled == 1
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "delivered"
    assert stored.message_id == 4321
    # refund после подтверждённой доставки запрещён
    assert await bj.blackjack_store.refund_undelivered(rid) is False


@pytest.mark.asyncio
async def test_reconcile_no_marker_is_unknown_no_refund(pg_pool):
    """'unknown' + маркер не найден → UNKNOWN: БЕЗ refund.

    Отсутствие маркера не доказывает недоставку (сообщение могло быть
    удалено/изменено/не влезть в лимит истории). Раздача живёт
    обычным тайм-аутом, попытка reconcile зачитывается."""
    from games import blackjack as bjgame
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    await bj.blackjack_store.mark_delivery(rid, "unknown")
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE blackjack_rounds SET created_at=$2 WHERE round_id=$1",
            rid, datetime.now(UTC) - timedelta(minutes=10))
    client = _FakeClient(_FakeChannel([_FakeMessage(1, "-# ref other")]))
    handled = await bjgame.reconcile_undelivered(client)
    assert handled == 0
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.status == "active"
    assert stored.delivery_state == "unknown"
    # ставка НЕ возвращена — останется до разрешения раздачи
    assert await _balance() == 500
    async with pg_pool.acquire() as conn:
        attempts = await conn.fetchval(
            "SELECT reconcile_attempts FROM blackjack_rounds "
            "WHERE round_id=$1", rid)
    assert attempts == 1


@pytest.mark.asyncio
async def test_reconcile_pending_round_refunded(pg_pool):
    """Крах между create и send → 'pending' → refund без сканирования."""
    from games import blackjack as bjgame
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE blackjack_rounds SET created_at=$2 WHERE round_id=$1",
            rid, datetime.now(UTC) - timedelta(minutes=10))
    # канал недоступен — для 'pending' скан не нужен
    client = _FakeClient(None)
    await bjgame.reconcile_undelivered(client)
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.status == "refunded"
    assert await _balance() == 1000


# ------------------------------------------------------------------
# Атомарный захват доставки (claim CAS) — §5
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_delivery_single_winner(pg_pool):
    """Два параллельных обработчика → ровно один получает 'sending'."""
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    results = await asyncio.gather(
        bj.blackjack_store.claim_delivery(rid, "attempt-A"),
        bj.blackjack_store.claim_delivery(rid, "attempt-B"),
    )
    assert sorted(results) == [False, True]
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "sending"


@pytest.mark.asyncio
async def test_claim_delivery_rejected_when_in_flight_or_done(pg_pool):
    """'sending'/'unknown'/'delivered' не клеймятся — blind resend
    запрещён; 'failed' (pre-send сбой) — повторная попытка разрешена."""
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    assert await bj.blackjack_store.claim_delivery(rid, "a1") is True
    # второй claim во время 'sending' — отказ
    assert await bj.blackjack_store.claim_delivery(rid, "a2") is False
    # 'unknown' — тоже отказ
    await bj.blackjack_store.mark_delivery(rid, "unknown", "a1")
    assert await bj.blackjack_store.claim_delivery(rid, "a3") is False
    # 'failed' → retry разрешён (pre-send доказанно не дошёл)
    await bj.blackjack_store.mark_delivery(rid, "failed")
    assert await bj.blackjack_store.claim_delivery(rid, "a4") is True


@pytest.mark.asyncio
async def test_mark_delivery_attempt_cas(pg_pool):
    """mark_delivery с attempt_id — только от владельца 'sending'.
    Устаревший/чужой обработчик не может перезаписать состояние."""
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    await bj.blackjack_store.claim_delivery(rid, "owner")
    # чужой attempt → перехода нет
    await bj.blackjack_store.mark_delivery(rid, "failed", "intruder")
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "sending"
    # владелец → переход есть
    await bj.blackjack_store.mark_delivery(rid, "unknown", "owner")
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "unknown"


@pytest.mark.asyncio
async def test_refund_blocked_while_sending(pg_pool):
    """Гонка доставка↔refund: 'sending' → refund_undelivered=False,
    ставка не возвращается пока исход HTTP неизвестен."""
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    await bj.blackjack_store.claim_delivery(rid, "a1")
    assert await bj.blackjack_store.refund_undelivered(rid) is False
    assert await _balance() == 500


@pytest.mark.asyncio
async def test_detach_message_transfers_card_ownership(pg_pool):
    """Новая раздача снимает message_id у старой settled-раздачи —
    старые обработчики теряют право редактировать карточку.
    Активная раздача не затрагивается."""
    await _set_balance(10000)
    old_rid, _ = await bj.blackjack_store.create_round(
        G, U, 55, 0, _game())
    await bj.blackjack_store.apply_action(old_rid, "stand")  # → settled
    await bj.blackjack_store.mark_message(old_rid, 999)
    # вторая (новая) раздача получает ту же карточку
    new_rid, _ = await bj.blackjack_store.create_round(
        G + 1, U, 55, 0, _game())
    moved = await bj.blackjack_store.detach_message(
        55, 999, except_round_id=new_rid)
    assert moved == 1
    old = await bj.blackjack_store.get_round(old_rid)
    assert old.message_id is None
    # повторный detach — идемпотентно 0
    assert await bj.blackjack_store.detach_message(
        55, 999, except_round_id=new_rid) == 0


@pytest.mark.asyncio
async def test_expire_due_skips_proven_undelivered(pg_pool):
    """§7: 'pending'/'failed' недоставленные раздачи НЕ рассчитываются
    как игра — их закрывает refund, а не auto-stand."""
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE blackjack_rounds SET deadline=$2 WHERE round_id=$1",
            rid, datetime.now(UTC) - timedelta(seconds=1))
    settled = await bj.blackjack_store.expire_due()
    assert all(s.round_id != rid for s in settled)
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.status == "active"  # ждёт reconcile/refund


# ------------------------------------------------------------------
# Типизированная проверка доставки — §4
# ------------------------------------------------------------------

class _FakeHistoryError(Exception):
    pass


class _FakeHistoryFail:
    """История, падающая посередине итерации."""

    def __init__(self, messages, fail_after):
        self._messages = messages
        self._fail_after = fail_after

    def __aiter__(self):
        async def gen():
            for i, m in enumerate(self._messages):
                if i >= self._fail_after:
                    raise _FakeHistoryError("discord http 500")
                yield m
        return gen()


class _FakeChannelFail(_FakeChannel):
    def __init__(self, messages, fail_after):
        super().__init__(messages)
        self._fail_after = fail_after

    def history(self, **kw):
        return _FakeHistoryFail(self._messages, self._fail_after)


class _FakeClientNoFetch:
    """get_channel → None, fetch_channel → Forbidden."""

    def get_channel(self, _cid):
        return None

    async def fetch_channel(self, _cid):
        raise _FakeHistoryError("discord.Forbidden")


class _FakeComponentBad:
    def to_dict(self):
        raise RuntimeError("cannot serialize")


class _FakeMessageBadComponents:
    def __init__(self, mid):
        self.id = mid
        self.components = [_FakeComponentBad()]


async def _old_round(pg_pool, rid):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE blackjack_rounds SET created_at=$2 WHERE round_id=$1",
            rid, datetime.now(UTC) - timedelta(minutes=10))


async def _mk_unknown(pg_pool, stake=500):
    await _set_balance(5000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, stake, _game())
    await bj.blackjack_store.claim_delivery(rid, "a1")
    await bj.blackjack_store.mark_delivery(rid, "unknown", "a1")
    await _old_round(pg_pool, rid)
    return rid


@pytest.mark.asyncio
async def test_check_delivery_pending_is_confirmed_not_sent(pg_pool):
    """'pending' — HTTP не вызывался → CONFIRMED_NOT_SENT (единственный
    путь к автоматическому refund)."""
    from games.blackjack import DeliveryCheck, check_delivery
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    stored = await bj.blackjack_store.get_round(rid)
    status, msg = await check_delivery(_FakeClient(None), stored)
    assert status is DeliveryCheck.CONFIRMED_NOT_SENT
    assert msg is None


@pytest.mark.asyncio
async def test_reconcile_api_error_mid_history_no_refund(pg_pool):
    """Discord HTTPException посередине чтения истории → UNKNOWN,
    без refund и без 'failed'."""
    from games import blackjack as bjgame
    rid = await _mk_unknown(pg_pool)
    channel = _FakeChannelFail(
        [_FakeMessage(i, f"-# ref x{i}") for i in range(10)], 3)
    handled = await bjgame.reconcile_undelivered(_FakeClient(channel))
    assert handled == 0
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "unknown"
    assert stored.status == "active"
    assert await _balance() == 4500


@pytest.mark.asyncio
async def test_reconcile_channel_unavailable_no_refund(pg_pool):
    """Канал недоступен (Forbidden/удалён) → UNKNOWN, не 'failed'."""
    from games import blackjack as bjgame
    rid = await _mk_unknown(pg_pool)
    handled = await bjgame.reconcile_undelivered(_FakeClientNoFetch())
    assert handled == 0
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "unknown"
    assert stored.status == "active"
    assert await _balance() == 4500


@pytest.mark.asyncio
async def test_reconcile_scan_limit_no_refund(pg_pool):
    """История > лимита сканирования без маркера → UNKNOWN."""
    from games import blackjack as bjgame
    from games.blackjack import _SCAN_LIMIT
    rid = await _mk_unknown(pg_pool)
    msgs = [_FakeMessage(i, f"-# ref other{i}") for i in range(_SCAN_LIMIT)]
    handled = await bjgame.reconcile_undelivered(
        _FakeClient(_FakeChannel(msgs)))
    assert handled == 0
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "unknown"
    assert stored.status == "active"
    assert await _balance() == 4500


@pytest.mark.asyncio
async def test_reconcile_component_parse_error_no_refund(pg_pool):
    """Ошибка to_dict() CV2-компонентов → UNKNOWN (скан неполный)."""
    from games import blackjack as bjgame
    rid = await _mk_unknown(pg_pool)
    msgs = [
        _FakeMessageBadComponents(1),
        _FakeMessage(2, f"-# ref {rid}"),  # маркер есть, но скан уже UNKNOWN
    ]
    handled = await bjgame.reconcile_undelivered(
        _FakeClient(_FakeChannel(msgs)))
    assert handled == 0
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "unknown"
    assert stored.status == "active"
    assert await _balance() == 4500


@pytest.mark.asyncio
async def test_reconcile_deleted_delivered_message_no_refund(pg_pool):
    """Сообщение было доставлено, но удалено → маркера нет →
    UNKNOWN, НЕ refund (доказать недоставку нельзя)."""
    from games import blackjack as bjgame
    rid = await _mk_unknown(pg_pool)
    # история есть, маркера нет — как будто сообщение удалено
    handled = await bjgame.reconcile_undelivered(
        _FakeClient(_FakeChannel([_FakeMessage(9, "обычное сообщение")])))
    assert handled == 0
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.status == "active"
    assert await _balance() == 4500


@pytest.mark.asyncio
async def test_reconcile_repeat_eventually_disputed(pg_pool, monkeypatch):
    """Повторный reconcile: счётчик растёт, после MAX → 'disputed'
    (админ-диагностика), refund так и не выполняется."""
    from games import blackjack as bjgame
    monkeypatch.setattr(bjgame, "MAX_RECONCILE_ATTEMPTS", 2)
    rid = await _mk_unknown(pg_pool)
    client = _FakeClient(_FakeChannel([_FakeMessage(1, "no marker")]))
    await bjgame.reconcile_undelivered(client)
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "unknown"
    await bjgame.reconcile_undelivered(client)
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "disputed"
    # 'disputed' исключён из list_undelivered — больше не сканируется
    handled = await bjgame.reconcile_undelivered(client)
    assert handled == 0
    # refund по-прежнему запрещён
    assert await bj.blackjack_store.refund_undelivered(rid) is False
    assert await _balance() == 4500


@pytest.mark.asyncio
async def test_reconcile_finds_marker_after_restart_pg(pg_pool):
    """Рестарт между claim и сохранением message_id: 'sending' старше
    grace → reconcile находит маркер → delivered (не refund, не resend)."""
    from games import blackjack as bjgame
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    await bj.blackjack_store.claim_delivery(rid, "crashed-attempt")
    # процесс «умер» — состояние осталось 'sending', message_id NULL
    await _old_round(pg_pool, rid)
    msg = _FakeMessage(7777, f"карта\n-# ref {rid}")
    handled = await bjgame.reconcile_undelivered(
        _FakeClient(_FakeChannel([msg])))
    assert handled == 1
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "delivered"
    assert stored.message_id == 7777
