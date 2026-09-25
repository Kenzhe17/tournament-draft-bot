"""Rock-Paper-Scissors game view."""

import discord
from discord.ui import Button, Modal, TextInput, View

from games.rps_game import RPSChoice, determine_winner, get_bot_choice
from models.minigame import MinigameSession
from storage.minigame_store import minigame_store


class BetModal(Modal, title="Введите ставку"):
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

            # Create session
            session = await minigame_store.create_session(
                interaction.guild_id, "rps", interaction.user.id, bet
            )

            if not session:
                await interaction.response.send_message(
                    "❌ Недостаточно монет", ephemeral=True
                )
                return

            # Show game view
            view = RPSGameView(session, is_pve=True)
            embed = discord.Embed(
                title="🪨 Камень-Ножницы-Бумага",
                description=f"Ставка: {bet} 🪙\nВыберите ваш ход!",
                color=discord.Color.dark_blue(),
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        except ValueError:
            await interaction.response.send_message(
                "❌ Введите корректное число", ephemeral=True
            )


class RPSGameView(View):
    """View for RPS game."""

    def __init__(self, session: MinigameSession, is_pve: bool = True) -> None:
        super().__init__(timeout=180)
        self.session = session
        self.is_pve = is_pve

    @discord.ui.button(label="🪨 Камень", style=discord.ButtonStyle.primary, custom_id="rps_rock")
    async def rock_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Handle rock choice."""
        await self.handle_choice(interaction, RPSChoice.ROCK)

    @discord.ui.button(label="📄 Бумага", style=discord.ButtonStyle.primary, custom_id="rps_paper")
    async def paper_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Handle paper choice."""
        await self.handle_choice(interaction, RPSChoice.PAPER)

    @discord.ui.button(label="✂️ Ножницы", style=discord.ButtonStyle.primary, custom_id="rps_scissors")
    async def scissors_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Handle scissors choice."""
        await self.handle_choice(interaction, RPSChoice.SCISSORS)

    async def handle_choice(self, interaction: discord.Interaction, choice: RPSChoice) -> None:
        """Handle player choice."""
        if self.is_pve:
            # PvE mode
            bot_choice = get_bot_choice()
            winner = determine_winner(choice, bot_choice)

            player_won = winner == 1
            winnings = 0

            if player_won:
                winnings = int(self.session.player1_bet * 2)
                await minigame_store.complete_session(
                    self.session.session_id, interaction.user.id, winnings
                )
                result = f"🎉 Вы победили! Выигрыш: {winnings} 🪙"
                color = discord.Color.green()
            elif winner is None:
                # Tie - return bet
                await minigame_store.complete_session(
                    self.session.session_id, interaction.user.id, self.session.player1_bet
                )
                result = "🤝 Ничья! Ставка возвращена."
                color = discord.Color.yellow()
            else:
                result = f"😢 Вы проиграли! Потеряно: {self.session.player1_bet} 🪙"
                color = discord.Color.red()

            embed = discord.Embed(
                title="🪨 Камень-Ножницы-Бумага",
                description=f"Ваш выбор: {choice.emoji} {choice.name}\n"
                f"Выбор бота: {bot_choice.emoji} {bot_choice.name}\n\n"
                f"{result}",
                color=color,
            )

            self.clear_items()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            # PvP mode - wait for second player
            self.session.player2_id = interaction.user.id
            # This would need additional logic for PvP
            await interaction.response.send_message(
                "PvP режим в разработке", ephemeral=True
            )
