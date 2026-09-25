"""Guess color game view."""

import discord
from discord.ui import Button, Modal, TextInput, View

from games.guess_color import Color, random_color
from models.minigame import MinigameSession
from storage.minigame_store import minigame_store


class ColorBetModal(Modal, title="Введите ставку"):
    """Modal for entering bet amount."""

    bet = TextInput(label="Ставка (монет)", placeholder="100", required=True)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Handle bet submission."""
        try:
            bet = int(self.bet.value)
            if bet < 10:
                await interaction.response.send_message(
                    "❌ Минимальная ставка: 10 монет", ephemeral=True
                )
                return

            session = await minigame_store.create_session(
                interaction.guild_id, "guess_color", interaction.user.id, bet
            )

            if not session:
                await interaction.response.send_message(
                    "❌ Недостаточно монет", ephemeral=True
                )
                return

            view = GuessColorGameView(session)
            embed = discord.Embed(
                title="🎨 Угадай цвет",
                description=f"Ставка: {bet} 🪙\nУгадайте цвет!",
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        except ValueError:
            await interaction.response.send_message(
                "❌ Введите корректное число", ephemeral=True
            )


class GuessColorGameView(View):
    """View for guess color game."""

    def __init__(self, session: MinigameSession) -> None:
        super().__init__(timeout=180)
        self.session = session

    @discord.ui.button(label="🔴 Красный", style=discord.ButtonStyle.danger, custom_id="color_red")
    async def red_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Handle red choice."""
        await self.handle_choice(interaction, Color.RED)

    @discord.ui.button(label="🔵 Синий", style=discord.ButtonStyle.primary, custom_id="color_blue")
    async def blue_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Handle blue choice."""
        await self.handle_choice(interaction, Color.BLUE)

    @discord.ui.button(label="🟢 Зелёный", style=discord.ButtonStyle.success, custom_id="color_green")
    async def green_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Handle green choice."""
        await self.handle_choice(interaction, Color.GREEN)

    async def handle_choice(self, interaction: discord.Interaction, choice: Color) -> None:
        """Handle player choice."""
        bot_color = random_color()
        player_won = choice == bot_color
        winnings = 0

        if player_won:
            winnings = int(self.session.player1_bet * 3)
            await minigame_store.complete_session(
                self.session.session_id, interaction.user.id, winnings
            )
            result = f"🎉 Вы победили! Выигрыш: {winnings} 🪙"
            color = discord.Color.green()
        else:
            result = f"😢 Вы проиграли! Потеряно: {self.session.player1_bet} 🪙"
            color = discord.Color.red()

        embed = discord.Embed(
            title="🎨 Угадай цвет",
            description=f"Ваш выбор: {choice.emoji} {choice.name}\n"
            f"Загаданный цвет: {bot_color.emoji} {bot_color.name}\n\n"
            f"{result}",
            color=color,
        )

        self.clear_items()
        await interaction.response.edit_message(embed=embed, view=self)
