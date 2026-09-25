"""Dice roll game view."""

import discord
from discord.ui import Button, Modal, TextInput, View

from games.dice_roll import roll_dice
from models.minigame import MinigameSession
from storage.minigame_store import minigame_store


class DiceBetModal(Modal, title="Введите ставку"):
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
                interaction.guild_id, "dice_roll", interaction.user.id, bet
            )

            if not session:
                await interaction.response.send_message(
                    "❌ Недостаточно монет", ephemeral=True
                )
                return

            view = DiceRollGameView(session)
            embed = discord.Embed(
                title="🎲 Кубик",
                description=f"Ставка: {bet} 🪙\nНажмите чтобы бросить кубик!",
                color=discord.Color.blue(),
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        except ValueError:
            await interaction.response.send_message(
                "❌ Введите корректное число", ephemeral=True
            )


class DiceRollGameView(View):
    """View for dice roll game."""

    def __init__(self, session: MinigameSession) -> None:
        super().__init__(timeout=180)
        self.session = session

    @discord.ui.button(label="🎲 Бросить", style=discord.ButtonStyle.primary, custom_id="dice_roll")
    async def roll_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Handle roll."""
        player_roll = roll_dice()
        bot_roll = roll_dice()

        if player_roll > bot_roll:
            winnings = int(self.session.player1_bet * 2)
            await minigame_store.complete_session(
                self.session.session_id, interaction.user.id, winnings
            )
            result = f"🎉 Вы победили! ({player_roll} > {bot_roll}) Выигрыш: {winnings} 🪙"
            color = discord.Color.green()
        elif player_roll < bot_roll:
            result = f"😢 Вы проиграли! ({player_roll} < {bot_roll}) Потеряно: {self.session.player1_bet} 🪙"
            color = discord.Color.red()
        else:
            # Tie - return bet
            await minigame_store.complete_session(
                self.session.session_id, interaction.user.id, self.session.player1_bet
            )
            result = f"🤝 Ничья! ({player_roll} = {bot_roll}) Ставка возвращена."
            color = discord.Color.yellow()

        embed = discord.Embed(
            title="🎲 Кубик",
            description=f"Ваш бросок: {player_roll}\nБот бросок: {bot_roll}\n\n{result}",
            color=color,
        )

        self.clear_items()
        await interaction.response.edit_message(embed=embed, view=self)
