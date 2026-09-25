"""Игра крестики-нолики."""

import random
from typing import Literal, Optional


class TicTacToeGame:
    """Логика игры крестики-нолики."""

    def __init__(self, is_pve: bool = True) -> None:
        """Инициализировать игру."""
        self.board = [" " for _ in range(9)]  # 3x3 доска
        self.current_player = "X"  # X всегда первый
        self.game_over = False
        self.winner = None
        self.is_pve = is_pve
        self.is_pvp = not is_pve

    def make_move(self, position: int) -> tuple[Literal["valid", "invalid", "win", "draw", "continue"], str]:
        """Сделать ход.

        Args:
            position: Позиция на доске (0-8)

        Returns:
            (result, message) - результат и сообщение
        """
        if self.game_over:
            return "invalid", "Игра уже завершена!"

        if position < 0 or position > 8:
            return "invalid", "Неверная позиция!"

        if self.board[position] != " ":
            return "invalid", "Эта клетка уже занята!"

        # Сделать ход
        self.board[position] = self.current_player

        # Проверить на победу
        if self.check_win(self.current_player):
            self.game_over = True
            self.winner = self.current_player
            return "win", f"🎉 Игрок {self.current_player} победил!"

        # Проверить на ничью
        if " " not in self.board:
            self.game_over = True
            return "draw", "🤝 Ничья!"

        # Сменить игрока
        self.current_player = "O" if self.current_player == "X" else "X"

        # Если PvE и теперь ход бота
        if self.is_pve and self.current_player == "O":
            bot_move = self.get_bot_move()
            self.board[bot_move] = "O"

            # Проверить на победу бота
            if self.check_win("O"):
                self.game_over = True
                self.winner = "O"
                return "win", "🤖 Бот победил!"

            # Проверить на ничью
            if " " not in self.board:
                self.game_over = True
                return "draw", "🤝 Ничья!"

            self.current_player = "X"
            return "continue", f"🤖 Бот выбрал клетку {bot_move}"

        return "continue", f"Ход игрока {self.current_player}"

    def check_win(self, player: str) -> bool:
        """Проверить победил ли игрок."""
        win_combinations = [
            [0, 1, 2],  # Верхняя строка
            [3, 4, 5],  # Средняя строка
            [6, 7, 8],  # Нижняя строка
            [0, 3, 6],  # Левый столбец
            [1, 4, 7],  # Средний столбец
            [2, 5, 8],  # Правый столбец
            [0, 4, 8],  # Диагональ
            [2, 4, 6],  # Обратная диагональ
        ]

        for combo in win_combinations:
            if all(self.board[pos] == player for pos in combo):
                return True
        return False

    def get_bot_move(self) -> int:
        """Получить ход бота (случайный)."""
        available_positions = [i for i, cell in enumerate(self.board) if cell == " "]
        return random.choice(available_positions)

    def get_multiplier(self) -> float:
        """Получить множитель выигрыша."""
        return 2.0  # 2x множитель как в плане

    def get_board_display(self) -> str:
        """Получить отображение доски."""
        display = ""
        for i in range(0, 9, 3):
            row = "|".join([self.board[i], self.board[i+1], self.board[i+2]])
            display += row + "\n"
            if i < 6:
                display += "-+-+-\n"
        return display

    def get_state(self) -> dict:
        """Получить текущее состояние игры."""
        return {
            "board": self.board,
            "current_player": self.current_player,
            "game_over": self.game_over,
            "winner": self.winner,
            "is_pve": self.is_pve,
            "is_pvp": self.is_pvp,
        }
