"""PG-интеграция: долговременные награды Coin Drop на реальном
PostgreSQL (pgserver, изолированный кластер).

Проверяем:
- record() идемпотентен по grant_key (фиксация победителя);
- claim_due/claim_key под lease — параллельные worker не берут
  одну награду (SKIP LOCKED);
- полный цикл pending → processing → completed;
- «перезапуск» до начисления и после commit до complete —
  награда выдаётся ровно один раз (дедупликация add_balance_once);
- retry/backoff: строка не подхватывается до next_attempt_at;
- MAX_BALANCE: кап применяется, награда всё равно завершается.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")
pytest_asyncio = pytest.importorskip("pytest_asyncio")

from cogs import coin_drop  # noqa: E402
from storage import drop_reward_store as drs  # noqa: E402
from storage.user_balance_store import user_balance_store  # noqa: E402


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_drop_rewards")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri, monkeypatch):
    """Изолированный пул как storage.db._pool: оба store
    (drop rewards + balance) ходят в него через get_pool()."""
    import storage.db as dbmod
    pool = await asyncpg.create_pool(pg_uri, min_size=1, max_size=4)
    prev = dbmod._pool
    dbmod._pool = pool
    await dbmod.init_db()
    async with pool.acquire() as conn:
        await conn.execute(drs.DDL)
        await conn.execute(drs.INDEX_DDL)
        await conn.execute(user_balance_store._GRANT_DDL)
        await conn.execute("DELETE FROM drop_coin_rewards")
        await conn.execute("DELETE FROM match_coin_grants")
        await conn.execute("DELETE FROM user_balance")
    monkeypatch.setattr(drs.drop_reward_store, "_ddl_done", False)
    try:
        yield pool
    finally:
        dbmod._pool = prev
        await pool.close()


def _cog():
    return coin_drop.CoinDropCog(bot=SimpleNamespace(loop=None))


async def _balance(gid, uid):
    return await user_balance_store.get_balance(gid, uid)


@pytest.mark.asyncio
async def test_record_idempotent_by_grant_key(pg_pool):
    """Повторная фиксация того же grant_key не создаёт дубль
    и не меняет победителя/сумму."""
    s = drs.drop_reward_store
    r1 = await s.record("speeddrop:e1", "speed", 1, 42, 300)
    r2 = await s.record("speeddrop:e1", "speed", 1, 99, 999)
    assert r1["user_id"] == 42 and r1["amount"] == 300
    assert r2["user_id"] == 42 and r2["amount"] == 300  # не перезаписано
    assert await s.pending_count() == 1


@pytest.mark.asyncio
async def test_full_cycle_pending_to_completed(pg_pool):
    """record → claim_key → process → complete: монеты на балансе,
    статус completed, повторная обработка невозможна."""
    s = drs.drop_reward_store
    await s.record("speeddrop:e2", "speed", 1, 7, 250)
    row = await s.claim_key("speeddrop:e2")
    assert row and row["status"] == "processing"
    assert row["attempts"] == 1
    # Повторный claim той же строки под lease — None.
    assert await s.claim_key("speeddrop:e2") is None
    ok = await _cog()._process_drop_reward(row)
    assert ok is True
    assert await _balance(1, 7) == 350  # 100 стартовых + 250
    row2 = await s.claim_due()
    assert all(r["grant_key"] != "speeddrop:e2" for r in row2)


@pytest.mark.asyncio
async def test_restart_before_payout_recovers(pg_pool):
    """«Перезапуск» между фиксацией и начислением: новая
    claim_due-партия подхватывает pending и начисляет."""
    s = drs.drop_reward_store
    await s.record("raffledrop:e3", "raffle", 1, 55, 400)
    # Имитация рестарта: новый «процесс» — просто claim_due.
    rows = await s.claim_due()
    mine = [r for r in rows if r["grant_key"] == "raffledrop:e3"]
    assert len(mine) == 1
    assert await _cog()._process_drop_reward(mine[0]) is True
    assert await _balance(1, 55) == 500


@pytest.mark.asyncio
async def test_credit_committed_but_complete_lost(pg_pool):
    """Commit начисления прошёл, ответ/complete потерян: lease
    истекает → повторная обработка → add_balance_once дедуплицирует
    → баланс НЕ удваивается, строка доходит до completed."""
    s = drs.drop_reward_store
    await s.record("speeddrop:e4", "speed", 1, 21, 600)
    row = await s.claim_key("speeddrop:e4")
    # Симулируем commit начисления без complete (краш между ними).
    await user_balance_store.add_balance_once(1, 21, 600, "speeddrop:e4")
    bal_after_commit = await _balance(1, 21)
    # Lease истёк — «следующий worker» забирает строку снова.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE drop_coin_rewards SET locked_until = now() - interval '1s' "
            "WHERE grant_key = 'speeddrop:e4'")
    rows = await s.claim_due()
    mine = [r for r in rows if r["grant_key"] == "speeddrop:e4"]
    assert len(mine) == 1
    assert await _cog()._process_drop_reward(mine[0]) is True
    assert await _balance(1, 21) == bal_after_commit  # без двойного кредита


@pytest.mark.asyncio
async def test_two_workers_get_disjoint_rows(pg_pool):
    """Два параллельных worker: SKIP LOCKED → непересекающиеся
    наборы наград, каждая обработана ровно одним."""
    s = drs.drop_reward_store
    for i in range(6):
        await s.record(f"speeddrop:w{i}", "speed", 1, 100 + i, 10)
    b1, b2 = await asyncio.gather(s.claim_due(5), s.claim_due(5))
    keys1 = {r["grant_key"] for r in b1}
    keys2 = {r["grant_key"] for r in b2}
    assert not (keys1 & keys2)
    assert len(keys1 | keys2) == 6


@pytest.mark.asyncio
async def test_retry_backoff_delays_pickup(pg_pool, monkeypatch):
    """Сбой начисления → retry() с next_attempt_at в будущем:
    claim_due не берёт строку до срока; после срока — берёт."""
    s = drs.drop_reward_store
    await s.record("speeddrop:e5", "speed", 1, 31, 150)
    row = await s.claim_key("speeddrop:e5")
    with monkeypatch.context() as mp:
        mp.setattr(user_balance_store, "_use_db", True)
        async def boom(*a, **k):
            raise RuntimeError("pg down")
        mp.setattr(user_balance_store, "add_balance_once", boom)
        assert await _cog()._process_drop_reward(row) is False
    # Строка вернулась в pending с будущим next_attempt_at.
    assert await s.claim_due() == []
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE drop_coin_rewards SET next_attempt_at = now() "
            "WHERE grant_key = 'speeddrop:e5'")
    rows = await s.claim_due()
    assert any(r["grant_key"] == "speeddrop:e5" for r in rows)


@pytest.mark.asyncio
async def test_max_balance_caps_reward_but_completes(pg_pool):
    """Участник у MAX_BALANCE: add_balance_once зачисляет кап,
    награда завершается completed (недоплата — правило экономики,
    не потеря обязательства)."""
    s = drs.drop_reward_store
    await user_balance_store.set_balance(1, 88, 100000)  # MAX_BALANCE
    await s.record("speeddrop:e6", "speed", 1, 88, 5000)
    row = await s.claim_key("speeddrop:e6")
    assert await _cog()._process_drop_reward(row) is True
    assert await _balance(1, 88) == 100000  # кап, не 105000


@pytest.mark.asyncio
async def test_repeat_processing_same_reward_once(pg_pool):
    """Повторная обработка одной награды (retry/recovery):
    баланс увеличивается ровно на amount."""
    s = drs.drop_reward_store
    await s.record("raffledrop:e7", "raffle", 1, 44, 700)
    cog = _cog()
    for _ in range(3):
        rows = await s.claim_due()
        mine = [r for r in rows if r["grant_key"] == "raffledrop:e7"]
        if not mine:
            break
        await cog._process_drop_reward(mine[0])
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE drop_coin_rewards SET locked_until = now() - interval '1s' "
                "WHERE grant_key = 'raffledrop:e7' AND status != 'completed'")
    assert await _balance(1, 44) == 800  # 100 + 700, не 1600/2400
