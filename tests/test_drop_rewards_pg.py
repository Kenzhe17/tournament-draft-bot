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


@pytest.mark.asyncio
async def test_record_different_user_keeps_first_owner(pg_pool):
    """Повторный grant_key с ДРУГИМ user_id не перезаписывает
    победителя — «other» определяется по row.user_id."""
    s = drs.drop_reward_store
    r1 = await s.record("speeddrop:own", "speed", 1, 42, 300)
    r2 = await s.record("speeddrop:own", "speed", 1, 99, 300)
    assert r2["user_id"] == 42  # первый владелец сохранён
    assert int(r2["user_id"]) != 99  # → grant вернёт "other"


@pytest.mark.asyncio
async def test_stale_worker_cannot_complete_or_retry(pg_pool):
    """Lease перехвачен другим worker (attempts вырос):
    устаревший complete/retry со старым attempts → False,
    строка не портится."""
    s = drs.drop_reward_store
    await s.record("speeddrop:f1", "speed", 1, 5, 100)
    row1 = await s.claim_key("speeddrop:f1")       # attempts=1
    assert row1["attempts"] == 1
    # Lease «истёк», второй worker забрал строку.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE drop_coin_rewards SET locked_until = now() - interval '1s' "
            "WHERE grant_key = 'speeddrop:f1'")
    row2 = await s.claim_due()
    row2 = [r for r in row2 if r["grant_key"] == "speeddrop:f1"][0]
    assert row2["attempts"] == 2
    # Устаревший worker пытается завершить/отложить чужую обработку.
    assert await s.complete("speeddrop:f1", attempts=1) is False
    assert await s.retry("speeddrop:f1", 1, "stale", 30) is False
    # Актуальный владелец завершает успешно.
    assert await s.complete("speeddrop:f1", attempts=2) is True
    # completed → pending невозможен даже прямым retry.
    assert await s.retry("speeddrop:f1", 2, "late", 30) is False
    async with pg_pool.acquire() as conn:
        st = await conn.fetchval(
            "SELECT status FROM drop_coin_rewards WHERE grant_key='speeddrop:f1'")
    assert st == "completed"


@pytest.mark.asyncio
async def test_lease_expiry_during_processing_single_credit(pg_pool):
    """Lease истёк прямо во время обработки: второй worker
    подхватывает строку, но add_balance_once дедуплицирует —
    баланс увеличен ровно один раз, completed ставит новый
    владелец lease."""
    s = drs.drop_reward_store
    await s.record("speeddrop:f2", "speed", 1, 6, 800)
    row1 = await s.claim_key("speeddrop:f2")  # attempts=1, lease 120s
    # Lease истекает до завершения обработки.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE drop_coin_rewards SET locked_until = now() - interval '1s' "
            "WHERE grant_key = 'speeddrop:f2'")
    # Worker B забирает и обрабатывает.
    rows = await s.claim_due()
    rowB = [r for r in rows if r["grant_key"] == "speeddrop:f2"][0]
    assert rowB["attempts"] == 2
    assert await _cog()._process_drop_reward(rowB) is True
    # Устаревший worker A «догоняет» — его complete отклонён fencing'ом.
    assert await s.complete("speeddrop:f2", attempts=1) is False
    assert await _balance(1, 6) == 900  # 100 + 800, не 1700


@pytest.mark.asyncio
async def test_button_claim_and_worker_disjoint(pg_pool):
    """Обработчик кнопки (claim_key) и worker (claim_due) не
    получают одну и ту же награду параллельно."""
    s = drs.drop_reward_store
    for i in range(4):
        await s.record(f"speeddrop:cc{i}", "speed", 1, 200 + i, 50)
    # «Кнопка» забирает один ключ напрямую.
    btn_row = await s.claim_key("speeddrop:cc0")
    assert btn_row is not None
    # Worker забирает партию — cc0 уже processing под lease,
    # в выдачу не попадает.
    rows = await s.claim_due(10)
    keys = {r["grant_key"] for r in rows}
    assert "speeddrop:cc0" not in keys
    assert keys == {"speeddrop:cc1", "speeddrop:cc2", "speeddrop:cc3"}


@pytest.mark.asyncio
async def test_repeat_recovery_run_idempotent(pg_pool):
    """Повторный запуск recovery (claim_due второй раз):
    completed-строки не выдаются, pending — ровно один раз."""
    s = drs.drop_reward_store
    await s.record("raffledrop:rr", "raffle", 1, 15, 250)
    cog = _cog()
    await cog._process_drop_reward(await s.claim_key("raffledrop:rr"))
    # «Повторный recovery» — claim_due не видит completed.
    rows = await s.claim_due()
    assert all(r["grant_key"] != "raffledrop:rr" for r in rows)
    assert await _balance(1, 15) == 350


@pytest.mark.asyncio
async def test_pg_outage_file_owner_survives_concurrent_grant(pg_pool, tmp_path, monkeypatch):
    """Критическая гонка: PG down → A в pending-файл → PG recovery
    → конкурентные grant(B) + drain. Победитель остаётся A,
    начисление ровно один раз."""
    import storage.db as dbmod
    from storage.drop_pending_store import DropPendingStore
    from cogs import coin_drop as cd

    pending = DropPendingStore(tmp_path / "pending.json")
    monkeypatch.setattr(cd, "drop_pending_store", pending)
    cog = _cog()

    real_record = drs.drop_reward_store.record
    state = {"pg_down": True}

    async def flaky_record(*a, **k):
        if state["pg_down"]:
            raise RuntimeError("pg down")
        return await real_record(*a, **k)

    monkeypatch.setattr(drs.drop_reward_store, "record", flaky_record)
    # 1) PG down: A фиксируется в файл.
    res_a = await cd.grant_drop_reward(cog, 1, 501, 300,
                                       "speeddrop:race", "speed")
    assert res_a == "queued"
    assert pending.get("speeddrop:race")["user_id"] == 501

    # 2) PG восстановился.
    state["pg_down"] = False
    bal_b_before = await _balance(1, 502)

    # 3) Конкурентно: grant от B + drain файла — под _grant_lock.
    res_b, _ = await asyncio.gather(
        cd.grant_drop_reward(cog, 1, 502, 300, "speeddrop:race", "speed"),
        cog._drain_pending_file(),
    )
    assert res_b == "other"          # B НЕ перехватил ключ
    assert pending.get("speeddrop:race") is None  # файл перенесён

    # 4) В PG ровно одна строка, владелец — A.
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, status FROM drop_coin_rewards "
            "WHERE grant_key='speeddrop:race'")
    assert row is not None
    assert int(row["user_id"]) == 501

    # 5) Начисление — только A, ровно один раз.
    claimed = await drs.drop_reward_store.claim_key("speeddrop:race")
    if claimed:
        await cog._process_drop_reward(claimed)
    assert await _balance(1, 501) == 400   # 100 + 300
    assert await _balance(1, 502) == bal_b_before  # B — ничего
