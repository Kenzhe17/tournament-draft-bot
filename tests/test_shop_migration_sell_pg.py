"""PG regression-тесты: миграция каталога JSON→PG, восстановление
метаданных товаров и атомарная продажа (sell-операции).

Сценарии: пустая PG + JSON, частичная PG + JSON, ручные правки,
повторная миграция, NULL-метаданные, конфликты источников,
crash-before/after-credit в продаже, короткая выплата MAX_BALANCE.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

import storage.db as dbmod
import storage.shop_store as shop_store_mod
import storage.user_balance_store as ubs_mod
from storage.shop_store import InventoryStore, ShopStore
from storage.user_balance_store import user_balance_store
from storage.shop_purchase_ops_store import purchase_ops_store
from storage import shop_catalog
from services import shop_purchase_service as svc

G, U = 555, 424242

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS shop_items (
    item_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    price INTEGER NOT NULL,
    category TEXT NOT NULL,
    rarity TEXT NOT NULL,
    is_sellable BOOLEAN DEFAULT TRUE,
    emoji TEXT,
    is_active BOOLEAN DEFAULT TRUE
);
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
CREATE TABLE IF NOT EXISTS shop_catalog_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

TABLES = ("shop_items", "user_balance", "match_coin_grants",
          "shop_operations", "shop_catalog_meta")


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_mig")
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

    monkeypatch.setattr(shop_store_mod, "SHOP_FILE", shop_file)
    monkeypatch.setattr(shop_store_mod, "INVENTORY_FILE", inv_file)
    monkeypatch.setattr(
        ubs_mod.decay_tracker, "should_apply_decay", lambda g, u: False
    )
    monkeypatch.setattr(
        ubs_mod.decay_tracker, "mark_processed", lambda g, u: None
    )
    purchase_ops_store._use_db = True
    purchase_ops_store._ddl_done = False
    user_balance_store._use_db = True

    shop = ShopStore()
    inv = InventoryStore()
    monkeypatch.setattr(svc, "inventory_store", inv)
    return {"shop": shop, "inv": inv, "pool": pg_pool,
            "shop_file": shop_file}


def _write_json(path, items: dict):
    path.write_text(json.dumps(items, ensure_ascii=False),
                    encoding="utf-8")


def _item_dict(item_id, **kw):
    d = {
        "id": item_id, "name": item_id, "description": "",
        "price": 100, "cosmetic_type": "tag", "rarity": "basic",
        "value": "v", "category": "tags", "item_type": "cosmetic",
        "is_sellable": True, "is_active": True,
    }
    d.update(kw)
    return d


# ---------------------------------------------------------- JSON → PG

@pytest.mark.asyncio
async def test_empty_pg_imports_json_user_items(env):
    """Пустая PG + JSON с пользовательским товаром: товар
    импортируется ДО досева дефолтов, источник 'json'."""
    _write_json(env["shop_file"], {
        "custom_user_item": _item_dict(
            "custom_user_item", price=777, rep_price=3,
            bundle=["a", "b"], available_from="2025-01-01",
        ),
        "hidden_user_item": _item_dict(
            "hidden_user_item", is_active=False, price=50,
        ),
    })
    shop = ShopStore()
    status = await shop_catalog.sync_to_store(shop)
    assert status == "json_migrated"

    rows = await shop_catalog.load_all()
    by_id = {r["id"]: r for r in rows}
    assert "custom_user_item" in by_id
    assert by_id["custom_user_item"]["price"] == 777
    assert by_id["custom_user_item"]["rep_price"] == 3
    assert by_id["custom_user_item"]["bundle"] == ["a", "b"]
    assert by_id["custom_user_item"]["available_from"] == "2025-01-01"
    # Скрытый JSON-товар остаётся скрытым.
    assert by_id["hidden_user_item"]["is_active"] is False
    # Дефолты досеяны после импорта.
    assert "icon_heart" in by_id
    # Пользовательские товары не затёрты дефолтами.
    assert shop.get_item("custom_user_item").price == 777


@pytest.mark.asyncio
async def test_json_migration_idempotent_and_marker(env):
    """Повторный sync не реимпортирует JSON (маркер стоит)."""
    _write_json(env["shop_file"], {
        "once_item": _item_dict("once_item", price=11),
    })
    shop = ShopStore()
    await shop_catalog.sync_to_store(shop)
    # Удаляем строку из PG и повторяем sync — JSON не должен
    # воскресить её (миграция уже выполнена).
    async with env["pool"].acquire() as conn:
        await conn.execute(
            "DELETE FROM shop_items WHERE item_id='once_item'")
    status = await shop_catalog.sync_to_store(shop)
    assert status == "pg"
    rows = await shop_catalog.load_all()
    assert "once_item" not in {r["id"] for r in rows}


@pytest.mark.asyncio
async def test_json_skipped_when_panel_edits_exist(env):
    """В PG есть ручные правки → JSON не воскрешает товары."""
    _write_json(env["shop_file"], {
        "resurrect_me": _item_dict("resurrect_me"),
    })
    # Имитируем уже отредактированный каталог.
    await shop_catalog.upsert_item(
        _item_dict("panel_item", price=999), source="panel")
    shop = ShopStore()
    status = await shop_catalog.sync_to_store(shop)
    # json_skipped — не миграция, каталог из PG.
    assert status == "pg"
    rows = await shop_catalog.load_all()
    ids = {r["id"] for r in rows}
    assert "resurrect_me" not in ids
    assert "panel_item" in ids
    # Дефолты всё равно досеиваются.
    assert "icon_heart" in ids


@pytest.mark.asyncio
async def test_partial_pg_completes_interrupted_migration(env):
    """Краш на середине импорта: часть JSON-строк уже в PG,
    маркера нет → следующий sync докачивает остальные."""
    _write_json(env["shop_file"], {
        "j1": _item_dict("j1"), "j2": _item_dict("j2"),
        "j3": _item_dict("j3"),
    })
    # Имитация прерванной миграции: одна строка есть, маркера нет.
    await shop_catalog.upsert_item(_item_dict("j1"), source="json")
    shop = ShopStore()
    status = await shop_catalog.sync_to_store(shop)
    assert status == "json_migrated"
    rows = await shop_catalog.load_all()
    ids = {r["id"] for r in rows}
    assert {"j1", "j2", "j3"} <= ids


# ------------------------------------------------- metadata migration

@pytest.mark.asyncio
async def test_metadata_migration_restores_and_is_idempotent(env):
    """NULL bundle/rep_price/season восстанавливаются из
    согласных источников; повторный запуск — no-op."""
    # «Старая» строка PG: метаданные потеряны (NULL).
    await shop_catalog.upsert_item(
        _item_dict("old_item", price=321), source="default")
    # Источники: JSON задаёт rep_price + сезон, дефолт bundle.
    _write_json(env["shop_file"], {
        "old_item": _item_dict(
            "old_item", price=321, rep_price=7,
            available_until="2026-12-31",
        ),
    })
    rows = await shop_catalog.load_all()
    report = await shop_catalog.migrate_item_metadata(rows, [
        _item_dict("old_item", rep_price=7,
                   available_until="2026-12-31"),
    ])
    restored = {(r["item_id"], r["field"]) for r in report["restored"]}
    assert ("old_item", "rep_price") in restored
    assert ("old_item", "available_until") in restored

    rows = await shop_catalog.load_all()
    it = next(r for r in rows if r["id"] == "old_item")
    assert it["rep_price"] == 7
    assert it["available_until"] == "2026-12-31"
    # Цена пользователя не тронута.
    assert it["price"] == 321

    # Повторный запуск: восстанавливать нечего.
    report2 = await shop_catalog.migrate_item_metadata(rows, [])
    assert report2["restored"] == []


@pytest.mark.asyncio
async def test_metadata_migration_conflict_keeps_pg(env):
    """PG-значение задано и отличается от источника → конфликт,
    PG сохраняется."""
    await shop_catalog.upsert_item(
        _item_dict("conf_item", rep_price=5), source="panel")
    rows = await shop_catalog.load_all()
    report = await shop_catalog.migrate_item_metadata(rows, [
        _item_dict("conf_item", rep_price=9),
    ])
    assert any(
        c["item_id"] == "conf_item" and c["field"] == "rep_price"
        and c["resolution"] == "kept_pg"
        for c in report["conflicts"]
    )
    rows = await shop_catalog.load_all()
    it = next(r for r in rows if r["id"] == "conf_item")
    assert it["rep_price"] == 5


@pytest.mark.asyncio
async def test_metadata_migration_ambiguous_sources_skipped(
    env, monkeypatch
):
    """Источники расходятся → конфликт, значение не пишется."""
    from models.shop_item import ShopItem
    import storage.shop_items as si

    # JSON-источник: rep_price=3; дефолт: rep_price=4 → ambiguous.
    monkeypatch.setattr(
        si, "default_shop_items",
        lambda: [ShopItem.from_dict(
            _item_dict("amb_item", rep_price=4))],
    )
    report = await shop_catalog.migrate_item_metadata(
        [{"id": "amb_item", "rep_price": None}],
        [_item_dict("amb_item", rep_price=3)],
    )
    assert any(
        c["item_id"] == "amb_item"
        and c["resolution"] == "skipped_ambiguous"
        for c in report["conflicts"]
    )


# ------------------------------------------------------------- sell

async def _mk_sell_op(env, price=1000, payload=None):
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "sell", "tag_x", price,
        payload=payload or {"cosmetic": {"item_id": "tag_x"}},
    )
    return op_id


@pytest.mark.asyncio
async def test_sell_recovery_completes_interrupted_sale(env):
    """Crash после create, до credit: recovery зачисляет монеты
    один раз и удаляет предмет."""
    inv = env["inv"]
    from models.shop_item import PlayerCosmetic
    inv.add_cosmetic(PlayerCosmetic(
        guild_id=G, user_id=U, item_id="tag_x"))
    async with env["pool"].acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_balance (guild_id, user_id, balance)
            VALUES ($1,$2,0)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET balance=0
            """, G, U)

    op_id = await _mk_sell_op(env, price=1000)
    # Имитируем charged без credited (крэш между credit и marker).
    await purchase_ops_store.set_status(op_id, "charged")

    op = await purchase_ops_store.get(op_id)
    result = await svc._recover_sell_op(op)
    assert result == "done"

    bal = await user_balance_store.get_balance(G, U)
    assert bal == 1000
    # Предмет удалён.
    cosmetics = inv.get_player_inventory(G, U)
    assert not any(c.item_id == "tag_x" for c in cosmetics)

    # Повторный recovery — без двойного начисления.
    op = await purchase_ops_store.get(op_id)
    if op and op["status"] in ("reserved", "charged", "unknown"):
        await svc._recover_sell_op(op)
    bal2 = await user_balance_store.get_balance(G, U)
    assert bal2 == 1000


@pytest.mark.asyncio
async def test_sell_recovery_no_double_credit(env):
    """credited уже зафиксирован → recovery не начисляет повторно,
    только доводит удаление предмета."""
    inv = env["inv"]
    from models.shop_item import PlayerCosmetic
    inv.add_cosmetic(PlayerCosmetic(
        guild_id=G, user_id=U, item_id="tag_y"))
    async with env["pool"].acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_balance (guild_id, user_id, balance)
            VALUES ($1,$2,0)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET balance=0
            """, G, U)
    # Имитируем: credit уже прошёл (charged + credited).
    credited = await user_balance_store.add_balance_once(
        G, U, 500, "sellop:preseeded")
    assert credited == 500

    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "sell", "tag_y", 500,
        payload={"credited": 500,
                 "cosmetic": {"item_id": "tag_y"}},
    )
    await purchase_ops_store.set_status(op_id, "charged")

    op = await purchase_ops_store.get(op_id)
    # Подменяем ключ гранта: recovery использует sellop:{op_id} —
    # другой ключ → было бы двойное начисление. Проверяем, что
    # payload.credited защищает от этого.
    result = await svc._recover_sell_op(op)
    assert result == "done"
    bal = await user_balance_store.get_balance(G, U)
    # Только исходные 500 — recovery НЕ начислил ещё 500.
    assert bal == 500


@pytest.mark.asyncio
async def test_sell_recovery_shortfall_marks_unknown(env):
    """Баланс у cap → зачисление 0 → op unknown с shortfall,
    предмет НЕ удаляется."""
    from config import MAX_BALANCE
    inv = env["inv"]
    from models.shop_item import PlayerCosmetic
    inv.add_cosmetic(PlayerCosmetic(
        guild_id=G, user_id=U, item_id="tag_z"))
    async with env["pool"].acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_balance (guild_id, user_id, balance)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET balance=$3
            """,
            G, U, MAX_BALANCE,
        )

    op_id = await _mk_sell_op(env, price=1000)
    await purchase_ops_store.set_status(op_id, "charged")
    op = await purchase_ops_store.get(op_id)
    result = await svc._recover_sell_op(op)
    assert result == "unknown"
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "unknown"
    assert op["payload"].get("shortfall") == 1000
    # Предмет на месте — ничего не потеряно.
    cosmetics = inv.get_player_inventory(G, U)
    assert any(c.item_id == "tag_z" for c in cosmetics)
