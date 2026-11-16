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
import shutil
import time
from pathlib import Path
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
                   item_type, role_id, required_level, source,
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
            except Exception:  # noqa: BLE001 — битый jsonb → None
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
            "source": r["source"],
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


_META_DDL = """
CREATE TABLE IF NOT EXISTS shop_catalog_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_META_KEY_MIGRATION = "json_migration_done"

# Поля, которые старая миграция JSON→PG могла потерять.
# Цены, видимость, названия и прочие пользовательские настройки
# миграцией НЕ трогаются.
_META_FIELDS = ("available_from", "available_until", "bundle", "rep_price")

# Источники, означающие ручные правки каталога. Если такие строки
# уже есть в PG — JSON считается устаревшим снимком и НЕ
# используется для воскрешения удалённых товаров.
_HUMAN_SOURCES = ("panel", "discord_admin")


async def _meta_get(conn, key: str) -> str | None:
    await conn.execute(_META_DDL)
    row = await conn.fetchrow(
        "SELECT value FROM shop_catalog_meta WHERE key=$1", key
    )
    return row["value"] if row else None


async def _meta_set(conn, key: str, value: str) -> None:
    await conn.execute(_META_DDL)
    await conn.execute(
        """
        INSERT INTO shop_catalog_meta (key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value, updated_at = now()
        """,
        key, value,
    )


async def json_migration_done() -> bool:
    """Была ли уже выполнена миграция JSON→PG."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await _meta_get(conn, _META_KEY_MIGRATION) is not None
    except Exception:  # noqa: BLE001
        return False


async def _read_json_catalog() -> tuple[list[dict[str, Any]], int]:
    """Прочитать data/shop_items.json напрямую (включая скрытые).

    Возвращает (валидные item-дикты, число отброшенных битых записей).
    """
    from models.shop_item import ShopItem
    from storage.shop_store import SHOP_FILE

    if not SHOP_FILE.exists():
        return [], 0
    try:
        raw = json.loads(SHOP_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("shop catalog: cannot read JSON %s: %s",
                     SHOP_FILE, exc)
        return [], 0
    if not isinstance(raw, dict):
        logger.error("shop catalog: JSON root is not an object")
        return [], 0
    items: list[dict[str, Any]] = []
    bad = 0
    for item_id, data in raw.items():
        try:
            if not isinstance(data, dict):
                raise TypeError("not a dict")
            data = dict(data)
            data.setdefault("id", item_id)
            item = ShopItem.from_dict(data)
            if not item.id:
                raise ValueError("empty id")
            items.append(item.to_dict())
        except Exception as exc:  # noqa: BLE001 — битая запись не роняет миграцию
            bad += 1
            logger.warning("shop catalog: bad JSON item %s: %s",
                           item_id, exc)
    return items, bad


def _backup_json_catalog() -> Path | None:
    """Резервная копия исходного JSON перед первой миграцией.

    Файл не перезаписывается повторными миграциями — первый
    снимок production-каталога сохраняется навсегда.
    """
    from storage.shop_store import SHOP_FILE

    if not SHOP_FILE.exists():
        return None
    backup = SHOP_FILE.with_name(
        f"{SHOP_FILE.stem}.pre-pg-migration{SHOP_FILE.suffix}"
    )
    try:
        if not backup.exists():
            shutil.copy2(SHOP_FILE, backup)
            logger.info("shop catalog: JSON backup → %s", backup)
        return backup
    except OSError as exc:
        logger.error("shop catalog: JSON backup failed: %s", exc)
        return None


async def seed_defaults(
    items: list[dict[str, Any]], *, source: str = "default"
) -> int:
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
                        $18, now())
                ON CONFLICT (item_id) DO NOTHING
                """,
                *_item_params(it), source,
            )
            # INSERT 0 1 / INSERT 0 0
            if result.endswith("1"):
                inserted += 1
    return inserted


def _meta_equal(field: str, a: Any, b: Any) -> bool:
    if field == "bundle":
        return sorted(a or []) == sorted(b or [])
    return a == b


async def migrate_item_metadata(
    rows: list[dict[str, Any]],
    extra_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Идемпотентно восстановить метаданные существующих строк.

    Для каждого товара и каждого поля из _META_FIELDS:
    - PG-значение задано → сохраняется; расхождение с источником
      фиксируется как конфликт (PG считается авторитетным).
    - PG NULL → восстанавливается из источника, если все
      источники согласны; расхождение источников → конфликт.
    - Все NULL → пропуск.

    Никогда не трогает цену, видимость, название и прочие
    пользовательские поля. Повторный запуск — no-op.

    Возвращает отчёт {restored, skipped, conflicts}.
    """
    report: dict[str, Any] = {
        "restored": [], "skipped": [], "conflicts": [],
    }
    if not rows:
        return report

    # Источники: JSON-каталог (production-данные) важнее дефолтов.
    sources: list[dict[str, dict[str, Any]]] = []
    if extra_sources:
        sources.append({it["id"]: it for it in extra_sources})
    try:
        from storage.shop_items import default_shop_items
        sources.append(
            {it.id: it.to_dict() for it in default_shop_items()}
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("shop catalog: defaults source unavailable: %s", exc)

    updates: list[tuple[str, str, Any]] = []  # (item_id, field, value)
    for row in rows:
        item_id = row.get("id")
        if not item_id:
            continue
        srcs = [s[item_id] for s in sources if item_id in s]
        for field in _META_FIELDS:
            pg_val = row.get(field)
            src_vals = [
                s.get(field) for s in srcs
                if s.get(field) is not None
            ]
            if pg_val is not None:
                # Конфликт: PG задан, источник отличается.
                for sv in src_vals:
                    if not _meta_equal(field, pg_val, sv):
                        report["conflicts"].append({
                            "item_id": item_id, "field": field,
                            "pg": pg_val, "source": sv,
                            "resolution": "kept_pg",
                        })
                        break
                continue
            if not src_vals:
                report["skipped"].append(
                    {"item_id": item_id, "field": field,
                     "reason": "all_null"}
                )
                continue
            distinct = []
            for sv in src_vals:
                if not any(_meta_equal(field, sv, d) for d in distinct):
                    distinct.append(sv)
            if len(distinct) > 1:
                report["conflicts"].append({
                    "item_id": item_id, "field": field,
                    "sources": distinct,
                    "resolution": "skipped_ambiguous",
                })
                continue
            updates.append((item_id, field, distinct[0]))
            report["restored"].append(
                {"item_id": item_id, "field": field,
                 "value": distinct[0]}
            )

    if updates:
        pool = await get_pool()
        async with pool.acquire() as conn, conn.transaction():
            for item_id, field, value in updates:
                if field == "bundle":
                    await conn.execute(
                        "UPDATE shop_items SET bundle=$2::jsonb, "
                        "updated_at=now() WHERE item_id=$1",
                        item_id, json.dumps(value),
                    )
                else:
                    await conn.execute(
                        f"UPDATE shop_items SET {field}=$2, "
                        "updated_at=now() WHERE item_id=$1",
                        item_id, value,
                    )
    if report["restored"] or report["conflicts"]:
        logger.info(
            "shop catalog metadata migration: restored=%d skipped=%d "
            "conflicts=%d",
            len(report["restored"]), len(report["skipped"]),
            len(report["conflicts"]),
        )
        for c in report["conflicts"][:20]:
            logger.warning("shop catalog metadata conflict: %s", c)
    return report


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


async def _maybe_migrate_json(rows: list[dict[str, Any]]) -> str | None:
    """Миграция JSON → PG. Возвращает статус или None, если не нужна.

    Безопасный порядок:
    1. Маркер json_migration_done уже стоит → JSON не трогаем.
    2. В PG есть ручные правки (source panel/discord_admin) →
       JSON считается устаревшим, импорт пропускаем, id логируем.
    3. Бэкап JSON → импорт всех валидных записей
       (ON CONFLICT DO NOTHING — существующее не затирается,
       в т.ч. скрытые товары и пользовательские цены).
    4. Только после полного импорта — маркер. Аварийное
       прерывание оставляет маркер снятым → миграция
       докатывается при следующем старте.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await _meta_get(conn, _META_KEY_MIGRATION) is not None:
            return None
        human = [
            r["id"] for r in rows
            if (r.get("source") or "") in _HUMAN_SOURCES
        ]
        if human:
            logger.warning(
                "shop catalog: JSON migration skipped — PG уже "
                "содержит ручные правки (%d строк, sources=%s)",
                len(human), sorted({r.get("source") for r in rows}),
            )
            await _meta_set(conn, _META_KEY_MIGRATION,
                            f"skipped:human_edits:{int(time.time())}")
            return "json_skipped"

    json_items, bad = await _read_json_catalog()
    if not json_items:
        logger.info("shop catalog: JSON empty/absent — nothing to migrate")
        async with pool.acquire() as conn:
            await _meta_set(conn, _META_KEY_MIGRATION,
                            f"empty:{int(time.time())}")
        return "json_empty"

    backup = _backup_json_catalog()
    if backup is None:
        # Нет подтверждённой резервной копии — импорт запрещён.
        logger.error(
            "shop catalog: JSON migration aborted — backup failed"
        )
        return "json_backup_failed"

    n = await seed_defaults(json_items, source="json")
    logger.info(
        "shop catalog: JSON→PG imported %d items (%d skipped as "
        "existing, %d invalid dropped), backup=%s",
        n, len(json_items) - n, bad, backup,
    )
    async with pool.acquire() as conn:
        await _meta_set(
            conn, _META_KEY_MIGRATION,
            f"done:{n}:{int(time.time())}",
        )
    return "json_migrated"


async def sync_to_store(shop_store) -> str:
    """Синхронизировать in-memory shop_store с PG.

    Возвращает 'pg' | 'json_migrated' | 'json_skipped' |
    'json_empty' | 'seeded' | 'empty'.

    Порядок:
    - PG авторитетен; JSON→PG миграция выполняется только по
      маркеру и только до появления ручных правок.
    - Метаданные существующих строк (bundle/rep_price/сезон)
      восстанавливаются идемпотентно из JSON+дефолтов.
    - Дефолтные id, отсутствующие в PG, досеиваются —
      скрытые и отредактированные строки не трогаются.
    """
    from models.shop_item import ShopItem

    await ensure_schema()
    rows = await load_all()

    # 1. Миграция JSON→PG (только если ещё не выполнена и в PG
    #    нет ручных правок). Идёт ДО досева дефолтов, чтобы
    #    пользовательские товары не потерялись.
    mig = None
    try:
        # Проверяем маркер даже при непустом PG — миграция могла
        # быть прервана на середине (частичный импорт).
        mig = await _maybe_migrate_json(rows)
    except Exception as exc:  # noqa: BLE001 — не роняем синк
        logger.error("shop catalog: JSON migration error: %s", exc)
    if mig in ("json_migrated", "json_skipped", "json_empty"):
        rows = await load_all()

    # 2. Восстановление метаданных существующих строк
    #    (bundle/rep_price/available_*), потерянных старой
    #    миграцией. Идемпотентно, конфликты — в отчёт.
    if rows:
        try:
            json_items, _ = await _read_json_catalog()
            report = await migrate_item_metadata(rows, json_items)
            if report["restored"]:
                rows = await load_all()
        except Exception as exc:  # noqa: BLE001 — не роняем синк
            logger.error("shop catalog: metadata migration error: %s",
                         exc)

    # 3. Досев дефолтных id, отсутствующих в PG.
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
        # json_skipped/json_empty — миграция не выполнялась,
        # каталог полностью из PG → обычный 'pg'.
        return "json_migrated" if mig == "json_migrated" else "pg"

    # PG пуст и JSON пуст/не смигрирован — сидим дефолты.
    payload = defaults
    for it, p in zip(default_shop_items(), payload):
        p["is_active"] = True
    n = await seed_defaults(payload)
    rows = await load_all()
    shop_store.replace_all(
        [ShopItem.from_dict(r) for r in rows]
    )
    logger.info("shop catalog seeded: %d new items", n)
    return "seeded"
