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
async def test_reconcile_no_marker_proves_failed_and_refunds(pg_pool):
    """'unknown' + маркер не найден → 'failed' → refund."""
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
    assert handled == 1
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.status == "refunded"
    assert await _balance() == 1000


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
