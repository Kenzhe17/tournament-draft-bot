
"""View для полуфиналов — кнопки победителей и генерации матчей."""

from __future__ import annotations

from config import replace_emojis

import logging
from typing import TYPE_CHECKING

import discord

from models.tournament import TournamentPhase, TournamentSize
from storage.json_store import store
from utils.embeds import build_embed_for_phase
from utils.permissions import is_org_check
from views.bet_views import BetButton, ViewBetsButton, ToggleBettingButton
from views.room_buttons import RoomButton, AdminRoomsButton

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class AdminStatsMatchSelectView(discord.ui.View):
    """View for admin to select a match, then a team, then fill stats for that team."""

    def __init__(self, guild_id: int, tournament, match_type: str):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type

        # Add buttons for each match
        if match_type == "qualifier":
            matches = tournament.qualifier_matches
        elif match_type == "semifinal":
            matches = tournament.semifinal_matches
        elif match_type == "final":
            matches = [tournament.final_teams]
        else:
            return

        for i, match in enumerate(matches):
            # Get team names
            teams = []
            for team_index in match:
                team_data = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
                captain = team_data.get("captain", f"П{team_index + 1}")
                team_name = tournament.team_names.get(team_index, captain)
                teams.append(team_name)

            label = f"Игра #{i + 1}: {teams[0]} vs {teams[1]}"
            self.add_item(AdminStatsMatchButton(guild_id, tournament, match_type, i, label, match))


class AdminStatsMatchButton(discord.ui.Button):
    """Button to select a match and show team selection."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int, label: str, match: list):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"admin_stats_match:{guild_id}:{match_type}:{match_index}"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.match = match

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check permissions
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                replace_emojis("❌ У вас нет прав для использования админ-панели!"),
                ephemeral=True
            )
            return

        # Show team selection view
        team_view = AdminStatsTeamSelectView(self.guild_id, self.tournament, self.match_type, self.match_index, self.match)

        embed = discord.Embed(
            title=replace_emojis("📊 Выберите команду"),
            description="Выберите команду для заполнения статистики:",
            color=discord.Color.gold()
        )

        await interaction.response.send_message(embed=embed, view=team_view, ephemeral=True)


class AdminStatsTeamSelectView(discord.ui.View):
    """View for admin to select a team to fill stats for."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int, match: list):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.match = match

        # Add buttons for each team
        for team_index in match:
            team_data = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
            captain = team_data.get("captain", f"П{team_index + 1}")
            team_name = tournament.team_names.get(team_index, captain)
            self.add_item(AdminStatsTeamButton(guild_id, tournament, match_type, match_index, team_index, team_name))


class AdminStatsTeamButton(discord.ui.Button):
    """Button to select a team and open modal for that team's players."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int, team_index: int, team_name: str):
        super().__init__(
            label=team_name,
            style=discord.ButtonStyle.secondary,
            custom_id=f"admin_stats_team:{guild_id}:{match_type}:{match_index}:{team_index}"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index
        self.team_name = team_name

    async def callback(self, interaction: discord.Interaction) -> None:
        # Get players for this team
        team_data = self.tournament.teams[self.team_index] if self.team_index < len(self.tournament.teams) else {}
        players = []
        for circle in range(1, 5):
            player = team_data.get(f"circle{circle}")
            if player:
                players.append(player)

        # Open modal with K/D fields for this team's players
        modal = AdminStatsModal(self.guild_id, self.tournament, self.match_type, self.match_index, self.team_index, self.team_name, players)
        await interaction.response.send_modal(modal)


class AdminStatsModal(discord.ui.Modal, title="Статистика команды"):
    """Modal for admin to fill K/D for 4 players in a team."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int, team_index: int, team_name: str, players: list):
        super().__init__(title=f"Статистика: {team_name}")
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index
        self.team_name = team_name
        self.players = players

        # Create input fields for each player (max 4 for a team)
        for i, player_name in enumerate(players):
            kd_input = discord.ui.TextInput(
                label=f"K/D {player_name}",
                placeholder="7/5",
                required=True,
                max_length=7
            )
            setattr(self, f"kd_{i}", kd_input)
            self.add_item(kd_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from storage.json_store import store

        # Parse and validate statistics
        match_id = f"{self.match_type}_{self.match_index}"
        stats = {}

        for i, player_name in enumerate(self.players):
            kd_field = getattr(self, f"kd_{i}")

            try:
                # Parse format: "kills/deaths"
                kd_parts = kd_field.value.split('/')
                if len(kd_parts) != 2:
                    raise ValueError("Invalid format")

                kills = int(kd_parts[0].strip())
                deaths = int(kd_parts[1].strip())

                # Validate ranges
                if kills < 0 or kills > 35:
                    await interaction.response.send_message(
                        replace_emojis(f"⚠️ Некорректный формат K/D у {player_name}! Убийства должны быть от 0 до 35, смерти от 0 до 15. Пример: 24/10"),
                        ephemeral=True
                    )
                    return

                if deaths < 0 or deaths > 15:
                    await interaction.response.send_message(
                        replace_emojis(f"⚠️ Некорректный формат K/D у {player_name}! Убийства должны быть от 0 до 35, смерти от 0 до 15. Пример: 24/10"),
                        ephemeral=True
                    )
                    return

                stats[player_name] = {
                    "kills": kills,
                    "deaths": deaths
                }
            except (ValueError, IndexError):
                await interaction.response.send_message(
                    replace_emojis(f"⚠️ Некорректный формат K/D у {player_name}! Убийства должны быть от 0 до 35, смерти от 0 до 15. Пример: 24/10"),
                    ephemeral=True
                )
                return

        # Store in tournament temp stats
        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message(replace_emojis("❌ Турнир не найден."), ephemeral=True)
            return

        if match_id not in tournament.temp_match_stats:
            tournament.temp_match_stats[match_id] = {}

        # Merge with existing stats
        tournament.temp_match_stats[match_id].update(stats)
        store.set(tournament)

        # Update tournament message
        try:
            from bot import TournamentBot
            bot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)
        except Exception as e:
            import logging
            logging.error(f"Error updating tournament message after stats: {e}", exc_info=True)

        await interaction.response.send_message(
            replace_emojis(f"✅ Статистика для Игра #{self.match_index + 1} успешно внесена!"),
            ephemeral=True
        )


class AdminPanelSelect(discord.ui.Select):
    """Select menu for admin functions."""

    def __init__(self, guild_id: int, tournament, match_type: str):
        options = [
            discord.SelectOption(
                label="Выбрать победителя",
                value="select_winner",
                description="Отметить победителя матча"
            ),
            discord.SelectOption(
                label="Заполнить статистику",
                value="fill_stats",
                description="Внести данные турнира"
            ),
            discord.SelectOption(
                label="Управление комнатами",
                value="manage_rooms",
                description="Настройка турнирных комнат"
            ),
        ]
        super().__init__(
            placeholder="Панель организатора...",
            options=options,
            custom_id=f"admin_panel:{guild_id}:{match_type}",
            min_values=1,
            max_values=1
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check permissions
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                replace_emojis("❌ У вас нет прав для использования админ-панели!"),
                ephemeral=True
            )
            return

        selected = self.values[0]

        if selected == "select_winner":
            # Create match selection view
            match_view = MatchWinnerSelectView(self.guild_id, self.tournament, self.match_type)

            embed = discord.Embed(
                description=replace_emojis("🏆 **Выберите матч для фиксации результата:**"),
                color=discord.Color.green()
            )

            await interaction.response.send_message(embed=embed, view=match_view, ephemeral=True)

        elif selected == "fill_stats":
            # Show match selection view for admin stats filling
            match_view = AdminStatsMatchSelectView(self.guild_id, self.tournament, self.match_type)

            embed = discord.Embed(
                description=replace_emojis("🏆 **Выберите матч для внесения статистики:**"),
                color=discord.Color.green()
            )

            await interaction.response.send_message(embed=embed, view=match_view, ephemeral=True)

        elif selected == "manage_rooms":
            # Trigger AdminRoomsButton callback
            admin_rooms_btn = AdminRoomsButton(self.guild_id)
            admin_rooms_btn.tournament = self.tournament
            admin_rooms_btn.match_type = self.match_type
            await admin_rooms_btn.callback(interaction)


class WinnerConfirmationView(discord.ui.View):
    """View for confirming winner selection."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int, team_index: int, team_name: str):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index
        self.team_name = team_name

        self.add_item(ConfirmWinnerButton(guild_id, tournament, match_type, match_index, team_index, team_name))
        self.add_item(CancelWinnerButton(guild_id, tournament, match_type, match_index))


class ConfirmWinnerButton(discord.ui.Button):
    """Button to confirm winner selection."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int, team_index: int, team_name: str):
        super().__init__(
            label=f"Подтвердить победу {team_name}",
            style=discord.ButtonStyle.success,
            custom_id=f"confirm_winner:{guild_id}:{match_type}:{match_index}:{team_index}"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index
        self.team_name = team_name

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check permissions
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                replace_emojis("❌ У вас нет прав для использования админ-панели!"),
                ephemeral=True
            )
            return

        # Finalize winner selection
        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message(replace_emojis("❌ Турнир не найден."), ephemeral=True)
            return

        # Check if winner already set to prevent duplicate finalization
        if self.match_type == "qualifier":
            if tournament.qualifier_winners[self.match_index] is not None:
                await interaction.response.send_message(
                    replace_emojis("❌ Результат этого матча уже зафиксирован."),
                    ephemeral=True
                )
                return
        elif self.match_type == "semifinal":
            if tournament.semifinal_pending_winners[self.match_index] is not None:
                await interaction.response.send_message(
                    replace_emojis("❌ Результат этого матча уже зафиксирован."),
                    ephemeral=True
                )
                return
        elif self.match_type == "final":
            if tournament.final_pending_winner is not None:
                await interaction.response.send_message(
                    replace_emojis("❌ Результат этого матча уже зафиксирован."),
                    ephemeral=True
                )
                return

        # Call the appropriate winner setter based on match type
        if self.match_type == "qualifier":
            tournament.set_qualifier_winner(self.match_index, self.team_index)
        elif self.match_type == "semifinal":
            tournament.set_semifinal_winner(self.match_index, self.team_index)
        elif self.match_type == "final":
            tournament.set_final_winner(self.team_index)

        store.set(tournament)

        # Update tournament message
        try:
            from bot import TournamentBot
            bot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)
        except Exception as e:
            import logging
            logging.error(f"Error updating tournament message after winner selection: {e}", exc_info=True)

        # TODO: Implement betting payout
        # TODO: Enable captain stats access

        await interaction.response.edit_message(
            content=f"Победитель Игра #{self.match_index + 1} ({self.team_name}) успешно зафиксирован!",
            embed=None,
            view=None
        )


class CancelWinnerButton(discord.ui.Button):
    """Button to cancel and go back to team selection."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int):
        super().__init__(
            label="Назад / Изменить",
            style=discord.ButtonStyle.danger,
            custom_id=f"cancel_winner:{guild_id}:{match_type}:{match_index}"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check permissions
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                replace_emojis("❌ У вас нет прав для использования админ-панели!"),
                ephemeral=True
            )
            return

        # Go back to team selection
        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message(replace_emojis("❌ Турнир не найден."), ephemeral=True)
            return

        # Get teams for this match
        if self.match_type == "qualifier":
            match = tournament.qualifier_matches[self.match_index]
        elif self.match_type == "semifinal":
            match = tournament.semifinal_matches[self.match_index]
        elif self.match_type == "final":
            match = tournament.final_teams
        else:
            await interaction.response.send_message(replace_emojis("❌ Неверный тип матча."), ephemeral=True)
            return

        # Get team names
        teams = []
        for team_index in match:
            team_data = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
            captain = team_data.get("captain", f"П{team_index + 1}")
            team_name = tournament.team_names.get(team_index, captain)
            teams.append((team_index, team_name))

        # Create team selection view
        team_view = TeamWinnerSelectView(self.guild_id, self.tournament, self.match_type, self.match_index, teams)

        embed = discord.Embed(
            description=replace_emojis(f"🏆 **Кто победил в Игра #{self.match_index + 1}?**"),
            color=discord.Color.green()
        )

        await interaction.response.edit_message(embed=embed, view=team_view)


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
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                replace_emojis("❌ Только организаторы (роль 'org') могут генерировать матчи."),
                ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            tournament = store.get(self.guild_id)
            if not tournament:
                await interaction.edit_original_response(
                    content=replace_emojis("❌ Турнир не найден."),
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
                    content=replace_emojis(f"❌ Турнир не в фазе команд. Текущая фаза: {tournament.phase.value}"),
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
                        await player_stats_store.update_player(tournament.guild_id, user_id, player, result="none", count_game=False)
                        # Give participation reward
                        await user_balance_store.add_balance(tournament.guild_id, user_id, 20)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)
        except Exception as e:
            logger.error(f"Error generating matches: {e}", exc_info=True)
            await interaction.edit_original_response(
                content=replace_emojis(f"❌ Ошибка при генерации матчей: {str(e)}")
            )


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
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."),
                ephemeral=True,
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
                replace_emojis("❌ Только капитан может назвать свою команду."),
                ephemeral=True
            )
            return

        # Check if this team has already changed their name
        if not tournament.is_team_name_editable(team_index):
            await interaction.response.send_message(
                replace_emojis("❌ Ваша команда уже изменила название. Можно изменить только один раз."),
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
                replace_emojis("❌ Название не может быть пустым."),
                ephemeral=True
            )
            return

        tournament = store.get(self.guild_id)
        if tournament:
            # Check if this team can still edit their name
            if not tournament.is_team_name_editable(self.team_index):
                await interaction.response.send_message(
                    replace_emojis("❌ Ваша команда уже изменила название. Можно изменить только один раз."),
                    ephemeral=True
                )
                return

            tournament.team_names[self.team_index] = name
            tournament.team_names_changed_teams.add(self.team_index)
            store.set(tournament)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            replace_emojis("✅ Название команды изменено."),
            ephemeral=True
        )


class TeamsView(discord.ui.View):
    """View с кнопкой генерации матчей после драфта."""

    def __init__(self, guild_id: int, tournament):
        super().__init__(timeout=None)
        self.add_item(GenerateMatchesButton(guild_id))

        # Add team name button if any team can still edit their name
        has_editable_team = any(tournament.is_team_name_editable(i) for i in range(len(tournament.teams)))
        if has_editable_team:
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
                    self.add_item(MatchButton(guild_id, tournament, "qualifier", i, f"Игра #{i + 1}: {team1_name} vs {team2_name}"))
        elif match_type == "semifinal":
            for i, (team1, team2) in enumerate(tournament.semifinal_matches):
                if tournament.semifinal_winners[i] is None:
                    team1_name = self._get_team_name(team1)
                    team2_name = self._get_team_name(team2)
                    self.add_item(MatchButton(guild_id, tournament, "semifinal", i, f"Игра #{i + 1}: {team1_name} vs {team2_name}"))
        elif match_type == "final":
            team1_name = self._get_team_name(tournament.final_teams[0])
            team2_name = self._get_team_name(tournament.final_teams[1])
            self.add_item(MatchButton(guild_id, tournament, "final", 0, f"Финал: {team1_name} vs {team2_name}"))

    def _get_team_name(self, team_index: int) -> str:
        """Get team name or default to captain name."""
        team_data = self.tournament.teams[team_index] if team_index < len(self.tournament.teams) else {}
        captain = team_data.get("captain", f"П{team_index + 1}")
        return self.tournament.team_names.get(team_index, captain)


class MatchButton(discord.ui.Button):
    """Button to select a match."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int, label: str):
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
        team_view = TeamWinnerSelectView(self.guild_id, self.tournament, self.match_type, self.match_index, teams)

        embed = discord.Embed(
            description=replace_emojis(f"🏆 **Кто победил в Игра #{self.match_index + 1}?**"),
            color=discord.Color.green()
        )

        await interaction.response.edit_message(embed=embed, view=team_view)


class TeamWinnerSelectView(discord.ui.View):
    """View for selecting the winning team."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int, teams: list[tuple[int, str]]):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index

        # Check if winner already selected
        winner_already_selected = False
        if match_type == "qualifier":
            winner_already_selected = tournament.qualifier_winners[match_index] is not None
        elif match_type == "semifinal":
            winner_already_selected = tournament.semifinal_pending_winners[match_index] is not None
        elif match_type == "final":
            winner_already_selected = tournament.final_pending_winner is not None

        for team_index, team_name in teams:
            # Disable button if winner already selected
            disabled = winner_already_selected
            self.add_item(TeamWinnerButton(guild_id, tournament, match_type, match_index, team_index, team_name, disabled))


class TeamWinnerButton(discord.ui.Button):
    """Button to select the winning team."""

    def __init__(self, guild_id: int, tournament, match_type: str, match_index: int, team_index: int, team_name: str, disabled: bool = False):
        super().__init__(
            label=team_name,
            style=discord.ButtonStyle.success,
            custom_id=f"team_winner_select:{guild_id}:{match_type}:{match_index}:{team_index}",
            disabled=disabled
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index
        self.team_name = team_name

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                replace_emojis("❌ У вас нет прав для использования админ-панели!"),
                ephemeral=True
            )
            return

        # Show confirmation view
        confirmation_view = WinnerConfirmationView(
            self.guild_id, self.tournament, self.match_type, self.match_index, self.team_index, self.team_name
        )

        embed = discord.Embed(
            title="⚠️ Подтвердите выбор победителя",
            description=f"Вы действительно хотите объявить **{self.team_name}** победителем в Игра #{self.match_index + 1}?\n*Это действие подведёт итоги ставок и откроет доступ к статистике.*",
            color=discord.Color.orange()
        )

        await interaction.response.edit_message(embed=embed, view=confirmation_view)


class QualifiersView(discord.ui.View):
    """View с кнопками победителей отборочных матчей."""

    def __init__(self, guild_id: int, matches: list[tuple[int, int]], winners: list, tournament):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament

        # Add admin panel select menu
        self.add_item(AdminPanelSelect(guild_id, tournament, "qualifier"))

        # Add team name button if any team can still edit their name
        has_editable_team = any(tournament.is_team_name_editable(i) for i in range(len(tournament.teams)))
        if has_editable_team:
            self.add_item(TeamNameButton(guild_id, tournament))

        # Add betting buttons
        self.add_item(BetButton(guild_id, tournament, matches, "qualifiers"))
        self.add_item(ViewBetsButton(guild_id, tournament, matches, "qualifiers"))

        # Add room buttons for each match (only if not filled)
        for i, (team_a, team_b) in enumerate(matches):
            # Check if room is already filled
            if i not in tournament.qualifier_rooms:
                # Get team names
                team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
                team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
                captain_a = team_a_data.get("captain", f"П{team_a + 1}")
                captain_b = team_b_data.get("captain", f"П{team_b + 1}")
                name_a = tournament.team_names.get(team_a, captain_a)
                name_b = tournament.team_names.get(team_b, captain_b)

                self.add_item(RoomButton("qualifier", i, team_a, team_b, name_a, name_b, is_admin=False))

        # Add captain fill buttons for pending matches
        from views.match_stats_view import CaptainFillButton
        for i, winner in enumerate(tournament.qualifier_winners):
            if winner is not None:
                # Add fill button for both teams in this match
                match = matches[i]
                match_id = f"qualifier_{i}"
                # Check if team 0 has filled stats
                team0_filled = any(
                    tournament.teams[match[0]].get(f"circle{c}") in tournament.temp_match_stats.get(match_id, {})
                    for c in range(1, 5)
                )
                if not team0_filled:
                    self.add_item(CaptainFillButton(guild_id, tournament, "qualifier", i, match[0]))
                # Check if team 1 has filled stats
                team1_filled = any(
                    tournament.teams[match[1]].get(f"circle{c}") in tournament.temp_match_stats.get(match_id, {})
                    for c in range(1, 5)
                )
                if not team1_filled:
                    self.add_item(CaptainFillButton(guild_id, tournament, "qualifier", i, match[1]))


class SemifinalsView(discord.ui.View):
    """View с кнопками победителей полуфиналов."""

    def __init__(self, guild_id: int, matches: list[tuple[int, int]], winners: list, tournament):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament

        # Add admin panel select menu
        self.add_item(AdminPanelSelect(guild_id, tournament, "semifinal"))

        # Add team name button if any team can still edit their name
        has_editable_team = any(tournament.is_team_name_editable(i) for i in range(len(tournament.teams)))
        if has_editable_team:
            self.add_item(TeamNameButton(guild_id, tournament))

        # Add betting buttons
        self.add_item(BetButton(guild_id, tournament, matches, "semifinals"))
        self.add_item(ViewBetsButton(guild_id, tournament, matches, "semifinals"))

        # Add room buttons for each match (only if not filled)
        for i, (team_a, team_b) in enumerate(matches):
            # Check if room is already filled
            if i not in tournament.semifinal_rooms:
                # Get team names
                team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
                team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
                captain_a = team_a_data.get("captain", f"П{team_a + 1}")
                captain_b = team_b_data.get("captain", f"П{team_b + 1}")
                name_a = tournament.team_names.get(team_a, captain_a)
                name_b = tournament.team_names.get(team_b, captain_b)

                self.add_item(RoomButton("semifinal", i, team_a, team_b, name_a, name_b, is_admin=False))

        # Add captain fill buttons for pending matches
        from views.match_stats_view import CaptainFillButton
        for i, winner in enumerate(tournament.semifinal_pending_winners):
            if winner is not None:
                # Add fill button for both teams in this match
                match = matches[i]
                match_id = f"semifinal_{i}"
                # Check if team 0 has filled stats
                team0_filled = any(
                    tournament.teams[match[0]].get(f"circle{c}") in tournament.temp_match_stats.get(match_id, {})
                    for c in range(1, 5)
                )
                if not team0_filled:
                    self.add_item(CaptainFillButton(guild_id, tournament, "semifinal", i, match[0]))
                # Check if team 1 has filled stats
                team1_filled = any(
                    tournament.teams[match[1]].get(f"circle{c}") in tournament.temp_match_stats.get(match_id, {})
                    for c in range(1, 5)
                )
                if not team1_filled:
                    self.add_item(CaptainFillButton(guild_id, tournament, "semifinal", i, match[1]))
