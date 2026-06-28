"""Betting view and modals for tournament betting system."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from models.tournament import TournamentPhase
from storage.bets_store import bets_store
from storage.user_balance_store import user_balance_store

if TYPE_CHECKING:
    from bot import TournamentBot
    from models.tournament import Tournament

logger = logging.getLogger(__name__)


class MatchSelectView(discord.ui.View):
    """View for selecting a match to bet on."""

    def __init__(self, guild_id: int, tournament: Tournament):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament

        # Add buttons for available matches based on tournament phase
        if tournament.phase == TournamentPhase.QUALIFIERS:
            for i, (team1, team2) in enumerate(tournament.qualifier_matches):
                team1_name = self._get_team_name(team1)
                team2_name = self._get_team_name(team2)
                self.add_item(MatchButton(guild_id, tournament, "qualifier", i, f"{team1_name} vs {team2_name}"))
        elif tournament.phase == TournamentPhase.SEMIFINALS:
            for i, (team1, team2) in enumerate(tournament.semifinal_matches):
                team1_name = self._get_team_name(team1)
                team2_name = self._get_team_name(team2)
                self.add_item(MatchButton(guild_id, tournament, "semifinal", i, f"{team1_name} vs {team2_name}"))
        elif tournament.phase == TournamentPhase.FINAL:
            team1_name = self._get_team_name(tournament.final_teams[0])
            team2_name = self._get_team_name(tournament.final_teams[1])
            self.add_item(MatchButton(guild_id, tournament, "final", 0, f"{team1_name} vs {team2_name}"))

    def _get_team_name(self, team_index: int) -> str:
        """Get team name or default to captain name."""
        team_data = self.tournament.teams[team_index] if team_index < len(self.tournament.teams) else {}
        captain = team_data.get("captain", f"П{team_index + 1}")
        return self.tournament.team_names.get(team_index, captain)


class MatchButton(discord.ui.Button):
    """Button to select a match."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"match_select:{guild_id}:{match_type}:{match_index}"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index

    async def callback(self, interaction: discord.Interaction) -> None:
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

        # Create team selection view
        team_view = TeamSelectView(self.guild_id, self.tournament, self.match_type, self.match_index, teams)

        embed = discord.Embed(
            title="🎯 Выберите команду",
            description=f"{teams[0][1]} vs {teams[1][1]}",
            color=discord.Color.gold()
        )

        await interaction.response.edit_message(embed=embed, view=team_view)


class TeamSelectView(discord.ui.View):
    """View for selecting a team to bet on."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, teams: list[tuple[int, str]]):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index

        for team_index, team_name in teams:
            self.add_item(TeamButton(guild_id, tournament, match_type, match_index, team_index, team_name))


class TeamButton(discord.ui.Button):
    """Button to select a team."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_index: int, team_name: str):
        super().__init__(
            label=team_name,
            style=discord.ButtonStyle.secondary,
            custom_id=f"team_select:{guild_id}:{match_type}:{match_index}:{team_index}"
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
            str(self.guild_id),  # Use guild_id as tournament_id for now
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


class BettingButton(discord.ui.Button):
    """Main button to open betting interface."""

    def __init__(self, guild_id: int, tournament: Tournament):
        super().__init__(
            label="💰 Сделать ставку",
            style=discord.ButtonStyle.primary,
            custom_id=f"betting_main:{guild_id}"
        )
        self.guild_id = guild_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        # Create match selection view
        match_view = MatchSelectView(self.guild_id, self.tournament)

        embed = discord.Embed(
            title="💰 Ставки на турнир",
            description="Выберите матч для ставки:",
            color=discord.Color.gold()
        )

        await interaction.response.send_message(embed=embed, view=match_view, ephemeral=True)
