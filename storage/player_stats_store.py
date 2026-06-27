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

    async def get(self, guild_id: int, name: str) -> PlayerStats | None:
        """Получить статистику игрока."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT guild_id, name, elo, wins, finals, games FROM player_stats WHERE guild_id = $1 AND name = $2",
                    guild_id, name
                )
                if row:
                    return PlayerStats(guild_id=row["guild_id"], name=row["name"], elo=row["elo"], wins=row["wins"], finals=row["finals"], games=row["games"])
                return None
        else:
            key = f"{guild_id}:{name}"
            return self._stats.get(key)

    async def get_all(self, guild_id: int) -> list[PlayerStats]:
        """Получить статистику всех игроков сервера."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT guild_id, name, elo, wins, finals, games FROM player_stats WHERE guild_id = $1",
                    guild_id
                )
                return [PlayerStats(guild_id=row["guild_id"], name=row["name"], elo=row["elo"], wins=row["wins"], finals=row["finals"], games=row["games"]) for row in rows]
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
                    INSERT INTO player_stats (guild_id, name, elo, wins, finals, games)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (guild_id, name)
                    DO UPDATE SET elo = $3, wins = $4, finals = $5, games = $6
                    """,
                    stats.guild_id, stats.name, stats.elo, stats.wins, stats.finals, stats.games
                )
        else:
            key = f"{stats.guild_id}:{stats.name}"
            self._stats[key] = stats
            self.save()

    async def update_player(self, guild_id: int, name: str, result: str = "loss", count_game: bool = False) -> None:
        """Обновить статистику игрока после турнира.
        result: 'win' (+25 ELO), 'final' (+10 ELO), 'semifinal_win' (+0 ELO), 'qualifier_win' (+0 ELO), 'loss' (-25 ELO)
        count_game: если True, увеличивает games (только для финала)
        """
        key = f"{guild_id}:{name}"

        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                # Get current ELO
                row = await conn.fetchrow(
                    "SELECT elo, wins, finals, games FROM player_stats WHERE guild_id = $1 AND name = $2",
                    guild_id, name
                )

                if row:
                    current_elo = row["elo"]
                    wins = row["wins"]
                    finals = row["finals"]
                    games = row["games"]
                else:
                    current_elo = 1000
                    wins = 0
                    finals = 0
                    games = 0

                # Calculate ELO change
                if result == "win":
                    elo_change = 25
                    wins += 1
                elif result == "final":
                    elo_change = 10
                    finals += 1
                elif result == "semifinal_win":
                    elo_change = 0
                elif result == "qualifier_win":
                    elo_change = 0
                else:  # loss
                    elo_change = -25

                if count_game:
                    games += 1

                new_elo = current_elo + elo_change

                await conn.execute(
                    """
                    INSERT INTO player_stats (guild_id, name, elo, wins, finals, games)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (guild_id, name)
                    DO UPDATE SET elo = $3, wins = $4, finals = $5, games = $6
                    """,
                    guild_id, name, new_elo, wins, finals, games
                )
        else:
            if key not in self._stats:
                self._stats[key] = PlayerStats(guild_id=guild_id, name=name)

            if result == "win":
                self._stats[key].elo += 25
                self._stats[key].wins += 1
            elif result == "final":
                self._stats[key].elo += 10
                self._stats[key].finals += 1
            elif result == "semifinal_win":
                self._stats[key].elo += 0
            elif result == "qualifier_win":
                self._stats[key].elo += 0
            else:  # loss
                self._stats[key].elo -= 25

            if count_game:
                self._stats[key].games += 1
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
                    SELECT guild_id, name, elo, wins, finals, games
                    FROM player_stats
                    WHERE guild_id = $1 AND games > 0
                    ORDER BY elo DESC
                    LIMIT $2 OFFSET $3
                    """,
                    guild_id, per_page, offset
                )
                return [PlayerStats(guild_id=row["guild_id"], name=row["name"], elo=row["elo"], wins=row["wins"], finals=row["finals"], games=row["games"]) for row in rows]
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
