"""Игра быстрый тест на реакцию."""

import random
import time
from typing import Literal


class ReflexTestGame:
    """Логика игры быстрый тест на реакцию."""

    EMOJIS = ["⚡", "🔥", "💥", "⭐", "🎯", "🚀", "💎", "🌟"]

    def __init__(self) -> None:
        """Инициализировать игру."""
        self.game_over = False
        self.won = False
        self.target_emoji = random.choice(self.EMOJIS)
        self.start_time = None
        self.reaction_time = None
        self.threshold = 1.0  # Порог в секундах для победы

    def start(self) -> str:
        """Начать игру и вернуть эмодзи."""
        self.start_time = time.time()
        return self.target_emoji

    def attempt(self, emoji: str) -> tuple[Literal["correct", "incorrect", "timeout", "already_finished"], str]:
        """Попытаться нажать на эмодзи.

        Returns:
            (result, message) - результат и сообщение
        """
        if self.game_over:
            return "already_finished", "Игра уже завершена!"

        if self.start_time is None:
            return "timeout", "Игра не началась!"

        if emoji != self.target_emoji:
            return "incorrect", f"❌ Неверный эмодзи! Требуется: {self.target_emoji}"

        self.reaction_time = time.time() - self.start_time
        self.game_over = True

        if self.reaction_time <= self.threshold:
            self.won = True
            return "correct", f"🎉 Отлично! Ваша реакция: {self.reaction_time:.3f} сек"
        else:
            self.won = False
            return "timeout", f"😢 Медленно! Ваша реакция: {self.reaction_time:.3f} сек (порог: {self.threshold} сек)"

    def get_multiplier(self) -> float:
        """Получить множитель выигрыша."""
        return 2.0  # 2x множитель как в плане

    def get_state(self) -> dict:
        """Получить текущее состояние игры."""
        return {
            "game_over": self.game_over,
            "won": self.won,
            "target_emoji": self.target_emoji,
            "reaction_time": self.reaction_time,
            "threshold": self.threshold,
        }
