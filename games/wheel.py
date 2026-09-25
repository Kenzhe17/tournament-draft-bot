"""Игра колесо фортуны."""

import random
from typing import Literal


class WheelGame:
    """Логика игры колесо фортуны."""

    # Сектора колеса с множителями
    SECTORS = [
        {"multiplier": 0.5, "name": "Половина", "emoji": "🔻", "weight": 15},
        {"multiplier": 1.0, "name": "Обычный", "emoji": "🟢", "weight": 25},
        {"multiplier": 2.0, "name": "Двойной", "emoji": "🔵", "weight": 20},
        {"multiplier": 3.0, "name": "Тройной", "emoji": "🟣", "weight": 15},
        {"multiplier": 5.0, "name": "Пятерной", "emoji": "🟡", "weight": 10},
        {"multiplier": 10.0, "name": "Джекпот", "emoji": "🌟", "weight": 5},
        {"multiplier": 0.0, "name": "Банкрот", "emoji": "💀", "weight": 10},
    ]

    def __init__(self) -> None:
        """Инициализировать игру."""
        self.game_over = False
        self.won = False
        self.result_sector = None

    def spin(self) -> dict:
        """Крутить колесо.

        Returns:
            Словарь с результатом вращения
        """
        if self.game_over:
            return {"error": "Игра уже завершена!"}

        # Взвешенный случайный выбор сектора
        sectors = []
        weights = []
        for sector in self.SECTORS:
            sectors.append(sector)
            weights.append(sector["weight"])

        self.result_sector = random.choices(sectors, weights=weights, k=1)[0]
        self.game_over = True

        if self.result_sector["multiplier"] == 0.0:
            self.won = False
        else:
            self.won = True

        return {
            "sector": self.result_sector,
            "multiplier": self.result_sector["multiplier"],
            "name": self.result_sector["name"],
            "emoji": self.result_sector["emoji"],
        }

    def get_multiplier(self) -> float:
        """Получить множитель выигрыша."""
        if self.result_sector:
            return self.result_sector["multiplier"]
        return 0.0

    def get_state(self) -> dict:
        """Получить текущее состояние игры."""
        return {
            "game_over": self.game_over,
            "won": self.won,
            "result_sector": self.result_sector,
        }
