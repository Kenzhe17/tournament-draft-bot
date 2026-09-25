"""Coin flip game view."""

import discord
from discord.ui import Button, Modal, TextInput, View

from games.coin_flip import CoinSide, flip_coin
from models.minigame import MinigameSession
from storage.minigame_store import minigame_store


class CoinBetModal(Modal, title="Введите ставку"):
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
                interaction.guild_id, "coin_flip", interaction.user.id, bet
            )

            if not session:
                await interaction.response.send_message(
                    "❌ Недостаточно монет", ephemeral=True
                )
                return

            # Show game view
            view = CoinFlipGameView(session, is_pve=True)
            embed = discord.Embed(
                title="🦅 Монетка",
                description=f"Ставка: {bet} 🪙\nВыберите: орел или решка!",
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        except ValueError:
            await interaction.response.send_message(
                "❌ Введите корректное число", ephemeral=True
            )


class CoinFlipGameView(View):
    """View for coin flip game."""

    def __init__(self, session: MinigameSession, is_pve: bool = True) -> None:
        super().__init__(timeout=180)
        self.session = session
        self.is_pve = is_pve

    @discord.ui.button(label="🦅 Орел", style=discord.ButtonStyle.primary, custom_id="coin_heads")
    async def heads_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Handle heads choice."""
        await self.handle_choice(interaction, CoinSide.HEADS)

    @discord.ui.button(label="🪙 Решка", style=discord.ButtonStyle.primary, custom_id="coin_tails")
    async def tails_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Handle tails choice."""
        await self.handle_choice(interaction, CoinSide.TAILS)

    async def handle_choice(self, interaction: discord.Interaction, choice: CoinSide) -> None:
        """Handle player choice."""
        if self.is_pve:
            # PvE mode
            bot_choice = flip_coin()
            player_won = choice == bot_choice
            winnings = 0

            if player_won:
                winnings = int(self.session.player1_bet * 2)
                await minigame_store.complete_session(
                    self.session.session_id, interaction.user.id, winnings
                )
                result = f"🎉 Вы победили! Выигрыш: {winnings} 🪙"
                color = discord.Color.green()
            else:
                result = f"😢 Вы проиграли! Потеряно: {self.session.player1_bet} 🪙"
                color = discord.Color.red()

            embed = discord.Embed(
                title="🦅 Монетка",
                description=f"Ваш выбор: {choice.emoji} {choice.name}\n"
                f"Выбор бота: {bot_choice.emoji} {bot_choice.name}\n\n"
                f"{result}",
                color=color,
            )

            self.clear_items()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            # PvP mode
            await interaction.response.send_message(
                "PvP режим в разработке", ephemeral=True
            )
