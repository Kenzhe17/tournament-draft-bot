
"""Betting view and modals for tournament betting system."""

from __future__ import annotations

from config import replace_emojis

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
                self.add_item(MatchButton(guild_id, tournament, "qualifiers", i, f"{team1_name} vs {team2_name}"))
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
        if self.match_type == "qualifiers":
            match = self.tournament.qualifier_matches[self.match_index]
        elif self.match_type == "semifinal":
            match = self.tournament.semifinal_matches[self.match_index]
        elif self.match_type == "final":
            match = self.tournament.final_teams
        else:
            await interaction.response.send_message(replace_emojis("❌ Неверный тип матча."), ephemeral=True)
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
            title=replace_emojis("🎯 Выберите команду"),
            description=f"{teams[0][1]} vs {teams[1][1]}",
            color=discord.Color.gold()
        )

        await interaction.response.send_message(embed=embed, view=team_view, ephemeral=True)


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
        # Get team names for this match
        if self.match_type == "qualifiers":
            match = self.tournament.qualifier_matches[self.match_index]
        elif self.match_type == "semifinal":
            match = self.tournament.semifinal_matches[self.match_index]
        elif self.match_type == "final":
            match = self.tournament.final_teams
        else:
            return

        team_a_index, team_b_index = match
        team_a_data = self.tournament.teams[team_a_index] if team_a_index < len(self.tournament.teams) else {}
        team_b_data = self.tournament.teams[team_b_index] if team_b_index < len(self.tournament.teams) else {}
        team_a_name = self.tournament.team_names.get(team_a_index, team_a_data.get("captain", f"Team {team_a_index}"))
        team_b_name = self.tournament.team_names.get(team_b_index, team_b_data.get("captain", f"Team {team_b_index}"))

        modal = BetAmountModal(
            self.guild_id,
            self.tournament,
            self.match_type,
            self.match_index,
            self.team_index,
            self.team_name,
            team_a_name,
            team_b_name
        )
        await interaction.response.send_modal(modal)


class BetAmountModal(discord.ui.Modal, title="Сумма ставки"):
    """Modal for entering bet amount."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_index: int, team_name: str, team_a_name: str, team_b_name: str):
        super().__init__()
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index
        self.team_name = team_name
        self.team_a_name = team_a_name
        self.team_b_name = team_b_name

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
            await interaction.response.send_message(replace_emojis("❌ Неверная сумма. Введите число."), ephemeral=True)
            return

        if amount < 20:
            await interaction.response.send_message(replace_emojis("❌ Минимальная ставка 20 🪙"), ephemeral=True)
            return

        # Check user balance
        balance = await user_balance_store.get_balance(self.guild_id, interaction.user.id)
        if balance < amount:
            await interaction.response.send_message(replace_emojis(f"❌ Недостаточно средств. Ваш баланс: {balance} 🪙"), ephemeral=True)
            return

        # Check if user is in the match and betting against themselves
        user_team_index = self._get_user_team_index(interaction.user.id)
        if user_team_index is not None and user_team_index != self.team_index:
            await interaction.response.send_message(replace_emojis("❌ Вы не можете ставить против своей команды."), ephemeral=True)
            return

        # Get match ID early for bet checking
        match_id = f"{self.match_type}_{self.match_index}"

        # Check if user already has a bet on this match
        from storage.bet_store import bet_store
        existing_bet = await bet_store.get_user_bet(self.guild_id, interaction.user.id, self.tournament.id, match_id)
        if existing_bet:
            # User already has a bet - calculate difference
            if amount > existing_bet.amount:
                # Increasing bet - deduct additional amount
                additional_amount = amount - existing_bet.amount
                await user_balance_store.subtract_balance(self.guild_id, interaction.user.id, additional_amount)
            elif amount < existing_bet.amount:
                # Decreasing bet - refund difference
                refund_amount = existing_bet.amount - amount
                await user_balance_store.add_balance(self.guild_id, interaction.user.id, refund_amount)
            else:
                # Same amount - no balance change
                additional_amount = 0
        else:
            # New bet - deduct full amount
            await user_balance_store.subtract_balance(self.guild_id, interaction.user.id, amount)

        # Initialize odds if not already done
        from models.bet import Bet
        from utils.embeds import get_team_avg_elo
        current_odds = bet_store.get_current_odds(match_id)

        if not current_odds:
            # Get match teams
            if self.match_type == "qualifiers":
                match = self.tournament.qualifier_matches[self.match_index]
            elif self.match_type == "semifinal":
                match = self.tournament.semifinal_matches[self.match_index]
            elif self.match_type == "final":
                match = self.tournament.final_teams
            else:
                return

            # Initialize odds based on ELO
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
            tournament_id=self.tournament.id,
            match_id=match_id,
            team_name=self.team_name,
            team_index=self.team_index,
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
            replace_emojis(f"✅ Ставка {amount} 🪙 на {self.team_name} добавлена! Коэффициент: {actual_odds:.2f}x"),
            ephemeral=True
        )

    def _get_user_team_index(self, user_id: int) -> int | None:
        """Get the team index if user is participating in this match, or None otherwise."""
        # Get teams in this match
        if self.match_type == "qualifiers":
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
            title=replace_emojis("💰 Ставки на турнир"),
            description="Выберите матч для ставки:",
            color=discord.Color.gold()
        )

        await interaction.response.send_message(embed=embed, view=match_view, ephemeral=True)
