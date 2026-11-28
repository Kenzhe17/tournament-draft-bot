# ruff: noqa: B008
"""Players API: поиск и карточка игрока из player_stats (PG)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from control_center import rbac
from control_center.auth import PanelUser, require_perm
from storage.db import get_pool

router = APIRouter(prefix="/api/players", tags=["players"])

_COLS = (
    "user_id, name, elo, wins, finals, games, current_streak, "
    "best_win_streak, total_kills, total_deaths, level, xp, "
    "total_earnings, reputation, tournament_participations"
)


def _ser(row: Any) -> dict[str, Any]:
    d = dict(row)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


@router.get("/search")
async def search_players(
    q: str = Query(default="", max_length=64),
    limit: int = Query(default=20, ge=1, le=100),
    user: PanelUser = Depends(require_perm(rbac.PERM_PANEL_VIEW)),
) -> dict[str, Any]:
    """Поиск по нику (ILIKE) или Discord user_id (точное)."""
    gid = user.guild_id
    if gid is None:
        raise HTTPException(400, "guild context required")
    pool = await get_pool()
    async with pool.acquire() as conn:
        if q.isdigit():
            rows = await conn.fetch(
                f"SELECT {_COLS} FROM player_stats "
                "WHERE guild_id = $1 AND user_id = $2",
                gid, int(q),
            )
        else:
            rows = await conn.fetch(
                f"SELECT {_COLS} FROM player_stats "
                "WHERE guild_id = $1 AND name ILIKE '%' || $2 || '%' "
                "ORDER BY elo DESC NULLS LAST LIMIT $3",
                gid, q, limit,
            )
    return {"items": [_ser(r) for r in rows], "count": len(rows)}


@router.get("/leaderboard")
async def leaderboard(
    limit: int = Query(default=20, ge=1, le=100),
    user: PanelUser = Depends(require_perm(rbac.PERM_PANEL_VIEW)),
) -> dict[str, Any]:
    gid = user.guild_id
    if gid is None:
        raise HTTPException(400, "guild context required")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_COLS} FROM player_stats "
            "WHERE guild_id = $1 AND games > 0 "
            "ORDER BY elo DESC NULLS LAST LIMIT $2",
            gid, limit,
        )
    return {"items": [_ser(r) for r in rows], "count": len(rows)}


@router.get("/{user_id}")
async def player_detail(
    user_id: int,
    user: PanelUser = Depends(require_perm(rbac.PERM_PANEL_VIEW)),
) -> dict[str, Any]:
    gid = user.guild_id
    if gid is None:
        raise HTTPException(400, "guild context required")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLS}, description, birthday, spouse_id, "
            "total_elo_change, last_elo_change, xp_to_next_level "
            "FROM player_stats WHERE guild_id = $1 AND user_id = $2",
            gid, user_id,
        )
        if not row:
            raise HTTPException(404, "player not found")
        recent = await conn.fetch(
            """
            SELECT match_id, played_at, elo_change, result, opponent_elo
            FROM player_elo_history
            WHERE guild_id = $1 AND user_id = $2
            ORDER BY played_at DESC LIMIT 20
            """,
            gid, user_id,
        ) if await _has_table(conn, "player_elo_history") else []
    return {"player": _ser(row), "elo_history": [_ser(r) for r in recent]}


async def _has_table(conn: Any, name: str) -> bool:
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_name = $1)", name))
