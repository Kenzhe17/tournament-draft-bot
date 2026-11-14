from config import replace_emojis, get_emoji
import discord
from discord.ui import Modal, TextInput, View, Button
from utils.design import compose
from storage.bet_store import bet_store
from storage.user_balance_store import user_balance_store
from models.bet import Bet


async def _execute_bet(interaction: discord.Interaction, *, guild_id, tournament,
                       match_type, match_index, team_name,
                       team_a_name, team_b_name, amount) -> str | None:
    """Place the bet. Returns error message or None on success."""
    balance = await user_balance_store.get_balance(guild_id, interaction.user.id)
    if amount > balance:
        return replace_emojis(
            f"Недостаточно средств. Ваш баланс: {balance} {get_emoji('money')}")

    user_team_index = _get_user_team_index(tournament, match_type, match_index,
                                           interaction.user.id)
    if user_team_index is not None:
        if (team_name == team_a_name and user_team_index == 1) or (
                team_name == team_b_name and user_team_index == 0):
            return replace_emojis("Нельзя ставить против своей команды!")

    existing_bet = await bet_store.get_user_bet_in_phase(
        guild_id, interaction.user.id, tournament.id, match_type)
    if existing_bet:
        return replace_emojis("Можно ставить только 1 раз за этап!")

    match_id = f"{tournament.id}_{match_type}_{match_index}"
    odds_data = await bet_store.get_current_odds_db(match_id)
    if odds_data:
        actual_odds = odds_data.team_a_odds if team_name == team_a_name \
            else odds_data.team_b_odds
    else:
        actual_odds = 1.9

    bet = Bet(
        guild_id=guild_id, user_id=interaction.user.id,
        user_name=interaction.user.display_name, tournament_id=tournament.id,
        match_id=match_id, team_name=str(team_name),
        team_index=0 if team_name == team_a_name else 1,
        amount=amount, odds=actual_odds)

    try:
        # Deduct balance before saving bet
        await user_balance_store.subtract_balance(guild_id, interaction.user.id, amount)
        await bet_store.save_bet(bet, team_a_name, team_b_name)
        from bot import TournamentBot
        bot = interaction.client
        await bot.update_tournament_message(interaction.guild, tournament)
    except Exception as e:
        import logging
        logging.error(f"Error placing bet: {e}", exc_info=True)
        return replace_emojis(f"Ошибка при создании ставки: {str(e)}")
    return None


class _BetConfirmView(View):
    """Review card: сумма, команда, коэффициент → подтверждение."""

    def __init__(self, guild_id, tournament, match_type, match_index,
                 team_name, team_a_name, team_b_name, amount, odds):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_name = team_name
        self.team_a_name = team_a_name
        self.team_b_name = team_b_name
        self.amount = amount
        self.odds = odds

        confirm_btn = Button(
            label="Подтвердить ставку",
            style=discord.ButtonStyle.success,
        )
        confirm_btn.callback = self._confirm
        self.add_item(confirm_btn)

        cancel_btn = Button(
            label="Отмена",
            style=discord.ButtonStyle.secondary,
        )
        cancel_btn.callback = self._cancel
        self.add_item(cancel_btn)

    async def _confirm(self, interaction: discord.Interaction):
        # Re-validate everything at confirm time (balance may have changed)
        error = await _execute_bet(
            interaction, guild_id=self.guild_id, tournament=self.tournament,
            match_type=self.match_type, match_index=self.match_index,
            team_name=self.team_name, team_a_name=self.team_a_name,
            team_b_name=self.team_b_name, amount=self.amount)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.edit_message(
            view=compose(
                text=(
                    f"### Ставка принята\n"
                    f"**{self.amount:,}** {get_emoji('money')} → "
                    f"**{self.team_name}** `{self.odds:.2f}x`"
                ),
                view=None,
            ),
        )

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=compose(text="-# Ставка отменена.", view=None),
        )


def _get_user_team_index(tournament, match_type, match_index, user_id):
    if match_type == "qualifiers":
        match = tournament.qualifier_matches[match_index]
    elif match_type == "semifinal":
        match = tournament.semifinal_matches[match_index]
    else:
        match = (tournament.final_teams[0], tournament.final_teams[1])

    team_a_index, team_b_index = match
    team_a = tournament.teams[team_a_index] if team_a_index < len(tournament.teams) else {}
    team_b = tournament.teams[team_b_index] if team_b_index < len(tournament.teams) else {}

    for circle in range(1, 5):
        player = team_a.get(f"circle{circle}")
        if player == str(user_id):
            return 0

    for circle in range(1, 5):
        player = team_b.get(f"circle{circle}")
        if player == str(user_id):
            return 1

    return None


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
            await interaction.response.send_message(replace_emojis("Некорректная сумма. Введите число."), ephemeral=True)
            return

        if amount <= 0:
            await interaction.response.send_message(replace_emojis("Сумма должна быть больше 0."), ephemeral=True)
            return

        balance = await user_balance_store.get_balance(self.guild_id, interaction.user.id)
        if amount > balance:
            await interaction.response.send_message(replace_emojis(f"Недостаточно средств. Ваш баланс: {balance} {get_emoji('money')}"), ephemeral=True)
            return

        user_team_index = _get_user_team_index(self.tournament, self.match_type,
                                               self.match_index, interaction.user.id)
        if user_team_index is not None:
            if (self.team_name == self.team_a_name and user_team_index == 1) or (self.team_name == self.team_b_name and user_team_index == 0):
                await interaction.response.send_message(replace_emojis("Нельзя ставить против своей команды!"), ephemeral=True)
                return

        existing_bet = await bet_store.get_user_bet_in_phase(self.guild_id, interaction.user.id, self.tournament.id, self.match_type)
        if existing_bet:
            await interaction.response.send_message(replace_emojis("Можно ставить только 1 раз за этап!"), ephemeral=True)
            return

        match_id = f"{self.tournament.id}_{self.match_type}_{self.match_index}"
        odds_data = await bet_store.get_current_odds_db(match_id)
        if odds_data:
            actual_odds = odds_data.team_a_odds if self.team_name == self.team_a_name else odds_data.team_b_odds
        else:
            actual_odds = 1.9

        confirm = _BetConfirmView(
            self.guild_id, self.tournament, self.match_type,
            self.match_index, self.team_name, self.team_a_name,
            self.team_b_name, amount, actual_odds)
        await interaction.response.send_message(
            view=compose(
                text=(
                    f"### Проверьте ставку\n"
                    f"- Сумма: **{amount:,}** {get_emoji('money')}\n"
                    f"- Команда: **{self.team_name}**\n"
                    f"- Коэффициент: `{actual_odds:.2f}x`\n"
                    f"- Баланс после: **{balance - amount:,}** {get_emoji('money')}\n"
                    "-# Средства спишутся только после подтверждения."
                ),
                view=confirm,
            ),
            ephemeral=True,
        )
