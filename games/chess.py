"""Шахматы - PvP игра (placeholder)."""

import discord


class ChessModal(discord.ui.Modal, title="Шахматы"):
    """Модал для ставки в Шахматы."""

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
            "🚧 Шахматы в разработке! Скоро будет доступно.",
            ephemeral=True
        )
