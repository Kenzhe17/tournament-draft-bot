"""Время реакции - PvP игра (упрощенная PvE версия)."""

import random
import discord
import time
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class ReactionTimeGame:
    """Логика игры Время реакции."""

    def __init__(self):
        self.emojis = ["🎯", "⚡", "🔥", "💥", "⭐"]

    def get_random_emoji(self) -> str:
        """Получить случайный эмодзи."""
        return random.choice(self.emojis)


class ReactionTimeModal(discord.ui.Modal, title="Время реакции"):
    """Модал для ставки в Время реакции."""

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
        game = ReactionTimeGame()
        emoji = game.get_random_emoji()

        # Создать view для игры
        from views.reaction_time_view import ReactionTimeView
        view = ReactionTimeView(self.guild_id, self.user_id, bet, emoji, game)

        await view.start_game(interaction)
