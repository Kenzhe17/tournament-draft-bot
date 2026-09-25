"""View для игры колесо фортуны."""

import discord
from discord import app_commands
from discord.ui import Modal, TextInput, View, Button, button
from games.wheel import WheelGame


class WheelBetModal(Modal, title="🎡 Колесо фортуны"):
    """Modal для ввода ставки."""

    bet = TextInput(label="Ставка (монеты)", placeholder="Введите сумму ставки", min_length=1, max_length=10)

    def __init__(self, guild_id: int, user_id: int) -> None:
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Обработать отправку формы."""
        from storage.user_balance_store import user_balance_store

        try:
            bet = int(self.bet.value)
        except ValueError:
            await interaction.response.send_message("❌ Введите корректную ставку!", ephemeral=True)
            return

        if bet < 20:
            await interaction.response.send_message("❌ Минимальная ставка: 20 🪙", ephemeral=True)
            return

        if bet > 500:
            await interaction.response.send_message("❌ Максимальная ставка: 500 🪙", ephemeral=True)
            return

        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        if balance < bet:
            await interaction.response.send_message("❌ Недостаточно монет!", ephemeral=True)
            return

        # Списать ставку
        await user_balance_store.add_balance(self.guild_id, self.user_id, -bet)

        # Создать embed
        embed = discord.Embed(
            title="🎡 Колесо фортуны",
            description="Нажмите кнопку чтобы крутить колесо!",
            color=discord.Color.gold()
        )
        embed.add_field(name="Ставка", value=f"{bet} 🪙", inline=True)

        # Показать сектора
        sectors_text = "\n".join([
            f"{s['emoji']} {s['name']} - {s['multiplier']}x"
            for s in WheelGame.SECTORS
        ])
        embed.add_field(name="Сектора", value=sectors_text, inline=False)

        # Создать view
        view = WheelGameView(self.guild_id, self.user_id, bet)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class WheelGameView(View):
    """View для игры колесо фортуны."""

    def __init__(self, guild_id: int, user_id: int, bet: int) -> None:
        super().__init__(timeout=300)  # 5 минут на игру
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.game = WheelGame()

    @button(label="🎰 Крутить!", style=discord.ButtonStyle.primary)
    async def spin(self, interaction: discord.Interaction, button: Button) -> None:
        """Крутить колесо."""
        from storage.user_balance_store import user_balance_store

        result = self.game.spin()

        if "error" in result:
            await interaction.response.send_message(result["error"], ephemeral=True)
            return

        sector = result["sector"]
        multiplier = result["multiplier"]

        # Создать embed с результатом
        if multiplier == 0.0:
            color = discord.Color.red()
            title = "💀 БАНКРОТ!"
            description = f"😢 Вы потеряли всю ставку!"
        elif multiplier >= 5.0:
            color = discord.Color.gold()
            title = f"🌟 {sector['name']}!"
            description = f"🎉 Отличный выигрыш!"
        else:
            color = discord.Color.blue()
            title = f"{sector['emoji']} {sector['name']}!"
            description = f"Хороший результат!"

        embed = discord.Embed(
            title=title,
            description=description,
            color=color
        )
        embed.add_field(name="Ставка", value=f"{self.bet} 🪙", inline=True)
        embed.add_field(name="Множитель", value=f"{multiplier}x", inline=True)

        if multiplier > 0:
            winnings = int(self.bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)
            embed.add_field(name="Выигрыш", value=f"{winnings} 🪙", inline=False)
        else:
            embed.add_field(name="Потеряно", value=f"{self.bet} 🪙", inline=False)

        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)
