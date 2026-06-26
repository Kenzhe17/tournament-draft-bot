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
                for name, stats_data in data.items():
                    self._stats[name] = PlayerStats.from_dict(stats_data)
        except (json.JSONDecodeError, KeyError):
            # Если файл повреждён, начинаем с пустого состояния
            self._stats = {}

    def save(self) -> None:
        """Сохранить статистику в файл."""
        data = {name: stats.to_dict() for name, stats in self._stats.items()}
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get(self, name: str) -> PlayerStats | None:
        """Получить статистику игрока."""
        return self._stats.get(name)

    def get_all(self) -> list[PlayerStats]:
        """Получить статистику всех игроков."""
        return list(self._stats.values())

    def set(self, stats: PlayerStats) -> None:
        """Сохранить статистику игрока."""
        self._stats[stats.name] = stats
        self.save()

    def update_player(self, name: str, won: bool = False) -> None:
        """Обновить статистику игрока после турнира."""
        if name not in self._stats:
            self._stats[name] = PlayerStats(name=name)
        
        self._stats[name].games += 1
        if won:
            self._stats[name].wins += 1
        
        self.save()

    def get_leaderboard(self, page: int = 1, per_page: int = 10) -> list[PlayerStats]:
        """Получить страницу лидерборда, отсортированную по победам."""
        # Filter players with at least 1 game
        players_with_games = [p for p in self._stats.values() if p.games > 0]
        
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

    def get_total_pages(self, per_page: int = 10) -> int:
        """Получить общее количество страниц."""
        players_with_games = len([p for p in self._stats.values() if p.games > 0])
        return (players_with_games + per_page - 1) // per_page


# Глобальный экземпляр хранилища
player_stats_store = PlayerStatsStore()
