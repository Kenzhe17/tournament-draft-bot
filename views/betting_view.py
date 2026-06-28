"""Betting view and modals for tournament betting system."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from models.tournament import TournamentPhase
from storage.bets_store import bets_store
from storage.user_balance_store import user_balance_store
from utils.permissions import is_admin_check

if TYPE_CHECKING:
    from bot import TournamentBot
    from models.tournament import Tournament

logger = logging.getLogger(__name__)


class BetAmountModal(discord.ui.Modal, title="Сумма ставки"):
    """Modal for entering bet amount."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_index: int, team_name: str):
        super().__init__()
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index
        self.team_name = team_name

        self.amount_input = discord.ui.TextInput(
            label="Сумма ставки",
            placeholder="Минимум 20 🪙",
            min_length=1,
            max_length=10,
            required=True
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            amount = int(self.amount_input.value)
        except ValueError:
            await interaction.response.send_message("❌ Неверная сумма. Введите число.", ephemeral=True)
            return

        if amount < 20:
            await interaction.response.send_message("❌ Минимальная ставка 20 🪙", ephemeral=True)
            return

        # Check user balance
        balance = await user_balance_store.get_balance(self.guild_id, interaction.user.id)
        if balance < amount:
            await interaction.response.send_message(f"❌ Недостаточно средств. Ваш баланс: {balance} 🪙", ephemeral=True)
            return

        # Check if user is in the match (restriction)
        if self._is_user_in_match(interaction.user.id):
            await interaction.response.send_message("❌ Вы не можете делать ставки на матчи, в которых участвуете.", ephemeral=True)
            return

        # Deduct balance
        await user_balance_store.subtract_balance(self.guild_id, interaction.user.id, amount)

        # Create bet
        await bets_store.create_bet(
            self.guild_id,
            self.tournament.guild_id,  # Use guild_id as tournament_id for now
            interaction.user.id,
            self.match_type,
            self.match_index,
            self.team_index,
            amount
        )

        await interaction.response.send_message(
            f"✅ Ставка {amount} 🪙 на {self.team_name} принята!",
            ephemeral=True
        )

    def _is_user_in_match(self, user_id: int) -> bool:
        """Check if user is participating in this match."""
        # Get teams in this match
        if self.match_type == "qualifier":
            match = self.tournament.qualifier_matches[self.match_index]
        elif self.match_type == "semifinal":
            match = self.tournament.semifinal_matches[self.match_index]
        elif self.match_type == "final":
            match = self.tournament.final_teams
        else:
            return False

        # Check if user is in any of the teams
        for team_index in match:
            team = self.tournament.teams[team_index] if team_index < len(self.tournament.teams) else {}
            for circle in range(1, 5):
                player = team.get(f"circle{circle}")
                if player:
                    player_user_id = self.tournament.player_user_ids.get(player, 0)
                    if player_user_id == user_id:
                        return True
        return False


class BetButton(discord.ui.Button):
    """Button to place a bet on a team."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_index: int, team_name: str):
        super().__init__(
            label=f"💰 {team_name}",
            style=discord.ButtonStyle.secondary,
            custom_id=f"bet:{guild_id}:{match_type}:{match_index}:{team_index}"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index
        self.team_name = team_name

    async def callback(self, interaction: discord.Interaction) -> None:
        modal = BetAmountModal(
            self.guild_id,
            self.tournament,
            self.match_type,
            self.match_index,
            self.team_index,
            self.team_name
        )
        await interaction.response.send_modal(modal)


class BettingView(discord.ui.View):
    """View for betting on matches."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, teams: list[tuple[int, str]]):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index

        for team_index, team_name in teams:
            self.add_item(BetButton(guild_id, tournament, match_type, match_index, team_index, team_name))


class OpenBettingButton(discord.ui.Button):
    """Button for admins to open betting for a match."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int):
        super().__init__(
            label="💰 Открыть ставки",
            style=discord.ButtonStyle.primary,
            custom_id=f"open_betting:{guild_id}:{match_type}:{match_index}"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только администраторы могут открывать ставки.",
                ephemeral=True
            )
            return

        # Get teams for this match
        if self.match_type == "qualifier":
            match = self.tournament.qualifier_matches[self.match_index]
        elif self.match_type == "semifinal":
            match = self.tournament.semifinal_matches[self.match_index]
        elif self.match_type == "final":
            match = self.tournament.final_teams
        else:
            await interaction.response.send_message("❌ Неверный тип матча.", ephemeral=True)
            return

        # Get team names
        teams = []
        for team_index in match:
            team_data = self.tournament.teams[team_index] if team_index < len(self.tournament.teams) else {}
            captain = team_data.get("captain", f"П{team_index + 1}")
            team_name = self.tournament.team_names.get(team_index, captain)
            teams.append((team_index, team_name))

        # Create betting view
        betting_view = BettingView(self.guild_id, self.tournament, self.match_type, self.match_index, teams)

        # Send betting message
        match_name = {
            "qualifier": "Отборочный",
            "semifinal": "Полуфинал",
            "final": "Финал"
        }.get(self.match_type, "Матч")

        embed = discord.Embed(
            title=f"💰 Ставки открыты! {match_name} {self.match_index + 1}",
            description="Нажмите на команду, чтобы сделать ставку.",
            color=discord.Color.gold()
        )

        for team_index, team_name in teams:
            embed.add_field(name=team_name, value="💰", inline=False)

        await interaction.response.send_message(embed=embed, view=betting_view)
