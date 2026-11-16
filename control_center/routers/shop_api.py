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
    PanelUser, require_perm, verify_csrf,
)
from storage import shop_catalog

router = APIRouter(prefix="/api/shop", tags=["shop"])

MANAGE = rbac.PERM_SETTINGS_MANAGE

CATEGORIES = {"icons", "tags", "services", "roles", "access", "cases"}
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


def _ser(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"], "name": r["name"],
        "description": r["description"], "price": r["price"],
        "category": r["category"], "rarity": r["rarity"],
        "is_sellable": r["is_sellable"], "is_active": r["is_active"],
        "cosmetic_type": r["cosmetic_type"], "value": r["value"],
        "item_type": r["item_type"], "role_id": r["role_id"],
        "required_level": r["required_level"],
    }


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
    user: PanelUser = Depends(require_perm("shop.manage")),
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
    user: PanelUser = Depends(require_perm("shop.manage")),
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
    user: PanelUser = Depends(require_perm("shop.manage")),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    return await _set_active(item_id, False, user)


@router.post("/items/{item_id}/show")
async def show_item(
    item_id: str,
    user: PanelUser = Depends(require_perm("shop.manage")),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    return await _set_active(item_id, True, user)


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: str,
    user: PanelUser = Depends(require_perm("shop.manage")),
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
