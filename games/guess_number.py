"""Игра в угадай число."""

import random
import uuid
from typing import Literal


class GuessNumberGame:
    """Логика игры в угадай число."""

    def __init__(self, secret_number: int | None = None, session_id: str | None = None) -> None:
        """Инициализировать игру."""
        self.session_id = session_id if session_id else str(uuid.uuid4())
        self.secret_number = secret_number if secret_number is not None else random.randint(1, 100)
        self.attempts_left = 7
        self.game_over = False
        self.won = False

    def make_guess(self, guess: int) -> tuple[Literal["correct", "too_high", "too_low", "game_over"], str]:
        """Сделать попытку угадать число.

        Returns:
            (result, message) - результат и сообщение для игрока
        """
        if self.game_over:
            return "game_over", "Игра уже завершена!"

        self.attempts_left -= 1

        if guess == self.secret_number:
            self.game_over = True
            self.won = True
            return "correct", f"🎉 Поздравляем! Вы угадали число {self.secret_number}!"

        if self.attempts_left <= 0:
            self.game_over = True
            return "game_over", f"😢 Игра окончена! Загаданное число было {self.secret_number}."

        if guess < self.secret_number:
            return "too_low", f"📈 Больше! Попыток осталось: {self.attempts_left}"
        else:
            return "too_high", f"📉 Меньше! Попыток осталось: {self.attempts_left}"

    def get_multiplier(self) -> float:
        """Получить множитель выигрыша."""
        return 5.0  # 5x множитель как в плане

    def get_state(self) -> dict:
        """Получить текущее состояние игры."""
        return {
            "secret_number": self.secret_number,
            "attempts_left": self.attempts_left,
            "game_over": self.game_over,
            "won": self.won,
        }
