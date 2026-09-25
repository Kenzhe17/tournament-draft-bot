"""Память - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class MemoryGame:
    """Логика игры Память."""

    def __init__(self):
        self.emojis = ["🍎", "🍊", "🍋", "🍇", "🍓", "🍒", "🥝", "🍑", "🥭", "🍍", "🥥", "🍌"]

    def generate_sequence(self, length: int = 5) -> list[str]:
        """Сгенерировать последовательность эмодзи."""
        return [random.choice(self.emojis) for _ in range(length)]


class MemoryModal(discord.ui.Modal, title="Память"):
    """Модал для ставки в Память."""

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
        game = MemoryGame()
        sequence = game.generate_sequence(5)

        # Создать embed с последовательностью
        embed = discord.Embed(
            title="🧠 Память",
            description=f"**Ставка:** {bet} 🪙\n**Множитель:** 2x\n\n**Запомните последовательность:**\n{' '.join(sequence)}",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="⏱️ Время",
            value="У вас есть 5 секунд чтобы запомнить!",
            inline=False
        )

        # Создать view для начала теста
        from views.memory_view import MemoryView
        view = MemoryView(self.guild_id, self.user_id, bet, sequence)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
