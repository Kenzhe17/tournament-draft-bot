"""Хранилище статистики игроков."""

import json
from pathlib import Path
from typing import Any

from models.player_stats import PlayerStats

DATA_DIR = Path("data")
STATS_FILE = DATA_DIR / "player_stats.json"


class PlayerStatsStore:
    """Хранилище статистики игроков в JSON-файле."""

    def __init__(self) -> None:
        self._stats: dict[str, PlayerStats] = {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить статистику из файла."""
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
        """Сохранить статистику в файл."""
        data = {name: stats.to_dict() for name, stats in self._stats.items()}
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get(self, guild_id: int, name: str) -> PlayerStats | None:
        """Получить статистику игрока."""
        key = f"{guild_id}:{name}"
        return self._stats.get(key)

    def get_all(self, guild_id: int) -> list[PlayerStats]:
        """Получить статистику всех игроков сервера."""
        return [p for p in self._stats.values() if p.guild_id == guild_id]

    def set(self, stats: PlayerStats) -> None:
        """Сохранить статистику игрока."""
        key = f"{stats.guild_id}:{stats.name}"
        self._stats[key] = stats
        self.save()

    def update_player(self, guild_id: int, name: str, won: bool = False) -> None:
        """Обновить статистику игрока после турнира."""
        key = f"{guild_id}:{name}"
        if key not in self._stats:
            self._stats[key] = PlayerStats(guild_id=guild_id, name=name)

        self._stats[key].games += 1
        if won:
            self._stats[key].wins += 1

        self.save()

    def get_leaderboard(self, guild_id: int, page: int = 1, per_page: int = 10) -> list[PlayerStats]:
        """Получить страницу лидерборда, отсортированную по победам."""
        # Filter players with at least 1 game and from the same guild
        players_with_games = [p for p in self._stats.values() if p.games > 0 and p.guild_id == guild_id]

        # Sort by wins (descending), then by win rate (descending)
        sorted_players = sorted(
            players_with_games,
            key=lambda p: (p.wins, p.win_rate),
            reverse=True
        )

        # Pagination
        start = (page - 1) * per_page
        end = start + per_page
        return sorted_players[start:end]

    def get_total_pages(self, guild_id: int, per_page: int = 10) -> int:
        """Получить общее количество страниц."""
        players_with_games = len([p for p in self._stats.values() if p.games > 0 and p.guild_id == guild_id])
        return (players_with_games + per_page - 1) // per_page

    def reset(self, guild_id: int) -> None:
        """Сбросить всю статистику сервера."""
        keys_to_remove = [key for key in self._stats if key.startswith(f"{guild_id}:")]
        for key in keys_to_remove:
            del self._stats[key]
        self.save()

    def reset_all(self) -> None:
        """Сбросить всю статистику всех серверов."""
        self._stats = {}
        self.save()


# Глобальный экземпляр хранилища
player_stats_store = PlayerStatsStore()
