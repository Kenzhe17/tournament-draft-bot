"""Player statistics service."""

from __future__ import annotations

import logging
from typing import Optional

from models.player_stats import PlayerStats
from storage.db import get_pool

logger = logging.getLogger(__name__)


class PlayerStatsService:
    """Service for player statistics."""

    @staticmethod
    async def get_player_stats(user_id: int, guild_id: int) -> Optional[PlayerStats]:
        """Get player statistics."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM player_stats WHERE user_id = $1 AND guild_id = $2",
                user_id, guild_id
            )

            if not row:
                return None

            return PlayerStats(
                user_id=row["user_id"],
                guild_id=row["guild_id"],
                username=row["name"],
                elo=row["elo"],
                wins=row["wins"],
                games=row["games"],
                total_kills=row["total_kills"],
                total_deaths=row["total_deaths"],
                best_match_kills=row["best_match_kills"],
                total_elo_change=row["total_elo_change"],
                last_elo_change=row["last_elo_change"],
                xp=row["xp"],
                level=row["level"],
                xp_to_next_level=row["xp_to_next_level"],
                total_earnings=row["total_earnings"],
                tournament_participations=row["tournament_participations"],
                description=row["description"],
                avatar_url=row["avatar_url"],
                daily_tournament_coins=row["daily_tournament_coins"],
                daily_coins_date=row["daily_coins_date"],
                finals=row["finals"],
                current_streak=row["current_streak"],
                best_win_streak=row["best_win_streak"],
                best_loss_streak=row["best_loss_streak"],
            )

    @staticmethod
    async def update_player_stats(
        user_id: int,
        guild_id: int,
        kills: int = 0,
        deaths: int = 0,
        wins: int = 0,
        losses: int = 0,
        elo_change: int = 0,
    ) -> None:
        """Update player statistics."""
        stats = await PlayerStatsService.get_player_stats(user_id, guild_id)
        if not stats:
            stats = PlayerStats(
                user_id=user_id,
                guild_id=guild_id,
                username="",
            )

        # Update stats
        stats.total_kills += kills
        stats.total_deaths += deaths
        stats.total_wins += wins
        stats.total_losses += losses
        stats.elo += elo_change
        stats.games += 1
        stats.total_elo_change += elo_change
        stats.last_elo_change = elo_change

        # Save to database
        await PlayerStatsService._save_player_stats(stats)

    @staticmethod
    async def _save_player_stats(stats: PlayerStats) -> None:
        """Save player stats to database."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO player_stats (
                    user_id, guild_id, name, elo, wins, games,
                    total_kills, total_deaths, best_match_kills,
                    total_elo_change, last_elo_change, xp, level,
                    xp_to_next_level, total_earnings, tournament_participations,
                    description, avatar_url, daily_tournament_coins,
                    daily_coins_date, finals, current_streak,
                    best_win_streak, best_loss_streak
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    elo = EXCLUDED.elo,
                    wins = EXCLUDED.wins,
                    games = EXCLUDED.games,
                    total_kills = EXCLUDED.total_kills,
                    total_deaths = EXCLUDED.total_deaths,
                    best_match_kills = EXCLUDED.best_match_kills,
                    total_elo_change = EXCLUDED.total_elo_change,
                    last_elo_change = EXCLUDED.last_elo_change,
                    xp = EXCLUDED.xp,
                    level = EXCLUDED.level,
                    xp_to_next_level = EXCLUDED.xp_to_next_level,
                    total_earnings = EXCLUDED.total_earnings,
                    tournament_participations = EXCLUDED.tournament_participations,
                    description = EXCLUDED.description,
                    avatar_url = EXCLUDED.avatar_url,
                    daily_tournament_coins = EXCLUDED.daily_tournament_coins,
                    daily_coins_date = EXCLUDED.daily_coins_date,
                    finals = EXCLUDED.finals,
                    current_streak = EXCLUDED.current_streak,
                    best_win_streak = EXCLUDED.best_win_streak,
                    best_loss_streak = EXCLUDED.best_loss_streak
                """,
                stats.user_id,
                stats.guild_id,
                stats.username,
                stats.elo,
                stats.wins,
                stats.games,
                stats.total_kills,
                stats.total_deaths,
                stats.best_match_kills,
                stats.total_elo_change,
                stats.last_elo_change,
                stats.xp,
                stats.level,
                stats.xp_to_next_level,
                stats.total_earnings,
                stats.tournament_participations,
                stats.description,
                stats.avatar_url,
                stats.daily_tournament_coins,
                stats.daily_coins_date,
                stats.finals,
                stats.current_streak,
                stats.best_win_streak,
                stats.best_loss_streak,
            )

    @staticmethod
    async def get_leaderboard(guild_id: int, limit: int = 10) -> list[PlayerStats]:
        """Get leaderboard."""
        # TODO: Implement database query
        return []
