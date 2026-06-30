"""Хранилище статистики игроков."""

import json
from pathlib import Path
from typing import Any

from models.player_stats import PlayerStats

DATA_DIR = Path("data")
STATS_FILE = DATA_DIR / "player_stats.json"


class PlayerStatsStore:
    """Хранилище статистики игроков в PostgreSQL."""

    def __init__(self) -> None:
        self._stats: dict[str, PlayerStats] = {}
        self._use_db = False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить статистику из файла (fallback)."""
        if not STATS_FILE.exists():
            return

        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for key, stats_data in data.items():
                    # Migration: if key doesn't contain ":", it's old format (no guild_id)
                    if ":" not in key:
                        # Migrate old format to new format with guild_id=0
                        # This preserves old data but marks it as from unknown guild
                        stats_data["guild_id"] = 0
                        new_key = f"0:{key}"
                        self._stats[new_key] = PlayerStats.from_dict(stats_data)
                    else:
                        self._stats[key] = PlayerStats.from_dict(stats_data)
        except (json.JSONDecodeError, KeyError):
            # Если файл повреждён, начинаем с пустого состояния
            self._stats = {}

    def save(self) -> None:
        """Сохранить статистику в файл (fallback)."""
        data = {name: stats.to_dict() for name, stats in self._stats.items()}
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def get(self, guild_id: int, user_id: int) -> PlayerStats | None:
        """Получить статистику игрока."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT guild_id, user_id, name, elo, wins, finals, games, current_streak, best_win_streak, best_loss_streak, total_kills, total_deaths, best_match_kills, total_elo_change, last_elo_change FROM player_stats WHERE guild_id = $1 AND user_id = $2",
                    guild_id, user_id
                )
                if row:
                    return PlayerStats(guild_id=row["guild_id"], user_id=row["user_id"], name=row["name"], elo=row["elo"], wins=row["wins"], finals=row["finals"], games=row["games"], current_streak=row["current_streak"], best_win_streak=row["best_win_streak"], best_loss_streak=row["best_loss_streak"], total_kills=row.get("total_kills", 0), total_deaths=row.get("total_deaths", 0), best_match_kills=row.get("best_match_kills", 0), total_elo_change=row.get("total_elo_change", 0), last_elo_change=row.get("last_elo_change", 0))
                return None
        else:
            key = f"{guild_id}:{user_id}"
            return self._stats.get(key)

    async def get_all(self, guild_id: int) -> list[PlayerStats]:
        """Получить статистику всех игроков сервера."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT guild_id, user_id, name, elo, wins, finals, games, current_streak, best_win_streak, best_loss_streak, total_kills, total_deaths, best_match_kills, total_elo_change, last_elo_change FROM player_stats WHERE guild_id = $1",
                    guild_id
                )
                return [PlayerStats(guild_id=row["guild_id"], user_id=row["user_id"], name=row["name"], elo=row["elo"], wins=row["wins"], finals=row["finals"], games=row["games"], current_streak=row["current_streak"], best_win_streak=row["best_win_streak"], best_loss_streak=row["best_loss_streak"], total_kills=row.get("total_kills", 0), total_deaths=row.get("total_deaths", 0), best_match_kills=row.get("best_match_kills", 0), total_elo_change=row.get("total_elo_change", 0), last_elo_change=row.get("last_elo_change", 0)) for row in rows]
        else:
            return [p for p in self._stats.values() if p.guild_id == guild_id]

    async def set(self, stats: PlayerStats) -> None:
        """Сохранить статистику игрока."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO player_stats (guild_id, user_id, name, elo, wins, finals, games, current_streak, best_win_streak, best_loss_streak, total_kills, total_deaths, best_match_kills, total_elo_change, last_elo_change)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET name = $3, elo = $4, wins = $5, finals = $6, games = $7, current_streak = $8, best_win_streak = $9, best_loss_streak = $10, total_kills = $11, total_deaths = $12, best_match_kills = $13, total_elo_change = $14, last_elo_change = $15
                    """,
                    stats.guild_id, stats.user_id, stats.name, stats.elo, stats.wins, stats.finals, stats.games, stats.current_streak, stats.best_win_streak, stats.best_loss_streak, stats.total_kills, stats.total_deaths, stats.best_match_kills, stats.total_elo_change, stats.last_elo_change
                )
        else:
            key = f"{stats.guild_id}:{stats.user_id}"
            self._stats[key] = stats
            self.save()

    async def update_player(self, guild_id: int, user_id: int, name: str, result: str = "none", count_game: bool = False, set_elo: int | None = None, kills: int = 0, deaths: int = 0) -> None:
        """Обновить статистику игрока.

        Note: The old ELO calculation logic has been removed. ELO is now calculated
        through the new rating system in utils/rating_calculator.py based on:
        - Circle (1-4)
        - Position within team (1-4)
        - Team result (win/loss)
        - Individual K/D performance

        This method now only handles basic operations:
        - set_elo: Directly set ELO to a specific value
        - count_game: Increment games counter
        - kills/deaths: Update total kills and deaths
        - result: Only used for legacy compatibility, no ELO changes

        For new rating system, use process_match_result() in views/kd_input_view.py
        """
        key = f"{guild_id}:{user_id}"

        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                # Get current stats
                row = await conn.fetchrow(
                    "SELECT elo, wins, finals, games, current_streak, best_win_streak, best_loss_streak, total_kills, total_deaths, best_match_kills, total_elo_change, last_elo_change FROM player_stats WHERE guild_id = $1 AND user_id = $2",
                    guild_id, user_id
                )

                if row:
                    current_elo = row["elo"]
                    wins = row["wins"]
                    finals = row["finals"]
                    games = row["games"]
                    current_streak = row["current_streak"]
                    best_win_streak = row["best_win_streak"]
                    best_loss_streak = row["best_loss_streak"]
                    total_kills = row.get("total_kills", 0)
                    total_deaths = row.get("total_deaths", 0)
                    best_match_kills = row.get("best_match_kills", 0)
                    total_elo_change = row.get("total_elo_change", 0)
                    last_elo_change = row.get("last_elo_change", 0)
                else:
                    current_elo = 1000
                    wins = 0
                    finals = 0
                    games = 0
                    current_streak = 0
                    best_win_streak = 0
                    best_loss_streak = 0
                    total_kills = 0
                    total_deaths = 0
                    best_match_kills = 0
                    total_elo_change = 0
                    last_elo_change = 0

                # Only handle set_elo and count_game - no ELO calculation based on result
                if set_elo is not None:
                    current_elo = set_elo

                if count_game:
                    games += 1

                # Update kills and deaths
                total_kills += kills
                total_deaths += deaths
                if kills > best_match_kills:
                    best_match_kills = kills

                await conn.execute(
                    """
                    INSERT INTO player_stats (guild_id, user_id, name, elo, wins, finals, games, current_streak, best_win_streak, best_loss_streak, total_kills, total_deaths, best_match_kills, total_elo_change, last_elo_change)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET name = $3, elo = $4, wins = $5, finals = $6, games = $7, current_streak = $8, best_win_streak = $9, best_loss_streak = $10, total_kills = $11, total_deaths = $12, best_match_kills = $13, total_elo_change = $14, last_elo_change = $15
                    """,
                    guild_id, user_id, name, current_elo, wins, finals, games, current_streak, best_win_streak, best_loss_streak, total_kills, total_deaths, best_match_kills, total_elo_change, last_elo_change
                )
        else:
            if key not in self._stats:
                self._stats[key] = PlayerStats(guild_id=guild_id, user_id=user_id, name=name)

            # Only handle set_elo and count_game - no ELO calculation based on result
            if set_elo is not None:
                self._stats[key].elo = set_elo

            if count_game:
                self._stats[key].games += 1

            # Update kills and deaths
            self._stats[key].total_kills += kills
            self._stats[key].total_deaths += deaths
            if kills > self._stats[key].best_match_kills:
                self._stats[key].best_match_kills = kills

            self.save()

    async def get_leaderboard(self, guild_id: int, page: int = 1, per_page: int = 10) -> list[PlayerStats]:
        """Получить страницу лидерборда, отсортированную по ELO."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                offset = (page - 1) * per_page
                rows = await conn.fetch(
                    """
                    SELECT guild_id, user_id, name, elo, wins, finals, games, current_streak, best_win_streak, best_loss_streak, total_kills, total_deaths, best_match_kills, total_elo_change
                    FROM player_stats
                    WHERE guild_id = $1 AND games > 0 AND user_id > 0
                    ORDER BY elo DESC
                    LIMIT $2 OFFSET $3
                    """,
                    guild_id, per_page, offset
                )
                return [PlayerStats(guild_id=row["guild_id"], user_id=row["user_id"], name=row["name"], elo=row["elo"], wins=row["wins"], finals=row["finals"], games=row["games"], current_streak=row["current_streak"], best_win_streak=row["best_win_streak"], best_loss_streak=row["best_loss_streak"], total_kills=row.get("total_kills", 0), total_deaths=row.get("total_deaths", 0), best_match_kills=row.get("best_match_kills", 0), total_elo_change=row.get("total_elo_change", 0)) for row in rows]
        else:
            # Filter players with at least 1 game and from the same guild
            players_with_games = [p for p in self._stats.values() if p.games > 0 and p.guild_id == guild_id]

            # Sort by ELO (descending)
            sorted_players = sorted(
                players_with_games,
                key=lambda p: p.elo,
                reverse=True
            )

            # Pagination
            start = (page - 1) * per_page
            end = start + per_page
            return sorted_players[start:end]

    async def get_total_pages(self, guild_id: int, per_page: int = 10) -> int:
        """Получить общее количество страниц."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) FROM player_stats WHERE guild_id = $1 AND games > 0",
                    guild_id
                )
                count = row["count"] if row else 0
                return (count + per_page - 1) // per_page
        else:
            players_with_games = len([p for p in self._stats.values() if p.games > 0 and p.guild_id == guild_id])
            return (players_with_games + per_page - 1) // per_page

    async def reset(self, guild_id: int) -> None:
        """Сбросить всю статистику сервера."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM player_stats WHERE guild_id = $1", guild_id)
        else:
            keys_to_remove = [key for key in self._stats if key.startswith(f"{guild_id}:")]
            for key in keys_to_remove:
                del self._stats[key]
            self.save()

    async def reset_all(self) -> None:
        """Сбросить всю статистику всех серверов."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM player_stats")
        else:
            self._stats = {}
            self.save()

    def enable_db(self) -> None:
        """Enable database mode."""
        self._use_db = True


# Глобальный экземпляр хранилища
player_stats_store = PlayerStatsStore()
