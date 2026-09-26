"""Слово дня (Wordle) - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class WordleGame:
    """Логика игры Слово дня."""

    def __init__(self):
        self.words = [
            "Мир", "Дом", "Сад", "Лес", "Река", "Гора", "Море", "Небо",
            "Солнце", "Луна", "Звезда", "Снег", "Дождь", "Ветер", "Огонь",
            "Вода", "Земля", "Воздух", "Камень", "Дерево", "Цветок", "Птица",
            "Рыба", "Зверь", "Человек", "Рука", "Нога", "Глаз", "Ухо",
            "Рот", "Нос", "Волосы", "Кожа", "Кровь", "Кость", "Мышца",
            "Сердце", "Мозг", "Душа", "Дух", "Тело", "Жизнь", "Смерть",
            "Рождение", "Любовь", "Ненависть", "Радость", "Грусть", "Страх",
            "Удача", "Успех", "Провал", "Победа", "Поражение", "Война", "Мир",
        ]

    def get_random_word(self) -> str:
        """Получить случайное слово."""
        return random.choice(self.words)


class WordleModal(discord.ui.Modal, title="Слово дня"):
    """Модал для ставки в Слово дня."""

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
        game = WordleGame()
        word = game.get_random_word()

        # Создать view для игры
        from views.wordle_view import WordleView
        view = WordleView(self.guild_id, self.user_id, bet, word, game)

        await view.show_word(interaction)
