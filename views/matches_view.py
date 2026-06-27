"""View для полуфиналов — кнопки победителей и генерации матчей."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from models.tournament import TournamentPhase
from storage.json_store import store
from utils.embeds import build_embed_for_phase
from utils.permissions import is_admin_check

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
        await interaction.response.defer()

        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.edit_original_response(
                content="❌ Только администраторы могут генерировать матчи."
            )
            return

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

        tournament.generate_bracket()
        store.set(tournament)

        # Update games for all players (tournament started)
        from storage.player_stats_store import player_stats_store
        for team in tournament.teams:
            for circle in range(1, 5):
                player = team.get(f"circle{circle}")
                if player:
                    # Get user_id from tournament's player_user_ids
                    user_id = tournament.player_user_ids.get(player, 0)
                    await player_stats_store.update_player(tournament.guild_id, user_id, player, result="none", count_game=True)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)
        await interaction.response.defer()


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
        match = tournament.semifinal_matches[self.match_index]
        losing_team_index = match[1] if match[0] == self.team_index else match[0]
        winning_team = tournament.teams[self.team_index] if self.team_index < len(tournament.teams) else {}
        losing_team = tournament.teams[losing_team_index] if losing_team_index < len(tournament.teams) else {}

        # Winning team gets +0 ELO for semifinal win
        for circle in range(1, 5):
            player = winning_team.get(f"circle{circle}")
            if player:
                user_id = tournament.player_user_ids.get(player, 0)
                await player_stats_store.update_player(tournament.guild_id, user_id, player, result="semifinal_win", count_game=False)

        # Losing team gets -25 ELO
        for circle in range(1, 5):
            player = losing_team.get(f"circle{circle}")
            if player:
                user_id = tournament.player_user_ids.get(player, 0)
                await player_stats_store.update_player(tournament.guild_id, user_id, player, result="loss", count_game=False)

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

        # Winning team gets +0 ELO for qualifier win
        for circle in range(1, 5):
            player = winning_team.get(f"circle{circle}")
            if player:
                user_id = tournament.player_user_ids.get(player, 0)
                await player_stats_store.update_player(tournament.guild_id, user_id, player, result="qualifier_win", count_game=False)

        # Losing team gets -25 ELO
        for circle in range(1, 5):
            player = losing_team.get(f"circle{circle}")
            if player:
                user_id = tournament.player_user_ids.get(player, 0)
                await player_stats_store.update_player(tournament.guild_id, user_id, player, result="loss", count_game=False)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)
        await interaction.response.defer()


class QualifiersView(discord.ui.View):
    """View с кнопками победителей отборочных матчей."""

    def __init__(self, guild_id: int, matches: list[tuple[int, int]], winners: list, tournament):
        super().__init__(timeout=None)
        for i, (team_a, team_b) in enumerate(matches):
            if winners[i] is not None:
                continue
            # Get team names
            team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
            team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
            captain_a = team_a_data.get("captain", f"П{team_a + 1}")
            captain_b = team_b_data.get("captain", f"П{team_b + 1}")
            name_a = tournament.team_names.get(team_a, captain_a)
            name_b = tournament.team_names.get(team_b, captain_b)

            self.add_item(QualifierWinnerButton(guild_id, i, team_a, name_a))
            self.add_item(QualifierWinnerButton(guild_id, i, team_b, name_b))


class SemifinalsView(discord.ui.View):
    """View с кнопками победителей полуфиналов."""

    def __init__(self, guild_id: int, matches: list[tuple[int, int]], winners: list, tournament):
        super().__init__(timeout=None)
        for i, (team_a, team_b) in enumerate(matches):
            if winners[i] is not None:
                continue
            # Get team names
            team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
            team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
            captain_a = team_a_data.get("captain", f"П{team_a + 1}")
            captain_b = team_b_data.get("captain", f"П{team_b + 1}")
            name_a = tournament.team_names.get(team_a, captain_a)
            name_b = tournament.team_names.get(team_b, captain_b)

            self.add_item(SemifinalWinnerButton(guild_id, i, team_a, name_a))
            self.add_item(SemifinalWinnerButton(guild_id, i, team_b, name_b))
