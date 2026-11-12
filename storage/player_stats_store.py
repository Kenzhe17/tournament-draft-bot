"""Хранилище статистики игроков."""

import json
from pathlib import Path
from typing import Any

from models.player_stats import PlayerStats

from config import DATA_DIR  # noqa: E402 — единый путь из config
from storage.json_atomic import atomic_write_json
STATS_FILE = DATA_DIR / "player_stats.json"


class PlayerStatsStore:
    """Хранилище статистики игроков в PostgreSQL."""

    def __init__(self) -> None:
        self._stats: dict[str, PlayerStats] = {}
        self._use_db = True  # Использовать PostgreSQL
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
        atomic_write_json(STATS_FILE, data)

    @staticmethod
    def _stats_from_row(row) -> PlayerStats:
        """asyncpg.Record -> PlayerStats (общая конвертация)."""
        return PlayerStats(
            guild_id=row["guild_id"], user_id=row["user_id"],
            name=row["name"], elo=int(row["elo"]),
            wins=row["wins"], finals=row["finals"], games=row["games"],
            current_streak=row["current_streak"],
            best_win_streak=row["best_win_streak"],
            best_loss_streak=row["best_loss_streak"],
            total_kills=row.get("total_kills", 0),
            total_deaths=row.get("total_deaths", 0),
            total_assists=row.get("total_assists", 0),
            total_damage=row.get("total_damage", 0),
            best_match_kills=row.get("best_match_kills", 0),
            total_elo_change=row.get("total_elo_change", 0),
            last_elo_change=row.get("last_elo_change", 0),
            xp=row.get("xp", 0), level=row.get("level", 1),
            xp_to_next_level=PlayerStats._calculate_xp_to_next_level(
                row.get("level", 1)
            ),
            total_earnings=row.get("total_earnings", 0),
            tournament_participations=row.get(
                "tournament_participations", 0
            ),
            description=row.get("description", ""),
            daily_tournament_coins=row.get("daily_tournament_coins", 0),
            daily_coins_date=row.get("daily_coins_date", ""),
            reputation=row.get("reputation", 0),
            birthday=row.get("birthday", "") or "",
            spouse_id=row.get("spouse_id", 0) or 0,
        )

    _STATS_COLUMNS = (
        "guild_id, user_id, name, elo, wins, finals, games, "
        "current_streak, best_win_streak, best_loss_streak, "
        "total_kills, total_deaths, total_assists, total_damage, "
        "best_match_kills, total_elo_change, last_elo_change, xp, "
        "level, xp_to_next_level, total_earnings, "
        "tournament_participations, description, "
        "daily_tournament_coins, daily_coins_date, reputation, "
        "birthday, spouse_id"
    )

    async def get(self, guild_id: int, user_id: int) -> PlayerStats | None:
        """Получить статистику игрока."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT {self._STATS_COLUMNS} FROM player_stats "
                    "WHERE guild_id = $1 AND user_id = $2",
                    guild_id, user_id
                )
                if row:
                    return self._stats_from_row(row)
                return None
        else:
            key = f"{guild_id}:{user_id}"
            return self._stats.get(key)

    async def get_many(
        self, guild_id: int, user_ids
    ) -> dict[int, "PlayerStats"]:
        """Пакетно получить статистику игроков одним запросом.

        Возвращает {user_id: PlayerStats} только для найденных.
        Используется embed-билдерами вместо N+1 запросов get().
        """
        uids = []
        seen = set()
        for u in user_ids:
            try:
                iu = int(u)
            except (TypeError, ValueError):
                continue
            if iu and iu not in seen:
                seen.add(iu)
                uids.append(iu)
        if not uids:
            return {}
        if self._use_db:
            from storage.db import get_pool
            from utils import perf_metrics

            pool = await get_pool()
            async with perf_metrics.async_timing("pg_stats_query_ms"):
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        f"SELECT {self._STATS_COLUMNS} FROM player_stats "
                        "WHERE guild_id = $1 AND user_id = ANY($2::bigint[])",
                        guild_id, uids,
                    )
            perf_metrics.incr("pg_stats_queries")
            return {
                int(r["user_id"]): self._stats_from_row(r) for r in rows
            }
        out = {}
        for uid in uids:
            stats = self._stats.get(f"{guild_id}:{uid}")
            if stats is not None:
                out[uid] = stats
        return out

    async def get_all(self, guild_id: int) -> list[PlayerStats]:
        """Получить статистику всех игроков сервера."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT {self._STATS_COLUMNS} FROM player_stats "
                    "WHERE guild_id = $1",
                    guild_id
                )
                return [self._stats_from_row(row) for row in rows]
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
                    INSERT INTO player_stats (guild_id, user_id, name, elo, wins, finals, games, current_streak, best_win_streak, best_loss_streak, total_kills, total_deaths, total_assists, total_damage, best_match_kills, total_elo_change, last_elo_change, xp, level, xp_to_next_level, total_earnings, tournament_participations, description, daily_tournament_coins, daily_coins_date, reputation, birthday, spouse_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28)
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET name = $3, elo = $4, wins = $5, finals = $6, games = $7, current_streak = $8, best_win_streak = $9, best_loss_streak = $10, total_kills = $11, total_deaths = $12, total_assists = $13, total_damage = $14, best_match_kills = $15, total_elo_change = $16, last_elo_change = $17, xp = $18, level = $19, xp_to_next_level = $20, total_earnings = $21, tournament_participations = $22, description = $23, daily_tournament_coins = $24, daily_coins_date = $25, birthday = $27, spouse_id = $28
                    """,
                    stats.guild_id, stats.user_id, stats.name, stats.elo, stats.wins, stats.finals, stats.games, stats.current_streak, stats.best_win_streak, stats.best_loss_streak, stats.total_kills, stats.total_deaths, stats.total_assists, stats.total_damage, stats.best_match_kills, stats.total_elo_change, stats.last_elo_change, stats.xp, stats.level, stats.xp_to_next_level, stats.total_earnings, stats.tournament_participations, stats.description, stats.daily_tournament_coins, stats.daily_coins_date, stats.reputation, stats.birthday, stats.spouse_id
                )
        else:
            key = f"{stats.guild_id}:{stats.user_id}"
            existing = self._stats.get(key)
            if existing is not None:
                # reputation принадлежит Reputation 2.0 — не перезаписываем
                # устаревшим снимком из объекта статистики.
                stats.reputation = existing.reputation
            self._stats[key] = stats
            self.save()

    async def set_many(
        self,
        stats_list: list,
        finalize_match_id: str | None = None,
        guild_id_for_finalize: int | None = None,
    ) -> None:
        """Массовое сохранение статистики ОДНОЙ транзакцией.

        Используется финализацией матча: ELO/статистика всех игроков
        применяются атомарно — сбой посреди не оставляет половину
        команды с начисленным результатом.

        Если передан finalize_match_id, в ТОЙ ЖЕ транзакции пишется
        запись match_finalizations(status='stats_done') — она
        переживает рестарт и позволяет отличить «статистика
        закоммичена» от «матч не обработан».
        """
        if self._use_db:
            from storage.db import get_pool
            from storage.finalize_store import mark_stats_done_in_tx
            pool = await get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for stats in stats_list:
                        await conn.execute(
                            """
                            INSERT INTO player_stats (guild_id, user_id, name, elo, wins, finals, games, current_streak, best_win_streak, best_loss_streak, total_kills, total_deaths, total_assists, total_damage, best_match_kills, total_elo_change, last_elo_change, xp, level, xp_to_next_level, total_earnings, tournament_participations, description, daily_tournament_coins, daily_coins_date, reputation, birthday, spouse_id)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28)
                            ON CONFLICT (guild_id, user_id)
                            DO UPDATE SET name = $3, elo = $4, wins = $5, finals = $6, games = $7, current_streak = $8, best_win_streak = $9, best_loss_streak = $10, total_kills = $11, total_deaths = $12, total_assists = $13, total_damage = $14, best_match_kills = $15, total_elo_change = $16, last_elo_change = $17, xp = $18, level = $19, xp_to_next_level = $20, total_earnings = $21, tournament_participations = $22, description = $23, daily_tournament_coins = $24, daily_coins_date = $25, birthday = $27, spouse_id = $28
                            """,
                            stats.guild_id, stats.user_id, stats.name, stats.elo, stats.wins, stats.finals, stats.games, stats.current_streak, stats.best_win_streak, stats.best_loss_streak, stats.total_kills, stats.total_deaths, stats.total_assists, stats.total_damage, stats.best_match_kills, stats.total_elo_change, stats.last_elo_change, stats.xp, stats.level, stats.xp_to_next_level, stats.total_earnings, stats.tournament_participations, stats.description, stats.daily_tournament_coins, stats.daily_coins_date, stats.reputation, stats.birthday, stats.spouse_id
                        )
                    if finalize_match_id:
                        await mark_stats_done_in_tx(
                            conn,
                            guild_id_for_finalize or 0,
                            finalize_match_id,
                        )
            return
        for stats in stats_list:
            key = f"{stats.guild_id}:{stats.user_id}"
            existing = self._stats.get(key)
            if existing is not None:
                # reputation принадлежит Reputation 2.0 — не перезаписываем
                # устаревшим снимком из объекта статистики.
                stats.reputation = existing.reputation
            self._stats[key] = stats
        self.save()

    async def update(self, guild_id: int, user_id: int, stats: PlayerStats) -> None:
        """Обновить статистику игрока."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE player_stats SET
                    name = $1,
                    description = $2,
                    avatar_url = $3,
                    birthday = $4,
                    spouse_id = $5
                    WHERE guild_id = $6 AND user_id = $7""",
                    stats.name,
                    stats.description,
                    stats.avatar_url,
                    stats.birthday,
                    stats.spouse_id,
                    guild_id,
                    user_id
                )
        else:
            key = f"{guild_id}:{user_id}"
            self._stats[key] = stats
            self.save()

    async def update_player(self, guild_id: int, user_id: int, name: str, result: str = "none", count_game: bool = False, set_elo: int | None = None) -> None:
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
                    "SELECT elo, wins, finals, games, current_streak, best_win_streak, best_loss_streak, total_kills, total_deaths, best_match_kills, total_elo_change, last_elo_change, description FROM player_stats WHERE guild_id = $1 AND user_id = $2",
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
                    ORDER BY elo DESC, user_id ASC
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
                    "SELECT COUNT(*) FROM player_stats WHERE guild_id = $1 AND games > 0 AND user_id > 0",
                    guild_id
                )
                count = row["count"] if row else 0
                return (count + per_page - 1) // per_page
        else:
            players_with_games = len([p for p in self._stats.values() if p.games > 0 and p.guild_id == guild_id])
            return (players_with_games + per_page - 1) // per_page

    async def get_leaderboard_by_level(self, guild_id: int, page: int = 1, per_page: int = 10) -> list[PlayerStats]:
        """Получить страницу лидерборда, отсортированную по уровню."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                offset = (page - 1) * per_page
                rows = await conn.fetch(
                    """
                    SELECT guild_id, user_id, name, elo, wins, finals, games, current_streak, best_win_streak, best_loss_streak, total_kills, total_deaths, best_match_kills, total_elo_change, level, xp, total_earnings
                    FROM player_stats
                    WHERE guild_id = $1 AND games > 0 AND user_id > 0
                    ORDER BY level DESC, xp DESC, user_id ASC
                    LIMIT $2 OFFSET $3
                    """,
                    guild_id, per_page, offset
                )
                return [PlayerStats(guild_id=row["guild_id"], user_id=row["user_id"], name=row["name"], elo=row["elo"], wins=row["wins"], finals=row["finals"], games=row["games"], current_streak=row["current_streak"], best_win_streak=row["best_win_streak"], best_loss_streak=row["best_loss_streak"], total_kills=row.get("total_kills", 0), total_deaths=row.get("total_deaths", 0), best_match_kills=row.get("best_match_kills", 0), total_elo_change=row.get("total_elo_change", 0), level=row.get("level", 1), xp=row.get("xp", 0), total_earnings=row.get("total_earnings", 0)) for row in rows]
        else:
            # Filter players with at least 1 game and from the same guild
            players_with_games = [p for p in self._stats.values() if p.games > 0 and p.guild_id == guild_id]

            # Sort by level (descending), then XP
            sorted_players = sorted(
                players_with_games,
                key=lambda p: (p.level, p.xp),
                reverse=True
            )

            # Pagination
            start = (page - 1) * per_page
            end = start + per_page
            return sorted_players[start:end]

    async def get_leaderboard_by_earnings(self, guild_id: int, page: int = 1, per_page: int = 10) -> list[PlayerStats]:
        """Получить страницу лидерборда, отсортированную по текущему балансу."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                offset = (page - 1) * per_page
                rows = await conn.fetch(
                    """
                    SELECT ps.guild_id, ps.user_id, ps.name, ps.elo, ps.wins, ps.finals, ps.games, ps.current_streak, ps.best_win_streak, ps.best_loss_streak, ps.total_kills, ps.total_deaths, ps.best_match_kills, ps.total_elo_change, ps.level, ps.xp, ps.total_earnings, COALESCE(ub.balance, 100) as current_balance
                    FROM player_stats ps
                    LEFT JOIN user_balance ub ON ps.guild_id = ub.guild_id AND ps.user_id = ub.user_id
                    WHERE ps.guild_id = $1 AND ps.games > 0 AND ps.user_id > 0
                    ORDER BY COALESCE(ub.balance, 100) DESC, ps.user_id ASC
                    LIMIT $2 OFFSET $3
                    """,
                    guild_id, per_page, offset
                )
                return [PlayerStats(guild_id=row["guild_id"], user_id=row["user_id"], name=row["name"], elo=row["elo"], wins=row["wins"], finals=row["finals"], games=row["games"], current_streak=row["current_streak"], best_win_streak=row["best_win_streak"], best_loss_streak=row["best_loss_streak"], total_kills=row.get("total_kills", 0), total_deaths=row.get("total_deaths", 0), best_match_kills=row.get("best_match_kills", 0), total_elo_change=row.get("total_elo_change", 0), level=row.get("level", 1), xp=row.get("xp", 0), total_earnings=row.get("total_earnings", 0)) for row in rows]
        else:
            # Filter players with at least 1 game and from the same guild
            players_with_games = [p for p in self._stats.values() if p.games > 0 and p.guild_id == guild_id]

            # Get balances for all players
            from storage.user_balance_store import user_balance_store
            player_balances = {}
            for player in players_with_games:
                balance = await user_balance_store.get_balance(guild_id, player.user_id)
                player_balances[player.user_id] = balance

            # Sort by current balance (descending)
            sorted_players = sorted(
                players_with_games,
                key=lambda p: player_balances.get(p.user_id, 0),
                reverse=True
            )

            # Pagination
            start = (page - 1) * per_page
            end = start + per_page
            return sorted_players[start:end]

    async def get_player_rank(self, guild_id: int, user_id: int) -> int | None:
        """Получить место игрока в ELO рейтинге."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                target = await conn.fetchrow(
                    "SELECT elo, games FROM player_stats WHERE guild_id = $1 AND user_id = $2",
                    guild_id, user_id
                )
                if target is None or target["games"] <= 0:
                    return None
                rank = await conn.fetchval(
                    """
                    SELECT COUNT(*) + 1
                    FROM player_stats
                    WHERE guild_id = $1 AND user_id > 0 AND games > 0 AND (
                        elo > $2
                        OR (elo = $2 AND user_id < $3)
                    )
                    """,
                    guild_id, target["elo"], user_id
                )
                return rank if rank and rank > 0 else None
        else:
            players_with_games = [p for p in self._stats.values() if p.games > 0 and p.guild_id == guild_id and p.user_id > 0]
            sorted_players = sorted(players_with_games, key=lambda p: (p.elo, -p.user_id), reverse=True)
            for i, player in enumerate(sorted_players, 1):
                if player.user_id == user_id:
                    return i
            return None

    async def get_player_rank_by_level(self, guild_id: int, user_id: int) -> int | None:
        """Получить место игрока в рейтинге по уровню."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                target = await conn.fetchrow(
                    "SELECT level, xp, games FROM player_stats WHERE guild_id = $1 AND user_id = $2",
                    guild_id, user_id
                )
                if target is None or target["games"] <= 0:
                    return None
                rank = await conn.fetchval(
                    """
                    SELECT COUNT(*) + 1
                    FROM player_stats
                    WHERE guild_id = $1 AND user_id > 0 AND games > 0 AND (
                        level > $2
                        OR (level = $2 AND xp > $3)
                        OR (level = $2 AND xp = $3 AND user_id < $4)
                    )
                    """,
                    guild_id, target["level"], target["xp"], user_id
                )
                return rank if rank and rank > 0 else None
        else:
            players_with_games = [p for p in self._stats.values() if p.games > 0 and p.guild_id == guild_id and p.user_id > 0]
            sorted_players = sorted(players_with_games, key=lambda p: (p.level, p.xp, -p.user_id), reverse=True)
            for i, player in enumerate(sorted_players, 1):
                if player.user_id == user_id:
                    return i
            return None

    async def get_player_rank_by_earnings(self, guild_id: int, user_id: int) -> int | None:
        """Получить место игрока в рейтинге по балансу."""
        if self._use_db:
            from storage.db import get_pool
            from storage.user_balance_store import user_balance_store
            pool = await get_pool()
            async with pool.acquire() as conn:
                target = await conn.fetchrow(
                    "SELECT games FROM player_stats WHERE guild_id = $1 AND user_id = $2",
                    guild_id, user_id
                )
                if target is None or target["games"] <= 0:
                    return None
                user_balance = await user_balance_store.get_balance(guild_id, user_id)
                rank = await conn.fetchval(
                    """
                    SELECT COUNT(*) + 1
                    FROM player_stats ps
                    LEFT JOIN user_balance ub ON ps.guild_id = ub.guild_id AND ps.user_id = ub.user_id
                    WHERE ps.guild_id = $1 AND ps.user_id > 0 AND ps.games > 0 AND (
                        COALESCE(ub.balance, 100) > $2
                        OR (COALESCE(ub.balance, 100) = $2 AND ps.user_id < $3)
                    )
                    """,
                    guild_id, user_balance, user_id
                )
                return rank if rank and rank > 0 else None
        else:
            players_with_games = [p for p in self._stats.values() if p.games > 0 and p.guild_id == guild_id and p.user_id > 0]
            from storage.user_balance_store import user_balance_store
            player_balances = {}
            for player in players_with_games:
                balance = await user_balance_store.get_balance(guild_id, player.user_id)
                player_balances[player.user_id] = balance
            sorted_players = sorted(players_with_games, key=lambda p: (player_balances.get(p.user_id, 0), -p.user_id), reverse=True)
            for i, player in enumerate(sorted_players, 1):
                if player.user_id == user_id:
                    return i
            return None

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

    async def reset_player(self, guild_id: int, user_id: int) -> None:
        """Сбросить статистику конкретного игрока."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM player_stats WHERE guild_id = $1 AND user_id = $2", guild_id, user_id)
        else:
            key = f"{guild_id}:{user_id}"
            if key in self._stats:
                del self._stats[key]
                self.save()

    async def try_marry(self, guild_id: int, user_a: int, user_b: int) -> bool:
        """Атомарно связать двух свободных игроков (spouse_id).

        UPDATE с условием spouse_id=0 в одной транзакции — защита от
        двойного принятия и гонок. True — брак записан, False —
        кто-то уже занят или строка не найдена.
        """
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                try:
                    async with conn.transaction():
                        r1 = await conn.execute(
                            "UPDATE player_stats SET spouse_id=$3 "
                            "WHERE guild_id=$1 AND user_id=$2 AND spouse_id=0",
                            guild_id, user_a, user_b)
                        r2 = await conn.execute(
                            "UPDATE player_stats SET spouse_id=$3 "
                            "WHERE guild_id=$1 AND user_id=$2 AND spouse_id=0",
                            guild_id, user_b, user_a)
                        if r1 != "UPDATE 1" or r2 != "UPDATE 1":
                            raise _MarryRejected
                except _MarryRejected:
                    return False
            return True
        # JSON fallback
        sa = self._stats.get(f"{guild_id}:{user_a}")
        sb = self._stats.get(f"{guild_id}:{user_b}")
        if sa is None or sb is None:
            return False
        if sa.spouse_id or sb.spouse_id:
            return False
        sa.spouse_id = user_b
        sb.spouse_id = user_a
        self.save()
        return True

    def enable_db(self) -> None:
        """Enable database mode."""
        self._use_db = True


class _MarryRejected(Exception):
    pass


# Глобальный экземпляр хранилища
player_stats_store = PlayerStatsStore()
