"""Угадай слово - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class WordGuessGame:
    """Логика игры Угадай слово."""

    def __init__(self):
        self.words = [
            {
                "word": "яблоко",
                "hints": ["Это фрукт", "Красный или зелёный", "Растёт на дереве"]
            },
            {
                "word": "солнце",
                "hints": ["Это в небе", "Светит днём", "Горячее"]
            },
            {
                "word": "книга",
                "hints": ["Это можно читать", "Много страниц", "На полке"]
            },
            {
                "word": "музыка",
                "hints": ["Можно слушать", "Есть ритм", "Создают инструменты"]
            },
            {
                "word": "вода",
                "hints": ["Жидкость", "Без неё жизнь невозможна", "Прозрачная"]
            },
            {
                "word": "компьютер",
                "hints": ["Электронное устройство", "С клавиатурой", "Для работы и игр"]
            },
            {
                "word": "велосипед",
                "hints": ["Транспорт", "На двух колёсах", "Нужно крутить педали"]
            },
            {
                "word": "кофе",
                "hints": ["Напиток", "Тёмный", "Утром бодрит"]
            },
            {
                "word": "дом",
                "hints": ["Жилище", "Есть крыша", "Там живут люди"]
            },
            {
                "word": "море",
                "hints": ["Вода", "Солёная", "Пляж"]
            },
        ]

    def get_random_word(self) -> dict:
        """Получить случайное слово с подсказками."""
        return random.choice(self.words)


class WordGuessModal(discord.ui.Modal, title="Угадай слово"):
    """Модал для ставки в Угадай слово."""

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
        game = WordGuessGame()
        word_data = game.get_random_word()
        word = word_data["word"]
        hints = word_data["hints"]

        # Создать embed с подсказками
        embed = discord.Embed(
            title="📝 Угадай слово",
            description=f"**Ставка:** {bet} 🪙\n**Множитель:** 4x\n\n**Подсказки:**",
            color=discord.Color.blue()
        )

        for i, hint in enumerate(hints, 1):
            embed.add_field(
                name=f"Подсказка {i}",
                value=hint,
                inline=False
            )

        embed.add_field(
            name="⏱️ Время",
            value="60 секунд на ответ",
            inline=False
        )

        # Создать view для ответа
        from views.word_guess_view import WordGuessView
        view = WordGuessView(self.guild_id, self.user_id, bet, word, hints)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
