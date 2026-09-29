from config import replace_emojis
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
    
    def __init__(self, guild_id: int, tournament, match_index: int, team_index: int, team_name: str, match_type: str, team_a_name: str, team_b_name: str):
        super().__init__()
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_index = match_index
        self.team_index = team_index
        self.team_name = team_name
        self.match_type = match_type
        self.team_a_name = team_a_name
        self.team_b_name = team_b_name
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Handle modal submission."""
        try:
            amount = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message(
                replace_emojis("❌ Пожалуйста, введите корректное число."),
                ephemeral=True
            )
            return
        
        if amount <= 0:
            await interaction.response.send_message(
                replace_emojis("❌ Сумма должна быть положительным числом."),
                ephemeral=True
            )
            return
        
        try:
            # Check if betting is open for this phase
            if not self.tournament.is_betting_open() or self.tournament.betting_phase != self.match_type:
                await interaction.response.send_message(
                    replace_emojis("❌ Ставки закрыты."),
                    ephemeral=True
                )
                return
            
            # Check user balance
            balance = await user_balance_store.get_balance(self.guild_id, interaction.user.id)
            if balance < amount:
                await interaction.response.send_message(
                    replace_emojis("❌ Недостаточно средств. Ваш баланс: {balance} 💰"),
                    ephemeral=True
                )
                return
            
            # Check if user is playing in the match
            user_team_index = self._get_user_team_index(interaction.user.id)
            if user_team_index is not None:
                if user_team_index != self.team_index:
                    await interaction.response.send_message(
                        replace_emojis("❌ Вы не можете ставить против своей команды."),
                        ephemeral=True
                    )
                    return
            
            # Get match ID early for bet checking
            match_id = f"{self.match_type}_{self.match_index}"

            # Check if user already has a bet on this match
            existing_bet = await bet_store.get_user_bet(self.guild_id, interaction.user.id, match_id)
            if existing_bet:
                # User already has a bet - only deduct additional amount (difference)
                if amount <= existing_bet.amount:
                    await interaction.response.send_message(
                        replace_emojis(f"❌ Сумма должна быть больше текущей ставки ({existing_bet.amount} 💰)."),
                        ephemeral=True
                    )
                    return
                additional_amount = amount - existing_bet.amount
                await user_balance_store.subtract_balance(self.guild_id, interaction.user.id, additional_amount)
            else:
                # New bet - deduct full amount
                await user_balance_store.subtract_balance(self.guild_id, interaction.user.id, amount)

            # Get match teams for odds initialization
            if self.match_type == "qualifiers":
                match = self.tournament.qualifier_matches[self.match_index]
            elif self.match_type == "semifinals":
                match = self.tournament.semifinal_matches[self.match_index]
            elif self.match_type == "final":
                match = self.tournament.final_teams
            else:
                return

            # Initialize odds if not already done
            from models.bet import Bet
            current_odds = bet_store.get_current_odds(match_id)

            if not current_odds:
                # Initialize odds based on ELO
                from utils.embeds import get_team_avg_elo
                team_a_data = self.tournament.teams[match[0]] if match[0] < len(self.tournament.teams) else {}
                team_b_data = self.tournament.teams[match[1]] if match[1] < len(self.tournament.teams) else {}
                avg_elo_a = await get_team_avg_elo(team_a_data, self.tournament)
                avg_elo_b = await get_team_avg_elo(team_b_data, self.tournament)
                bet_store.initialize_match_odds(match_id, self.team_a_name, self.team_b_name, avg_elo_a, avg_elo_b)

            # Create bet (odds will be set dynamically in save_bet)
            bet = Bet(
                guild_id=self.guild_id,
                user_id=interaction.user.id,
                user_name=interaction.user.display_name,
                match_id=match_id,
                team_name=self.team_name,
                amount=amount,  # Full amount
                odds=0.0  # Will be set in save_bet
            )
            # Get the odds BEFORE saving (so we show the odds user actually bet on)
            current_odds = bet_store.get_current_odds(match_id)
            if self.team_name == self.team_a_name:
                actual_odds = current_odds.team_a_odds if current_odds else 1.9
            else:
                actual_odds = current_odds.team_b_odds if current_odds else 1.9

            # Save the bet (this will update odds)
            await bet_store.save_bet(bet, self.team_a_name, self.team_b_name)
            
            # Update tournament message
            from bot import TournamentBot
            bot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, self.tournament)
            
            await interaction.response.send_message(
                replace_emojis(f"✅ Ставка добавлена\n\n{amount} 💰 → {self.team_name} `({actual_odds:.2f}x)`"),
                ephemeral=True
            )
        except Exception as e:
            import logging
            logging.error(f"Error placing bet: {e}", exc_info=True)
            await interaction.response.send_message(
                replace_emojis(f"❌ Ошибка при создании ставки: {str(e)}"),
                ephemeral=True
            )
    
    def _get_user_team_index(self, user_id: int) -> int | None:
        """Get the team index if user is participating in this match, or None otherwise."""
        # Get teams in this match
        if self.match_type == "qualifiers":
            match = self.tournament.qualifier_matches[self.match_index]
        elif self.match_type == "semifinals":
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
