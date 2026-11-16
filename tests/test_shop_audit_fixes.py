"""Regression-тесты полного аудита SHOP 2.0 (classic).

Покрывает: owner-only управление каталогом, PG↔Discord
согласованность каталога, восстановление ассортимента,
поля ShopItem (season/bundle/rep_price), скрытые товары,
безопасная продажа ролей, wardrobe-recovery, навигация.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

import discord  # noqa: F401 — нужен для views

from models.shop_item import (
    CosmeticRarity,
    CosmeticType,
    ShopItem,
)
from storage.shop_store import InventoryStore, ShopStore
import storage.db as dbmod
import storage.shop_catalog as shop_catalog

G, U = 999, 31337
OWNER = 42
ADMIN = 777

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
    "shop_items", "user_balance", "match_coin_grants",
    "shop_operations", "rep_balance", "rep_balance_grants",
    "player_stats", "reputation_transactions", "rep_log_outbox",
)


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_audit")
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
def catalog_env(monkeypatch, tmp_path, pg_pool):
    """Изолированный shop_store + PG-каталог."""
    import storage.shop_store as shop_store_mod
    import storage.shop_ops_store as ops_mod
    import storage.user_balance_store as ubs_mod
    from services import shop_purchase_service as svc
    from storage.shop_purchase_ops_store import purchase_ops_store
    import storage.shop_purchase_ops_store as pops_mod

    monkeypatch.setattr(
        shop_store_mod, "SHOP_FILE", tmp_path / "shop_items.json"
    )
    monkeypatch.setattr(
        shop_store_mod, "INVENTORY_FILE",
        tmp_path / "player_inventory.json",
    )
    monkeypatch.setattr(ops_mod, "OPS_FILE", tmp_path / "shop_ops.json")
    monkeypatch.setattr(pops_mod, "OPS_JSON", tmp_path / "pops.json")
    monkeypatch.setattr(
        ubs_mod.decay_tracker, "should_apply_decay", lambda g, u: False
    )
    monkeypatch.setattr(
        ubs_mod.decay_tracker, "mark_processed", lambda g, u: None
    )
    purchase_ops_store._use_db = True
    purchase_ops_store._ddl_done = False

    shop = ShopStore()
    inv = InventoryStore()
    monkeypatch.setattr(svc, "shop_store", shop)
    monkeypatch.setattr(svc, "inventory_store", inv)
    return {"shop": shop, "inv": inv, "pool": pg_pool}


# ============================================================ P0: owner-only


def test_admin_flags_owner_only(monkeypatch):
    """Только BOT_OWNER_ID управляет каталогом: ADMIN_USER_IDS и
    Discord Administrator прав не дают."""
    import config
    monkeypatch.setattr(config, "BOT_OWNER_ID", OWNER, raising=False)
    monkeypatch.setattr(config, "ADMIN_USER_IDS", {ADMIN}, raising=False)
    import views.shop2_view as v2

    owner = SimpleNamespace(id=OWNER)
    admin_user = SimpleNamespace(
        id=ADMIN, guild_permissions=SimpleNamespace(administrator=True)
    )
    discord_admin = SimpleNamespace(
        id=555, guild_permissions=SimpleNamespace(administrator=True)
    )
    regular = SimpleNamespace(
        id=556, guild_permissions=SimpleNamespace(administrator=False)
    )
    assert v2._is_admin(owner) is True
    assert v2._is_admin(admin_user) is False
    assert v2._is_admin(discord_admin) is False
    assert v2._is_admin(regular) is False
    assert v2._is_admin_uid(OWNER) is True
    assert v2._is_admin_uid(ADMIN) is False
    assert v2._is_admin_uid(555) is False


@pytest.mark.asyncio
async def test_require_owner_denies_non_owner(monkeypatch):
    import config
    monkeypatch.setattr(config, "BOT_OWNER_ID", OWNER, raising=False)
    import views.shop2_view as v2

    denied = SimpleNamespace(
        user=SimpleNamespace(id=ADMIN),
        response=SimpleNamespace(
            is_done=lambda: False,
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    assert await v2._require_owner(denied) is False
    denied.response.send_message.assert_awaited()

    allowed = SimpleNamespace(
        user=SimpleNamespace(id=OWNER),
        response=SimpleNamespace(is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    assert await v2._require_owner(allowed) is True


# ==================================================== P1: навигация/ожидание


def test_no_missing_await_on_async_view_builders():
    """edit_card(...) не должен получать coroutine от async-builder
    без await (регрессия: build_subcat_view)."""
    import pathlib
    import re
    src = pathlib.Path("views/shop2_view.py").read_text(
        encoding="utf-8"
    )
    bad = []
    for m in re.finditer(r"edit_card\(([^)]*)\)", src):
        args = m.group(1)
        # второй аргумент — builder call без await
        if re.search(r",\s*(?!await\b)(build_\w+|_open_\w+)\(", args):
            bad.append(src[:m.start()].count("\n") + 1)
    assert not bad, f"missing await in edit_card at lines {bad}"


@pytest.mark.asyncio
async def test_cat_select_routes_specialized_views(monkeypatch):
    """Выбор pets/cases/services/color_roles открывает
    специализированный экран, а не пустой общий каталог."""
    import views.shop2_view as v2

    calls = {}

    async def fake_builder(name):
        async def _b(g, u):
            calls[name] = (g, u)
            return f"view:{name}"
        return _b

    builders = {
        "pets": await fake_builder("pets"),
        "cases": await fake_builder("cases"),
        "services": await fake_builder("services"        ),
        "color_roles": await fake_builder("color_roles"),
    }
    monkeypatch.setattr(v2, "_CATEGORY_BUILDERS", builders)

    captured = []

    async def fake_edit_card(interaction, view):
        captured.append(view)

    monkeypatch.setattr(v2, "edit_card", fake_edit_card)

    for cat in ("pets", "cases", "services", "color_roles"):
        sel = v2._CatSelect(G, U)
        interaction = SimpleNamespace(
            guild_id=G, user=SimpleNamespace(id=U),
            response=SimpleNamespace(is_done=lambda: False),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        # discord.ui.Select.values — read-only property;
        # подменяем на время callback.
        monkeypatch.setattr(
            type(sel), "values", property(lambda s, _v=cat: [_v])
        )
        await sel.callback(interaction)
        assert captured[-1] == f"view:{cat}", cat


# ================================================= P1: каталог/поля


@pytest.mark.asyncio
async def test_catalog_roundtrip_new_fields(pg_pool, catalog_env):
    """available_from/until, bundle, rep_price переживают
    upsert → load_all → ShopItem.from_dict."""
    item = {
        "id": "bundle_test", "name": "B", "description": "d",
        "price": 5000, "category": "bundles", "rarity": "special",
        "is_sellable": True, "is_active": True,
        "cosmetic_type": "tag", "value": "[B]",
        "item_type": "cosmetic", "role_id": None,
        "required_level": 0,
        "available_from": "2026-12-01", "available_until": "2026-12-31",
        "bundle": ["tag_a", "icon_b"], "rep_price": 4,
    }
    await shop_catalog.upsert_item(item, actor_id=OWNER)
    rows = await shop_catalog.load_all()
    row = next(r for r in rows if r["id"] == "bundle_test")
    assert row["available_from"] == "2026-12-01"
    assert row["available_until"] == "2026-12-31"
    assert row["bundle"] == ["tag_a", "icon_b"]
    assert row["rep_price"] == 4
    si = ShopItem.from_dict(row)
    assert si.bundle == ["tag_a", "icon_b"]
    assert si.rep_price == 4
    assert si.available_from == "2026-12-01"


@pytest.mark.asyncio
async def test_sync_reseeds_missing_defaults_keeps_hidden(
    pg_pool, catalog_env
):
    """sync_to_store досеивает отсутствующие дефолтные id, но
    НЕ реактивирует намеренно скрытые товары и НЕ трогает
    отредактированные."""
    # Сидим один скрытый товар и один дефолтный с изменённой ценой
    await shop_catalog.upsert_item(
        {
            "id": "icon_letter", "name": "WW", "description": "",
            "price": 999, "category": "icons", "rarity": "basic",
            "is_sellable": True, "is_active": True,
            "cosmetic_type": "icon", "value": "icon_letter",
            "item_type": "cosmetic", "role_id": None,
            "required_level": 0,
        },
        actor_id=OWNER,
    )
    await shop_catalog.upsert_item(
        {
            "id": "tag_pro", "name": "[PRO]", "description": "",
            "price": 700, "category": "tags", "rarity": "basic",
            "is_sellable": True, "is_active": False,  # скрыт
            "cosmetic_type": "tag", "value": "[PRO]",
            "item_type": "cosmetic", "role_id": None,
            "required_level": 0,
        },
        actor_id=OWNER,
    )
    mode = await shop_catalog.sync_to_store(catalog_env["shop"])
    assert mode == "pg"
    all_items = {
        i.id: i for i in catalog_env["shop"].get_all_items_raw()
    }
    # Дефолтный отсутствовавший товар восстановлен
    assert "frame_ember" in all_items
    assert "bundle_shadow" in all_items
    assert "tag_shadow" in all_items
    # Скрытый остался скрытым
    assert all_items["tag_pro"].is_active is False
    # Отредактированная цена не затёрта сидом
    assert all_items["icon_letter"].price == 999
    # В активный каталог скрытый не попадает
    active_ids = {i.id for i in catalog_env["shop"].get_all_items()}
    assert "tag_pro" not in active_ids
    assert "frame_ember" in active_ids


@pytest.mark.asyncio
async def test_save_item_confirmed(pg_pool):
    """save_item_confirmed: True при записи в PG, False при сбое."""
    item = ShopItem(
        id="sv1", name="SV", description="", price=100,
        cosmetic_type=CosmeticType.ICON, rarity=CosmeticRarity.BASIC,
        value="v", category="icons",
    )
    assert await shop_catalog.save_item_confirmed(
        item, actor_id=OWNER
    ) is True
    rows = await shop_catalog.load_all()
    assert any(r["id"] == "sv1" and r["price"] == 100 for r in rows)

    # PG недоступен → False, исключение не пробрасывается
    async def boom():
        raise RuntimeError("pg down")
    orig = shop_catalog.get_pool
    shop_catalog.get_pool = boom
    try:
        assert await shop_catalog.save_item_confirmed(
            item, actor_id=OWNER
        ) is False
    finally:
        shop_catalog.get_pool = orig


@pytest.mark.asyncio
async def test_admin_view_lists_hidden_items(catalog_env, monkeypatch):
    """Админский каталог показывает и скрытые товары."""
    import views.shop2_view as v2
    shop = ShopStore()
    monkeypatch.setattr(v2, "shop_store", shop)
    shop.add_item(ShopItem(
        id="hid1", name="HID", description="", price=1,
        cosmetic_type=CosmeticType.ICON, rarity=CosmeticRarity.BASIC,
        value="v", category="icons", is_active=False,
    ))
    shop.add_item(ShopItem(
        id="act1", name="ACT", description="", price=1,
        cosmetic_type=CosmeticType.ICON, rarity=CosmeticRarity.BASIC,
        value="v", category="icons", is_active=True,
    ))
    found = set()

    def _collect(view):
        def _walk(c):
            for ch in getattr(c, "children", []) or []:
                _walk(ch)
            opts = getattr(c, "options", None)
            if opts:
                for o in opts:
                    found.add(getattr(o, "value", None))
        for comp in getattr(view, "children", []) or []:
            _walk(comp)
        container = getattr(view, "container", None)
        if container is not None:
            _walk(container)

    # Админский список пагинирован — обходим все страницы
    for page in range(5):
        _collect(await v2.build_admin_view(G, OWNER, page=page))
    assert "hid1" in found and "act1" in found


# ===================================================== P1: продажа ролей


@pytest.mark.asyncio
async def test_ensure_role_removed(monkeypatch):
    """Продажа role-предмета: подтверждённое снятие роли."""
    from cogs import sell as sell_mod

    role = SimpleNamespace(id=100)
    member_with = SimpleNamespace(roles=[role])
    member_without = SimpleNamespace(roles=[])

    class FakeGuild:
        def __init__(self, member):
            self._m = member
        async def fetch_member(self, uid):
            return self._m
        def get_role(self, rid):
            return role

    # Роль уже отсутствует → True без вызова remove_role
    g = FakeGuild(member_without)
    assert await sell_mod._ensure_role_removed(g, U, 100) is True

    # Роль есть, remove_role успешен, повторная проверка — нет роли
    calls = []

    async def fake_remove(guild, uid, rid):
        calls.append(rid)
        return True

    monkeypatch.setattr(
        "utils.role_manager.remove_role", fake_remove, raising=False
    )
    # member с ролью на первый fetch, без — на второй
    seq = [member_with, member_without]

    class SeqGuild(FakeGuild):
        async def fetch_member(self, uid):
            return seq.pop(0) if seq else member_without

    assert await sell_mod._ensure_role_removed(SeqGuild(None), U, 100) is True
    assert calls == [100]

    # remove_role False и роль осталась → False (продажа запрещена)
    async def fake_remove_fail(guild, uid, rid):
        return False
    monkeypatch.setattr(
        "utils.role_manager.remove_role", fake_remove_fail,
        raising=False,
    )
    seq2 = [member_with, member_with]

    class SeqGuild2(FakeGuild):
        async def fetch_member(self, uid):
            return seq2.pop(0) if seq2 else member_with

    assert await sell_mod._ensure_role_removed(SeqGuild2(None), U, 100) is False


@pytest.mark.asyncio
async def test_credit_sale_once_idempotent(pg_pool, catalog_env):
    """Потерянный ответ после COMMIT не дублирует и не теряет
    начисление продажи."""
    from cogs import sell as sell_mod
    from storage.user_balance_store import user_balance_store

    await user_balance_store.add_balance(G, U, 0)  # ensure row? нет — add
    # Прямой тест: исключение после коммита → probe тем же ключом
    import storage.user_balance_store as ubs_mod
    orig = ubs_mod.user_balance_store.add_balance_once
    state = {"failed": False}

    async def flaky(g, u, amount, key):
        res = await orig(g, u, amount, key)
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("response lost after commit")
        return res

    monkeypatch_target = ubs_mod.user_balance_store
    monkeypatch_target.add_balance_once = flaky
    try:
        # _credit_sale_once использует user_balance_store напрямую
        import cogs.sell as s
        # патчим там, где функция ищет стор
        await s._credit_sale_once(G, U, 250)
    finally:
        monkeypatch_target.add_balance_once = orig
    bal = await user_balance_store.get_balance(G, U)
    # Начислено ровно один раз (баланс мог быть инициализирован
    # дефолтом — проверяем дельту через grant-таблицу)
    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM match_coin_grants "
            "WHERE user_id=$1 AND match_id LIKE 'sell:%'",
            U,
        )
    assert n == 1


# ===================================================== P1: wardrobe recovery


@pytest.mark.asyncio
async def test_wardrobe_op_recovery(pg_pool, catalog_env):
    """kind='wardrobe': charged-операция после рестарта —
    предмет в гардеробе → done; иначе refund обеих валют."""
    from services import shop_purchase_service as svc
    from storage.shop_purchase_ops_store import purchase_ops_store
    from storage.user_balance_store import user_balance_store
    from storage.wardrobe_store import wardrobe_store
    from storage.rep_balance_store import rep_balance_store

    # Подготовить баланс
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_balance (guild_id,user_id,balance) "
            "VALUES ($1,$2,5000) ON CONFLICT DO NOTHING", G, U)
        await conn.execute(
            "INSERT INTO player_stats (guild_id,user_id,reputation) "
            "VALUES ($1,$2,50) ON CONFLICT DO NOTHING", G, U)

    # Операция 1: предмет НЕ в гардеробе → refund
    op1 = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op1, G, U, "wardrobe", "1001", 1200,
        payload={"ff_item": "1001"}, rep_amount=1,
    )
    await purchase_ops_store.charge(op1, G, U, 1200, rep_amount=1)
    # Имитируем состояние после crash: списано, гардероб пуст
    status = await svc._recover_wardrobe_op(
        await purchase_ops_store.get(op1)
    )
    assert status == "refunded"
    op = await purchase_ops_store.get(op1)
    assert op["status"] == "refunded"

    # Операция 2: предмет УЖЕ в гардеробе → done без выдачи
    wardrobe_store._data[f"{G}:{U}"] = {"1002": {"equipped": False}}
    op2 = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op2, G, U, "wardrobe", "1002", 800,
        payload={"ff_item": "1002"}, rep_amount=1,
    )
    await purchase_ops_store.charge(op2, G, U, 800, rep_amount=1)
    status = await svc._recover_wardrobe_op(
        await purchase_ops_store.get(op2)
    )
    assert status == "done"
    op = await purchase_ops_store.get(op2)
    assert op["status"] == "done"
