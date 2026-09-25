"""View для викторин - общий модал для ставок."""

import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class QuizBetModal(discord.ui.Modal):
    """Базовый модал для ставок в викторины."""

    def __init__(self, guild_id: int, user_id: int, game_name: str):
        super().__init__(title=f"Ставка - {game_name}")
        self.guild_id = guild_id
        self.user_id = user_id

        self.bet = discord.ui.TextInput(
            label="Ставка (🪙)",
            placeholder=f"Мин: {MIN_BET}, Макс: {MAX_BET}",
            min_length=1,
            max_length=10,
            required=True
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Проверить ставку и передать дальше."""
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

        # Вернуть ставку для использования в конкретной игре
        await interaction.response.send_message(
            f"✅ Ставка {bet} 🪙 принята!",
            ephemeral=True
        )
