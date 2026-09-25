"""Виселица - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class HangmanGame:
    """Логика игры Виселица."""

    def __init__(self):
        self.words = [
            "программирование",
            "компьютер",
            "алгоритм",
            "база данных",
            "интернет",
            "разработка",
            "функция",
            "переменная",
            "цикл",
            "массив",
            "класс",
            "объект",
            "интерфейс",
            "библиотека",
            "фреймворк",
        ]

    def get_random_word(self) -> str:
        """Получить случайное слово."""
        return random.choice(self.words)

    def get_masked_word(self, word: str, guessed_letters: set[str]) -> str:
        """Получить слово с загаданными буквами."""
        return " ".join(letter if letter in guessed_letters else "_" for letter in word)


class HangmanModal(discord.ui.Modal, title="Виселица"):
    """Модал для ставки в Виселицу."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id

        self.bet = discord.ui.TextInput(
            label="Ставка (🪙)",
            placeholder="Введите сумму ставки",
            min_length=1,
            max_length=10,
            required=True
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Начать игру с указанной ставкой."""
        try:
            bet = int(self.bet.value)
        except ValueError:
            await interaction.response.send_message(
                "❌ Ставка должна быть числом!",
                ephemeral=True
            )
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        if balance < bet:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. У вас: {balance} 🪙",
                ephemeral=True
            )
            return

        # Проверить лимиты ставок
        if bet < MIN_BET or bet > MAX_BET:
            await interaction.response.send_message(
                f"❌ Ставка должна быть между {MIN_BET} и {MAX_BET} 🪙",
                ephemeral=True
            )
            return

        # Списать ставку
        await user_balance_store.subtract_balance(self.guild_id, self.user_id, bet)

        # Создать сессию игры
        game = HangmanGame()
        word = game.get_random_word()
        masked_word = game.get_masked_word(word, set())

        # Создать embed с виселицей
        embed = discord.Embed(
            title="🎯 Виселица",
            description=f"**Ставка:** {bet} 🪙\n**Множитель:** 2x\n\n**Слово:**\n{masked_word}",
            color=discord.Color.purple()
        )

        embed.add_field(
            name="📝 Ошибки",
            value="0/6",
            inline=False
        )

        embed.add_field(
            name="⏱️ Время",
            value="120 секунд",
            inline=False
        )

        # Создать view для игры
        from views.hangman_view import HangmanView
        view = HangmanView(self.guild_id, self.user_id, bet, word, set())

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
