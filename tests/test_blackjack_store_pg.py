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
    rid = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
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
    rid1 = await bj.blackjack_store.create_round(G, U, 55, 100, _game())
    with pytest.raises(bj.ActiveGameExists):
        await bj.blackjack_store.create_round(G, U, 55, 100, _game())
    # другой пользователь — можно
    rid2 = await bj.blackjack_store.create_round(G, U + 1, 55, 100, _game())
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
    if any(isinstance(r, str) for r in results):
        spent += 600
    if any(r is True for r in results):
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
    rid = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
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
    rid = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
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
    rid = await bj.blackjack_store.create_round(G, U, 55, 500, game)
    stored = await bj.blackjack_store.get_round(rid)
    assert stored.status == "settled"
    assert stored.outcome == OUT_PUSH
    # push → gross=stake → баланс вернулся к 1000
    assert await _balance() == 1000


@pytest.mark.asyncio
async def test_refund_only_before_message(pg_pool):
    await _set_balance(1000)
    rid = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
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
    rid = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    await bj.blackjack_store.mark_message(rid, 999)
    assert await bj.blackjack_store.refund_undelivered(rid) is False
    assert await _balance() == 500


@pytest.mark.asyncio
async def test_expire_due_auto_stands(pg_pool):
    await _set_balance(2000)
    rid = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
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
    rid = await bj.blackjack_store.create_round(G, U, 55, 500, _game())
    active = await bj.blackjack_store.list_active()
    assert any(s.round_id == rid for s in active)
