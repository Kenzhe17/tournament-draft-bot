"""Betting statistics store for tracking user betting performance."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class BettingStatsStore:
    """Store for managing betting statistics."""

    def __init__(self) -> None:
        self._use_db = False

    def enable_db(self) -> None:
        """Enable database storage."""
        self._use_db = True

    async def record_bet_result(self, guild_id: int, user_id: int, amount: int, won: bool) -> None:
        """Record a bet result for statistics."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                if won:
                    await conn.execute(
                        """
                        INSERT INTO betting_stats (guild_id, user_id, total_won, total_bets, successful_bets, best_win)
                        VALUES ($1, $2, $3, 1, 1, $3)
                        ON CONFLICT (guild_id, user_id)
                        DO UPDATE SET
                            total_won = betting_stats.total_won + $3,
                            total_bets = betting_stats.total_bets + 1,
                            successful_bets = betting_stats.successful_bets + 1,
                            best_win = CASE WHEN $3 > betting_stats.best_win THEN $3 ELSE betting_stats.best_win END
                        """,
                        guild_id, user_id, amount
                    )
                else:
                    await conn.execute(
                        """
                        INSERT INTO betting_stats (guild_id, user_id, total_lost, total_bets, worst_loss)
                        VALUES ($1, $2, $3, 1, $3)
                        ON CONFLICT (guild_id, user_id)
                        DO UPDATE SET
                            total_lost = betting_stats.total_lost + $3,
                            total_bets = betting_stats.total_bets + 1,
                            worst_loss = CASE WHEN $3 > betting_stats.worst_loss THEN $3 ELSE betting_stats.worst_loss END
                        """,
                        guild_id, user_id, amount
                    )

    async def get_user_stats(self, guild_id: int, user_id: int) -> dict:
        """Get betting statistics for a user."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT total_won, total_lost, total_bets, successful_bets, best_win, worst_loss
                    FROM betting_stats
                    WHERE guild_id = $1 AND user_id = $2
                    """,
                    guild_id, user_id
                )
                if row:
                    return {
                        "total_won": row["total_won"],
                        "total_lost": row["total_lost"],
                        "total_bets": row["total_bets"],
                        "successful_bets": row["successful_bets"],
                        "best_win": row["best_win"],
                        "worst_loss": row["worst_loss"],
                        "success_rate": (row["successful_bets"] / row["total_bets"] * 100) if row["total_bets"] > 0 else 0
                    }
                else:
                    return {
                        "total_won": 0,
                        "total_lost": 0,
                        "total_bets": 0,
                        "successful_bets": 0,
                        "best_win": 0,
                        "worst_loss": 0,
                        "success_rate": 0
                    }
        else:
            return {
                "total_won": 0,
                "total_lost": 0,
                "total_bets": 0,
                "successful_bets": 0,
                "best_win": 0,
                "worst_loss": 0,
                "success_rate": 0
            }

    async def get_leaderboard(self, guild_id: int, page: int = 1, per_page: int = 10) -> list[dict]:
        """Get betting leaderboard sorted by total profit."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                offset = (page - 1) * per_page
                rows = await conn.fetch(
                    """
                    SELECT user_id, total_won, total_lost, total_bets, successful_bets, best_win, worst_loss
                    FROM betting_stats
                    WHERE guild_id = $1 AND total_bets > 0
                    ORDER BY (total_won - total_lost) DESC
                    LIMIT $2 OFFSET $3
                    """,
                    guild_id, per_page, offset
                )
                return [
                    {
                        "user_id": row["user_id"],
                        "total_won": row["total_won"],
                        "total_lost": row["total_lost"],
                        "total_bets": row["total_bets"],
                        "successful_bets": row["successful_bets"],
                        "best_win": row["best_win"],
                        "worst_loss": row["worst_loss"],
                        "profit": row["total_won"] - row["total_lost"],
                        "success_rate": (row["successful_bets"] / row["total_bets"] * 100) if row["total_bets"] > 0 else 0
                    }
                    for row in rows
                ]
        else:
            return []


betting_stats_store = BettingStatsStore()
