"""PG-интеграционные тесты durable-операций покупок (SHOP 2.0).

Реальная PostgreSQL через pgserver: проверяется транзакционная
атомарность charge (баланс + shop_operations), идемпотентность
возвратов через match_coin_grants и восстановление после
имитации краша (операция остаётся charged, recover_pending
доводит её до терминального состояния).
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
from storage.shop_purchase_ops_store import purchase_ops_store

G, U = 777, 4242

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
CREATE TABLE IF NOT EXISTS player_stats (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    name TEXT DEFAULT '',
    reputation INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
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
"""


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_shop")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_DDL)
        await conn.execute("DELETE FROM user_balance")
        await conn.execute("DELETE FROM match_coin_grants")
        await conn.execute("DELETE FROM shop_operations")
        await conn.execute("DELETE FROM player_stats")
        await conn.execute("DELETE FROM rep_balance")
        await conn.execute("DELETE FROM rep_balance_grants")
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
    """JSON-инвентарь в tmp + реальный PG для баланса/операций."""
    shop_file = tmp_path / "shop_items.json"
    inv_file = tmp_path / "player_inventory.json"
    pops_json = tmp_path / "shop_purchase_ops.json"

    import storage.shop_ops_store as ops_mod

    monkeypatch.setattr(shop_store_mod, "SHOP_FILE", shop_file)
    monkeypatch.setattr(shop_store_mod, "INVENTORY_FILE", inv_file)
    monkeypatch.setattr(ops_mod, "OPS_FILE", tmp_path / "shop_ops.json")
    monkeypatch.setattr(pops_mod, "OPS_JSON", pops_json)
    # Decay не должен срабатывать в тестах
    monkeypatch.setattr(
        ubs_mod.decay_tracker, "should_apply_decay", lambda g, u: False
    )
    monkeypatch.setattr(
        ubs_mod.decay_tracker, "mark_processed", lambda g, u: None
    )

    shop = ShopStore()
    inv = InventoryStore()
    ops = ops_mod.ShopOpsStore()

    monkeypatch.setattr(svc, "shop_store", shop)
    monkeypatch.setattr(svc, "inventory_store", inv)
    monkeypatch.setattr(svc, "shop_ops_store", ops)
    # purchase_ops_store — реальный PG-синглтон (не патчим)
    purchase_ops_store._use_db = True
    purchase_ops_store._ddl_done = False
    user_balance_store._use_db = True

    item = ShopItem(
        id="tag_pg", name="[PG]", description="", price=500,
        cosmetic_type=CosmeticType.TAG, rarity=CosmeticRarity.BASIC,
        value="[PG]", category="tags",
    )
    shop.add_item(item)
    return {"shop": shop, "inv": inv, "item": item, "pool": pg_pool}


async def _set_balance(pool, amount, g=G, u=U):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_balance (guild_id, user_id, balance)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET balance=$3
            """,
            g, u, amount,
        )
        # Достаточный spendable REP (миграция из reputation)
        await conn.execute(
            """
            INSERT INTO player_stats (guild_id, user_id, reputation)
            VALUES ($1,$2,10000)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET reputation=10000
            """,
            g, u,
        )


async def _balance(pool, g=G, u=U):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT balance FROM user_balance "
            "WHERE guild_id=$1 AND user_id=$2",
            g, u,
        )


async def _op_status(op_id):
    return (await purchase_ops_store.get(op_id))["status"]


def _owned(env, g=G, u=U):
    return {c.item_id for c in env["inv"].get_player_inventory(g, u)}


# ------------------------------------------------------------------ tests


@pytest.mark.asyncio
async def test_purchase_success_pg(env):
    await _set_balance(env["pool"], 2000)
    res = await svc.purchase(G, U, "tag_pg")
    assert res.ok and res.granted == ["tag_pg"]
    assert await _balance(env["pool"]) == 1500
    assert "tag_pg" in _owned(env)
    op = await purchase_ops_store.recent(guild_id=G, limit=5)
    assert op[0]["status"] == "done"
    assert op[0]["kind"] == "purchase"


@pytest.mark.asyncio
async def test_purchase_insufficient_pg(env):
    await _set_balance(env["pool"], 100)
    res = await svc.purchase(G, U, "tag_pg")
    assert not res.ok and res.insufficient
    assert await _balance(env["pool"]) == 100
    assert "tag_pg" not in _owned(env)
    op = await purchase_ops_store.recent(guild_id=G, limit=5)
    assert op[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_duplicate_purchase_single_charge(env):
    await _set_balance(env["pool"], 5000)
    r1 = await svc.purchase(G, U, "tag_pg")
    r2 = await svc.purchase(G, U, "tag_pg")
    assert r1.ok and not r2.ok
    assert await _balance(env["pool"]) == 4500


@pytest.mark.asyncio
async def test_concurrent_same_item_purchase(env):
    """Два одновременных purchase() — ровно одно списание и выдача."""
    await _set_balance(env["pool"], 5000)
    r1, r2 = await asyncio.gather(
        svc.purchase(G, U, "tag_pg"),
        svc.purchase(G, U, "tag_pg"),
    )
    assert sorted([r1.ok, r2.ok]) == [False, True]
    assert await _balance(env["pool"]) == 4500
    assert list(_owned(env)).count("tag_pg") == 1


@pytest.mark.asyncio
async def test_crash_after_charge_recovery_grants(env):
    """Краш между charge и grant: recover_pending довыдаёт предмет."""
    await _set_balance(env["pool"], 2000)
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "purchase", "tag_pg", 500,
        payload={"components": ["tag_pg"], "bundle": False},
    )
    charge = await purchase_ops_store.charge(op_id, G, U, 500)
    assert charge == "charged"
    assert await _balance(env["pool"]) == 1500
    assert "tag_pg" not in _owned(env)

    resolved = await svc.recover_pending(bot=None)
    assert resolved >= 1
    assert "tag_pg" in _owned(env)
    assert await _op_status(op_id) == "done"
    assert await _balance(env["pool"]) == 1500


@pytest.mark.asyncio
async def test_crash_after_charge_no_double_on_recover(env):
    """Повторный recover не выдаёт и не списывает повторно."""
    await _set_balance(env["pool"], 2000)
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "purchase", "tag_pg", 500,
        payload={"components": ["tag_pg"], "bundle": False},
    )
    await purchase_ops_store.charge(op_id, G, U, 500)
    await svc.recover_pending(bot=None)
    await svc.recover_pending(bot=None)
    assert await _balance(env["pool"]) == 1500
    assert list(_owned(env)).count("tag_pg") == 1
    assert await _op_status(op_id) == "done"


@pytest.mark.asyncio
async def test_reserved_op_fails_without_charge(env):
    """Краш сразу после reserve: денег не списано, op → failed."""
    await _set_balance(env["pool"], 2000)
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(op_id, G, U, "purchase", "tag_pg", 500)
    await svc.recover_pending(bot=None)
    assert await _op_status(op_id) == "failed"
    assert await _balance(env["pool"]) == 2000


@pytest.mark.asyncio
async def test_bundle_partial_failure_compensation(env, monkeypatch):
    """Частичная выдача набора: выданное отзывается, деньги возвращаются,
    ранее принадлежавшие компоненты не трогаются."""
    c1 = ShopItem(
        id="b_c1", name="c1", description="", price=100,
        cosmetic_type=CosmeticType.TAG, rarity=CosmeticRarity.BASIC,
        value="c1", category="tags",
    )
    c2 = ShopItem(
        id="b_c2", name="c2", description="", price=100,
        cosmetic_type=CosmeticType.TAG, rarity=CosmeticRarity.BASIC,
        value="c2", category="tags",
    )
    bundle = ShopItem(
        id="b_bundle", name="BUNDLE", description="", price=900,
        cosmetic_type=CosmeticType.TAG, rarity=CosmeticRarity.SPECIAL,
        value="b", category="bundles", bundle=["b_c1", "b_c2"],
    )
    env["shop"].add_item(c1)
    env["shop"].add_item(c2)
    env["shop"].add_item(bundle)
    # c2 уже принадлежал пользователю до покупки набора
    from models.shop_item import PlayerCosmetic
    env["inv"].add_cosmetic(PlayerCosmetic(
        guild_id=G, user_id=U, item_id="b_c2", equipped=False,
    ))
    await _set_balance(env["pool"], 5000)

    real_grant = svc._grant_item
    calls = {"n": 0}

    async def flaky(g, u, item, guild):
        calls["n"] += 1
        if item.id == "b_c1":
            return False, "injected failure"
        return await real_grant(g, u, item, guild)

    monkeypatch.setattr(svc, "_grant_item", flaky)

    res = await svc.purchase(G, U, "b_bundle")
    assert res.ok and res.refunded
    # Деньги возвращены полностью
    assert await _balance(env["pool"]) == 5000
    owned = _owned(env)
    # c1 не выдан (отозван/не создан), c2 — прежний, не удалён
    assert "b_c1" not in owned
    assert "b_c2" in owned
    op = await purchase_ops_store.recent(guild_id=G, limit=5)
    assert op[0]["status"] == "refunded"


@pytest.mark.asyncio
async def test_bundle_grants_only_missing(env):
    """Набор выдаёт только недостающие компоненты."""
    c1 = ShopItem(
        id="m_c1", name="c1", description="", price=100,
        cosmetic_type=CosmeticType.TAG, rarity=CosmeticRarity.BASIC,
        value="c1", category="tags",
    )
    bundle = ShopItem(
        id="m_bundle", name="B", description="", price=900,
        cosmetic_type=CosmeticType.TAG, rarity=CosmeticRarity.SPECIAL,
        value="b", category="bundles", bundle=["m_c1", "tag_pg"],
    )
    env["shop"].add_item(c1)
    env["shop"].add_item(bundle)
    await _set_balance(env["pool"], 5000)
    # tag_pg уже куплен ранее? нет — купим его заранее
    await svc.purchase(G, U, "tag_pg")
    bal_before = await _balance(env["pool"])

    res = await svc.purchase(G, U, "m_bundle")
    assert res.ok
    assert res.granted == ["m_c1"]  # tag_pg уже был — не перевыдан
    assert await _balance(env["pool"]) == bal_before - 900
    owned = _owned(env)
    assert "m_c1" in owned and "tag_pg" in owned


@pytest.mark.asyncio
async def test_repeated_refund_single_credit(env):
    """Повторный возврат по той же op не начисляет дважды."""
    await _set_balance(env["pool"], 1000)
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "purchase", "tag_pg", 500,
    )
    await purchase_ops_store.charge(op_id, G, U, 500)
    assert await _balance(env["pool"]) == 500

    r1 = await svc._refund_once(G, U, 500, op_id)
    r2 = await svc._refund_once(G, U, 500, op_id)
    r3 = await svc._refund_once(G, U, 500, op_id)
    assert r1["credited"] == 500
    # Повторные вызовы возвращают recorded amount, баланс не растёт
    assert await _balance(env["pool"]) == 1000
    grants = await env["pool"].fetchval(
        "SELECT count(*) FROM match_coin_grants "
        "WHERE match_id=$1 AND user_id=$2",
        f"shopref:{op_id}", U,
    )
    assert grants == 1


@pytest.mark.asyncio
async def test_refund_response_loss_no_double(env):
    """Грант shopref:{op} выполнился, ответ «потерян» — повторный
    refund() возвращает recorded amount без повторного начисления."""
    await _set_balance(env["pool"], 1000)
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "purchase", "tag_pg", 500,
    )
    await purchase_ops_store.charge(op_id, G, U, 500)

    # «Первый» refund выполнился на уровне БД
    credited = await user_balance_store.add_balance_once(
        G, U, 500, f"shopref:{op_id}"
    )
    assert credited == 500
    assert await _balance(env["pool"]) == 1000

    # Ответ потерялся → сервис повторяет refund → recorded amount
    res = await purchase_ops_store.refund(op_id, G, U, 500)
    assert res == {
        "credited": 500, "pending": 0,
        "rep_credited": 0, "rep_pending": 0,
    }
    assert await _balance(env["pool"]) == 1000  # НЕ 1500


@pytest.mark.asyncio
async def test_settle_pending_refund_max_balance(env):
    """Возврат при MAX_BALANCE: недоплата уходит в pending и
    досчитывается после освобождения места — без двойного гранта."""
    from config import MAX_BALANCE

    await _set_balance(env["pool"], MAX_BALANCE)
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "case", "case_x", 1000,
        payload={"refund_pending": 400},
    )
    await purchase_ops_store.set_status(op_id, "unknown")

    # Базовый грант «частично» зафиксирован (имитация cap):
    # записываем recorded amount 600 вручную через add_balance_once
    # на пониженном балансе нельзя — делаем прямо: грант на 600
    # уже «существует» → settle увидит uncovered=400.
    async with env["pool"].acquire() as conn:
        await conn.execute(
            """
            INSERT INTO match_coin_grants (match_id, guild_id, user_id, amount)
            VALUES ($1,$2,$3,600)
            ON CONFLICT (match_id, user_id) DO NOTHING
            """,
            f"shopref:{op_id}", G, U,
        )

    # Баланс на cap → shoprefp не может начислить
    remaining = await purchase_ops_store.settle_pending_refund(
        await purchase_ops_store.get(op_id)
    )
    assert remaining == 400
    assert await _balance(env["pool"]) == MAX_BALANCE

    # Освобождаем место → досчёт проходит
    await _set_balance(env["pool"], MAX_BALANCE - 1000)
    remaining = await purchase_ops_store.settle_pending_refund(
        await purchase_ops_store.get(op_id)
    )
    assert remaining == 0
    assert await _balance(env["pool"]) == MAX_BALANCE - 600
    # Полный возврат = 600 (base) + 400 (settled) = 1000, без дублей
    total = await env["pool"].fetchval(
        "SELECT COALESCE(SUM(amount),0) FROM match_coin_grants "
        "WHERE guild_id=$1 AND user_id=$2 AND match_id LIKE $3",
        G, U, f"%{op_id}%",
    )
    assert total == 1000


@pytest.mark.asyncio
async def test_role_item_grant_failure_refunds(env):
    """Ошибка выдачи Discord-роли (guild=None) → полный возврат."""
    role_item = ShopItem(
        id="role_x", name="Role", description="", price=700,
        cosmetic_type=CosmeticType.COLOR, rarity=CosmeticRarity.PREMIUM,
        value="#fff", category="roles", item_type="role", role_id=12345,
    )
    env["shop"].add_item(role_item)
    await _set_balance(env["pool"], 2000)

    res = await svc.purchase(G, U, "role_x", guild=None)
    assert res.ok and res.refunded
    assert await _balance(env["pool"]) == 2000
    assert "role_x" not in _owned(env)
    op = await purchase_ops_store.recent(guild_id=G, limit=5)
    assert op[0]["status"] == "refunded"


@pytest.mark.asyncio
async def test_color_role_items_not_sellable():
    """Цветные роли сеются с is_sellable=False."""
    from storage.shop_items import _color_role_items
    import json as _json
    import os
    os.environ["SHOP_COLOR_ROLES"] = _json.dumps({
        "emerald": {"name": "Emerald", "role_id": 1,
                    "price": 100, "color": "#fff"}
    })
    try:
        items = _color_role_items()
    finally:
        os.environ.pop("SHOP_COLOR_ROLES", None)
    assert items and all(not i.is_sellable for i in items)
    assert all(i.category == "color_roles" for i in items)


@pytest.mark.asyncio
async def test_case_op_recovery_refunds(env):
    """Краш во время открытия кейса: charged case-op → возврат."""
    await _set_balance(env["pool"], 2000)
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "case", "basic", 300,
        payload={"case_id": "basic"},
    )
    await purchase_ops_store.charge(op_id, G, U, 300)
    assert await _balance(env["pool"]) == 1700

    await svc.recover_pending(bot=None)
    assert await _op_status(op_id) == "refunded"
    assert await _balance(env["pool"]) == 2000


@pytest.mark.asyncio
async def test_pet_op_recovery_no_pet_refunds(env, monkeypatch):
    """charged pet-op без питомца → возврат; с питомцем → done."""
    import storage.pet_store as pet_mod

    class _FakePetStore:
        def __init__(self, has_pet):
            self._has = has_pet
        async def get_user_pet(self, g, u):
            return object() if self._has else None

    await _set_balance(env["pool"], 2000)
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "pet", "fox", 400,
        payload={"pet_type": "fox"},
    )
    await purchase_ops_store.charge(op_id, G, U, 400)

    monkeypatch.setattr(pet_mod, "pet_store", _FakePetStore(False))
    await svc.recover_pending(bot=None)
    assert await _op_status(op_id) == "refunded"
    assert await _balance(env["pool"]) == 2000

    # Второй сценарий: питомец «есть» → done без возврата
    await _set_balance(env["pool"], 3000)
    op_id2 = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id2, G, U, "pet", "fox", 400,
    )
    await purchase_ops_store.charge(op_id2, G, U, 400)
    monkeypatch.setattr(pet_mod, "pet_store", _FakePetStore(True))
    await svc.recover_pending(bot=None)
    assert await _op_status(op_id2) == "done"
    assert await _balance(env["pool"]) == 2600  # списание осталось, возврата нет


@pytest.mark.asyncio
async def test_old_inventory_preserved(env):
    """Старые записи инвентаря не трогаются операциями магазина."""
    from models.shop_item import PlayerCosmetic
    env["inv"].add_cosmetic(PlayerCosmetic(
        guild_id=G, user_id=U, item_id="legacy_item", equipped=True,
    ))
    await _set_balance(env["pool"], 2000)
    res = await svc.purchase(G, U, "tag_pg")
    assert res.ok
    owned = env["inv"].get_player_inventory(G, U)
    legacy = [c for c in owned if c.item_id == "legacy_item"]
    assert legacy and legacy[0].equipped is True
