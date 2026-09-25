"""Анаграммы - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class AnagramsGame:
    """Логика игры Анаграммы."""

    def __init__(self):
        self.words = [
            {"word": "программа", "hint": "Компьютерное"},
            {"word": "алгоритм", "hint": "Последовательность действий"},
            {"word": "функция", "hint": "В программировании"},
            {"word": "переменная", "hint": "Хранит данные"},
            {"word": "библиотека", "hint": "Код для повторного использования"},
            {"word": "интерфейс", "hint": "Связь между системами"},
            {"word": "разработка", "hint": "Создание программ"},
            {"word": "структура", "hint": "Организация данных"},
            {"word": "последовательность", "hint": "Порядок элементов"},
            {"word": "компиляция", "hint": "Преобразование кода"},
        ]

    def get_random_anagram(self) -> dict:
        """Получить случайную анаграмму."""
        word_data = random.choice(self.words)
        word = word_data["word"]
        hint = word_data["hint"]

        # Перемешать буквы
        letters = list(word)
        random.shuffle(letters)
        anagram = "".join(letters)

        return {
            "word": word,
            "anagram": anagram,
            "hint": hint
        }


class AnagramsModal(discord.ui.Modal, title="Анаграммы"):
    """Модал для ставки в Анаграммы."""

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
        game = AnagramsGame()
        anagram_data = game.get_random_anagram()
        word = anagram_data["word"]
        anagram = anagram_data["anagram"]
        hint = anagram_data["hint"]

        # Создать embed с анаграммой
        embed = discord.Embed(
            title="🔤 Анаграммы",
            description=f"**Ставка:** {bet} 🪙\n**Множитель:** 3x\n\n**Соберите слово:**\n{anagram}",
            color=discord.Color.green()
        )

        embed.add_field(
            name="💡 Подсказка",
            value=hint,
            inline=False
        )

        embed.add_field(
            name="⏱️ Время",
            value="60 секунд",
            inline=False
        )

        # Создать view для ответа
        from views.anagrams_view import AnagramsView
        view = AnagramsView(self.guild_id, self.user_id, bet, word, anagram)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
