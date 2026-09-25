"""View для игры бутылочка."""

import discord
from discord import app_commands
from discord.ui import Modal, TextInput, View, Button, button
from games.spin_bottle import SpinBottleGame


class SpinBottleBetModal(Modal, title="🍾 Бутылочка"):
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

        if bet < 25:
            await interaction.response.send_message("❌ Минимальная ставка: 25 🪙", ephemeral=True)
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
            title="🍾 Бутылочка",
            description="Крутите бутылочку! Если она укажет на кого-то - вы получаете вызов и выигрываете!",
            color=discord.Color.blue()
        )
        embed.add_field(name="Ставка", value=f"{bet} 🪙", inline=True)

        # Создать view
        view = SpinBottleGameView(self.guild_id, self.user_id, bet)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class SpinBottleGameView(View):
    """View для игры бутылочка."""

    def __init__(self, guild_id: int, user_id: int, bet: int) -> None:
        super().__init__(timeout=300)  # 5 минут на игру
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.game = SpinBottleGame()

    @button(label="🍾 Крутить!", style=discord.ButtonStyle.primary)
    async def spin(self, interaction: discord.Interaction, button: Button) -> None:
        """Крутить бутылочку."""
        from storage.user_balance_store import user_balance_store

        result = self.game.spin()

        if "error" in result:
            await interaction.response.send_message(result["error"], ephemeral=True)
            return

        spin_result = result["result"]
        dare = result["dare"]
        won = result["won"]

        # Создать embed с результатом
        if won:
            color = discord.Color.gold()
            title = "🎉 Указало на вас!"
            description = f"🍾 Бутылочка указала на вас! Ваш вызов: {dare}"
        else:
            color = discord.Color.red()
            title = "😢 Промах!"
            description = f"🍾 Бутылочка указала в воздух! Вы пропустили."

        embed = discord.Embed(
            title=title,
            description=description,
            color=color
        )
        embed.add_field(name="Ставка", value=f"{self.bet} 🪙", inline=True)

        if won:
            winnings = int(self.bet * self.game.get_multiplier())
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)
            embed.add_field(name="Выигрыш", value=f"{winnings} 🪙", inline=False)
            embed.set_footer(text=f"Множитель: {self.game.get_multiplier()}x")
        else:
            embed.add_field(name="Потеряно", value=f"{self.bet} 🪙", inline=False)

        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)
