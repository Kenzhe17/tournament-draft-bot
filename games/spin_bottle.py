"""Игра бутылочка."""

import random
from typing import Literal


class SpinBottleGame:
    """Логика игры бутылочка."""

    DARES = [
        "Сделать 10 приседаний",
        "Сказать комплимент случайному участнику",
        "Сказать правду о себе",
        "Изобразить животное",
        "Сделать смешное лицо",
        "Сказать что-то на иностранном языке",
        "Сделать 5 отжиманий",
        "Покажите свою любимую эмодзи",
        "Скажите что вы любите больше всего",
        "Сделайте смешный звук",
    ]

    def __init__(self) -> None:
        """Инициализировать игру."""
        self.game_over = False
        self.won = False
        self.result = None
        self.dare = None

    def spin(self) -> dict:
        """Крутить бутылочку.

        Returns:
            Словарь с результатом вращения
        """
        if self.game_over:
            return {"error": "Игра уже завершена!"}

        # Случайный результат: указывает на игрока или на воздух
        # 60% шанс что укажет на игрока (победа)
        self.result = random.choice(["player", "miss", "player", "miss", "player", "player"])
        self.dare = random.choice(self.DARES)
        self.game_over = True

        if self.result == "player":
            self.won = True
        else:
            self.won = False

        return {
            "result": self.result,
            "dare": self.dare,
            "won": self.won,
        }

    def get_multiplier(self) -> float:
        """Получить множитель выигрыша."""
        return 2.0  # 2x множитель как в плане

    def get_state(self) -> dict:
        """Получить текущее состояние игры."""
        return {
            "game_over": self.game_over,
            "won": self.won,
            "result": self.result,
            "dare": self.dare,
        }
