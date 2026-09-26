"""Шашки - PvP игра (placeholder)."""

import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class CheckersModal(discord.ui.Modal, title="Шашки"):
    """Модал для ставки в Шашки."""

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
        """Игра в разработке."""
        await interaction.response.send_message(
            "🚧 Шашки в разработке! Скоро будет доступно.",
            ephemeral=True
        )
