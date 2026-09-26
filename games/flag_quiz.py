"""Угадай флаг - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class FlagQuizGame:
    """Логика игры Угадай флаг."""

    def __init__(self):
        self.flags = [
            {"country": "Россия", "emoji": "🇷🇺", "continent": "Европа/Азия"},
            {"country": "США", "emoji": "🇺🇸", "continent": "Северная Америка"},
            {"country": "Германия", "emoji": "🇩🇪", "continent": "Европа"},
            {"country": "Франция", "emoji": "🇫🇷", "continent": "Европа"},
            {"country": "Япония", "emoji": "🇯🇵", "continent": "Азия"},
            {"country": "Бразилия", "emoji": "🇧🇷", "continent": "Южная Америка"},
            {"country": "Китай", "emoji": "🇨🇳", "continent": "Азия"},
            {"country": "Италия", "emoji": "🇮🇹", "continent": "Европа"},
            {"country": "Великобритания", "emoji": "🇬🇧", "continent": "Европа"},
            {"country": "Канада", "emoji": "🇨🇦", "continent": "Северная Америка"},
        ]

    def get_random_flag(self) -> dict:
        """Получить случайный флаг."""
        return random.choice(self.flags)


class FlagQuizModal(discord.ui.Modal, title="Угадай флаг"):
    """Модал для ставки в Угадай флаг."""

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
        game = FlagQuizGame()
        flag_data = game.get_random_flag()

        # Создать view для игры
        from views.flag_quiz_view import FlagQuizView
        view = FlagQuizView(self.guild_id, self.user_id, bet, flag_data, game)

        await view.show_flag(interaction)
