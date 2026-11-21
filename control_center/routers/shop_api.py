"""API каталога магазина: CRUD товаров в PostgreSQL.

Панель — единственный редактор каталога. Записи идут в
shop_items (PG), после каждой мутации шлётся pg_notify —
бот перечитывает каталог без рестарта. Инвентари игроков
(user_inventory / player_inventory.json) не затрагиваются:
скрытие товара (is_active=false) не удаляет покупки.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from control_center import rbac
from control_center.auth import (
    PanelUser, current_user, require_perm, verify_csrf,
)
from storage import shop_catalog

router = APIRouter(prefix="/api/shop", tags=["shop"])

MANAGE = rbac.PERM_SETTINGS_MANAGE

# Категории — фактическая модель SHOP 2.0 (shop_items.category).
# pets/cases — не товары каталога (свои подсистемы), access —
# не существует в модели.
CATEGORIES = {
    "icons", "tags", "frames", "bundles", "color_roles",
    "services", "roles",
}
RARITIES = {"basic", "premium", "elite", "special"}
ITEM_TYPES = {"cosmetic", "role", "service"}
COSMETIC_TYPES = {"color", "icon", "tag", "frame"}


class ItemBody(BaseModel):
    id: str | None = Field(default=None, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    price: int = Field(ge=0, le=10**12)
    category: str = Field(min_length=1, max_length=50)
    rarity: str = "basic"
    is_sellable: bool = True
    is_active: bool = True
    cosmetic_type: str = "icon"
    value: str = Field(default="", max_length=500)
    item_type: str = "cosmetic"
    role_id: int | None = None
    required_level: int = Field(default=0, ge=0, le=10000)
    available_from: str | None = Field(default=None, max_length=40)
    available_until: str | None = Field(default=None, max_length=40)
    bundle: list[str] | None = None
    rep_price: int | None = Field(default=None, ge=0, le=999)


def _validate(body: ItemBody) -> None:
    if body.category not in CATEGORIES:
        raise HTTPException(422, f"unknown category: {body.category}")
    if body.rarity not in RARITIES:
        raise HTTPException(422, f"unknown rarity: {body.rarity}")
    if body.item_type not in ITEM_TYPES:
        raise HTTPException(422, f"unknown item_type: {body.item_type}")
    if body.cosmetic_type not in COSMETIC_TYPES:
        raise HTTPException(422, f"unknown cosmetic_type: {body.cosmetic_type}")
    if body.item_type == "role" and not body.role_id:
        raise HTTPException(422, "role items require role_id")
    # Двойная оплата обязательна: платный товар не может иметь
    # rep_price=0 (обход REP-требования).
    if body.price > 0 and body.rep_price == 0:
        raise HTTPException(
            422, "paid items cannot have rep_price=0"
        )
    if body.bundle is not None:
        if not body.bundle or len(body.bundle) > 50:
            raise HTTPException(422, "bundle must contain 1..50 ids")
        if any(not isinstance(c, str) or not c for c in body.bundle):
            raise HTTPException(422, "bundle ids must be non-empty strings")


def _ser(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"], "name": r["name"],
        "description": r["description"], "price": r["price"],
        "category": r["category"], "rarity": r["rarity"],
        "is_sellable": r["is_sellable"], "is_active": r["is_active"],
        "cosmetic_type": r["cosmetic_type"], "value": r["value"],
        "item_type": r["item_type"], "role_id": r["role_id"],
        "required_level": r["required_level"],
        "available_from": r.get("available_from"),
        "available_until": r.get("available_until"),
        "bundle": r.get("bundle"),
        "rep_price": r.get("rep_price"),
    }


async def require_shop_owner(user: PanelUser = Depends(current_user)
                             ) -> PanelUser:
    """Управление каталогом — ТОЛЬКО BOT_OWNER_ID.

    Discord Administrator, ADMIN_USER_IDS, организаторы и
    модераторы (роли admin/moderator/organizer панели) прав
    не имеют — проверяем id напрямую, а не через perm-бандлы.
    """
    try:
        from config import BOT_OWNER_ID, GUILD_ID
        from control_center.config import PANEL_GUILD_ID
    except Exception:  # noqa: BLE001 — config недоступен: deny
        raise HTTPException(
            status_code=403,
            detail="shop catalog management is restricted to bot owner",
        )
    if int(BOT_OWNER_ID or 0) != user.id:
        raise HTTPException(status_code=403, detail="bot owner only")
    expected = PANEL_GUILD_ID or int(GUILD_ID or 0)
    if expected and user.guild_id and user.guild_id != expected:
        raise HTTPException(status_code=403, detail="wrong guild")
    return user


@router.get("/items")
async def list_items(
    include_inactive: bool = False,
    user: PanelUser = Depends(require_perm(rbac.PERM_SETTINGS_VIEW)),
) -> dict[str, Any]:
    rows = await shop_catalog.load_all()
    items = [_ser(r) for r in rows
             if include_inactive or r["is_active"]]
    return {"items": items, "total": len(items)}


@router.post("/items")
async def create_item(
    body: ItemBody,
    user: PanelUser = Depends(require_shop_owner),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    _validate(body)
    if not body.id:
        raise HTTPException(422, "id is required")
    item_id = body.id.strip().lower().replace(" ", "_")
    existing = await shop_catalog.load_all()
    if any(r["id"] == item_id for r in existing):
        raise HTTPException(409, f"item {item_id} already exists")
    payload = body.model_dump() | {"id": item_id}
    await shop_catalog.upsert_item(payload, actor_id=user.id)
    await shop_catalog.notify_catalog_changed()
    return {"id": item_id, "status": "created"}


@router.put("/items/{item_id}")
async def update_item(
    item_id: str,
    body: ItemBody,
    user: PanelUser = Depends(require_shop_owner),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    _validate(body)
    existing = {r["id"]: r for r in await shop_catalog.load_all()}
    if item_id not in existing:
        raise HTTPException(404, "item not found")
    payload = body.model_dump() | {"id": item_id}
    await shop_catalog.upsert_item(payload, actor_id=user.id)
    await shop_catalog.notify_catalog_changed()
    return {"id": item_id, "status": "updated"}


@router.post("/items/{item_id}/hide")
async def hide_item(
    item_id: str,
    user: PanelUser = Depends(require_shop_owner),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    return await _set_active(item_id, False, user)


@router.post("/items/{item_id}/show")
async def show_item(
    item_id: str,
    user: PanelUser = Depends(require_shop_owner),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    return await _set_active(item_id, True, user)


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: str,
    user: PanelUser = Depends(require_shop_owner),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    """Архивирование: помечаем is_active=false, запись сохраняется.

    Полное удаление запрещено — на item_id могут ссылаться
    покупки в инвентарях.
    """
    return await _set_active(item_id, False, user, archived=True)


async def _set_active(item_id: str, active: bool,
                      user: PanelUser, *, archived: bool = False
                      ) -> dict[str, Any]:
    rows = {r["id"]: r for r in await shop_catalog.load_all()}
    if item_id not in rows:
        raise HTTPException(404, "item not found")
    payload = _ser(rows[item_id]) | {"is_active": active}
    await shop_catalog.upsert_item(payload, actor_id=user.id,
                                   source="panel")
    await shop_catalog.notify_catalog_changed()
    return {"id": item_id,
            "status": "archived" if archived else
                      ("shown" if active else "hidden")}
