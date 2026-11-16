"""PostgreSQL-каталог магазина — авторитетный источник товаров.

До Control Center каталог жил в JSON (shop_items.json) и
перезаписывался initialize_shop_items() при каждом старте.
Теперь:

- схема shop_items расширяется недостающими колонками
  (cosmetic_type, value, item_type, role_id, required_level,
  source, updated_at/by) — идемпотентно;
- бот при старте загружает каталог из PG в shop_store (in-memory
  кэш для синхронных читателей); JSON остаётся fallback для
  режима без DATABASE_URL;
- панель пишет каталог только в PG и шлёт pg_notify
  'admin_config_changed' {"key": "shop.catalog"} — бот
  перечитывает каталог без рестарта;
- сидирование дефолтных товаров — INSERT ... ON CONFLICT
  DO NOTHING: панельные правки и товары, созданные через
  панель, никогда не затираются.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from storage.db import get_pool

logger = logging.getLogger(__name__)

SCHEMA = """
ALTER TABLE shop_items
    ADD COLUMN IF NOT EXISTS cosmetic_type TEXT NOT NULL DEFAULT 'icon',
    ADD COLUMN IF NOT EXISTS value TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS item_type TEXT NOT NULL DEFAULT 'cosmetic',
    ADD COLUMN IF NOT EXISTS role_id BIGINT,
    ADD COLUMN IF NOT EXISTS required_level INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS updated_by BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS available_from TEXT,
    ADD COLUMN IF NOT EXISTS available_until TEXT,
    ADD COLUMN IF NOT EXISTS bundle JSONB,
    ADD COLUMN IF NOT EXISTS rep_price INTEGER;
CREATE INDEX IF NOT EXISTS idx_shop_items_category
    ON shop_items(category);
CREATE INDEX IF NOT EXISTS idx_shop_items_active
    ON shop_items(is_active);
"""

_NOTIFY_KEY = "shop.catalog"


async def ensure_schema() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)


async def load_all() -> list[dict[str, Any]]:
    """Все товары каталога (PG → dict, совместимый с ShopItem)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT item_id, name, description, price, category, rarity,
                   is_sellable, emoji, is_active, cosmetic_type, value,
                   item_type, role_id, required_level,
                   available_from, available_until, bundle, rep_price
            FROM shop_items ORDER BY item_id
            """
        )
    out = []
    for r in rows:
        bundle = r["bundle"]
        if isinstance(bundle, str):
            try:
                bundle = json.loads(bundle)
            except Exception:
                bundle = None
        out.append({
            "id": r["item_id"],
            "name": r["name"],
            "description": r["description"] or "",
            "price": r["price"],
            "category": r["category"],
            "rarity": r["rarity"],
            "is_sellable": r["is_sellable"],
            "is_active": r["is_active"],
            "cosmetic_type": r["cosmetic_type"],
            "value": r["value"],
            "item_type": r["item_type"],
            "role_id": r["role_id"],
            "required_level": r["required_level"],
            "available_from": r["available_from"],
            "available_until": r["available_until"],
            "bundle": bundle,
            "rep_price": r["rep_price"],
        })
    return out


def _item_params(item: dict[str, Any]) -> tuple:
    """Общий набор bind-параметров для upsert/seed."""
    bundle = item.get("bundle")
    return (
        item["id"], item.get("name", ""), item.get("description", ""),
        int(item.get("price", 0)), item.get("category", ""),
        item.get("rarity", "basic"),
        bool(item.get("is_sellable", True)),
        bool(item.get("is_active", True)),
        item.get("cosmetic_type", "icon"), item.get("value", ""),
        item.get("item_type", "cosmetic"),
        item.get("role_id"), int(item.get("required_level", 0)),
        item.get("available_from"), item.get("available_until"),
        json.dumps(bundle) if bundle is not None else None,
        item.get("rep_price"),
    )


_UPSERT_SQL = """
    INSERT INTO shop_items
        (item_id, name, description, price, category, rarity,
         is_sellable, is_active, cosmetic_type, value,
         item_type, role_id, required_level,
         available_from, available_until, bundle, rep_price,
         source, updated_by, updated_at)
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,now())
    ON CONFLICT (item_id) DO UPDATE SET
        name = EXCLUDED.name,
        description = EXCLUDED.description,
        price = EXCLUDED.price,
        category = EXCLUDED.category,
        rarity = EXCLUDED.rarity,
        is_sellable = EXCLUDED.is_sellable,
        is_active = EXCLUDED.is_active,
        cosmetic_type = EXCLUDED.cosmetic_type,
        value = EXCLUDED.value,
        item_type = EXCLUDED.item_type,
        role_id = EXCLUDED.role_id,
        required_level = EXCLUDED.required_level,
        available_from = EXCLUDED.available_from,
        available_until = EXCLUDED.available_until,
        bundle = EXCLUDED.bundle,
        rep_price = EXCLUDED.rep_price,
        source = EXCLUDED.source,
        updated_by = EXCLUDED.updated_by,
        updated_at = now()
"""


async def upsert_item(item: dict[str, Any], *, actor_id: int = 0,
                      source: str = "panel") -> None:
    """Создать/обновить товар. Не трогает чужие поля."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
        await conn.execute(
            _UPSERT_SQL,
            *_item_params(item), source, actor_id,
        )


async def seed_defaults(items: list[dict[str, Any]]) -> int:
    """Идемпотентный сидинг: вставляет только отсутствующие id.

    Существующие строки (в т.ч. отредактированные панелью)
    не перезаписываются.
    """
    pool = await get_pool()
    inserted = 0
    async with pool.acquire() as conn:
        for it in items:
            result = await conn.execute(
                """
                INSERT INTO shop_items
                    (item_id, name, description, price, category, rarity,
                     is_sellable, is_active, cosmetic_type, value,
                     item_type, role_id, required_level,
                     available_from, available_until, bundle, rep_price,
                     source, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
                        'default', now())
                ON CONFLICT (item_id) DO NOTHING
                """,
                *_item_params(it),
            )
            # INSERT 0 1 / INSERT 0 0
            if result.endswith("1"):
                inserted += 1
    return inserted


async def save_item_confirmed(
    item, *, actor_id: int = 0, source: str = "discord_admin"
) -> bool:
    """Сохранить товар в PG с подтверждением результата.

    Для критических записей из Discord-админки: возвращает True
    только если upsert реально выполнен в PostgreSQL. При ошибке
    — False, вызывающий код обязан НЕ показывать успех и НЕ
    считать изменение сохранённым.
    """
    try:
        await upsert_item(item.to_dict(), actor_id=actor_id,
                          source=source)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("shop catalog save_item failed id=%s: %s",
                     getattr(item, "id", "?"), exc)
        return False


async def notify_catalog_changed() -> None:
    """Уведомить бота о смене каталога (через admin_config канал)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT pg_notify('admin_config_changed', $1)",
            json.dumps({"guild_id": 0, "key": _NOTIFY_KEY,
                        "version": 0}),
        )


async def sync_to_store(shop_store) -> str:
    """Синхронизировать in-memory shop_store с PG.

    Возвращает 'pg' | 'json_migrated' | 'seeded' | 'empty'.
    - PG непуст → PG авторитетен, перезаписываем кэш.
    - PG пуст, JSON непуст → мигрируем JSON → PG (один раз).
    - Оба пусты → сидим дефолты.
    """
    from models.shop_item import ShopItem

    await ensure_schema()
    rows = await load_all()

    # Восстановление ассортимента: дефолтные id, отсутствующие в PG,
    # досеиваются при КАЖДОЙ синхронизации. ON CONFLICT DO NOTHING —
    # существующие строки (в т.ч. скрытые is_active=false и
    # отредактированные панелью) не перезаписываются и не
    # реактивируются.
    from storage.shop_items import default_shop_items
    defaults = [it.to_dict() for it in default_shop_items()]
    have = {r["id"] for r in rows}
    missing = [d for d in defaults if d["id"] not in have]
    if missing:
        try:
            n = await seed_defaults(missing)
            if n:
                logger.info(
                    "shop catalog: restored %d missing default items", n
                )
                rows = await load_all()
        except Exception as exc:  # noqa: BLE001 — не роняем синк
            logger.warning("shop catalog: default reseed failed: %s", exc)

    if rows:
        items = []
        for r in rows:
            try:
                items.append(ShopItem.from_dict(r))
            except Exception as exc:  # noqa: BLE001 — битая строка не роняет синк
                logger.warning("shop catalog: bad row %s: %s",
                               r.get("id"), exc)
        shop_store.replace_all(items)
        return "pg"

    # PG пуст — проверить JSON-кэш/файл
    existing = shop_store.get_all_items()
    if existing:
        payload = [it.to_dict() | {"is_active": True} for it in existing]
        # to_dict не содержит is_active — добавляем вручную
        for it, p in zip(existing, payload):
            p["is_active"] = getattr(it, "is_active", True)
        await seed_defaults(payload)
        rows = await load_all()
        shop_store.replace_all(
            [ShopItem.from_dict(r) for r in rows]
        )
        return "json_migrated"

    # Всё пусто — сидим дефолты из shop_items.py
    from storage.shop_items import default_shop_items
    payload = [it.to_dict() for it in default_shop_items()]
    for it, p in zip(default_shop_items(), payload):
        p["is_active"] = True
    n = await seed_defaults(payload)
    rows = await load_all()
    shop_store.replace_all(
        [ShopItem.from_dict(r) for r in rows]
    )
    logger.info("shop catalog seeded: %d new items", n)
    return "seeded"
