from config import replace_emojis, get_emoji
"""Modal for entering bet amount."""

import discord
from discord.ui import Modal, TextInput

from storage.bet_store import bet_store
from storage.json_store import store


class BetAmountModal(Modal, title="Сумма ставки"):
    """Modal for entering bet amount."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int, team_name: str, team_a_name: str, team_b_name: str):
        super().__init__()
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_name = team_name
        self.team_a_name = team_a_name
        self.team_b_name = team_b_name

        self.amount = TextInput(
            label="Сумма ставки",
            placeholder="Введите сумму",
            required=True,
            max_length=10
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        amount_str = self.amount.value
        try:
            amount = int(amount_str)
        except ValueError:
            await interaction.response.send_message(
                replace_emojis("❌ Некорректная сумма. Введите число."),
                ephemeral=True
            )
            return

        if amount <= 0:
            await interaction.response.send_message(
                replace_emojis("❌ Сумма должна быть больше 0."),
                ephemeral=True
            )
            return

        # Get user balance
        from storage.user_balance_store import user_balance_store
        balance = await user_balance_store.get_balance(self.guild_id, interaction.user.id)

        if amount > balance:
            await interaction.response.send_message(
                replace_emojis(f"❌ Недостаточно средств. Ваш баланс: {balance} {get_emoji('money')}"),
                ephemeral=True
            )
            return

        # Check if user is participating in this match
        user_team_index = self._get_user_team_index(interaction.user.id)
        if user_team_index is not None:
            # User is on one of the teams - check if betting against own team
            if (self.team_name == self.team_a_name and user_team_index == 1) or \
               (self.team_name == self.team_b_name and user_team_index == 0):
                await interaction.response.send_message(
                    replace_emojis("❌ Нельзя ставить против своей команды!"),
                    ephemeral=True
                )
                return

        # Check if user already has a bet in this phase
        from storage.bets_store import bets_store
        existing_bet = await bets_store.get_user_bet_in_phase(
            self.guild_id,
            interaction.user.id,
            self.tournament.id,
            self.match_type
        )
        if existing_bet:
            await interaction.response.send_message(
                replace_emojis("❌ Можно ставить только 1 раз за этап!"),
                ephemeral=True
            )
            return

        # Get match_id
        match_id = f"{self.match_type}_{self.match_index}"

        # Get current odds
        odds_data = await bet_store.get_match_odds(self.guild_id, match_id)
        if odds_data:
            if self.team_name == self.team_a_name:
                actual_odds = odds_data.team_a_odds
            else:
                actual_odds = odds_data.team_b_odds
        else:
            actual_odds = 1.9

        # Create bet
        from models.bet import Bet
        bet = Bet(
            guild_id=self.guild_id,
            user_id=interaction.user.id,
            user_name=interaction.user.display_name,
            tournament_id=self.tournament.id,
            match_id=match_id,
            team_name=self.team_name,
            team_index=0 if self.team_name == self.team_a_name else 1,
            amount=amount,
            odds=actual_odds
        )

        try:
            import logging
            logging.info(f"Placing bet: match_id={match_id}, team_name='{self.team_name}', amount={amount}, odds={actual_odds}, match_type={self.match_type}")

            # Save the bet (this will update odds)
            await bet_store.save_bet(bet, self.team_a_name, self.team_b_name)

            # Update tournament message
            from bot import TournamentBot
            bot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, self.tournament)

            await interaction.response.send_message(
                replace_emojis(f"✅ Ставка добавлена\n\n{amount} ") + get_emoji('money') + f" → {self.team_name} `({actual_odds:.2f}x)`"),
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
        elif self.match_type == "semifinal":
            match = self.tournament.semifinal_matches[self.match_index]
        else:  # final
            match = (self.tournament.final_teams[0], self.tournament.final_teams[1])

        team_a_index, team_b_index = match
        team_a = self.tournament.teams[team_a_index] if team_a_index < len(self.tournament.teams) else {}
        team_b = self.tournament.teams[team_b_index] if team_b_index < len(self.tournament.teams) else {}

        # Check if user is in team A
        for circle in range(1, 5):
            player = team_a.get(f"circle{circle}")
            if player == str(user_id):
                return 0

        # Check if user is in team B
        for circle in range(1, 5):
            player = team_b.get(f"circle{circle}")
            if player == str(user_id):
                return 1

        return None
