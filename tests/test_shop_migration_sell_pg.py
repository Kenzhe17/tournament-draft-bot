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
async def test_json_diff_imports_missing_keeps_panel_edits(env):
    """Ручные правки в PG не блокируют восстановление: импортируются
    ТОЛЬКО отсутствующие в PG id, существующие строки (включая
    panel-отредактированные) не перезаписываются."""
    _write_json(env["shop_file"], {
        "resurrect_me": _item_dict("resurrect_me", price=555),
        "panel_item": _item_dict("panel_item", price=1),  # конфликт цены
    })
    await shop_catalog.upsert_item(
        _item_dict("panel_item", price=999), source="panel")
    shop = ShopStore()
    status = await shop_catalog.sync_to_store(shop)
    assert status == "json_migrated"
    rows = await shop_catalog.load_all()
    by_id = {r["id"]: r for r in rows}
    # Отсутствующий id восстановлен.
    assert "resurrect_me" in by_id
    assert by_id["resurrect_me"]["price"] == 555
    # Ручная правка НЕ перезаписана JSON.
    assert by_id["panel_item"]["price"] == 999
    # Дефолты досеиваются.
    assert "icon_heart" in by_id


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

async def _mk_sell_op(env, price=1000, payload=None, item_id="tag_x"):
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, G, U, "sell", item_id, price,
        payload=payload or {"cosmetic": {"item_id": item_id}},
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


# ------------------------------------------- sell state machine crashes

async def _reset_balance(env, amount=0):
    async with env["pool"].acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_balance (guild_id, user_id, balance)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET balance=$3
            """, G, U, amount)


def _add_item(env, item_id):
    from models.shop_item import PlayerCosmetic
    env["inv"].add_cosmetic(PlayerCosmetic(
        guild_id=G, user_id=U, item_id=item_id))


def _has_item(env, item_id):
    return any(
        c.item_id == item_id
        for c in env["inv"].get_player_inventory(G, U))


@pytest.mark.asyncio
async def test_sell_reserved_with_committed_grant(env):
    """Крэш ПОСЛЕ credit, ДО set_status(charged): op остаётся
    reserved, но грант sellop:{op_id} закоммичен. Recovery НЕ
    должен пометить failed — предмет изымается, двойного
    начисления нет."""
    _add_item(env, "tag_c1")
    await _reset_balance(env)
    op_id = await _mk_sell_op(
        env, price=700, item_id="tag_c1",
        payload={"ready": True,
                 "cosmetic": {"item_id": "tag_c1"}},
    )
    # Имитируем закоммиченный кредит с потерянным ответом:
    credited = await user_balance_store.add_balance_once(
        G, U, 700, f"sellop:{op_id}")
    assert credited == 700
    # op всё ещё reserved (set_status потерян)
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "reserved"

    # Полный recover_pending — reserved sell НЕ уходит в failed.
    await svc.recover_pending(bot=None)
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "done"
    assert not _has_item(env, "tag_c1")
    bal = await user_balance_store.get_balance(G, U)
    assert bal == 700  # ровно один кредит


@pytest.mark.asyncio
async def test_sell_reserved_no_credit_completes_sale(env):
    """Крэш после маркера ready, до credit: recovery завершает
    продажу (кредит + удаление) — intent подтверждён."""
    _add_item(env, "tag_c2")
    await _reset_balance(env)
    op_id = await _mk_sell_op(
        env, price=300, item_id="tag_c2",
        payload={"ready": True,
                 "cosmetic": {"item_id": "tag_c2"}},
    )
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "reserved"
    await svc.recover_pending(bot=None)
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "done"
    assert not _has_item(env, "tag_c2")
    assert await user_balance_store.get_balance(G, U) == 300


@pytest.mark.asyncio
async def test_sell_reserved_before_ready_is_aborted(env):
    """Крэш сразу после create, ДО preliminary checks: ready не
    выставлен → recovery НЕ начисляет и НЕ удаляет — op failed,
    предмет и баланс нетронуты."""
    _add_item(env, "tag_c2b")
    await _reset_balance(env)
    op_id = await _mk_sell_op(env, price=300, item_id="tag_c2b")
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "reserved"
    assert not op["payload"].get("ready")
    await svc.recover_pending(bot=None)
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "failed"
    assert op["payload"].get("reason") == "prechecks_not_completed"
    assert _has_item(env, "tag_c2b")
    assert await user_balance_store.get_balance(G, U) == 0
    async with env["pool"].acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM match_coin_grants "
            "WHERE match_id=$1", f"sellop:{op_id}")
    assert n == 0
    # Повторный recovery — без изменений.
    await svc.recover_pending(bot=None)
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "failed"
    assert _has_item(env, "tag_c2b")


@pytest.mark.asyncio
async def test_sell_unknown_shortfall_stays_unknown(env):
    """unknown sell-op с частичным кредитом НЕ переводится
    в refunded общим обработчиком — предмет сохранён."""
    from config import MAX_BALANCE
    _add_item(env, "tag_c3")
    await _reset_balance(env, MAX_BALANCE)
    op_id = await _mk_sell_op(
        env, price=900, item_id="tag_c3",
        payload={"ready": True,
                 "cosmetic": {"item_id": "tag_c3"}},
    )
    await purchase_ops_store.set_status(op_id, "charged")
    await purchase_ops_store.set_status(
        op_id, "unknown",
        payload={"shortfall": 900, "credited": 0},
    )
    await svc.recover_pending(bot=None)
    op = await purchase_ops_store.get(op_id)
    # Не refunded и не done — ждёт ручного разбора.
    assert op["status"] == "unknown"
    assert _has_item(env, "tag_c3")


@pytest.mark.asyncio
async def test_sell_concurrent_recovery_single_credit(env):
    """Два параллельных recovery одной операции → один грант,
    один результат."""
    _add_item(env, "tag_c4")
    await _reset_balance(env)
    op_id = await _mk_sell_op(
        env, price=800, item_id="tag_c4",
        payload={"ready": True,
                 "cosmetic": {"item_id": "tag_c4"}},
    )
    op = await purchase_ops_store.get(op_id)
    await svc._recover_sell_op(op)
    op = await purchase_ops_store.get(op_id)
    # Имитируем повторный запуск по «старому» снапшоту op.
    await svc._recover_sell_op(op)
    bal = await user_balance_store.get_balance(G, U)
    assert bal == 800
    async with env["pool"].acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM match_coin_grants "
            "WHERE match_id=$1", f"sellop:{op_id}")
    assert n == 1


@pytest.mark.asyncio
async def test_sell_lost_set_status_response(env):
    """Потерянный ответ PG после COMMIT set_status(charged):
    op читается как reserved, но credited уже в payload
    нельзя — грант является источником истины."""
    _add_item(env, "tag_c5")
    await _reset_balance(env)
    op_id = await _mk_sell_op(
        env, price=400, item_id="tag_c5",
        payload={"ready": True,
                 "cosmetic": {"item_id": "tag_c5"}},
    )
    # credit закоммичен, marker set_status «потерян» (не вызван)
    await user_balance_store.add_balance_once(
        G, U, 400, f"sellop:{op_id}")
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "reserved"
    assert op["payload"].get("credited") is None
    await svc._recover_sell_op(op)
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "done"
    assert not _has_item(env, "tag_c5")
    # Повторный recovery — ноль изменений.
    await svc._recover_sell_op(op)
    assert await user_balance_store.get_balance(G, U) == 400


# ------------------------------------------- sell precheck state machine

class _FakeRole:
    def __init__(self, rid):
        self.id = rid


class _FakeMember:
    def __init__(self, role_ids):
        self.roles = [_FakeRole(r) for r in role_ids]

    async def add_roles(self, role):
        if all(r.id != role.id for r in self.roles):
            self.roles.append(role)


class _FakeGuild:
    def __init__(self, member=None, role_id=None, fail_fetch=False):
        self._member = member
        self._role = _FakeRole(role_id) if role_id else None
        self._fail = fail_fetch

    async def fetch_member(self, uid):
        if self._fail:
            raise RuntimeError("discord api down")
        return self._member

    def get_role(self, rid):
        if self._role is not None and self._role.id == rid:
            return self._role
        return None


def _role_bot(guild):
    from types import SimpleNamespace
    return SimpleNamespace(get_guild=lambda gid: guild)


def _role_payload(role_id=777, **extra):
    p = {"item_type": "role", "role_id": role_id,
         "cosmetic": {"item_id": "role_item"}}
    p.update(extra)
    return p


async def _grant_count(env, op_id):
    async with env["pool"].acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM match_coin_grants "
            "WHERE match_id=$1", f"sellop:{op_id}")


@pytest.mark.asyncio
async def test_sell_precheck_crash_after_headroom_role_present(env):
    """Крэш после проверки баланса, ДО маркера role_present_before:
    роль на месте, мутаций не было → failed, ничего не трогаем."""
    _add_item(env, "role_item")
    await _reset_balance(env)
    member = _FakeMember([777])
    guild = _FakeGuild(member=member, role_id=777)
    op_id = await _mk_sell_op(
        env, price=500, item_id="role_item",
        payload=_role_payload())
    op = await purchase_ops_store.get(op_id)
    result = await svc._recover_sell_op(op, _role_bot(guild))
    assert result == "failed"
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "failed"
    assert op["payload"].get("reason") == "prechecks_not_completed"
    assert op["payload"].get("role_state") == "present"
    # Роль НЕ снята повторно, предмет цел, денег нет.
    assert any(r.id == 777 for r in member.roles)
    assert _has_item(env, "role_item")
    assert await user_balance_store.get_balance(G, U) == 0
    assert await _grant_count(env, op_id) == 0
    # Повторный recovery — те же failed, без изменений.
    await svc._recover_sell_op(op, _role_bot(guild))
    assert (await purchase_ops_store.get(op_id))["status"] == "failed"


@pytest.mark.asyncio
async def test_sell_precheck_role_removed_restores(env):
    """Крэш ПОСЛЕ снятия Discord-роли, ДО маркера ready:
    role_present_before + роль отсутствует → возврат роли
    best-effort, продажа aborted → failed. Предмет и деньги
    не тронуты."""
    _add_item(env, "role_item")
    await _reset_balance(env)
    member = _FakeMember([])  # роль уже снята live-потоком
    guild = _FakeGuild(member=member, role_id=777)
    op_id = await _mk_sell_op(
        env, price=500, item_id="role_item",
        payload=_role_payload(role_present_before=True))
    op = await purchase_ops_store.get(op_id)
    result = await svc._recover_sell_op(op, _role_bot(guild))
    assert result == "failed"
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "failed"
    assert op["payload"].get("reason") == "aborted_role_removed"
    assert op["payload"].get("role_restored") is True
    # Роль возвращена пользователю.
    assert any(r.id == 777 for r in member.roles)
    assert _has_item(env, "role_item")
    assert await user_balance_store.get_balance(G, U) == 0
    assert await _grant_count(env, op_id) == 0


@pytest.mark.asyncio
async def test_sell_precheck_role_absent_without_marker(env):
    """Роль изначально отсутствовала у пользователя (маркер
    role_present_before не ставился) → failed БЕЗ возврата
    роли: нельзя выдавать роль, которой не было."""
    _add_item(env, "role_item")
    await _reset_balance(env)
    member = _FakeMember([])
    guild = _FakeGuild(member=member, role_id=777)
    op_id = await _mk_sell_op(
        env, price=500, item_id="role_item",
        payload=_role_payload())
    op = await purchase_ops_store.get(op_id)
    result = await svc._recover_sell_op(op, _role_bot(guild))
    assert result == "failed"
    op = await purchase_ops_store.get(op_id)
    assert op["payload"].get("reason") == "prechecks_not_completed"
    # Роль НЕ добавлена — пользователь её не имел.
    assert not member.roles
    assert _has_item(env, "role_item")
    assert await user_balance_store.get_balance(G, U) == 0


@pytest.mark.asyncio
async def test_sell_precheck_role_unknown_waits(env):
    """Discord API недоступен при precheck → op остаётся
    pending (reserved), НИКАКИХ мутаций: ни кредита, ни
    удаления, ни решения по роли. Повторный recovery — то же."""
    _add_item(env, "role_item")
    await _reset_balance(env)
    guild = _FakeGuild(member=_FakeMember([]), role_id=777,
                       fail_fetch=True)
    op_id = await _mk_sell_op(
        env, price=500, item_id="role_item",
        payload=_role_payload(role_present_before=True))
    for _ in range(2):
        op = await purchase_ops_store.get(op_id)
        result = await svc._recover_sell_op(op, _role_bot(guild))
        assert result == "reserved"
        op = await purchase_ops_store.get(op_id)
        assert op["status"] == "reserved"
    assert _has_item(env, "role_item")
    assert await user_balance_store.get_balance(G, U) == 0
    assert await _grant_count(env, op_id) == 0


@pytest.mark.asyncio
async def test_sell_partial_credit_no_subtract(env):
    """Частичная выплата (баланс у cap): recovery фиксирует
    unknown + shortfall, предмет сохранён, обычное вычитание
    баланса НЕ выполняется — зачисленная часть остаётся."""
    from config import MAX_BALANCE
    _add_item(env, "tag_pc")
    # Запас места ровно 300 при цене 1000.
    await _reset_balance(env, MAX_BALANCE - 300)
    op_id = await _mk_sell_op(
        env, price=1000, item_id="tag_pc",
        payload={"ready": True,
                 "cosmetic": {"item_id": "tag_pc"}})
    op = await purchase_ops_store.get(op_id)
    result = await svc._recover_sell_op(op)
    assert result == "unknown"
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "unknown"
    assert op["payload"].get("shortfall") == 700
    assert op["payload"].get("credited") == 300
    # 300 зачислены и НЕ вычтены обратно — баланс у cap.
    assert await user_balance_store.get_balance(G, U) == MAX_BALANCE
    assert _has_item(env, "tag_pc")
    # Повторный recovery: грант записан (300) — не дублируется,
    # статус остаётся unknown для ручного разбора.
    op = await purchase_ops_store.get(op_id)
    await svc._recover_sell_op(op)
    assert (await purchase_ops_store.get(op_id))["status"] == "unknown"
    assert await _grant_count(env, op_id) == 1
    assert await user_balance_store.get_balance(G, U) == MAX_BALANCE


@pytest.mark.asyncio
async def test_sell_unknown_not_refunded_by_sweeper(env):
    """unknown sell-op с shortfall НЕ переводится в refunded
    settle_refund_debts — у sell нет refund-задолженностей,
    а generic-возврат закрыл бы op при предмете в инвентаре."""
    from config import MAX_BALANCE
    _add_item(env, "tag_sw")
    await _reset_balance(env, MAX_BALANCE)
    op_id = await _mk_sell_op(
        env, price=900, item_id="tag_sw",
        payload={"ready": True, "shortfall": 900, "credited": 0,
                 "cosmetic": {"item_id": "tag_sw"}})
    await purchase_ops_store.set_status(op_id, "unknown")
    settled = await svc.settle_refund_debts()
    assert settled == 0
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "unknown"
    assert _has_item(env, "tag_sw")


@pytest.mark.asyncio
async def test_sell_precheck_two_concurrent_recoveries(env):
    """Два параллельных recovery precheck-role-absent op:
    оба видят absent → оба пытаются восстановить роль;
    add_roles идемпотентен по факту, op failed один раз,
    денег/удалений нет."""
    import asyncio
    _add_item(env, "role_item")
    await _reset_balance(env)
    member = _FakeMember([])
    guild = _FakeGuild(member=member, role_id=777)
    op_id = await _mk_sell_op(
        env, price=500, item_id="role_item",
        payload=_role_payload(role_present_before=True))
    op1 = await purchase_ops_store.get(op_id)
    op2 = await purchase_ops_store.get(op_id)
    bot = _role_bot(guild)
    r1, r2 = await asyncio.gather(
        svc._recover_sell_op(op1, bot),
        svc._recover_sell_op(op2, bot),
    )
    assert sorted([r1, r2]) == ["failed", "failed"]
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "failed"
    assert _has_item(env, "role_item")
    assert await user_balance_store.get_balance(G, U) == 0
    assert await _grant_count(env, op_id) == 0


# ------------------------------------------- pending() PG failure (P0)

@pytest.mark.asyncio
async def test_pending_raises_on_pg_failure(env, monkeypatch):
    """PG недоступен → pending() ОБЯЗАН бросить исключение,
    а не вернуть пустой/неполный JSON-список: recovery не
    должен «не видеть» действующие финансовые операции."""
    _add_item(env, "tag_pg1")
    await _reset_balance(env)
    op_id = await _mk_sell_op(
        env, price=100, item_id="tag_pg1",
        payload={"ready": True,
                 "cosmetic": {"item_id": "tag_pg1"}})

    async def _boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr(purchase_ops_store, "_pool", _boom)
    with pytest.raises(RuntimeError):
        await purchase_ops_store.pending()
    # recover_pending тоже не должен «успешно» обработать
    # пустой список — ошибка доходит до вызывающего кода.
    with pytest.raises(RuntimeError):
        await svc.recover_pending(bot=None)
    # Операция не тронута.
    monkeypatch.undo()
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "reserved"


@pytest.mark.asyncio
async def test_pending_json_mode_and_fallback_merge(
        env, monkeypatch, tmp_path):
    """Настоящий JSON-режим (_use_db=False) → pending() читает
    JSON. PG-режим + op, созданная через JSON-fallback во время
    сбоя PG → она видна в pending() вместе с PG-операциями."""
    import storage.shop_purchase_ops_store as pops_mod
    monkeypatch.setattr(
        pops_mod, "OPS_JSON", tmp_path / "shop_purchase_ops.json")
    _add_item(env, "tag_j1")
    await _reset_balance(env)

    # 1) JSON-fallback op: PG «упал» в момент create().
    async def _boom():
        raise RuntimeError("pg down")
    monkeypatch.setattr(purchase_ops_store, "_pool", _boom)
    json_op_id = await _mk_sell_op(
        env, price=50, item_id="tag_j1",
        payload={"ready": True,
                 "cosmetic": {"item_id": "tag_j1"}})
    monkeypatch.undo()

    # 2) PG-режим: pending() видит И pg-операции, И json-fallback.
    pg_op_id = await _mk_sell_op(
        env, price=60, item_id="tag_j2",
        payload={"ready": True,
                 "cosmetic": {"item_id": "tag_j2"}})
    ops = await purchase_ops_store.pending()
    ids = {o["op_id"] for o in ops}
    assert json_op_id in ids
    assert pg_op_id in ids

    # 3) Настоящий JSON-режим.
    purchase_ops_store._use_db = False
    try:
        ops = await purchase_ops_store.pending()
        assert any(o["op_id"] == json_op_id for o in ops)
    finally:
        purchase_ops_store._use_db = True
        purchase_ops_store._json_ops.clear()
        purchase_ops_store._save_json()


# ------------------------------------------- role cleanup gate (P1)

@pytest.mark.asyncio
async def test_sell_recovery_role_forbidden_stays_charged(
        env, monkeypatch):
    """Discord Forbidden при снятии роли → роль остаётся →
    op НЕ done: предмет не удаляется, монеты не дублируются,
    op доступен для повторного recovery."""
    import discord as _discord
    _add_item(env, "role_item")
    await _reset_balance(env)
    member = _FakeMember([777])  # роль не снимается
    guild = _FakeGuild(member=member, role_id=777)
    op_id = await _mk_sell_op(
        env, price=500, item_id="role_item",
        payload=_role_payload(ready=True))

    async def _forbidden(g, u, r):
        raise _discord.Forbidden(
            _discord.http.HTTPException(
                None, "Forbidden"), "no perms")
    monkeypatch.setattr(
        "utils.role_manager.remove_role", _forbidden,
        raising=False)

    op = await purchase_ops_store.get(op_id)
    result = await svc._recover_sell_op(op, _role_bot(guild))
    assert result == "charged"
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "charged"  # НЕ done
    assert _has_item(env, "role_item")  # предмет цел
    # Кредит прошёл один раз и не повторяется.
    assert await user_balance_store.get_balance(G, U) == 500
    assert await _grant_count(env, op_id) == 1
    assert any(r.id == 777 for r in member.roles)


@pytest.mark.asyncio
async def test_sell_recovery_role_http_exception_waits(
        env, monkeypatch):
    """Discord HTTPException/потерянный ответ: remove_role
    «успешен», но роль фактически на месте → state=unknown →
    op остаётся charged, done не ставится."""
    _add_item(env, "role_item")
    await _reset_balance(env)
    member = _FakeMember([777])  # re-fetch всё ещё показывает роль
    guild = _FakeGuild(member=member, role_id=777)
    op_id = await _mk_sell_op(
        env, price=500, item_id="role_item",
        payload=_role_payload(ready=True))

    async def _lost_response(g, u, r):
        return True  # «успех», но Discord не применил
    monkeypatch.setattr(
        "utils.role_manager.remove_role", _lost_response,
        raising=False)

    op = await purchase_ops_store.get(op_id)
    result = await svc._recover_sell_op(op, _role_bot(guild))
    assert result == "charged"
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "charged"
    assert _has_item(env, "role_item")
    assert await _grant_count(env, op_id) == 1


@pytest.mark.asyncio
async def test_sell_recovery_role_fetch_fails_waits(env):
    """fetch_member недоступен → state=unknown → op charged,
    без done, без повторного кредита."""
    _add_item(env, "role_item")
    await _reset_balance(env)
    guild = _FakeGuild(member=_FakeMember([]), role_id=777,
                       fail_fetch=True)
    op_id = await _mk_sell_op(
        env, price=500, item_id="role_item",
        payload=_role_payload(ready=True))
    op = await purchase_ops_store.get(op_id)
    result = await svc._recover_sell_op(op, _role_bot(guild))
    assert result == "charged"
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "charged"
    assert _has_item(env, "role_item")
    assert await _grant_count(env, op_id) == 1


@pytest.mark.asyncio
async def test_sell_recovery_role_retry_then_done(env, monkeypatch):
    """Первый recovery: Discord недоступен → charged.
    Второй recovery: роль подтверждённо отсутствует →
    предмет удалён, done. Кредит — ровно один."""
    _add_item(env, "role_item")
    await _reset_balance(env)
    member = _FakeMember([])  # роль уже снята live-потоком
    guild_down = _FakeGuild(member=member, role_id=777,
                            fail_fetch=True)
    guild_up = _FakeGuild(member=member, role_id=777)
    op_id = await _mk_sell_op(
        env, price=500, item_id="role_item",
        payload=_role_payload(ready=True))

    op = await purchase_ops_store.get(op_id)
    r1 = await svc._recover_sell_op(op, _role_bot(guild_down))
    assert r1 == "charged"
    assert (await purchase_ops_store.get(op_id))["status"] == "charged"

    op = await purchase_ops_store.get(op_id)
    r2 = await svc._recover_sell_op(op, _role_bot(guild_up))
    assert r2 == "done"
    op = await purchase_ops_store.get(op_id)
    assert op["status"] == "done"
    assert not _has_item(env, "role_item")
    assert await user_balance_store.get_balance(G, U) == 500
    assert await _grant_count(env, op_id) == 1


@pytest.mark.asyncio
async def test_sell_recovery_role_no_bot_stays_charged(env):
    """bot=None при role-item → проверить Discord нельзя →
    charged, НЕ done."""
    _add_item(env, "role_item")
    await _reset_balance(env)
    op_id = await _mk_sell_op(
        env, price=500, item_id="role_item",
        payload=_role_payload(ready=True))
    op = await purchase_ops_store.get(op_id)
    result = await svc._recover_sell_op(op, bot=None)
    assert result == "charged"
    assert (await purchase_ops_store.get(op_id))["status"] == "charged"
    assert _has_item(env, "role_item")

