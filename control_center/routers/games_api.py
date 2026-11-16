"""API каталога мини-игр (таблица minigames, PostgreSQL).

Каталог читается ботом на каждую операцию (minigame_store) —
изменения применяются без рестарта и без NOTIFY.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from control_center import rbac
from control_center.auth import (
    PanelUser, require_perm, verify_csrf,
)
from storage.db import get_pool

router = APIRouter(prefix="/api/games", tags=["games"])


class GameBody(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=50)
    difficulty: str | None = Field(default=None, max_length=20)
    min_bet: int | None = Field(default=None, ge=0, le=10**12)
    max_bet: int | None = Field(default=None, ge=0, le=10**12)
    multiplier: float | None = Field(default=None, ge=0, le=10**6)
    is_pvp: bool | None = None
    is_pve: bool | None = None
    is_active: bool | None = None


def _ser(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in (
        "id", "command_name", "name", "description", "category",
        "difficulty", "min_bet", "max_bet", "multiplier",
        "is_pvp", "is_pve", "is_active",
    ) if k in r}


@router.get("/minigames")
async def list_games(
    include_inactive: bool = False,
    user: PanelUser = Depends(require_perm(rbac.PERM_SETTINGS_VIEW)),
) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if include_inactive:
            rows = await conn.fetch(
                "SELECT * FROM minigames ORDER BY category, name")
        else:
            rows = await conn.fetch(
                "SELECT * FROM minigames WHERE is_active = TRUE"
                " ORDER BY category, name")
    return {"games": [_ser(dict(r)) for r in rows]}


@router.put("/minigames/{game_id}")
async def update_game(
    game_id: str,
    body: GameBody,
    user: PanelUser = Depends(require_perm("games.manage")),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    updates = {k: v for k, v in body.model_dump().items()
               if v is not None}
    if not updates:
        raise HTTPException(422, "no fields to update")
    if ("min_bet" in updates and "max_bet" in updates
            and updates["min_bet"] > updates["max_bet"]):
        raise HTTPException(422, "min_bet > max_bet")

    pool = await get_pool()
    sets, args = [], []
    for k, v in updates.items():
        args.append(v)
        sets.append(f"{k} = ${len(args)}")
    args.append(game_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE minigames SET {', '.join(sets)}"
            f" WHERE id = ${len(args)} RETURNING *",
            *args,
        )
    if row is None:
        raise HTTPException(404, "game not found")
    return {"game": _ser(dict(row))}
