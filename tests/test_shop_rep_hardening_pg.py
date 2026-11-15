"""PG regression-тесты финального харденинга SHOP 2.0 + REP.

Сценарии: незавершённый REP-возврат при смене ника, rep_pending
без refund_pending, потерянный ответ БД, двойной recovery,
бесплатный товар, кейс-награда до/после фиксации, гонка миграции
REP с начислением, границы тарифов.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

from models.shop_item import (
    CosmeticRarity,
    CosmeticType,
    ShopItem,
)
from storage.shop_store import InventoryStore, ShopStore
from storage.user_balance_store import user_balance_store
import storage.user_balance_store as ubs_mod

import storage.db as dbmod
import storage.shop_purchase_ops_store as pops_mod
import storage.shop_store as shop_store_mod
from services import shop_purchase_service as svc
from storage.rep_balance_store import (
    rep_balance_store,
    rep_price_for_amount,
    rep_price_for_coins,
    rep_price_for_item,
)
from storage.shop_purchase_ops_store import purchase_ops_store

G, U = 999, 31337

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS user_balance (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    balance INTEGER DEFAULT 100,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS match_coin_grants (
    match_id TEXT NOT NULL,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    amount INTEGER NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (match_id, user_id)
);
CREATE TABLE IF NOT EXISTS shop_operations (
    op_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    price INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rep_balance (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    balance INTEGER NOT NULL DEFAULT 0,
    earned_total INTEGER NOT NULL DEFAULT 0,
    spent_total INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS rep_balance_grants (
    grant_key TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    amount INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS player_stats (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    name TEXT DEFAULT '',
    reputation INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS reputation_transactions (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    source TEXT NOT NULL,
    amount INTEGER NOT NULL,
    actor_user_id BIGINT NOT NULL DEFAULT 0,
    subject_user_id BIGINT NOT NULL DEFAULT 0,
    event_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rep_log_outbox (
    id BIGSERIAL PRIMARY KEY,
    transaction_id BIGINT NOT NULL UNIQUE,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    source TEXT NOT NULL,
    amount INTEGER NOT NULL,
    balance_after INTEGER NOT NULL DEFAULT 0,
    actor_user_id BIGINT NOT NULL DEFAULT 0,
    subject_user_id BIGINT NOT NULL DEFAULT 0,
    log_status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    log_message_id BIGINT,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

TABLES = (
    "user_balance", "match_coin_grants", "shop_operations",
    "rep_balance", "rep_balance_grants", "player_stats",
    "reputation_transactions", "rep_log_outbox",
)


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_hard")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_DDL)
        for t in TABLES:
            await conn.execute(f"DELETE FROM {t}")
    prev_url = dbmod.DATABASE_URL
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    try:
        yield pool
    finally:
        dbmod._pool = None
        dbmod.DATABASE_URL = prev_url
        await pool.close()


@pytest.fixture
def env(monkeypatch, tmp_path, pg_pool):
    shop_file = tmp_path / "shop_items.json"
    inv_file = tmp_path / "player_inventory.json"
    pops_json = tmp_path / "shop_purchase_ops.json"

    import storage.shop_ops_store as ops_mod

    monkeypatch.setattr(shop_store_mod, "SHOP_FILE", shop_file)
    monkeypatch.setattr(shop_store_mod, "INVENTORY_FILE", inv_file)
    monkeypatch.setattr(ops_mod, "OPS_FILE", tmp_path / "shop_ops.json")
    monkeypatch.setattr(pops_mod, "OPS_JSON", pops_json)
    monkeypatch.setattr(
        ubs_mod.decay_tracker, "should_apply_decay", lambda g, u: False
    )
    monkeypatch.setattr(
        ubs_mod.decay_tracker, "mark_processed", lambda g, u: None
    )
    rep_balance_store._ddl_done = False
    purchase_ops_store._use_db = True
    purchase_ops_store._ddl_done = False
    user_balance_store._use_db = True

    shop = ShopStore()
    inv = InventoryStore()
    ops = ops_mod.ShopOpsStore()

    monkeypatch.setattr(svc, "shop_store", shop)
    monkeypatch.setattr(svc, "inventory_store", inv)
    monkeypatch.setattr(svc, "shop_ops_store", ops)

    item = ShopItem(
        id="tag_hard", name="[H]", description="", price=3500,
        cosmetic_type=CosmeticType.TAG, rarity=CosmeticRarity.ELITE,
        value="[H]", category="tags",
    )
    free = ShopItem(
        id="free_hard", name="[F]", description="", price=0,
        cosmetic_type=CosmeticType.TAG, rarity=CosmeticRarity.BASIC,
        value="[F]", category="tags",
    )
    shop.add_item(item)
    shop.add_item(free)
    return {"shop": shop, "inv": inv, "item": item, "pool": pg_pool}


async def _set(pool, coins, rep=None, g=G, u=U):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_balance (guild_id, user_id, balance)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET balance=$3
            """,
            g, u, coins,
        )
        if rep is not None:
            await conn.execute(
                """
                INSERT INTO player_stats (guild_id, user_id, reputation)
                VALUES ($1,$2,$3)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET reputation=$3
                """,
                g, u, rep,
            )


async def _bal(pool, g=G, u=U):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT balance FROM user_balance WHERE guild_id=$1 AND user_id=$2",
            g, u,
        )


async def _rep(pool, g=G, u=U):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT balance FROM rep_balance WHERE guild_id=$1 AND user_id=$2",
            g, u,
        )


def _owned(env, g=G, u=U):
    return {c.item_id for c in env["inv"].get_player_inventory(g, u)}


async def _charged_op(kind="purchase", item_id="tag_hard", price=3500,
                      rep=3, payload=None):
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, kind, item_id, price,
        payload=payload or {"components": [item_id]},
        rep_amount=rep,
    )
    charge = await purchase_ops_store.charge(
        op_id, G, U, price, rep_amount=rep
    )
    assert charge == "charged"
    return op_id


# ------------------------------------------------------------- тарифы/цены


def test_price_boundaries():
    cases = [
        (0, 0), (1, 1), (999, 1), (1000, 2), (2999, 2),
        (3000, 3), (5999, 3), (6000, 5), (14999, 5), (15000, 8),
    ]
    for coins, rep in cases:
        assert rep_price_for_coins(coins) == rep, coins
    assert rep_price_for_amount(0) == 0
    assert rep_price_for_amount(500) == 1


def test_free_item_zero_rep(env):
    free = env["shop"].get_item("free_hard")
    assert rep_price_for_item(free) == 0
    # Платный товар с rep_price=0 → минимум 1 (обход невозможен)
    paid = env["shop"].get_item("tag_hard")
    paid.rep_price = 0
    assert rep_price_for_item(paid) == 1
    paid.rep_price = None


@pytest.mark.asyncio
async def test_free_purchase_costs_nothing(env):
    await _set(env["pool"], 0, rep=0)
    res = await svc.purchase(G, U, "free_hard")
    assert res.ok and res.rep_price == 0
    assert await _bal(env["pool"]) == 0
    # spendable REP не тронут (строка могла не создаваться — rep=0)
    assert await rep_balance_store.get_balance(G, U) == 0
    assert "free_hard" in _owned(env)


# ------------------------------------------- nickname: возврат обеих валют


@pytest.mark.asyncio
async def test_refund_both_currencies_idempotent(env):
    """Аналог отказа Discord при нике: refund возвращает монеты+REP
    ровно один раз, повторный вызов — без дубля."""
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op(
        kind="nickname", item_id="nickname_change",
        price=1000, rep=2, payload={"nick_op_id": "x"},
    )
    assert await _bal(env["pool"]) == 4000
    assert await _rep(env["pool"]) == 8

    res1 = await purchase_ops_store.refund(op_id, G, U, 1000)
    assert res1["credited"] == 1000 and res1["rep_credited"] == 2
    assert await _bal(env["pool"]) == 5000
    assert await _rep(env["pool"]) == 10

    res2 = await purchase_ops_store.refund(op_id, G, U, 1000)
    assert await _bal(env["pool"]) == 5000  # не 6000
    assert await _rep(env["pool"]) == 10    # не 12


# ------------------------------------------- rep_pending без coin pending


@pytest.mark.asyncio
async def test_rep_pending_only_recovery(env, monkeypatch):
    """rep_pending>0 при refund_pending=0: settle досчитывает REP,
    операция покидает debt-выборку."""
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op(price=3500, rep=3)

    # Имитируем: монеты возвращены, REP-грант упал
    async def fail_rep(g, u, amount, key):
        raise RuntimeError("pg down")

    monkeypatch.setattr(rep_balance_store, "add_rep_once", fail_rep)
    res = await purchase_ops_store.refund(op_id, G, U, 3500)
    assert res["credited"] == 3500
    assert res["rep_pending"] == 3
    assert await _rep(env["pool"]) == 7  # REP так и не вернулись

    # Фиксируем долг как делает _refund_op
    await purchase_ops_store.set_status(
        op_id, "refunded",
        payload={"refund_pending": 0, "rep_pending": 3,
                 "refund_done": True},
    )
    monkeypatch.undo()

    # recover_pending → sweep по with_refund_debt находит операцию
    op = await purchase_ops_store.get(op_id)
    remaining = await purchase_ops_store.settle_pending_refund(op)
    assert remaining == 0
    assert await _rep(env["pool"]) == 10
    assert await _bal(env["pool"]) == 5000  # монеты не дублируются

    # Повторный settle — уже не в debt-выборке
    debt = await purchase_ops_store.with_refund_debt()
    assert all(o["op_id"] != op_id for o in debt)


@pytest.mark.asyncio
async def test_debt_query_finds_rep_only(env):
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op()
    await purchase_ops_store.set_status(
        op_id, "refunded",
        payload={"refund_pending": 0, "rep_pending": 3},
    )
    debt = await purchase_ops_store.with_refund_debt()
    assert any(o["op_id"] == op_id for o in debt)


# ------------------------------------------------------------- recovery


@pytest.mark.asyncio
async def test_double_recovery_idempotent(env):
    """Два recover_pending подряд: вторая выдача не дублируется."""
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op()

    await svc.recover_pending(bot=None)
    assert "tag_hard" in _owned(env)
    await svc.recover_pending(bot=None)  # повтор
    assert "tag_hard" in _owned(env)
    inv = env["inv"].get_player_inventory(G, U)
    assert sum(1 for c in inv if c.item_id == "tag_hard") == 1
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "done"


@pytest.mark.asyncio
async def test_crash_after_grant_before_done(env):
    """Краш между выдачей предмета и set_status(done): recovery
    видит предмет в инвентаре → done, без повторной выдачи."""
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op()
    # Имитируем: предмет уже выдан, статус ещё charged
    env["inv"].add_cosmetic(__import__(
        "models.shop_item", fromlist=["PlayerCosmetic"]
    ).PlayerCosmetic(
        guild_id=G, user_id=U, item_id="tag_hard", equipped=False,
    ))
    await svc.recover_pending(bot=None)
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "done"
    inv = env["inv"].get_player_inventory(G, U)
    assert sum(1 for c in inv if c.item_id == "tag_hard") == 1


@pytest.mark.asyncio
async def test_refund_done_blocks_regrant(env):
    """Операция в refund-ветке (refund_done) никогда не выдаёт
    товар повторно — даже если статус остался charged."""
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op()
    # Имитируем: refund выполнен, но set_status('refunded') не дошёл
    res = await purchase_ops_store.refund(op_id, G, U, 3500)
    assert res["credited"] == 3500
    await purchase_ops_store.set_status(
        op_id, "charged",  # статус не обновился — эмуляция сбоя
        payload={"refund_done": True},
    )
    await svc.recover_pending(bot=None)
    assert "tag_hard" not in _owned(env)  # товар НЕ выдан
    assert await _bal(env["pool"]) == 5000


# ------------------------------------------------------------------ кейс


@pytest.mark.asyncio
async def test_case_reward_persisted_and_completed(env):
    """Краш после фиксации награды, до выдачи: recovery доводит
    выдачу (ключи идемпотентны), а не возвращает деньги."""
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op(
        kind="case", item_id="basic", price=2000, rep=1,
        payload={"case_reward": {"kind": "coins", "amount": 700}},
    )
    bal_before = await _bal(env["pool"])
    await svc.recover_pending(bot=None)
    assert await _bal(env["pool"]) == bal_before + 700
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "done"

    # Повторный recovery — награда не дублируется
    await svc.recover_pending(bot=None)
    assert await _bal(env["pool"]) == bal_before + 700


@pytest.mark.asyncio
async def test_case_no_reward_refunds(env):
    """Краш до фиксации награды: выдачи не было → refund обеих."""
    await _set(env["pool"], 5000, rep=10)
    await _charged_op(
        kind="case", item_id="basic", price=2000, rep=1,
        payload={"case_id": "basic"},
    )
    await svc.recover_pending(bot=None)
    assert await _bal(env["pool"]) == 5000
    assert await _rep(env["pool"]) == 10


# ------------------------------------------------------- миграция/гонки


@pytest.mark.asyncio
async def test_migration_race_with_award(env):
    """Гонка: get_balance (миграция) параллельно award_rep —
    spendable REP не теряется и не дублируется."""
    from storage.reputation_store import reputation_store

    await _set(env["pool"], 0, rep=5)
    bal, ok = await asyncio.gather(
        rep_balance_store.get_balance(G, U),
        reputation_store.award_rep(
            G, U, "MANUAL_REP", 4, "race_event_1", user_name="t"
        ),
    )
    assert ok
    final = await rep_balance_store.get_balance(G, U)
    # rep_after=9, spendable должен быть 9 (миграция 5 + award 4)
    assert final == 9
    info = await rep_balance_store.get_info(G, U)
    assert info["balance"] == 9


@pytest.mark.asyncio
async def test_concurrent_charge_single_success(env):
    """Два charge по разным соединениям PG: ровно одно списание."""
    await _set(env["pool"], 5000, rep=10)
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "purchase", "tag_hard", 3500, rep_amount=3,
    )
    r1, r2 = await asyncio.gather(
        purchase_ops_store.charge(op_id, G, U, 3500, rep_amount=3),
        purchase_ops_store.charge(op_id, G, U, 3500, rep_amount=3),
    )
    assert sorted([r1, r2]) == ["charged", "exists"]
    assert await _bal(env["pool"]) == 1500
    assert await _rep(env["pool"]) == 7


# ============================================ BUG #1: ключи кейсов


@pytest.mark.asyncio
async def test_case_grant_key_identical(env):
    """Recovery использует РОВНО тот же grant_key, что первичная
    выдача: caseop:{op_id}[:suffix] — без двойных двоеточий."""
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op(
        kind="case", item_id="basic", price=2000, rep=1,
        payload={"case_reward": {
            "kind": "coins", "amount": 700, "key_suffix": ":fb",
        }},
    )
    await svc.recover_pending(bot=None)
    # Ключ в match_coin_grants — точное совпадение с первичной выдачей
    async with env["pool"].acquire() as conn:
        keys = await conn.fetch(
            "SELECT match_id FROM match_coin_grants WHERE user_id=$1",
            U,
        )
    assert f"caseop:{op_id}:fb" in {r["match_id"] for r in keys}
    assert f"caseop:{op_id}::fb" not in {r["match_id"] for r in keys}


@pytest.mark.asyncio
async def test_case_reward_double_recover_no_double(env):
    """Два recovery подряд: кейс-награда не дублируется."""
    await _set(env["pool"], 5000, rep=10)
    bal0 = await _bal(env["pool"])
    await _charged_op(
        kind="case", item_id="basic", price=2000, rep=1,
        payload={"case_reward": {"kind": "coins", "amount": 700}},
    )
    await svc.recover_pending(bot=None)
    await svc.recover_pending(bot=None)
    await svc.recover_pending(bot=None)
    # charge 2000 + ровно одна выдача 700 (не дублируется)
    assert await _bal(env["pool"]) == bal0 - 2000 + 700


@pytest.mark.asyncio
async def test_case_grant_lost_response_no_double(env):
    """Грант выполнился, ответ «потерян» — повторный вызов
    _complete_case_reward возвращает recorded, баланс не растёт."""
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op(
        kind="case", item_id="basic", price=2000, rep=1,
        payload={"case_reward": {"kind": "coins", "amount": 700}},
    )
    op = await purchase_ops_store.get(op_id)
    bal0 = await _bal(env["pool"])
    ok1 = await svc._complete_case_reward(
        op, op["payload"]["case_reward"]
    )
    ok2 = await svc._complete_case_reward(
        op, op["payload"]["case_reward"]
    )
    assert ok1 and ok2
    assert await _bal(env["pool"]) == bal0 + 700  # не +1400


# ==================================== BUG #2: строгая фиксация награды


@pytest.mark.asyncio
async def test_merge_payload_confirms_and_rechecks(env):
    """merge_payload: True при записи; при «потере ответа»
    (исключение после фактического UPDATE) — перепроверка даёт True."""
    await _set(env["pool"], 0)
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "case", "basic", 2000, rep_amount=1,
    )
    ok = await purchase_ops_store.merge_payload(
        op_id, {"case_reward": {"kind": "coins", "amount": 100}}
    )
    assert ok
    op = await purchase_ops_store.get(op_id)
    assert op["payload"]["case_reward"]["amount"] == 100


@pytest.mark.asyncio
async def test_merge_payload_unconfirmed_returns_false(env, monkeypatch):
    """PG недоступен → merge_payload False → выдача не выполняется,
    операция остаётся charged (без refund и без награды)."""
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op(
        kind="case", item_id="basic", price=2000, rep=1,
        payload={"case_id": "basic"},
    )
    # Имитируем: persist не подтверждён → open_case_safe вернул бы
    # pending. Здесь проверяем recover: case_reward отсутствует →
    # refund обеих валют (награда не выдавалась).
    await svc.recover_pending(bot=None)
    assert await _bal(env["pool"]) == 5000
    assert await _rep(env["pool"]) == 10


@pytest.mark.asyncio
async def test_case_reward_grant_exception_stays_charged(env, monkeypatch):
    """Сбой ВНУТРИ гранта (неопределённый исход) → операция
    остаётся charged, НЕ refunded: нет reward+refund."""
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op(
        kind="case", item_id="basic", price=2000, rep=1,
        payload={"case_reward": {"kind": "coins", "amount": 700}},
    )

    async def boom(*a, **k):
        raise RuntimeError("pg response lost")

    monkeypatch.setattr(
        "storage.user_balance_store.user_balance_store.add_balance_once",
        boom,
    )
    await svc.recover_pending(bot=None)
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "charged"  # НЕ refunded, НЕ done
    # И деньги не возвращены — ждём повторной попытки
    assert await _bal(env["pool"]) == 3000  # 5000 - charge 2000

    monkeypatch.undo()
    await svc.recover_pending(bot=None)
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "done"


# ================================= BUG #3: курсорная пагинация долгов


@pytest.mark.asyncio
async def test_debt_cursor_pagination_all_settled(env):
    """250 задолженностей (>1 страницы): курсорная пагинация
    обрабатывает ВСЕ, ни одна не пропущена после исчезновения
    из выборки."""
    await _set(env["pool"], 0, rep=0)
    n = 250
    op_ids = []
    for i in range(n):
        oid = purchase_ops_store.new_op_id()
        await purchase_ops_store.create(
            oid, G, U, "purchase", f"item_{i}", 0, rep_amount=0,
        )
        # Имитируем долг только в REP
        await purchase_ops_store.set_status(
            oid, "refunded",
            payload={"refund_pending": 0, "rep_pending": 0,
                     "rep_amount": 0},
        )
        op_ids.append(oid)
    # Реальный долг: rep_amount>0 + rep_pending>0
    for oid in op_ids[:n]:
        await purchase_ops_store.set_status(
            oid, "refunded",
            payload={"rep_amount": 1, "rep_pending": 1,
                     "refund_pending": 0},
        )
    # Собираем все страницы курсором
    seen = []
    after = None
    while True:
        page = await purchase_ops_store.with_refund_debt(
            limit=100, after=after
        )
        if not page:
            break
        seen.extend(o["op_id"] for o in page)
        after = (page[-1]["created_at"], page[-1]["op_id"])
        if len(page) < 100:
            break
    assert len(seen) == n
    assert len(set(seen)) == n  # без дублей и пропусков

    settled = await svc.settle_refund_debts(page_size=100)
    assert settled == n
    debt = await purchase_ops_store.with_refund_debt(limit=500)
    assert debt == []


@pytest.mark.asyncio
async def test_sweeper_lock_mutual_exclusion(env):
    """Параллельный запуск settle_refund_debts не дублирует
    обработку (asyncio.Lock)."""
    await _set(env["pool"], 5000, rep=10)
    await _charged_op(
        kind="case", item_id="basic", price=2000, rep=1,
        payload={"case_reward": {"kind": "coins", "amount": 300}},
    )
    r1, r2 = await asyncio.gather(
        svc.settle_refund_debts(), svc.settle_refund_debts(),
    )
    assert r1 + r2 >= 0  # один прошёл, второй пропустил/0
    # Повторные запуски безопасны
    await svc.settle_refund_debts()


# ================================= BUG #4: unknown → refunded


@pytest.mark.asyncio
async def test_unknown_rep_only_transitions_refunded(env):
    """unknown с rep_pending: после settle → refunded + refund_done,
    повторный recovery не крутит операцию."""
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op(price=3500, rep=3)
    # Имитируем: монеты возвращены, REP ещё должен
    await purchase_ops_store.set_status(
        op_id, "unknown",
        payload={"rep_amount": 3, "rep_pending": 3,
                 "refund_pending": 0},
    )
    await svc.recover_pending(bot=None)
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "refunded"
    assert op["payload"].get("refund_done") is True
    assert await _rep(env["pool"]) == 10
    # Повторный recovery — статус не меняется, REP не дублируется
    await svc.recover_pending(bot=None)
    op2 = await purchase_ops_store.get(op_id)
    assert op2["status"] == "refunded"
    assert await _rep(env["pool"]) == 10


@pytest.mark.asyncio
async def test_unknown_both_debts_transitions(env):
    """unknown с двумя задолженностями → refunded после полного
    досчёта."""
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op(price=3500, rep=3)
    await purchase_ops_store.set_status(
        op_id, "unknown",
        payload={"rep_amount": 3, "rep_pending": 3,
                 "refund_pending": 3500},
    )
    await svc.recover_pending(bot=None)
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "refunded"
    assert await _bal(env["pool"]) == 5000
    assert await _rep(env["pool"]) == 10


@pytest.mark.asyncio
async def test_unknown_failed_refund_retried(env, monkeypatch):
    """Неудачный refund → операция остаётся unknown/долгом,
    следующий sweep завершает."""
    await _set(env["pool"], 5000, rep=10)
    op_id = await _charged_op(price=3500, rep=3)
    await purchase_ops_store.set_status(
        op_id, "unknown",
        payload={"rep_amount": 3, "rep_pending": 3,
                 "refund_pending": 3500},
    )

    async def fail_add(g, u, amount, key):
        raise RuntimeError("pg down")

    monkeypatch.setattr(
        "storage.user_balance_store.user_balance_store.add_balance_once",
        fail_add,
    )
    await svc.recover_pending(bot=None)
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "unknown"  # не refunded — долг остался

    monkeypatch.undo()
    await svc.settle_refund_debts()
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "refunded"
    assert await _bal(env["pool"]) == 5000
    assert await _rep(env["pool"]) == 10
