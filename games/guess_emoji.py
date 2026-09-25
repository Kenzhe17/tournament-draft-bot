"""Игра в угадай эмодзи."""

import random
from typing import Literal


class GuessEmojiGame:
    """Логика игры в угадай эмодзи."""

    # База эмодзи с категориями
    EMOJI_DATABASE = {
        # Животные
        "🐕": {"category": "животное", "hint": "Лучший друг человека"},
        "🐈": {"category": "животное", "hint": "Любит молоко и спит"},
        "🦁": {"category": "животное", "hint": "Царь зверей"},
        "🐘": {"category": "животное", "hint": "Имеет длинный хобот"},
        "🐻": {"category": "животное", "hint": "Любит мёд и спит зимой"},
        "🐰": {"category": "животное", "hint": "Прыгает и любит морковку"},
        "🦊": {"category": "животное", "hint": "Хитрая рыжая"},
        "🐼": {"category": "животное", "hint": "Чёрно-белый из Китая"},
        "🐯": {"category": "животное", "hint": "Полосатый хищник"},
        "🦒": {"category": "животное", "hint": "Имеет очень длинную шею"},

        # Еда
        "🍕": {"category": "еда", "hint": "Итальянский вкус с сыром"},
        "🍔": {"category": "еда", "hint": "Фастфуд с котлетой"},
        "🍟": {"category": "еда", "hint": "Жареные палочки"},
        "🍩": {"category": "еда", "hint": "Сладкая бублик с глазурью"},
        "🍦": {"category": "еда", "hint": "Холодное лакомство"},
        "🍪": {"category": "еда", "hint": "Сладкое с шоколадом"},
        "🍎": {"category": "еда", "hint": "Яблоко"},
        "🍌": {"category": "еда", "hint": "Жёлтый и длинный"},
        "🍇": {"category": "еда", "hint": "Много маленьких ягод"},
        "🍉": {"category": "еда", "hint": "Большая и красная снаружи"},

        # Предметы
        "⚽": {"category": "предмет", "hint": "Популярный в спорте"},
        "🎮": {"category": "предмет", "hint": "Для видеоигр"},
        "🎵": {"category": "предмет", "hint": "Музыкальная нота"},
        "📱": {"category": "предмет", "hint": "Телефон в кармане"},
        "💻": {"category": "предмет", "hint": "Ноутбук"},
        "🎸": {"category": "предмет", "hint": "Струнный музыкальный инструмент"},
        "🎨": {"category": "предмет", "hint": "Для рисования"},
        "📚": {"category": "предмет", "hint": "Книги"},
        "🎁": {"category": "предмет", "hint": "Подарок"},
        "🏆": {"category": "предмет", "hint": "Награда за победу"},
    }

    def __init__(self, secret_emoji: str | None = None) -> None:
        """Инициализировать игру."""
        self.secret_emoji = secret_emoji if secret_emoji is not None else random.choice(list(self.EMOJI_DATABASE.keys()))
        self.attempts_left = 3
        self.game_over = False
        self.won = False
        self.hints_given = 0

    def get_hints(self) -> list[str]:
        """Получить подсказки."""
        emoji_data = self.EMOJI_DATABASE[self.secret_emoji]
        hints = [emoji_data["category"], emoji_data["hint"]]

        # Дополнительные подсказки
        if self.secret_emoji in ["🐕", "🐈", "🦁", "🐘", "🐻", "🐰", "🦊", "🐼", "🐯", "🦒"]:
            hints.append("Это животное")
        elif self.secret_emoji in ["🍕", "🍔", "🍟", "🍩", "🍦", "🍪", "🍎", "🍌", "🍇", "🍉"]:
            hints.append("Это еда")
        else:
            hints.append("Это предмет")

        return hints

    def make_guess(self, guess: str) -> tuple[Literal["correct", "incorrect", "game_over"], str]:
        """Сделать попытку угадать эмодзи.

        Returns:
            (result, message) - результат и сообщение для игрока
        """
        if self.game_over:
            return "game_over", "Игра уже завершена!"

        self.attempts_left -= 1

        if guess == self.secret_emoji:
            self.game_over = True
            self.won = True
            return "correct", f"🎉 Поздравляем! Вы угадали эмодзи {self.secret_emoji}!"

        if self.attempts_left <= 0:
            self.game_over = True
            return "game_over", f"😢 Игра окончена! Загаданный эмодзи был {self.secret_emoji}."

        # Дать подсказку
        hints = self.get_hints()
        hint_text = hints[min(self.hints_given, len(hints) - 1)]
        self.hints_given += 1

        return "incorrect", f"❌ Неправильно! Подсказка: {hint_text}. Попыток осталось: {self.attempts_left}"

    def get_multiplier(self) -> float:
        """Получить множитель выигрыша."""
        return 3.0  # 3x множитель как в плане

    def get_state(self) -> dict:
        """Получить текущее состояние игры."""
        return {
            "secret_emoji": self.secret_emoji,
            "attempts_left": self.attempts_left,
            "game_over": self.game_over,
            "won": self.won,
            "hints_given": self.hints_given,
        }
