"""Бутылочка - PvP игра (в разработке)."""

import discord


class SpinBottleModal(discord.ui.Modal, title="Бутылочка"):
    """Модал для игры Бутылочка."""

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
        """Показать сообщение в разработке."""
        await interaction.response.send_message(
            "🚧 Игра 'Бутылочка' в разработке. Скоро будет доступна!",
            ephemeral=True
        )
