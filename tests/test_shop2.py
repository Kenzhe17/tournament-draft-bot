"""SHOP 2.0 — тесты сервиса покупок, каталога, шансов кейсов."""

from __future__ import annotations

import json

import pytest

from models.shop_item import (
    CosmeticRarity,
    CosmeticType,
    ShopItem,
)
from storage.shop_ops_store import ShopOpsStore
from storage.shop_store import InventoryStore, ShopStore
from storage.user_balance_store import user_balance_store

import storage.shop_store as shop_store_mod
from services import shop_purchase_service as svc


# ---------------------------------------------------------------- fixtures


class _Balance:
    """In-memory баланс с атомарной семантикой subtract."""

    def __init__(self) -> None:
        self.balances: dict[tuple[int, int], int] = {}
        self.grants: dict[str, int] = {}

    async def get_balance(self, g, u):
        return self.balances.get((g, u), 100)

    async def set(self, g, u, v):
        self.balances[(g, u)] = v

    async def subtract_balance(self, g, u, amount):
        cur = self.balances.get((g, u), 100)
        if cur < amount:
            raise ValueError("Insufficient balance")
        self.balances[(g, u)] = cur - amount

    async def add_balance(self, g, u, amount):
        self.balances[(g, u)] = self.balances.get((g, u), 100) + amount

    async def add_balance_once(self, g, u, amount, key):
        if key in self.grants:
            return self.grants[key]
        await self.add_balance(g, u, amount)
        self.grants[key] = amount
        return amount


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Изолированные JSON-хранилища + in-memory баланс."""
    shop_file = tmp_path / "shop_items.json"
    inv_file = tmp_path / "player_inventory.json"
    ops_file = tmp_path / "shop_ops.json"

    import storage.shop_ops_store as ops_mod
    import storage.shop_items as items_mod
    import storage.shop_purchase_ops_store as pops_mod
    import views.inventory2_view as inv2
    import views.shop2_view as shop2

    monkeypatch.setattr(shop_store_mod, "SHOP_FILE", shop_file)
    monkeypatch.setattr(shop_store_mod, "INVENTORY_FILE", inv_file)
    monkeypatch.setattr(ops_mod, "OPS_FILE", ops_file)
    monkeypatch.setattr(pops_mod, "OPS_JSON", tmp_path / "shop_purchase_ops.json")

    store = ShopStore()
    inv = InventoryStore()
    ops = ops_mod.ShopOpsStore()
    pops = pops_mod.ShopPurchaseOpsStore()
    pops._use_db = False  # unit-тесты без PG — JSON-фолбэк

    # Патчим ВСЕ модули, импортировавшие синглтоны по имени
    for mod, names in (
        (shop_store_mod, {"shop_store": store, "inventory_store": inv}),
        (svc, {"shop_store": store, "inventory_store": inv,
               "shop_ops_store": ops, "purchase_ops_store": pops}),
        (items_mod, {"shop_store": store}),
        (ops_mod, {"shop_ops_store": ops}),
        (pops_mod, {"purchase_ops_store": pops}),
        (shop2, {"shop_store": store, "inventory_store": inv,
                 "shop_ops_store": ops}),
        (inv2, {"shop_store": store, "inventory_store": inv,
                "shop_ops_store": ops}),
    ):
        for attr, val in names.items():
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, val)

    bal = _Balance()
    for name in (
        "get_balance", "subtract_balance", "add_balance",
        "add_balance_once",
    ):
        monkeypatch.setattr(user_balance_store, name, getattr(bal, name))

    # Unit-тесты без PG: REP-стоимость обнуляем (JSON-фолбэк
    # финансовых REP-операций запрещён — см. ops store).
    monkeypatch.setattr(svc, "rep_price_for_item", lambda item: 0)
    monkeypatch.setattr(svc, "rep_price_for_amount", lambda *a, **k: 0)

    # тестовый предмет
    item = ShopItem(
        id="tag_test", name="[TEST]", description="",
        price=500, cosmetic_type=CosmeticType.TAG,
        rarity=CosmeticRarity.BASIC, value="[TEST]",
        category="tags",
    )
    store.add_item(item)
    return {"shop": store, "inv": inv, "ops": ops, "bal": bal,
            "item": item}


# ---------------------------------------------------------------- catalog


def test_seed_is_upsert_not_wipe(env):
    """initialize_shop_items не должен стирать кастомные товары."""
    from storage.shop_items import initialize_shop_items

    custom = ShopItem(
        id="admin_custom", name="Custom", description="",
        price=1, cosmetic_type=CosmeticType.ICON,
        rarity=CosmeticRarity.BASIC, value="x", category="icons",
    )
    env["shop"].add_item(custom)
    initialize_shop_items()
    assert env["shop"].get_item("admin_custom") is not None
    assert env["shop"].get_item("tag_pro") is not None
    assert env["shop"].get_item("frame_void") is not None
    assert env["shop"].get_item("bundle_shadow") is not None


def test_color_roles_seeded_from_env(env, monkeypatch):
    monkeypatch.setenv("SHOP_COLOR_ROLES", json.dumps({
        "emerald": {"name": "Emerald", "role_id": 111,
                    "price": 2000, "color": "#57F287"}
    }))
    from storage.shop_items import _color_role_items
    items = _color_role_items()
    assert len(items) == 1
    assert items[0].item_type == "role"
    assert items[0].role_id == 111
    assert items[0].category == "color_roles"


def test_seasonal_window():
    past = ShopItem(
        id="x", name="x", description="", price=1,
        cosmetic_type=CosmeticType.ICON, rarity=CosmeticRarity.BASIC,
        value="x", category="icons",
        available_until="2000-01-01T00:00:00+00:00",
    )
    future = ShopItem(
        id="y", name="y", description="", price=1,
        cosmetic_type=CosmeticType.ICON, rarity=CosmeticRarity.BASIC,
        value="y", category="icons",
        available_from="2999-01-01T00:00:00+00:00",
    )
    active = ShopItem(
        id="z", name="z", description="", price=1,
        cosmetic_type=CosmeticType.ICON, rarity=CosmeticRarity.BASIC,
        value="z", category="icons", is_active=False,
    )
    assert not svc.item_available_now(past)
    assert not svc.item_available_now(future)
    assert not svc.item_available_now(active)


# ---------------------------------------------------------------- purchase


@pytest.mark.asyncio
async def test_purchase_success(env):
    env["bal"].balances[(1, 42)] = 1000
    res = await svc.purchase(1, 42, "tag_test")
    assert res.ok and not res.refunded
    assert env["bal"].balances[(1, 42)] == 500
    owned = {c.item_id for c in env["inv"].get_player_inventory(1, 42)}
    assert "tag_test" in owned
    ops = env["ops"].get_for_user(1, 42)
    assert ops and ops[0]["status"] == "done"


@pytest.mark.asyncio
async def test_purchase_insufficient(env):
    env["bal"].balances[(1, 42)] = 100
    res = await svc.purchase(1, 42, "tag_test")
    assert not res.ok and res.insufficient
    assert env["bal"].balances[(1, 42)] == 100


@pytest.mark.asyncio
async def test_purchase_duplicate_rejected(env):
    env["bal"].balances[(1, 42)] = 5000
    await svc.purchase(1, 42, "tag_test")
    res = await svc.purchase(1, 42, "tag_test")
    assert not res.ok
    assert env["bal"].balances[(1, 42)] == 4500  # без второго списания


@pytest.mark.asyncio
async def test_concurrent_purchases_no_double_charge(env):
    """Два одновременных purchase — только одно списание."""
    env["bal"].balances[(1, 42)] = 5000
    results = await _gather(
        svc.purchase(1, 42, "tag_test"),
        svc.purchase(1, 42, "tag_test"),
    )
    oks = [r for r in results if r.ok]
    assert len(oks) == 1
    assert env["bal"].balances[(1, 42)] == 4500


async def _gather(*aws):
    import asyncio
    return await asyncio.gather(*aws)


@pytest.mark.asyncio
async def test_bundle_purchase(env):
    env["shop"].add_item(ShopItem(
        id="b1", name="b1", description="", price=100,
        cosmetic_type=CosmeticType.TAG, rarity=CosmeticRarity.BASIC,
        value="b", category="bundles", bundle=["tag_test", "icon_x"],
    ))
    env["shop"].add_item(ShopItem(
        id="icon_x", name="ix", description="", price=50,
        cosmetic_type=CosmeticType.ICON, rarity=CosmeticRarity.BASIC,
        value="icon_paw", category="icons",
    ))
    env["bal"].balances[(1, 7)] = 1000
    res = await svc.purchase(1, 7, "b1")
    assert res.ok
    owned = {c.item_id for c in env["inv"].get_player_inventory(1, 7)}
    assert {"tag_test", "icon_x"} <= owned
    # повторная покупка набора — отказ (все компоненты есть)
    res2 = await svc.purchase(1, 7, "b1")
    assert not res2.ok


@pytest.mark.asyncio
async def test_service_item_rejected(env):
    env["shop"].add_item(ShopItem(
        id="svc1", name="svc", description="", price=10,
        cosmetic_type=CosmeticType.ICON, rarity=CosmeticRarity.BASIC,
        value="", category="services", item_type="service",
    ))
    res = await svc.purchase(1, 1, "svc1")
    assert not res.ok


@pytest.mark.asyncio
async def test_favorites_and_history(env):
    env["ops"].toggle_favorite(1, 5, "tag_test")
    assert env["ops"].is_favorite(1, 5, "tag_test")
    env["ops"].toggle_favorite(1, 5, "tag_test")
    assert not env["ops"].is_favorite(1, 5, "tag_test")

    env["ops"].record(
        guild_id=1, user_id=5, kind="purchase",
        item_id="tag_test", price=500, status="done",
    )
    hist = env["ops"].get_for_user(1, 5)
    assert len(hist) == 1 and hist[0]["price"] == 500


# ---------------------------------------------------------------- cases


def test_case_odds_from_drop_rates():
    from views.shop2_view import _case_odds, _case_rewards_preview
    from models.case import Case

    case = Case(
        id="c1", name="C1", description="", price=100,
        drop_rates={
            "item_basic": 0.10,
            "coins_100": 0.12, "coins_200": 0.10,
            "coins_300": 0.08, "coins_400": 0.06, "coins_500": 0.04,
            "nothing": 0.50,
        },
    )
    odds = _case_odds(case)
    assert abs(odds["coins"] - 40.0) < 0.01
    assert abs(odds["item"] - 10.0) < 0.01
    assert abs(odds["nothing"] - 50.0) < 0.01
    rewards = _case_rewards_preview(case)
    assert any("Монеты" in r for r in rewards)
    assert any("Ничего" in r for r in rewards)


def test_case_custom_rates_preserved(tmp_path, monkeypatch):
    """Кастомные drop_rates не должны перезаписываться при загрузке."""
    import storage.case_store as cs
    cases_file = tmp_path / "cases.json"
    monkeypatch.setattr(cs, "CASES_FILE", cases_file)
    store = cs.CaseStore()
    custom = {
        "custom": {
            "id": "custom", "name": "Custom", "description": "",
            "price": 10,
            "drop_rates": {"coins_100": 0.9, "nothing": 0.1},
            "is_active": True,
        }
    }
    cases_file.write_text(json.dumps(custom), encoding="utf-8")
    store.load()
    case = store.get_case("custom")
    assert case.drop_rates["coins_100"] == 0.9
    assert case.drop_rates["nothing"] == 0.1


# ---------------------------------------------------------------- wardrobe


def test_wardrobe_add_dedupes(tmp_path, monkeypatch):
    import storage.wardrobe_store as ws
    monkeypatch.setattr(ws, "FILE", tmp_path / "wardrobes.json")
    store = ws.WardrobeStore()
    assert store.add(1, 2, 123)
    assert not store.add(1, 2, 123)
    assert store.is_owned(1, 2, 123)
    assert not store.is_owned(1, 3, 123)


# ---------------------------------------------------------------- views


@pytest.mark.asyncio
async def test_main_view_builds(env):
    from views.shop2_view import build_main_view
    view = await build_main_view(1, 42)
    assert view is not None
    # container + text displays присутствуют
    texts = []

    def _walk(item):
        if hasattr(item, "content"):
            texts.append(item.content)
        for ch in getattr(item, "children", []) or []:
            _walk(ch)

    for child in view.children:
        _walk(child)
    joined = "\n".join(texts)
    assert "МАГАЗИН" in joined


@pytest.mark.asyncio
async def test_browse_view_filters(env):
    from views.shop2_view import build_browse_view
    view = await build_browse_view(
        1, 42, "tags", {"rarity": "basic", "owned": "no"}
    )
    assert view is not None


@pytest.mark.asyncio
async def test_inventory_view_builds(env):
    from views.inventory2_view import build_inventory_view
    env["inv"].add_cosmetic(
        __import__("models.shop_item", fromlist=["PlayerCosmetic"])
        .PlayerCosmetic(guild_id=1, user_id=42,
                        item_id="tag_test", equipped=True)
    )
    view = await build_inventory_view(1, 42)
    assert view is not None
