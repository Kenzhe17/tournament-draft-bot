"""Modal for entering bet amount."""

import discord
from discord.ui import Modal, TextInput
from storage.bet_store import bet_store
from storage.user_balance_store import user_balance_store
from storage.json_store import store


class BetAmountModal(Modal, title="Введите сумму ставки"):
    """Modal for entering bet amount."""
    
    amount = TextInput(
        label="Сумма ставки",
        placeholder="Введите сумму (например: 100)",
        min_length=1,
        max_length=10,
    )
    
    def __init__(self, guild_id: int, tournament, match_index: int, team_index: int, team_name: str, match_type: str):
        super().__init__()
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_index = match_index
        self.team_index = team_index
        self.team_name = team_name
        self.match_type = match_type
        self.amount.label = f"Сумма ставки на {team_name}"
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Handle modal submission."""
        try:
            amount = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message(
                "❌ Пожалуйста, введите корректное число.",
                ephemeral=True
            )
            return
        
        if amount <= 0:
            await interaction.response.send_message(
                "❌ Сумма должна быть положительным числом.",
                ephemeral=True
            )
            return
        
        try:
            # Check if betting is open
            if not self.tournament.betting_open:
                await interaction.response.send_message(
                    "❌ Ставки закрыты.",
                    ephemeral=True
                )
                return
            
            # Check user balance
            balance = await user_balance_store.get_balance(self.guild_id, interaction.user.id)
            if balance < amount:
                await interaction.response.send_message(
                    f"❌ Недостаточно средств. Ваш баланс: {balance} 🪙",
                    ephemeral=True
                )
                return
            
            # Check if user is playing in the match
            user_team_index = self._get_user_team_index(interaction.user.id)
            if user_team_index is not None:
                if user_team_index != self.team_index:
                    await interaction.response.send_message(
                        "❌ Вы не можете ставить против своей команды.",
                        ephemeral=True
                    )
                    return
            
            # Deduct balance
            await user_balance_store.subtract_balance(self.guild_id, interaction.user.id, amount)
            
            # Create and save bet
            from models.bet import Bet
            match_id = f"{self.match_type}_{self.match_index}"
            bet = Bet(
                guild_id=self.guild_id,
                user_id=interaction.user.id,
                user_name=interaction.user.display_name,
                match_id=match_id,
                team_name=self.team_name,
                amount=amount
            )
            await bet_store.save_bet(bet)
            
            # Update tournament message
            from bot import TournamentBot
            bot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, self.tournament)
            
            await interaction.response.send_message(
                f"✅ Ставка принята\n\n{amount} 🪙 → {self.team_name}",
                ephemeral=True
            )
        except Exception as e:
            import logging
            logging.error(f"Error placing bet: {e}", exc_info=True)
            await interaction.response.send_message(
                f"❌ Ошибка при создании ставки: {str(e)}",
                ephemeral=True
            )
    
    def _get_user_team_index(self, user_id: int) -> int | None:
        """Get the team index if user is participating in this match, or None otherwise."""
        # Get teams in this match
        if self.match_type == "qualifier":
            match = self.tournament.qualifier_matches[self.match_index]
        elif self.match_type == "semifinal":
            match = self.tournament.semifinal_matches[self.match_index]
        elif self.match_type == "final":
            match = self.tournament.final_teams
        else:
            return None
        
        # Check if user is in any of the teams
        for team_index in match:
            team = self.tournament.teams[team_index] if team_index < len(self.tournament.teams) else {}
            for circle in range(1, 5):
                player = team.get(f"circle{circle}")
                if player:
                    player_user_id = self.tournament.player_user_ids.get(player, 0)
                    if player_user_id == user_id:
                        return team_index
        return None
