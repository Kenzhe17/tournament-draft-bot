"""PG-интеграционные тесты двойной оплаты монеты+REP (SHOP 2.0).

Реальная PostgreSQL: атомарность списания двух валют в одной
транзакции shop_operations.charge, идемпотентные возвраты обеих
валют, миграция rep_balance из player_stats.reputation,
синхронизация начислений репутации.
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

G, U = 888, 5555

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


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_rep")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_DDL)
        for t in ("user_balance", "match_coin_grants", "shop_operations",
                  "rep_balance", "rep_balance_grants", "player_stats",
                  "reputation_transactions", "rep_log_outbox"):
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
        id="tag_rep", name="[REP]", description="", price=3500,
        cosmetic_type=CosmeticType.TAG, rarity=CosmeticRarity.ELITE,
        value="[REP]", category="tags",
    )
    shop.add_item(item)
    return {"shop": shop, "inv": inv, "item": item, "pool": pg_pool}


async def _set_balance(pool, coins, rep=None, g=G, u=U):
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


async def _balance(pool, g=G, u=U):
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


# ------------------------------------------------------------------ цены


def test_rep_price_tiers():
    assert rep_price_for_coins(1) == 1
    assert rep_price_for_coins(999) == 1
    assert rep_price_for_coins(1000) == 2
    assert rep_price_for_coins(2999) == 2
    assert rep_price_for_coins(3000) == 3
    assert rep_price_for_coins(5999) == 3
    assert rep_price_for_coins(6000) == 5
    assert rep_price_for_coins(14999) == 5
    assert rep_price_for_coins(15000) == 8
    assert rep_price_for_coins(100000) == 8


def test_rep_price_item_override():
    item = ShopItem(
        id="x", name="x", description="", price=5000,
        cosmetic_type=CosmeticType.TAG, rarity=CosmeticRarity.BASIC,
        value="x", category="tags", rep_price=1,
    )
    assert rep_price_for_item(item) == 1
    item.rep_price = None
    assert rep_price_for_item(item) == 3  # тариф 3000–5999


# ---------------------------------------------------------------- покупка


@pytest.mark.asyncio
async def test_dual_payment_success(env):
    """3500 монет → 3500 + 3 REP; обе валюты списаны атомарно."""
    await _set_balance(env["pool"], 5000, rep=10)
    res = await svc.purchase(G, U, "tag_rep")
    assert res.ok and res.rep_price == 3
    assert await _balance(env["pool"]) == 1500
    assert await _rep(env["pool"]) == 7
    assert "tag_rep" in _owned(env)
    # reputation не тронута
    async with env["pool"].acquire() as conn:
        rep_total = await conn.fetchval(
            "SELECT reputation FROM player_stats WHERE guild_id=$1 AND user_id=$2",
            G, U,
        )
    assert rep_total == 10


@pytest.mark.asyncio
async def test_insufficient_coins_nothing_charged(env):
    await _set_balance(env["pool"], 100, rep=10)
    res = await svc.purchase(G, U, "tag_rep")
    assert not res.ok and res.insufficient
    assert await _balance(env["pool"]) == 100
    assert await _rep(env["pool"]) == 10  # REP не списан
    assert "tag_rep" not in _owned(env)


@pytest.mark.asyncio
async def test_insufficient_rep_nothing_charged(env):
    await _set_balance(env["pool"], 5000, rep=2)
    res = await svc.purchase(G, U, "tag_rep")
    assert not res.ok and res.insufficient
    assert "REP" in res.message and "3" in res.message
    assert await _balance(env["pool"]) == 5000  # монеты не тронуты
    assert await _rep(env["pool"]) == 2
    assert "tag_rep" not in _owned(env)


@pytest.mark.asyncio
async def test_concurrent_purchase_single_dual_charge(env):
    await _set_balance(env["pool"], 10000, rep=20)
    r1, r2 = await asyncio.gather(
        svc.purchase(G, U, "tag_rep"),
        svc.purchase(G, U, "tag_rep"),
    )
    assert sorted([r1.ok, r2.ok]) == [False, True]
    assert await _balance(env["pool"]) == 6500
    assert await _rep(env["pool"]) == 17


@pytest.mark.asyncio
async def test_duplicate_click_single_charge(env):
    await _set_balance(env["pool"], 10000, rep=20)
    r1 = await svc.purchase(G, U, "tag_rep")
    r2 = await svc.purchase(G, U, "tag_rep")
    assert r1.ok and not r2.ok
    assert await _balance(env["pool"]) == 6500
    assert await _rep(env["pool"]) == 17


@pytest.mark.asyncio
async def test_crash_after_dual_charge_recovery(env):
    """Краш после списания обеих валют → recovery довыдаёт предмет,
    валюты не возвращаются и не списываются повторно."""
    await _set_balance(env["pool"], 5000, rep=10)
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "purchase", "tag_rep", 3500,
        payload={"components": ["tag_rep"], "bundle": False},
        rep_amount=3,
    )
    charge = await purchase_ops_store.charge(
        op_id, G, U, 3500, rep_amount=3
    )
    assert charge == "charged"
    assert await _balance(env["pool"]) == 1500
    assert await _rep(env["pool"]) == 7

    await svc.recover_pending(bot=None)
    assert "tag_rep" in _owned(env)
    assert await _balance(env["pool"]) == 1500
    assert await _rep(env["pool"]) == 7
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "done"


@pytest.mark.asyncio
async def test_grant_failure_refunds_both_currencies(env, monkeypatch):
    """Сбой выдачи → возврат монет И REP идемпотентно."""
    await _set_balance(env["pool"], 5000, rep=10)

    async def fail_grant(g, u, item, guild):
        return False, "injected"

    monkeypatch.setattr(svc, "_grant_item", fail_grant)
    res = await svc.purchase(G, U, "tag_rep")
    assert res.ok and res.refunded
    assert await _balance(env["pool"]) == 5000
    assert await _rep(env["pool"]) == 10
    assert "tag_rep" not in _owned(env)

    # Повторный refund — без двойного возврата
    op = (await purchase_ops_store.recent(guild_id=G, limit=1))[0]
    res2 = await purchase_ops_store.refund(
        op["op_id"], G, U, 3500
    )
    assert await _balance(env["pool"]) == 5000
    assert await _rep(env["pool"]) == 10


@pytest.mark.asyncio
async def test_rep_refund_response_loss_no_double(env):
    """Грант REP выполнился, ответ потерян — повтор не дублирует."""
    await _set_balance(env["pool"], 5000, rep=10)
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "purchase", "tag_rep", 3500, rep_amount=3,
    )
    await purchase_ops_store.charge(op_id, G, U, 3500, rep_amount=3)

    # Первый refund «выполнился» на уровне БД
    await rep_balance_store.add_rep_once(G, U, 3, f"shoprefrep:{op_id}")
    assert await _rep(env["pool"]) == 10

    # Повторный refund — recorded amount, баланс не растёт
    res = await purchase_ops_store.refund(op_id, G, U, 3500)
    assert res["rep_credited"] == 3
    assert await _rep(env["pool"]) == 10  # НЕ 13


@pytest.mark.asyncio
async def test_bundle_dual_payment(env):
    """Набор: REP считается от цены набора, атомарно с монетами."""
    c1 = ShopItem(
        id="rb_c1", name="c1", description="", price=100,
        cosmetic_type=CosmeticType.TAG, rarity=CosmeticRarity.BASIC,
        value="c1", category="tags",
    )
    bundle = ShopItem(
        id="rb_bundle", name="B", description="", price=8000,
        cosmetic_type=CosmeticType.TAG, rarity=CosmeticRarity.SPECIAL,
        value="b", category="bundles", bundle=["rb_c1", "tag_rep"],
    )
    env["shop"].add_item(c1)
    env["shop"].add_item(bundle)
    await _set_balance(env["pool"], 20000, rep=20)

    res = await svc.purchase(G, U, "rb_bundle")
    assert res.ok and res.rep_price == 5  # 6000–14999 → 5 REP
    assert await _balance(env["pool"]) == 12000
    assert await _rep(env["pool"]) == 15
    owned = _owned(env)
    assert "rb_c1" in owned and "tag_rep" in owned


@pytest.mark.asyncio
async def test_case_dual_payment_and_refund(env):
    """Кейс: charge монеты+REP → ошибка открытия → возврат обеих."""
    from storage.case_store import case_store

    case = case_store.get_case("basic")
    if case is None or not getattr(case, "is_active", False):
        pytest.skip("basic case not seeded")
    rep_price = rep_price_for_amount(case.price)
    await _set_balance(env["pool"], 5000, rep=10)

    # Имитируем сбой open_case
    async def boom(*a, **k):
        raise RuntimeError("case boom")

    orig = case_store.open_case
    case_store.open_case = boom
    try:
        res = await svc.open_case_safe(G, U, "basic", None)
    finally:
        case_store.open_case = orig
    assert res["type"] == "error"
    assert await _balance(env["pool"]) == 5000
    assert await _rep(env["pool"]) == 10  # rep_price REP возвращены


@pytest.mark.asyncio
async def test_pet_op_dual_charge(env, monkeypatch):
    """Питомец: dual charge; при сбое create_pet — возврат обеих."""
    import storage.pet_store as pet_mod

    class _PS:
        async def has_pet_slot(self, u):
            return True
        _catalog = type("C", (), {"get_pet": staticmethod(lambda t: object())})()
        async def create_pet(self, **kw):
            raise RuntimeError("pet fail")

    monkeypatch.setattr(pet_mod, "pet_store", _PS())
    await _set_balance(env["pool"], 5000, rep=10)
    ok, msg, pet = await svc.purchase_pet(G, U, "fox", 3000)
    assert not ok
    assert await _balance(env["pool"]) == 5000
    assert await _rep(env["pool"]) == 10  # 3000→3 REP возвращены


# ------------------------------------------------------- миграция/начисления


@pytest.mark.asyncio
async def test_rep_balance_migration_from_reputation(env):
    """rep_balance инициализируется из player_stats.reputation однократно."""
    await _set_balance(env["pool"], 0, rep=25)
    bal = await rep_balance_store.get_balance(G, U)
    assert bal == 25
    # Повтор — идемпотентно, не обнуляется
    bal2 = await rep_balance_store.get_balance(G, U)
    assert bal2 == 25
    info = await rep_balance_store.get_info(G, U)
    assert info["earned_total"] == 25 and info["spent_total"] == 0


@pytest.mark.asyncio
async def test_award_rep_syncs_spendable_balance(env):
    """Начисление через reputation_store обновляет расходуемый REP."""
    from storage.reputation_store import reputation_store

    await _set_balance(env["pool"], 0, rep=5)
    # миграция
    assert await rep_balance_store.get_balance(G, U) == 5

    ok = await reputation_store.award_rep(
        G, U, "MANUAL_REP", 3, "test_event_1", user_name="t"
    )
    assert ok
    assert await rep_balance_store.get_balance(G, U) == 8
    # reputation выросла на 3 (5→8)
    async with env["pool"].acquire() as conn:
        rep_total = await conn.fetchval(
            "SELECT reputation FROM player_stats WHERE guild_id=$1 AND user_id=$2",
            G, U,
        )
    assert rep_total == 8


@pytest.mark.asyncio
async def test_admin_adjust_syncs_spendable(env):
    from storage.reputation_store import reputation_store

    await _set_balance(env["pool"], 0, rep=10)
    assert await rep_balance_store.get_balance(G, U) == 10
    res = await reputation_store.admin_adjust_rep(
        G, 1, U, -4, target_name="t", reason="test"
    )
    assert res is not None
    assert await rep_balance_store.get_balance(G, U) == 6
    # и обратно
    res = await reputation_store.admin_adjust_rep(
        G, 1, U, 2, target_name="t", reason="test2"
    )
    assert await rep_balance_store.get_balance(G, U) == 8


@pytest.mark.asyncio
async def test_spend_history(env):
    await _set_balance(env["pool"], 5000, rep=10)
    await svc.purchase(G, U, "tag_rep")
    hist = await rep_balance_store.get_spend_history(G, U, limit=5)
    assert hist and hist[0]["rep"] == 3
    assert hist[0]["item_id"] == "tag_rep"
    assert hist[0]["status"] == "done"
