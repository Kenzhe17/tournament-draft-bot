from config import replace_emojis, get_emoji
import discord
from discord.ui import Modal, TextInput
from storage.bet_store import bet_store
from storage.user_balance_store import user_balance_store
from models.bet import Bet


class BetAmountModal(Modal, title="Сумма ставки"):
    def __init__(self, guild_id, tournament, match_type, match_index, team_name, team_a_name, team_b_name):
        super().__init__()
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_name = team_name
        self.team_a_name = team_a_name
        self.team_b_name = team_b_name
        self.amount = TextInput(label="Сумма ставки", placeholder="Введите сумму", required=True, max_length=10)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message(replace_emojis("❌ Некорректная сумма. Введите число."), ephemeral=True)
            return

        if amount <= 0:
            await interaction.response.send_message(replace_emojis("❌ Сумма должна быть больше 0."), ephemeral=True)
            return

        balance = await user_balance_store.get_balance(self.guild_id, interaction.user.id)
        if amount > balance:
            await interaction.response.send_message(replace_emojis(f"❌ Недостаточно средств. Ваш баланс: {balance} {get_emoji('money')}"), ephemeral=True)
            return

        user_team_index = self._get_user_team_index(interaction.user.id)
        if user_team_index is not None:
            if (self.team_name == self.team_a_name and user_team_index == 1) or (self.team_name == self.team_b_name and user_team_index == 0):
                await interaction.response.send_message(replace_emojis("❌ Нельзя ставить против своей команды!"), ephemeral=True)
                return

        existing_bet = await bet_store.get_user_bet_in_phase(self.guild_id, interaction.user.id, self.tournament.id, self.match_type)
        if existing_bet:
            await interaction.response.send_message(replace_emojis("❌ Можно ставить только 1 раз за этап!"), ephemeral=True)
            return

        match_id = f"{self.tournament.id}_{self.match_type}_{self.match_index}"
        odds_data = await bet_store.get_current_odds_db(match_id)
        if odds_data:
            actual_odds = odds_data.team_a_odds if self.team_name == self.team_a_name else odds_data.team_b_odds
        else:
            actual_odds = 1.9

        bet = Bet(guild_id=self.guild_id, user_id=interaction.user.id, user_name=interaction.user.display_name, tournament_id=self.tournament.id, match_id=match_id, team_name=str(self.team_name), team_index=0 if self.team_name == self.team_a_name else 1, amount=amount, odds=actual_odds)

        try:
            # Deduct balance before saving bet
            await user_balance_store.subtract_balance(self.guild_id, interaction.user.id, amount)

            await bet_store.save_bet(bet, self.team_a_name, self.team_b_name)
            from bot import TournamentBot
            bot = interaction.client
            await bot.update_tournament_message(interaction.guild, self.tournament)

            # Get the actual odds used (they may have changed during save)
            display_odds = bet.odds

            await interaction.response.send_message(
                replace_emojis(f"✅ Ставка добавлена\n\n{amount} ") + get_emoji('money') + f" → {self.team_name} `({display_odds:.2f}x)`",
                ephemeral=True
            )
        except Exception as e:
            import logging
            logging.error(f"Error placing bet: {e}", exc_info=True)
            # Use followup since interaction may already be responded to
            try:
                await interaction.followup.send_message(replace_emojis(f"❌ Ошибка при создании ставки: {str(e)}"), ephemeral=True)
            except Exception:
                # If followup also fails, we can't do much
                pass

    def _get_user_team_index(self, user_id):
        if self.match_type == "qualifiers":
            match = self.tournament.qualifier_matches[self.match_index]
        elif self.match_type == "semifinal":
            match = self.tournament.semifinal_matches[self.match_index]
        else:
            match = (self.tournament.final_teams[0], self.tournament.final_teams[1])

        team_a_index, team_b_index = match
        team_a = self.tournament.teams[team_a_index] if team_a_index < len(self.tournament.teams) else {}
        team_b = self.tournament.teams[team_b_index] if team_b_index < len(self.tournament.teams) else {}

        for circle in range(1, 5):
            player = team_a.get(f"circle{circle}")
            if player == str(user_id):
                return 0

        for circle in range(1, 5):
            player = team_b.get(f"circle{circle}")
            if player == str(user_id):
                return 1

        return None
