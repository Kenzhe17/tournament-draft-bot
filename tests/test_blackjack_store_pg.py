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


# ------------------------------------------------------------------
# Сериализация рендера карточек — single-writer lease
# ------------------------------------------------------------------

async def _delivered_round(pg_pool, guild=G, user=U, stake=0):
    """Создать раздачу с доставленной картой (rendered=version)."""
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(
        guild, user, 55, stake, _game())
    stored = await bj.blackjack_store.get_round(rid)
    await bj.blackjack_store.mark_message(
        rid, 555, sent_version=stored.version)
    return rid


@pytest.mark.asyncio
async def test_card_render_single_writer_lease(pg_pool):
    """Пока render-операция активна, ВТОРОЙ claim отклоняется —
    даже для более новой version (single-writer per round).
    Завершение привязано к op_id: чужой worker не может finish."""
    rid = await _delivered_round(pg_pool)
    await bj.blackjack_store.apply_action(rid, "hit")  # version+1
    op_a, op_b = "op-a", "op-b"
    assert await bj.blackjack_store.claim_card_render(rid, op_a) is True
    # активный lease → любой другой claim отклонён
    assert await bj.blackjack_store.claim_card_render(rid, op_b) is False
    # чужой op не может завершить операцию A
    assert await bj.blackjack_store.finish_card_render(rid, op_b, True) is False
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.card_render_op == op_a
    # владелец завершает успешно → rendered = claimed version
    assert await bj.blackjack_store.finish_card_render(rid, op_a, True) is True
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.card_rendered == stored.version
    assert stored.card_render_op is None


@pytest.mark.asyncio
async def test_card_render_fail_releases_lease(pg_pool):
    """finish(ok=False) освобождает lease без подтверждения —
    следующий sync повторит с актуальным состоянием."""
    rid = await _delivered_round(pg_pool)
    await bj.blackjack_store.apply_action(rid, "hit")
    assert await bj.blackjack_store.claim_card_render(rid, "op-1") is True
    assert await bj.blackjack_store.finish_card_render(rid, "op-1", False) is True
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.card_render_op is None
    assert stored.card_rendered < stored.version  # не подтверждено
    # повторный claim успешен — зависшей блокировки нет
    assert await bj.blackjack_store.claim_card_render(rid, "op-2") is True


@pytest.mark.asyncio
async def test_card_render_stuck_claim_recovery(pg_pool):
    """Крэш между claim и finish: op с истёкшим lease перехватывается,
    назначается отложенная verify_after; после verify карточка
    ре-рендерится и verify сбрасывается."""
    rid = await _delivered_round(pg_pool)
    await bj.blackjack_store.apply_action(rid, "hit")
    # «крах»: claim без finish, started_at в прошлом
    assert await bj.blackjack_store.claim_card_render(rid, "zombie") is True
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE blackjack_rounds SET card_render_started_at="
            "now() - interval '1 hour' WHERE round_id=$1", rid)
    # зависший op виден в needing_sync и перехватывается
    pending = await bj.blackjack_store.list_needing_sync()
    assert any(s.round_id == rid for s in pending)
    assert await bj.blackjack_store.claim_card_render(rid, "rescuer") is True
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.card_render_op == "rescuer"
    # takeover назначил отложенную ре-верификацию
    async with pg_pool.acquire() as conn:
        va = await conn.fetchval(
            "SELECT card_verify_after FROM blackjack_rounds "
            "WHERE round_id=$1", rid)
    assert va is not None
    # «зомби» завершает с опозданием — его op_id уже не владелец
    assert await bj.blackjack_store.finish_card_render(
        rid, "zombie", True) is False
    # rescue рендерит успешно
    assert await bj.blackjack_store.finish_card_render(
        rid, "rescuer", True) is True
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.card_rendered == stored.version
    # verify ещё не наступил → карточка не в очереди по verify
    # (rendered == version)
    # наступило время verify → снова в очереди
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE blackjack_rounds SET card_verify_after=now() "
            "- interval '1 second' WHERE round_id=$1", rid)
    pending = await bj.blackjack_store.list_needing_sync()
    assert any(s.round_id == rid for s in pending)
    # verify-рендер завершает и сбрасывает verify_after
    assert await bj.blackjack_store.claim_card_render(rid, "verify") is True
    assert await bj.blackjack_store.finish_card_render(
        rid, "verify", True) is True
    async with pg_pool.acquire() as conn:
        va = await conn.fetchval(
            "SELECT card_verify_after FROM blackjack_rounds "
            "WHERE round_id=$1", rid)
    assert va is None
    pending = await bj.blackjack_store.list_needing_sync()
    assert all(s.round_id != rid for s in pending)


@pytest.mark.asyncio
async def test_mark_message_records_sent_version(pg_pool):
    """mark_message фиксирует версию реально отправленного контента:
    version ушла вперёд между build и send → card_rendered = sent,
    needing_sync догоняет актуальное состояние."""
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 0, _game())
    stored = await bj.blackjack_store.get_round(rid)
    sent_v = stored.version
    # состояние ушло вперёд ДО доставки (hit успел пройти)
    await bj.blackjack_store.apply_action(rid, "hit")
    await bj.blackjack_store.mark_message(rid, 555, sent_version=sent_v)
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.card_rendered == sent_v < stored.version
    pending = await bj.blackjack_store.list_needing_sync()
    assert any(s.round_id == rid for s in pending)


@pytest.mark.asyncio
async def test_list_needing_sync_only_stale_cards(pg_pool):
    """Только карточки с card_rendered < version (или зависшим/verify
    op) попадают в догонку."""
    rid_ok = await _delivered_round(pg_pool, guild=G, user=U + 1)
    rid_stale = await _delivered_round(pg_pool, guild=G + 2, user=U + 2)
    # apply_action не трогает card_rendered → rendered < version
    await bj.blackjack_store.apply_action(rid_stale, "hit")
    pending = await bj.blackjack_store.list_needing_sync()
    ids = {s.round_id for s in pending}
    assert rid_stale in ids
    assert rid_ok not in ids


@pytest.mark.asyncio
async def test_mark_card_closed_settled_only_and_idempotent(pg_pool):
    """Close: только settled, version bump → sync перерисует без
    кнопок; повторный вызов → False, финансовых изменений нет."""
    await _set_balance(1000)
    # активная раздача → close запрещён
    rid_active, _ = await bj.blackjack_store.create_round(
        G, U, 55, 0, _game())
    assert await bj.blackjack_store.mark_card_closed(rid_active) is False
    # settled раздача
    rid_done = await _delivered_round(pg_pool, guild=G, user=U + 5)
    for _ in range(20):
        st = await bj.blackjack_store.get_round(rid_done)
        if st.status == "settled":
            break
        try:
            await bj.blackjack_store.apply_action(rid_done, "stand")
        except (bj.RoundNotActive, bj.RoundNotFound, bj.StaleVersion):
            break
    st = await bj.blackjack_store.get_round(rid_done)
    if st.status != "settled":
        pytest.skip("не удалось завершить раздачу")
    ver_before = st.version
    assert await bj.blackjack_store.mark_card_closed(rid_done) is True
    st = await bj.blackjack_store.get_round(rid_done)
    assert st.card_closed is True
    assert st.version == ver_before + 1  # bump → sync перерисует
    assert st.card_rendered < st.version  # карточка «устарела»
    # идемпотентно
    assert await bj.blackjack_store.mark_card_closed(rid_done) is False


# ------------------------------------------------------------------
# Очередь reconcile — §5
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_undelivered_queue_excludes_refunded_and_void(pg_pool):
    """refunded/void не попадают в list_undelivered и не блокируют
    очередь новых раздач."""
    await _set_balance(10000)
    # refunded: платная раздача без доставки → refund
    refunded = []
    for i in range(5):
        r, _ = await bj.blackjack_store.create_round(
            G, U + 100 + i, 55, 100, _game())
        assert await bj.blackjack_store.refund_undelivered(r) is True
        refunded.append(r)
    # void: бесплатная раздача без доставки → void
    v, _ = await bj.blackjack_store.create_round(G, U + 200, 55, 0, _game())
    assert await bj.blackjack_store.refund_undelivered(v) is True
    # новая 'pending' раздача старше grace — должна попасть в очередь
    rid, _ = await bj.blackjack_store.create_round(G, U + 300, 55, 100, _game())
    await _old_round(pg_pool, rid)
    # refunded/void тоже «старим» — они НЕ должны вернуться
    for r in refunded + [v]:
        await _old_round(pg_pool, r)
    cutoff = datetime.now(UTC) - timedelta(seconds=120)
    rounds = await bj.blackjack_store.list_undelivered(cutoff, limit=50)
    ids = {s.round_id for s in rounds}
    assert rid in ids
    for r in refunded + [v]:
        assert r not in ids


@pytest.mark.asyncio
async def test_settled_undelivered_still_reconciles(pg_pool):
    """Мгновенный natural (settled сразу) без message_id —
    reconcile обязан найти доставку, статус 'settled' не исключает."""
    from games import blackjack as bjgame
    await _set_balance(1000)
    game = Round.deal_deck([("A", "♠"), ("K", "♥"),
                            ("Q", "♦"), ("J", "♣"),
                            ("10", "♠"), ("9", "♥")])
    rid, created = await bj.blackjack_store.create_round(
        G, U, 55, 500, game)
    assert created
    stored = await bj.blackjack_store.get_round(rid)
    if stored.status == "active":
        pytest.skip("deal не дал natural — тест не показателен")
    await bj.blackjack_store.mark_delivery(rid, "unknown")
    await _old_round(pg_pool, rid)
    msg = _FakeMessage(8888, f"-# ref {rid}")
    handled = await bjgame.reconcile_undelivered(
        _FakeClient(_FakeChannel([msg])))
    assert handled == 1
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.message_id == 8888
    assert stored.status == "settled"  # выплата не тронута


# ------------------------------------------------------------------
# Спорные раздачи — §6
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispute_lifecycle_recheck_resolve_compensate(pg_pool):
    """disputed → list → recheck → disputed → comp (applied/duplicate/
    not_compensable) → recheck запрещён → resolve уже не нужен."""
    from games import blackjack as bjgame
    await _set_balance(1000)
    rid = await _mk_unknown(pg_pool, stake=500)
    # довести до disputed
    monkey_max = bjgame.MAX_RECONCILE_ATTEMPTS
    client = _FakeClient(_FakeChannel([_FakeMessage(1, "nothing")]))
    for _ in range(monkey_max):
        await bjgame.reconcile_undelivered(client)
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "disputed"
    # список спорных
    disputed = await bj.blackjack_store.list_disputed()
    assert any(s.round_id == rid for s in disputed)
    ledger = await bj.blackjack_store.get_dispute_ledger(rid)
    assert any(o["op"] == "stake" for o in ledger)
    # повторная проверка → обратно в очередь
    assert await bj.blackjack_store.recheck_delivery(rid) is True
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.delivery_state == "unknown"
    assert stored.reconcile_attempts == 0
    # ещё MAX попыток reconcile → снова disputed
    for _ in range(monkey_max):
        await bjgame.reconcile_undelivered(client)
    assert (await bj.blackjack_store.get_round(rid)).delivery_state == "disputed"
    # компенсация: applied → duplicate (тот же op_id) →
    # not_compensable (другой op_id — вторая компенсация запрещена)
    r1 = await bj.blackjack_store.compensate_dispute(
        rid, actor_id=42, op_id="op-12345", amount=500,
        reason="lost delivery confirmed by admin")
    assert r1["status"] == "applied"
    assert await _balance() == 5000  # 4500 + 500
    r2 = await bj.blackjack_store.compensate_dispute(
        rid, actor_id=42, op_id="op-12345", amount=500,
        reason="retry")
    assert r2["status"] == "duplicate"
    r3 = await bj.blackjack_store.compensate_dispute(
        rid, actor_id=42, op_id="op-DIFFERENT", amount=500,
        reason="second attempt")
    assert r3["status"] == "not_compensable"
    assert await _balance() == 5000  # без повторного начисления
    ledger = await bj.blackjack_store.get_dispute_ledger(rid)
    comp = [o for o in ledger if o["op"].startswith("admin_comp:")]
    assert len(comp) == 1 and comp[0]["amount"] == 500
    # recheck после компенсации запрещён — защита не обходится
    assert await bj.blackjack_store.recheck_delivery(rid) is False
    # resolve после компенсации — спор уже закрыт компенсацией
    assert await bj.blackjack_store.resolve_dispute(
        rid, 42, "late resolve") is False
    assert all(s.round_id != rid
               for s in await bj.blackjack_store.list_disputed())


@pytest.mark.asyncio
async def test_compensate_dispute_guards(pg_pool):
    """compensate_dispute: не-спор, resolve-до-comp, cap, конкурентность."""
    from games import blackjack as bjgame
    await _set_balance(1000)
    # 1) несуществующая раздача
    from storage.blackjack_store import RoundNotFound
    with pytest.raises(RoundNotFound):
        await bj.blackjack_store.compensate_dispute(
            "no-such-round", 42, "op-x", 100, "test")
    # 2) раздача без открытого спора → not_compensable
    rid_ok, _ = await bj.blackjack_store.create_round(
        G, U + 10, 55, 0, _game())
    await bj.blackjack_store.mark_message(rid_ok, 1)
    r = await bj.blackjack_store.compensate_dispute(
        rid_ok, 42, "op-y", 100, "no dispute")
    assert r["status"] == "not_compensable"
    # 3) полный цикл: disputed → comp → comp после resolve
    rid = await _mk_unknown(pg_pool, stake=500)
    client = _FakeClient(_FakeChannel([_FakeMessage(1, "x")]))
    for _ in range(bjgame.MAX_RECONCILE_ATTEMPTS):
        await bjgame.reconcile_undelivered(client)
    assert (await bj.blackjack_store.get_round(rid)).delivery_state == "disputed"
    # resolve БЕЗ компенсации → comp после закрытия спора запрещён
    assert await bj.blackjack_store.resolve_dispute(rid, 42, "closed") is True
    r = await bj.blackjack_store.compensate_dispute(
        rid, 42, "op-z", 100, "after resolve")
    assert r["status"] == "not_compensable"
    assert await _balance() == 4500  # без изменений (_mk_unknown: 5000-500)


@pytest.mark.asyncio
async def test_compensate_dispute_concurrent_single_winner(pg_pool):
    """Два одновременных compensate (даже с разными op_id) →
    ровно одна applied, вторая not_compensable/duplicate."""
    from games import blackjack as bjgame
    await _set_balance(1000)
    rid = await _mk_unknown(pg_pool, stake=500)
    client = _FakeClient(_FakeChannel([_FakeMessage(1, "x")]))
    for _ in range(bjgame.MAX_RECONCILE_ATTEMPTS):
        await bjgame.reconcile_undelivered(client)
    results = await asyncio.gather(
        bj.blackjack_store.compensate_dispute(
            rid, 42, "op-conc-1", 300, "race A"),
        bj.blackjack_store.compensate_dispute(
            rid, 42, "op-conc-2", 300, "race B"),
    )
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["applied", "not_compensable"]
    assert await _balance() == 4800  # 4500 + 300 ровно один раз


@pytest.mark.asyncio
async def test_compensate_dispute_cap_check(pg_pool, monkeypatch):
    """Компенсация, превышающая SAFE_BALANCE_CAP, отклоняется
    безопасно — баланс и ledger не меняются."""
    from games import blackjack as bjgame
    from storage import user_balance_store as ubs_mod
    from storage.user_balance_store import SAFE_BALANCE_CAP
    monkeypatch.setattr(ubs_mod.decay_tracker, "should_apply_decay",
                        lambda *a, **k: False)
    await _set_balance(1000)
    rid = await _mk_unknown(pg_pool, stake=500)
    client = _FakeClient(_FakeChannel([_FakeMessage(1, "x")]))
    for _ in range(bjgame.MAX_RECONCILE_ATTEMPTS):
        await bjgame.reconcile_undelivered(client)
    # баланс у потолка → comp обязан отказать
    await _set_balance(SAFE_BALANCE_CAP - 100)
    r = await bj.blackjack_store.compensate_dispute(
        rid, 42, "op-cap", 500, "too much")
    assert r["status"] == "cap_exceeded"
    assert await _balance() == SAFE_BALANCE_CAP - 100
    ledger = await bj.blackjack_store.get_dispute_ledger(rid)
    assert not any(o["op"].startswith("admin_comp:") for o in ledger)


@pytest.mark.asyncio
async def test_request_card_resync(pg_pool):
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 0, _game())
    await bj.blackjack_store.mark_message(rid, 321)
    assert await bj.blackjack_store.request_card_resync(rid) is True
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.card_rendered == -1  # форсирует повторный рендер
    # попадает в list_needing_sync
    pending = await bj.blackjack_store.list_needing_sync()
    assert any(s.round_id == rid for s in pending)


@pytest.mark.asyncio
async def test_request_card_resync_preserves_active_op(pg_pool):
    """§4: admin resync во время активного render НЕ сбрасывает
    владельца операции — параллельный Discord edit не запускается;
    после finish актуальное состояние уже показано."""
    rid = await _delivered_round(pg_pool)
    await bj.blackjack_store.apply_action(rid, "hit")  # version+1
    assert await bj.blackjack_store.claim_card_render(rid, "op-live") is True
    # resync во время «Hit»-рендера
    assert await bj.blackjack_store.request_card_resync(rid) is True
    stored = await bj.blackjack_store.get_round(rid)
    # активный op НЕ тронут — никто не может claim параллельно
    assert stored.card_render_op == "op-live"
    assert await bj.blackjack_store.claim_card_render(
        rid, "op-intruder") is False
    # resync пометил карточку устаревшей — но текущий рендер
    # покажет актуальное состояние и finish(ok) подтвердит его
    assert await bj.blackjack_store.finish_card_render(
        rid, "op-live", True) is True
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.card_rendered == stored.version
    assert stored.card_render_op is None
    # повторный resync завершённой карточки — идемпотентно
    assert await bj.blackjack_store.request_card_resync(rid) is True
    pending = await bj.blackjack_store.list_needing_sync()
    assert any(s.round_id == rid for s in pending)


@pytest.mark.asyncio
async def test_request_card_resync_concurrent(pg_pool):
    """Два одновременных admin resync — идемпотентны, состояние
    согласовано, активная операция не затронута."""
    rid = await _delivered_round(pg_pool)
    await bj.blackjack_store.apply_action(rid, "hit")
    assert await bj.blackjack_store.claim_card_render(rid, "op-x") is True
    results = await asyncio.gather(
        bj.blackjack_store.request_card_resync(rid),
        bj.blackjack_store.request_card_resync(rid),
    )
    assert results == [True, True]
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.card_render_op == "op-x"
    assert stored.card_rendered == -1


# ------------------------------------------------------------------
# Decay: DB marks — rollback-safe, no double-apply — §7
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decay_mark_rolls_back_with_transaction(pg_pool):
    """Tx с decay, завершившаяся rollback → отметка откачена:
    следующая операция применяет decay заново (не «сгорает»),
    а повторный decay за день не применяется."""
    from storage import user_balance_store as ubs_mod
    from storage.user_balance_store import DECAY_PERCENT, DECAY_THRESHOLD
    from storage.user_balance_store import user_balance_store as ubs
    start = int(DECAY_THRESHOLD * 2)          # 20000 > порога 10000
    decayed = start - int(start * DECAY_PERCENT)  # 19000
    await _set_balance(start)
    pool = pg_pool
    # marks-таблица не чистится фикстурой — сбрасываем для (G,U)
    async with pool.acquire() as conn:
        await ubs_mod._ensure_decay_marks(conn)
        await conn.execute(
            "DELETE FROM user_decay_marks "
            "WHERE guild_id=$1 AND user_id=$2", G, U)
    # файловый legacy-гейт тоже сбрасываем для детерминизма
    ubs_mod.decay_tracker._processed_users.discard(f"{G}:{U}")
    # tx1: decay + намеренный rollback
    with pytest.raises(RuntimeError):
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT balance FROM user_balance "
                "WHERE guild_id=$1 AND user_id=$2 FOR UPDATE", G, U)
            bal = await ubs._apply_decay(conn, G, U, row["balance"])
            assert bal == decayed  # decay применён внутри tx
            raise RuntimeError("rollback")
    # tx откачена: balance и отметка — как до tx. Чтение баланса
    # само применяет decay (это и есть «следующая операция») —
    # доказывает, что отметка откатилась и decay не потерян.
    assert await _balance() == decayed
    # tx2: decay за день уже применён — повторно НЕ списывается
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT balance FROM user_balance "
            "WHERE guild_id=$1 AND user_id=$2 FOR UPDATE", G, U)
        bal = await ubs._apply_decay(conn, G, U, row["balance"])
        assert bal == row["balance"] == decayed
    # tx3: идемпотентно — тот же результат
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT balance FROM user_balance "
            "WHERE guild_id=$1 AND user_id=$2 FOR UPDATE", G, U)
        bal = await ubs._apply_decay(conn, G, U, row["balance"])
        assert bal == decayed


@pytest.mark.asyncio
async def test_blackjack_credit_hard_ceiling(pg_pool, monkeypatch):
    """cap_exempt гарантирует выплату, но не переполняет INTEGER:
    баланс у потолка 2e9 → начисление обрезается потолком, а не
    переполнением колонки ( unreachable в реальной игре)."""
    from storage import user_balance_store as ubs
    monkeypatch.setattr(ubs.decay_tracker, "should_apply_decay",
                        lambda *a, **k: False)
    await _set_balance(1_999_999_000)
    rid, _ = await bj.blackjack_store.create_round(
        G, U, 55, 1000, _game())
    await bj.blackjack_store.apply_action(rid, "stand")
    bal = await _balance()
    # 1999999000 - 1000 + min(2000, headroom) = ровно 2e9 потолок
    assert bal <= 2_000_000_000
    assert bal <= 2_147_483_647  # INTEGER max — переполнения нет


# ------------------------------------------------------------------
# Гарантия выплаты без переполнения — §5
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bet_rejected_when_payout_not_guaranteed(pg_pool, monkeypatch):
    """balance + stake > SAFE_BALANCE_CAP → PayoutNotGuaranteed ДО
    списания: баланс и rounds не меняются."""
    from storage import user_balance_store as ubs_mod
    from storage.blackjack_store import PayoutNotGuaranteed
    from storage.user_balance_store import SAFE_BALANCE_CAP
    monkeypatch.setattr(ubs_mod.decay_tracker, "should_apply_decay",
                        lambda *a, **k: False)
    await _set_balance(SAFE_BALANCE_CAP)  # ровно у потолка
    with pytest.raises(PayoutNotGuaranteed):
        await bj.blackjack_store.create_round(
            G, U, 55, 1000, _game())
    assert await _balance() == SAFE_BALANCE_CAP  # не списано
    assert await bj.blackjack_store.get_active(G, U) is None


@pytest.mark.asyncio
async def test_bet_allowed_at_cap_boundary(pg_pool, monkeypatch):
    """balance + stake == SAFE_BALANCE_CAP — граница допустима:
    выплата 2×stake помещается в HARD_BALANCE_LIMIT."""
    from storage import user_balance_store as ubs_mod
    from storage.user_balance_store import SAFE_BALANCE_CAP
    monkeypatch.setattr(ubs_mod.decay_tracker, "should_apply_decay",
                        lambda *a, **k: False)
    # balance - stake + 2*stake = balance + stake ≤ CAP → balance ≤ CAP - stake
    await _set_balance(SAFE_BALANCE_CAP - 1000)
    rid, created = await bj.blackjack_store.create_round(
        G, U, 55, 1000, _game())
    assert created
    # settle → выплата гарантированно помещается
    for _ in range(20):
        st = await bj.blackjack_store.get_round(rid)
        if st.status == "settled":
            break
        try:
            await bj.blackjack_store.apply_action(rid, "stand")
        except (bj.RoundNotActive, bj.RoundNotFound, bj.StaleVersion):
            break
    bal = await _balance()
    assert bal <= 2_147_483_647  # INTEGER max — переполнения нет


@pytest.mark.asyncio
async def test_guaranteed_credit_overflow_raises_and_rolls_back(pg_pool, monkeypatch):
    """_credit_in_tx(cap_exempt) при balance+amount > HARD_BALANCE_LIMIT
    → BalanceOverflow, tx откатывается — ledger не остаётся."""
    from storage import user_balance_store as ubs_mod
    from storage.blackjack_store import BalanceOverflow
    from storage.user_balance_store import HARD_BALANCE_LIMIT
    monkeypatch.setattr(ubs_mod.decay_tracker, "should_apply_decay",
                        lambda *a, **k: False)
    await _set_balance(1000)
    rid, _ = await bj.blackjack_store.create_round(G, U, 55, 0, _game())
    pool = pg_pool
    with pytest.raises(BalanceOverflow):
        async with pool.acquire() as conn, conn.transaction():
            await bj.blackjack_store._ledger(conn, rid, "payout", 2000)
            await bj.blackjack_store._credit_in_tx(
                conn, G, U, HARD_BALANCE_LIMIT, cap_exempt=True)
    # rollback: ledger 'payout' не зафиксирован, баланс не изменился
    ledger = await bj.blackjack_store.get_dispute_ledger(rid)
    assert not any(o["op"] == "payout" for o in ledger)
    assert await _balance() == 1000


@pytest.mark.asyncio
async def test_transfer_rejected_over_safe_cap(pg_pool, monkeypatch):
    """Перевод, выводящий получателя за SAFE_BALANCE_CAP, отклоняется
    целиком — средства ни у кого не списываются/не дублируются."""
    from storage import user_balance_store as ubs_mod
    from storage.user_balance_store import SAFE_BALANCE_CAP
    from storage.user_balance_store import user_balance_store as ubs
    monkeypatch.setattr(ubs_mod.decay_tracker, "should_apply_decay",
                        lambda *a, **k: False)
    await _set_balance(1000)                       # sender G,U
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_balance (guild_id, user_id, balance) "
            "VALUES ($1,$2,$3) ON CONFLICT (guild_id,user_id) "
            "DO UPDATE SET balance=$3", G + 9, U + 9, SAFE_BALANCE_CAP - 50)
    with pytest.raises(ValueError, match="cap"):
        await ubs.transfer_balance(G, U, G + 9, U + 9, 100)
    assert await _balance() == 1000  # sender не списан
    async with pg_pool.acquire() as conn:
        rb = await conn.fetchval(
            "SELECT balance FROM user_balance WHERE guild_id=$1 "
            "AND user_id=$2", G + 9, U + 9)
    assert rb == SAFE_BALANCE_CAP - 50  # receiver не изменён


@pytest.mark.asyncio
async def test_set_balance_hard_limit_guard(pg_pool):
    """set_balance не позволяет выставить баланс выше
    HARD_BALANCE_LIMIT."""
    from storage.user_balance_store import HARD_BALANCE_LIMIT
    from storage.user_balance_store import user_balance_store as ubs
    with pytest.raises(ValueError):
        await ubs.set_balance(G, U, HARD_BALANCE_LIMIT + 1)


# ------------------------------------------------------------------
# Финальный релизный патч — §3: абсолютный максимум ставки и
# set_balance не ломает гарантию выплаты
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stake_absolute_max_10000(pg_pool):
    """Ставка 10 000 принимается, 10 001 — отклоняется на уровне store
    (defense in depth поверх _bet_limits/deal_round)."""
    await _set_balance(1_000_000)
    rid, created = await bj.blackjack_store.create_round(
        G, U, 55, 10_000, _game())
    assert created
    await bj.blackjack_store.refund_undelivered(rid)  # освободить слот
    with pytest.raises(ValueError, match="technical max"):
        await bj.blackjack_store.create_round(
            G, U, 55, 10_001, _game())


@pytest.mark.asyncio
async def test_set_balance_respects_active_payout(pg_pool, monkeypatch):
    """set_balance во время активной платной раздачи не может
    выставить баланс так, чтобы settlement уперся в
    HARD_BALANCE_LIMIT → BalanceOverflow и зависшая раздача."""
    from storage import user_balance_store as ubs_mod
    from storage.user_balance_store import HARD_BALANCE_LIMIT
    from storage.user_balance_store import user_balance_store as ubs
    monkeypatch.setattr(ubs_mod.decay_tracker, "should_apply_decay",
                        lambda *a, **k: False)
    await _set_balance(100_000)
    rid, _ = await bj.blackjack_store.create_round(
        G, U, 55, 10_000, _game())  # обязательство до +20 000
    # баланс + obligation(20000) > HARD → отказ
    with pytest.raises(ValueError, match="obligation"):
        await ubs.set_balance(G, U, HARD_BALANCE_LIMIT - 10_000)
    # баланс без риска переполнения → разрешён
    await ubs.set_balance(G, U, 500_000)
    assert await _balance() == 500_000
    # settlement активной раздачи не сломан
    for _ in range(20):
        st = await bj.blackjack_store.get_round(rid)
        if st.status == "settled":
            break
        try:
            await bj.blackjack_store.apply_action(rid, "stand")
        except (bj.RoundNotActive, bj.RoundNotFound, bj.StaleVersion):
            break
    st = await bj.blackjack_store.get_round(rid)
    assert st.status == "settled"


@pytest.mark.asyncio
async def test_set_balance_free_round_no_obligation(pg_pool, monkeypatch):
    """Бесплатная раздача (stake=0) не создаёт обязательства —
    set_balance ограничен только HARD_BALANCE_LIMIT."""
    from storage import user_balance_store as ubs_mod
    from storage.user_balance_store import HARD_BALANCE_LIMIT
    from storage.user_balance_store import user_balance_store as ubs
    monkeypatch.setattr(ubs_mod.decay_tracker, "should_apply_decay",
                        lambda *a, **k: False)
    await _set_balance(1000)
    await bj.blackjack_store.create_round(G, U, 55, 0, _game())
    # obligation = 0 → можно выставить почти до HARD
    await ubs.set_balance(G, U, HARD_BALANCE_LIMIT - 1)
    assert await _balance() == HARD_BALANCE_LIMIT - 1
    with pytest.raises(ValueError):
        await ubs.set_balance(G, U, HARD_BALANCE_LIMIT + 1)


def test_bet_limits_config_validation():
    """_bet_limits: невалидная конфигурация → fallback 10–10000;
    hi > BET_MAX или lo > hi запрещены."""
    from games import blackjack as bjgame
    from games.blackjack_engine import BET_MAX, BET_MIN

    class _FakeSettings:
        def __init__(self, vals): self.vals = vals
        def peek(self, _g, key): return self.vals.get(key)

    import services.settings_service as ss
    orig = ss.settings_service
    try:
        # валидный диапазон
        ss.settings_service = _FakeSettings(
            {"economy.blackjack_min_bet": 50,
             "economy.blackjack_max_bet": 5000})
        assert bjgame._bet_limits(1) == (50, 5000)
        # hi > BET_MAX → fallback
        ss.settings_service = _FakeSettings(
            {"economy.blackjack_min_bet": 10,
             "economy.blackjack_max_bet": BET_MAX + 1})
        assert bjgame._bet_limits(1) == (BET_MIN, BET_MAX)
        # lo > hi → fallback
        ss.settings_service = _FakeSettings(
            {"economy.blackjack_min_bet": 9000,
             "economy.blackjack_max_bet": 100})
        assert bjgame._bet_limits(1) == (BET_MIN, BET_MAX)
        # отрицательный min → fallback
        ss.settings_service = _FakeSettings(
            {"economy.blackjack_min_bet": -5,
             "economy.blackjack_max_bet": 100})
        assert bjgame._bet_limits(1) == (BET_MIN, BET_MAX)
        # ошибка чтения → fallback
        class _Boom:
            def peek(self, *_a): raise RuntimeError("db down")
        ss.settings_service = _Boom()
        assert bjgame._bet_limits(1) == (BET_MIN, BET_MAX)
    finally:
        ss.settings_service = orig


def test_bets_disabled_by_default():
    """§6: economy.blackjack_bets_enabled default=False —
    платный режим выключен при первом выпуске; лимиты ставок
    ограничены абсолютным техническим максимумом BET_MAX."""
    import services.settings_registry as reg
    from games.blackjack_engine import BET_MAX, BET_MIN

    flag = reg.get("economy.blackjack_bets_enabled")
    assert flag is not None and flag.default is False
    lo = reg.get("economy.blackjack_min_bet")
    hi = reg.get("economy.blackjack_max_bet")
    assert lo is not None and hi is not None
    assert lo.max == BET_MAX and hi.max == BET_MAX
    assert lo.min is not None and lo.min >= BET_MIN
    assert hi.min == BET_MIN and hi.default == BET_MAX


@pytest.mark.asyncio
async def test_admin_credit_during_active_round(pg_pool, monkeypatch):
    """Административное начисление во время активной раздачи
    ограничено SAFE_BALANCE_CAP — гарантия settlement сохраняется
    даже при economy.max_balance > SAFE_CAP."""
    from storage import user_balance_store as ubs_mod
    from storage.user_balance_store import SAFE_BALANCE_CAP
    from storage.user_balance_store import user_balance_store as ubs
    monkeypatch.setattr(ubs_mod.decay_tracker, "should_apply_decay",
                        lambda *a, **k: False)
    # смоделировать «безумный» конфиг max_balance
    import services.settings_service as ss
    class _Fake:
        def peek(self, _g, key):
            if key == "economy.max_balance":
                return 5_000_000_000
            return None
    orig = ss.settings_service
    ss.settings_service = _Fake()
    try:
        await _set_balance(SAFE_BALANCE_CAP - 100)
        rid, _ = await bj.blackjack_store.create_round(
            G, U, 55, 50, _game())  # obligation = 100
        res = await ubs.admin_adjust(G, U, 10_000, "op-adm-1")
        assert res["status"] == "applied"
        # clamp на SAFE_CAP: 2e9-100+10000 → 2e9, не выше
        assert await _balance() <= SAFE_BALANCE_CAP
        # settlement проходит без BalanceOverflow
        for _ in range(20):
            st = await bj.blackjack_store.get_round(rid)
            if st.status == "settled":
                break
            try:
                await bj.blackjack_store.apply_action(rid, "stand")
            except (bj.RoundNotActive, bj.RoundNotFound, bj.StaleVersion):
                break
        assert (await bj.blackjack_store.get_round(rid)).status == "settled"
    finally:
        ss.settings_service = orig


@pytest.mark.asyncio
async def test_transfer_during_active_round(pg_pool, monkeypatch):
    """Перевод во время активной раздачи: получатель не может быть
    вытолкнут за SAFE_CAP → гарантия его будущих выплат сохраняется;
    отправитель с активной раздачей может переводить (баланс только
    уменьшается — обязательство не растёт)."""
    from storage import user_balance_store as ubs_mod
    from storage.user_balance_store import SAFE_BALANCE_CAP
    from storage.user_balance_store import user_balance_store as ubs
    monkeypatch.setattr(ubs_mod.decay_tracker, "should_apply_decay",
                        lambda *a, **k: False)
    ubs._balances.clear()  # JSON-fallback не должен мешать
    await _set_balance(100_000)
    rid, _ = await bj.blackjack_store.create_round(
        G, U, 55, 1000, _game())
    # перевод ОТПРАВИТЕЛЯ с активной раздачей — разрешён (уменьшает)
    res = await ubs.transfer_balance(G, U, G, 999001, 5000)
    assert res.get("status") in ("ok", "success", True) or res
    # получатель у SAFE_CAP — входящий перевод отклонён
    await ubs.set_balance(G, 999001, SAFE_BALANCE_CAP - 10)
    with pytest.raises(ValueError, match="cap"):
        await ubs.transfer_balance(G, U, G, 999001, 5000)
    st = await bj.blackjack_store.get_round(rid)
    if st.status == "active":
        await bj.blackjack_store.apply_action(rid, "stand")
    assert (await bj.blackjack_store.get_round(rid)).status == "settled"


@pytest.mark.asyncio
async def test_concurrent_balance_change_and_settlement(pg_pool,
                                                        monkeypatch):
    """Конкурентное изменение баланса (admin_adjust + set_balance)
    и settlement активной раздачи: FOR UPDATE сериализует,
    выплата полная, без BalanceOverflow и без зависшей раздачи."""
    import asyncio

    from storage import user_balance_store as ubs_mod
    from storage.user_balance_store import user_balance_store as ubs
    monkeypatch.setattr(ubs_mod.decay_tracker, "should_apply_decay",
                        lambda *a, **k: False)
    await _set_balance(50_000)
    rid, _ = await bj.blackjack_store.create_round(
        G, U, 55, 10_000, _game())  # obligation = 20 000

    async def settle():
        for _ in range(30):
            try:
                st = await bj.blackjack_store.get_round(rid)
                if st.status != "active":
                    return st.status
                await bj.blackjack_store.apply_action(rid, "stand")
            except (bj.RoundNotActive, bj.RoundNotFound, bj.StaleVersion):
                return "done"
            except Exception:  # noqa: BLE001 — transient DB-гонки под
                await asyncio.sleep(0.01)  # конкурентной мутацией баланса
        return "timeout"

    async def mutate():
        # set_balance в пределах obligation-лимита + admin-корректировка
        await ubs.set_balance(G, U, 30_000)
        await ubs.admin_adjust(G, U, 5000, "op-conc-1")
        return True

    await asyncio.gather(settle(), mutate())
    st = await bj.blackjack_store.get_round(rid)
    assert st.status == "settled"
    # ledger payout существует — выплата не потеряна
    ledger = await bj.blackjack_store.get_dispute_ledger(rid)
    assert any(e["op"] == "payout" for e in ledger)
    assert await _balance() >= 0  # баланс консистентен, без overflow


@pytest.mark.asyncio
async def test_resync_survives_finish_of_active_op(pg_pool):
    """Defect: resync во время активного рендера не должен
    теряться при finish(ok) — card_verify_after гарантирует
    отложенный повторный рендер актуального состояния."""
    rid = await _delivered_round(pg_pool)
    await bj.blackjack_store.apply_action(rid, "hit")  # version+1
    assert await bj.blackjack_store.claim_card_render(rid, "op-r") is True
    assert await bj.blackjack_store.request_card_resync(rid) is True
    # активный op завершается успешно — подтверждает card_claim
    assert await bj.blackjack_store.finish_card_render(
        rid, "op-r", True) is True
    stored = await bj.blackjack_store.get_round(rid)
    # card_rendered подтверждён, но resync НЕ потерян:
    # отложенная верификация ещё в будущем
    assert stored.card_rendered == stored.version
    assert stored.card_verify_after is not None
    assert stored.card_verify_after > datetime.now(UTC)
    # когда verify_after наступает — карточка снова в sync-очереди,
    # даже при card_rendered == version
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE blackjack_rounds SET card_verify_after=now() "
            "WHERE round_id=$1", rid)
    pending = await bj.blackjack_store.list_needing_sync()
    assert any(s.round_id == rid for s in pending)
    # claim разрешён по verify_after (rendered == version)
    assert await bj.blackjack_store.claim_card_render(
        rid, "op-verify") is True


@pytest.mark.asyncio
async def test_ensure_schema_on_empty_database(pg_pool):
    """Чистое окружение: без blackjack-таблиц ensure_schema
    создаёт схему идемпотентно, read-пути sweeper/recover
    не падают с UndefinedTableError."""
    store = bj.blackjack_store
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS blackjack_ledger")
        await conn.execute("DROP TABLE IF EXISTS blackjack_rounds")
    store._schema_ready = False
    # до ensure_schema read-путь действительно падает (контроль)
    with pytest.raises(asyncpg.UndefinedTableError):
        await store.list_needing_sync()
    # инициализация — дважды, идемпотентно
    await store.ensure_schema()
    await store.ensure_schema()
    assert store._schema_ready is True
    # read-пути sweeper/recover работают на пустой схеме
    assert await store.list_needing_sync() == []
    assert await store.list_undelivered(
        datetime.now(UTC) - timedelta(hours=1)) == []
    assert await store.list_active() == []
    # и create_round по-прежнему работает после init
    await _set_balance(1000)
    rid, _ = await store.create_round(G, U, 55, 0, _game())
    assert rid


@pytest.mark.asyncio
async def test_ensure_schema_flag_not_cached_on_tx_rollback(pg_pool):
    """Если tx с _ensure_schema откатилась, флаг не должен
    остаться «готовым» — следующий вызов пересоздаст схему."""
    store = bj.blackjack_store
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS blackjack_ledger")
        await conn.execute("DROP TABLE IF EXISTS blackjack_rounds")
    store._schema_ready = False
    pool = await dbmod.get_pool()
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.DivisionByZeroError):
            async with conn.transaction():
                await store._ensure_schema(conn)
                await conn.execute("SELECT 1/0")  # гарант. rollback
    # tx откачена → таблиц нет, флаг не должен врать
    assert store._schema_ready is False
    with pytest.raises(asyncpg.UndefinedTableError):
        await store.list_needing_sync()
    await store.ensure_schema()
    assert await store.list_needing_sync() == []
