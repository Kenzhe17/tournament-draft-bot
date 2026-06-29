"""View для полуфиналов — кнопки победителей и генерации матчей."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from models.tournament import TournamentPhase, TournamentSize
from storage.json_store import store
from utils.embeds import build_embed_for_phase
from utils.permissions import is_admin_check
from views.bet_views import BetButton, ViewBetsButton, CloseBettingButton

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class GenerateMatchesButton(discord.ui.Button):
    """Кнопка генерации пар для матчей."""

    def __init__(self, guild_id: int):
        super().__init__(
            label="Распределить",
            style=discord.ButtonStyle.primary,
            custom_id=f"generate_matches:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только администраторы могут генерировать матчи.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.edit_original_response(
                content="❌ Турнир не найден."
            )
            return

        # Auto-fix phase mismatch: if phase is not TEAMS but we have teams, reset to TEAMS
        if tournament.phase != TournamentPhase.TEAMS and tournament.teams:
            tournament.phase = TournamentPhase.TEAMS
            tournament.qualifier_matches = []
            tournament.qualifier_winners = []
            tournament.semifinal_matches = []
            tournament.semifinal_winners = []
            tournament.final_teams = []
            tournament.winner_team_index = None
            store.set(tournament)

        if tournament.phase != TournamentPhase.TEAMS:
            await interaction.edit_original_response(
                content=f"❌ Турнир не в фазе команд. Текущая фаза: {tournament.phase.value}"
            )
            return

        logger.info(f"Generating bracket for tournament size: {tournament.size.value}, teams: {len(tournament.teams)}")
        tournament.generate_bracket()
        logger.info(f"After generate_bracket, phase: {tournament.phase.value}")
        store.set(tournament)

        # Update games for all players (tournament started)
        from storage.player_stats_store import player_stats_store
        from storage.user_balance_store import user_balance_store
        for team in tournament.teams:
            for circle in range(1, 5):
                player = team.get(f"circle{circle}")
                if player:
                    # Get user_id from tournament's player_user_ids
                    user_id = tournament.player_user_ids.get(player, 0)
                    await player_stats_store.update_player(tournament.guild_id, user_id, player, result="none", count_game=True)
                    # Give participation reward
                    await user_balance_store.add_balance(tournament.guild_id, user_id, 20)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)


class SemifinalWinnerButton(discord.ui.Button):
    """Кнопка выбора победителя полуфинала."""

    def __init__(self, guild_id: int, match_index: int, team_index: int, team_name: str):
        super().__init__(
            label=f"{team_name} победил",
            style=discord.ButtonStyle.success,
            custom_id=f"semi_win:{guild_id}:{match_index}:{team_index}",
        )
        self.guild_id = guild_id
        self.match_index = match_index
        self.team_index = team_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только администраторы могут фиксировать результаты.",
                ephemeral=True,
            )
            return

        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SEMIFINALS:
            await interaction.response.send_message(
                "❌ Полуфиналы не активны.", ephemeral=True
            )
            return

        # Проверяем, что team_index — участник этого матча
        match = tournament.semifinal_matches[self.match_index]
        if self.team_index not in match:
            await interaction.response.send_message(
                "❌ Неверная команда для этого матча.", ephemeral=True
            )
            return

        if tournament.semifinal_winners[self.match_index] is not None:
            await interaction.response.send_message(
                "❌ Результат этого матча уже зафиксирован.", ephemeral=True
            )
            return

        both_done = tournament.set_semifinal_winner(
            self.match_index, self.team_index
        )
        store.set(tournament)

        # Update stats for both teams
        from storage.player_stats_store import player_stats_store
        from models.tournament import TournamentSize
        match = tournament.semifinal_matches[self.match_index]
        losing_team_index = match[1] if match[0] == self.team_index else match[0]
        winning_team = tournament.teams[self.team_index] if self.team_index < len(tournament.teams) else {}
        losing_team = tournament.teams[losing_team_index] if losing_team_index < len(tournament.teams) else {}

        # Determine result type based on tournament size
        if tournament.size == TournamentSize.SIXTEEN:
            # 16 players: semifinal losers get -25, winners get ELO in final
            loser_result = "semifinal_loss"  # -25
            winner_result = "none"  # No ELO change yet
        else:
            # 32 players: semifinal losers get +25 (won qualifier), winners get ELO in final
            loser_result = "qualifier_win_semifinal_loss"  # +25
            winner_result = "none"  # No ELO change yet

        # Winning team gets no ELO change (will be updated in final)
        from storage.user_balance_store import user_balance_store
        for circle in range(1, 5):
            player = winning_team.get(f"circle{circle}")
            if player:
                user_id = tournament.player_user_ids.get(player, 0)
                await player_stats_store.update_player(tournament.guild_id, user_id, player, result=winner_result, count_game=False)
                # Give semifinal win reward
                await user_balance_store.add_balance(tournament.guild_id, user_id, 20)

        # Losing team gets ELO change based on tournament size
        for circle in range(1, 5):
            player = losing_team.get(f"circle{circle}")
            if player:
                user_id = tournament.player_user_ids.get(player, 0)
                await player_stats_store.update_player(tournament.guild_id, user_id, player, result=loser_result, count_game=False)

        # Resolve betting for this match
        from storage.bet_store import bet_store
        from storage.user_balance_store import user_balance_store

        # Get winning team name
        winning_team_data = tournament.teams[self.team_index] if self.team_index < len(tournament.teams) else {}
        winning_team_name = tournament.team_names.get(self.team_index, winning_team_data.get("captain", f"Team {self.team_index}"))
        
        match_id = f"semifinal_{self.match_index}"
        payouts = await bet_store.resolve_match_bets(tournament.guild_id, match_id, winning_team_name)

        # Pay out winners
        for user_id, payout in payouts.items():
            await user_balance_store.add_balance(tournament.guild_id, user_id, payout)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)
        await interaction.response.defer()


class TeamNameButton(discord.ui.Button):
    """Единая кнопка для капитанов назвать свою команду."""

    def __init__(self, guild_id: int, tournament):
        super().__init__(
            label="Название команды",
            style=discord.ButtonStyle.secondary,
            custom_id=f"team_name:{guild_id}",
        )
        self.guild_id = guild_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.TEAMS:
            await interaction.response.send_message(
                "❌ Невозможно назвать команду.", ephemeral=True
            )
            return

        # Find if user is a captain
        user_name = interaction.user.display_name
        team_index = None
        for i, team in enumerate(tournament.teams):
            if team.get("captain") == user_name:
                team_index = i
                break

        if team_index is None:
            await interaction.response.send_message(
                "❌ Только капитан может назвать свою команду.",
                ephemeral=True
            )
            return

        # Create modal for team name input
        modal = TeamNameModal(self.guild_id, team_index)
        await interaction.response.send_modal(modal)


class TeamNameModal(discord.ui.Modal, title="Название команды"):
    """Modal для ввода названия команды."""

    def __init__(self, guild_id: int, team_index: int):
        super().__init__()
        self.guild_id = guild_id
        self.team_index = team_index
        self.name_input = discord.ui.TextInput(
            label="Название команды",
            placeholder="Введите название...",
            max_length=30,
            required=True
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = self.name_input.value.strip()
        if not name:
            await interaction.response.send_message(
                "❌ Название не может быть пустым.",
                ephemeral=True
            )
            return

        tournament = store.get(self.guild_id)
        if tournament:
            tournament.team_names[self.team_index] = name
            store.set(tournament)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            f"✅ Название команды изменено на '{name}'.",
            ephemeral=True
        )


class TeamsView(discord.ui.View):
    """View с кнопкой генерации матчей после драфта."""

    def __init__(self, guild_id: int, tournament):
        super().__init__(timeout=None)
        self.add_item(GenerateMatchesButton(guild_id))

        self.add_item(TeamNameButton(guild_id, tournament))


class MatchWinnerSelectView(discord.ui.View):
    """View for selecting match winners."""

    def __init__(self, guild_id: int, tournament, match_type: str):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type

        # Add buttons for available matches based on match type
        if match_type == "qualifier":
            for i, (team1, team2) in enumerate(tournament.qualifier_matches):
                if tournament.qualifier_winners[i] is None:
                    team1_name = self._get_team_name(team1)
                    team2_name = self._get_team_name(team2)
                    self.add_item(MatchWinnerButton(guild_id, tournament, "qualifier", i, f"{team1_name} vs {team2_name}"))
        elif match_type == "semifinal":
            for i, (team1, team2) in enumerate(tournament.semifinal_matches):
                if tournament.semifinal_winners[i] is None:
                    team1_name = self._get_team_name(team1)
                    team2_name = self._get_team_name(team2)
                    self.add_item(MatchWinnerButton(guild_id, tournament, "semifinal", i, f"{team1_name} vs {team2_name}"))
        elif match_type == "final":
            team1_name = self._get_team_name(tournament.final_teams[0])
            team2_name = self._get_team_name(tournament.final_teams[1])
            self.add_item(MatchWinnerButton(guild_id, tournament, "final", 0, f"{team1_name} vs {team2_name}"))

    def _get_team_name(self, team_index: int) -> str:
        """Get team name or default to captain name."""
        team_data = self.tournament.teams[team_index] if team_index < len(self.tournament.teams) else {}
        captain = team_data.get("captain", f"П{team_index + 1}")
        return self.tournament.team_names.get(team_index, captain)


class MatchWinnerButton(discord.ui.Button):
    """Button to select a match and choose winner."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"match_winner_select:{guild_id}:{match_type}:{match_index}"
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
        team_view = TeamWinnerSelectView(self.guild_id, self.tournament, self.match_type, self.match_index, teams)

        embed = discord.Embed(
            title="🏆 Выберите победителя",
            description=f"{teams[0][1]} vs {teams[1][1]}",
            color=discord.Color.green()
        )

        await interaction.response.send_message(embed=embed, view=team_view, ephemeral=True, delete_after=3)


class TeamWinnerSelectView(discord.ui.View):
    """View for selecting the winning team."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int, teams: list[tuple[int, str]]):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index

        for team_index, team_name in teams:
            self.add_item(TeamWinnerButton(guild_id, tournament, match_type, match_index, team_index, team_name))


class TeamWinnerButton(discord.ui.Button):
    """Button to select the winning team."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int, team_index: int, team_name: str):
        super().__init__(
            label=team_name,
            style=discord.ButtonStyle.success,
            custom_id=f"team_winner_select:{guild_id}:{match_type}:{match_index}:{team_index}"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index
        self.team_name = team_name

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только администраторы могут выбирать победителей.",
                ephemeral=True
            )
            return

        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message("❌ Турнир не найден.", ephemeral=True)
            return

        # Get teams for this match
        if self.match_type == "qualifier":
            match = tournament.qualifier_matches[self.match_index]
        elif self.match_type == "semifinal":
            match = tournament.semifinal_matches[self.match_index]
        elif self.match_type == "final":
            match = tournament.final_teams
        else:
            await interaction.response.send_message("❌ Неверный тип матча.", ephemeral=True)
            return

        # Get team data for K/D input
        team1_index, team2_index = match
        team1_data = tournament.teams[team1_index] if team1_index < len(tournament.teams) else {}
        team2_data = tournament.teams[team2_index] if team2_index < len(tournament.teams) else {}

        # Get players from both teams with their circles
        team1_players = []
        team2_players = []
        
        for circle in range(1, 5):
            player1 = team1_data.get(f"circle{circle}")
            if player1:
                team1_players.append((player1, circle))
            
            player2 = team2_data.get(f"circle{circle}")
            if player2:
                team2_players.append((player2, circle))

        # Get team names
        team1_name = tournament.team_names.get(team1_index, team1_data.get("captain", f"Team {team1_index}"))
        team2_name = tournament.team_names.get(team2_index, team2_data.get("captain", f"Team {team2_index}"))

        # Store match info for K/D input
        match_info = {
            'match_type': self.match_type,
            'match_index': self.match_index,
            'winning_team_index': self.team_index,
            'team1_index': team1_index,
            'team2_index': team2_index,
            'team1_name': team1_name,
            'team2_name': team2_name,
            'team1_players': team1_players,
            'team2_players': team2_players,
            'temp_kd_data': {},
        }

        # Show K/D input view
        from views.kd_input_view import KDInputView
        kd_view = KDInputView(self.guild_id, tournament, match_info)

        embed = discord.Embed(
            title="📊 Ввод статистики матча",
            description=f"{team1_name} vs {team2_name}\n\nПобедитель: {self.team_name}\n\nВведите K/D для обеих команд:",
            color=discord.Color.blue()
        )

        await interaction.response.send_message(embed=embed, view=kd_view, ephemeral=True)


class SelectWinnerButton(discord.ui.Button):
    """Main button to open winner selection interface."""

    def __init__(self, guild_id: int, tournament, match_type: str):
        super().__init__(
            label="🏆 Выбрать победителя",
            style=discord.ButtonStyle.success,
            custom_id=f"select_winner:{guild_id}:{match_type}"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type

    async def callback(self, interaction: discord.Interaction) -> None:
        # Create match selection view
        match_view = MatchWinnerSelectView(self.guild_id, self.tournament, self.match_type)

        embed = discord.Embed(
            title="🏆 Выбор победителей",
            description="Выберите матч для определения победителя:",
            color=discord.Color.green()
        )

        await interaction.response.send_message(embed=embed, view=match_view, ephemeral=True, delete_after=3)


class QualifierWinnerButton(discord.ui.Button):
    """Кнопка выбора победителя отборочного матча."""

    def __init__(self, guild_id: int, match_index: int, team_index: int, team_name: str):
        super().__init__(
            label=f"{team_name} победил",
            style=discord.ButtonStyle.success,
            custom_id=f"qual_win:{guild_id}:{match_index}:{team_index}",
        )
        self.guild_id = guild_id
        self.match_index = match_index
        self.team_index = team_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только администраторы могут фиксировать результаты.",
                ephemeral=True,
            )
            return

        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.QUALIFIERS:
            await interaction.response.send_message(
                "❌ Отборочные матчи не активны.", ephemeral=True
            )
            return

        # Проверяем, что team_index — участник этого матча
        match = tournament.qualifier_matches[self.match_index]
        if self.team_index not in match:
            await interaction.response.send_message(
                "❌ Неверная команда для этого матча.", ephemeral=True
            )
            return

        if tournament.qualifier_winners[self.match_index] is not None:
            await interaction.response.send_message(
                "❌ Результат этого матча уже зафиксирован.", ephemeral=True
            )
            return

        both_done = tournament.set_qualifier_winner(
            self.match_index, self.team_index
        )
        store.set(tournament)

        # Update stats for both teams
        from storage.player_stats_store import player_stats_store
        match = tournament.qualifier_matches[self.match_index]
        losing_team_index = match[1] if match[0] == self.team_index else match[0]
        winning_team = tournament.teams[self.team_index] if self.team_index < len(tournament.teams) else {}
        losing_team = tournament.teams[losing_team_index] if losing_team_index < len(tournament.teams) else {}

        # Winning team gets no ELO change (will be updated in semifinals/final)
        for circle in range(1, 5):
            player = winning_team.get(f"circle{circle}")
            if player:
                user_id = tournament.player_user_ids.get(player, 0)
                await player_stats_store.update_player(tournament.guild_id, user_id, player, result="none", count_game=False)

        # Losing team gets -25 ELO
        for circle in range(1, 5):
            player = losing_team.get(f"circle{circle}")
            if player:
                user_id = tournament.player_user_ids.get(player, 0)
                await player_stats_store.update_player(tournament.guild_id, user_id, player, result="qualifier_loss", count_game=False)

        # Resolve betting for this match
        from storage.bet_store import bet_store
        from storage.user_balance_store import user_balance_store

        # Get winning team name
        winning_team_data = tournament.teams[self.team_index] if self.team_index < len(tournament.teams) else {}
        winning_team_name = tournament.team_names.get(self.team_index, winning_team_data.get("captain", f"Team {self.team_index}"))
        
        match_id = f"qualifier_{self.match_index}"
        payouts = await bet_store.resolve_match_bets(tournament.guild_id, match_id, winning_team_name)

        # Pay out winners
        for user_id, payout in payouts.items():
            await user_balance_store.add_balance(tournament.guild_id, user_id, payout)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)
        await interaction.response.defer()


class QualifiersView(discord.ui.View):
    """View с кнопками победителей отборочных матчей."""

    def __init__(self, guild_id: int, matches: list[tuple[int, int]], winners: list, tournament):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament

        # Add single winner selection button
        self.add_item(SelectWinnerButton(guild_id, tournament, "qualifier"))

        # Add betting buttons
        self.add_item(BetButton(guild_id, tournament, matches, "qualifier"))
        self.add_item(ViewBetsButton(guild_id, tournament, matches, "qualifier"))
        self.add_item(CloseBettingButton(guild_id))


class SemifinalsView(discord.ui.View):
    """View с кнопками победителей полуфиналов."""

    def __init__(self, guild_id: int, matches: list[tuple[int, int]], winners: list, tournament):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament

        # Add single winner selection button
        self.add_item(SelectWinnerButton(guild_id, tournament, "semifinal"))

        # Add betting buttons
        self.add_item(BetButton(guild_id, tournament, matches, "semifinal"))
        self.add_item(ViewBetsButton(guild_id, tournament, matches, "semifinal"))
        self.add_item(CloseBettingButton(guild_id))
