"""Views for filling match statistics by captains and admins."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ui import View, Button, Modal, TextInput

if TYPE_CHECKING:
    from models.tournament import Tournament


class CaptainFillButton(Button):
    """Button for captains to fill statistics for their team."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_index: int):
        super().__init__(
            label="📝 Заполнить",
            style=discord.ButtonStyle.primary,
            custom_id=f"captain_fill:{guild_id}:{match_type}:{match_index}:{team_index}"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check if user is captain of this team
        team = self.tournament.teams[self.team_index] if self.team_index < len(self.tournament.teams) else {}
        captain_name = team.get("captain", "")

        if interaction.user.display_name != captain_name:
            await interaction.response.send_message(
                "❌ Только капитан команды может заполнять статистику.",
                ephemeral=True
            )
            return

        # Check if winner is selected for this match
        if self.match_type not in self.tournament.pending_winners:
            await interaction.response.send_message(
                "❌ Сначала выберите победителя матча.",
                ephemeral=True
            )
            return

        if self.match_index not in self.tournament.pending_winners[self.match_type]:
            await interaction.response.send_message(
                "❌ Сначала выберите победителя матча.",
                ephemeral=True
            )
            return

        # Get players for this team
        players = []
        for circle in range(1, 5):
            player_name = team.get(f"circle{circle}", "")
            if player_name:
                players.append((player_name, circle))

        modal = CaptainFillModal(
            self.guild_id,
            self.tournament,
            self.match_type,
            self.match_index,
            self.team_index,
            players
        )
        await interaction.response.send_modal(modal)


class CaptainFillModal(Modal):
    """Modal for captain to fill statistics for their team."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_index: int, players: list[tuple[str, int]]):
        super().__init__(title="Статистика команды")
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index
        self.players = players

        # Create input fields for each player
        for player_name, circle in players:
            # Use game nickname for display if available
            display_name = tournament.player_game_nicknames.get(player_name, player_name)

            kills_input = TextInput(
                label=f"{display_name} - Kills",
                placeholder="Количество убийств",
                required=True,
                max_length=3,
                custom_id=f"{player_name}|{circle}|kills"
            )
            self.add_item(kills_input)

            deaths_input = TextInput(
                label=f"{display_name} - Deaths",
                placeholder="Количество смертей",
                required=True,
                max_length=3,
                custom_id=f"{player_name}|{circle}|deaths"
            )
            self.add_item(deaths_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from storage.json_store import store

        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Турнир не найден.",
                ephemeral=True
            )
            return

        # Initialize pending_kd_data structure
        if self.match_type not in tournament.pending_kd_data:
            tournament.pending_kd_data[self.match_type] = {}
        if self.match_index not in tournament.pending_kd_data[self.match_type]:
            tournament.pending_kd_data[self.match_type][self.match_index] = {}
        if self.team_index not in tournament.pending_kd_data[self.match_type][self.match_index]:
            tournament.pending_kd_data[self.match_type][self.match_index][self.team_index] = {}

        # Parse K/D for each player
        for item in self.children:
            if isinstance(item, TextInput):
                player_name, circle, data_type = item.custom_id.split('|')
                circle = int(circle)
                value = item.value.strip()

                try:
                    num_value = int(value)
                    if num_value < 0:
                        raise ValueError

                    # Store in pending_kd_data
                    if circle not in tournament.pending_kd_data[self.match_type][self.match_index][self.team_index]:
                        tournament.pending_kd_data[self.match_type][self.match_index][self.team_index][circle] = {}

                    if data_type == "kills":
                        tournament.pending_kd_data[self.match_type][self.match_index][self.team_index][circle] = (
                            num_value,
                            tournament.pending_kd_data[self.match_type][self.match_index][self.team_index][circle][1] if circle in tournament.pending_kd_data[self.match_type][self.match_index][self.team_index] else 0
                        )
                    else:  # deaths
                        tournament.pending_kd_data[self.match_type][self.match_index][self.team_index][circle] = (
                            tournament.pending_kd_data[self.match_type][self.match_index][self.team_index][circle][0] if circle in tournament.pending_kd_data[self.match_type][self.match_index][self.team_index] else 0,
                            num_value
                        )
                except (ValueError, IndexError):
                    await interaction.response.send_message(
                        f"❌ Неверное значение для {player_name}.",
                        ephemeral=True
                    )
                    return

        store.set(tournament)

        await interaction.response.send_message(
            "✅ Данные отправлены\n⏳ Ожидается подтверждение администратора",
            ephemeral=True
        )


class AdminFillButton(Button):
    """Button for admin to fill statistics for any match."""

    def __init__(self, guild_id: int, tournament: Tournament):
        super().__init__(
            label="📝 Заполнить статистику",
            style=discord.ButtonStyle.secondary,
            custom_id=f"admin_fill:{guild_id}"
        )
        self.guild_id = guild_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_admin_check

        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только администраторы могут использовать эту кнопку.",
                ephemeral=True
            )
            return

        # Show match selection view
        view = AdminMatchSelectView(self.guild_id, self.tournament)
        await interaction.response.send_message(
            "Выберите матч для заполнения статистики:",
            view=view,
            ephemeral=True
        )


class AdminMatchSelectView(View):
    """View for admin to select a match to fill statistics."""

    def __init__(self, guild_id: int, tournament: Tournament):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament

        # Add buttons for matches with pending winners
        for match_type in ["qualifier", "semifinal", "final"]:
            if match_type in tournament.pending_winners:
                for match_index, winner_index in tournament.pending_winners[match_type].items():
                    # Check if match is not yet confirmed
                    if match_type == "qualifier" and tournament.qualifier_winners[match_index] is not None:
                        continue
                    if match_type == "semifinal" and tournament.semifinal_winners[match_index] is not None:
                        continue
                    if match_type == "final" and tournament.winner_team_index is not None:
                        continue

                    # Get match info
                    if match_type == "qualifier":
                        match = tournament.qualifier_matches[match_index]
                    elif match_type == "semifinal":
                        match = tournament.semifinal_matches[match_index]
                    else:  # final
                        match = tournament.final_teams

                    team_a = match[0]
                    team_b = match[1]
                    team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
                    team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
                    captain_a = team_a_data.get("captain", f"Team {team_a}")
                    captain_b = team_b_data.get("captain", f"Team {team_b}")
                    name_a = tournament.team_names.get(team_a, captain_a)
                    name_b = tournament.team_names.get(team_b, captain_b)

                    button = Button(
                        label=f"{match_type.title()} #{match_index + 1}: {name_a} vs {name_b}",
                        style=discord.ButtonStyle.primary,
                        custom_id=f"select_match:{match_type}:{match_index}"
                    )
                    button.callback = self._create_callback(match_type, match_index)
                    self.add_item(button)

        if len(self.children) == 0:
            # No pending matches
            label = Button(
                label="Нет матчей для заполнения",
                style=discord.ButtonStyle.secondary,
                disabled=True
            )
            self.add_item(label)

    def _create_callback(self, match_type: str, match_index: int):
        async def callback(interaction: discord.Interaction):
            # Get match info
            if match_type == "qualifier":
                match = self.tournament.qualifier_matches[match_index]
            elif match_type == "semifinal":
                match = self.tournament.semifinal_matches[match_index]
            else:  # final
                match = self.tournament.final_teams

            team_a = match[0]
            team_b = match[1]

            # Get players for both teams
            team_a_players = []
            team_b_players = []

            team_a_data = self.tournament.teams[team_a] if team_a < len(self.tournament.teams) else {}
            team_b_data = self.tournament.teams[team_b] if team_b < len(self.tournament.teams) else {}

            for circle in range(1, 5):
                player_a = team_a_data.get(f"circle{circle}", "")
                if player_a:
                    team_a_players.append((player_a, circle))

                player_b = team_b_data.get(f"circle{circle}", "")
                if player_b:
                    team_b_players.append((player_b, circle))

            modal = AdminFillModal(
                self.guild_id,
                self.tournament,
                match_type,
                match_index,
                team_a,
                team_b,
                team_a_players,
                team_b_players
            )
            await interaction.response.send_modal(modal)
        return callback


class AdminFillModal(Modal):
    """Modal for admin to fill statistics for both teams."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_a: int, team_b: int, team_a_players: list[tuple[str, int]], team_b_players: list[tuple[str, int]]):
        super().__init__(title="Статистика матча")
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_a = team_a
        self.team_b = team_b
        self.team_a_players = team_a_players
        self.team_b_players = team_b_players

        # Team A inputs
        team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
        team_a_name = tournament.team_names.get(team_a, team_a_data.get("captain", f"Team {team_a}"))

        for player_name, circle in team_a_players:
            display_name = tournament.player_game_nicknames.get(player_name, player_name)

            kills_input = TextInput(
                label=f"{team_a_name} - {display_name} - Kills",
                placeholder="Количество убийств",
                required=False,
                max_length=3,
                custom_id=f"{team_a}|{player_name}|{circle}|kills"
            )
            self.add_item(kills_input)

            deaths_input = TextInput(
                label=f"{team_a_name} - {display_name} - Deaths",
                placeholder="Количество смертей",
                required=False,
                max_length=3,
                custom_id=f"{team_a}|{player_name}|{circle}|deaths"
            )
            self.add_item(deaths_input)

        # Team B inputs
        team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
        team_b_name = tournament.team_names.get(team_b, team_b_data.get("captain", f"Team {team_b}"))

        for player_name, circle in team_b_players:
            display_name = tournament.player_game_nicknames.get(player_name, player_name)

            kills_input = TextInput(
                label=f"{team_b_name} - {display_name} - Kills",
                placeholder="Количество убийств",
                required=False,
                max_length=3,
                custom_id=f"{team_b}|{player_name}|{circle}|kills"
            )
            self.add_item(kills_input)

            deaths_input = TextInput(
                label=f"{team_b_name} - {display_name} - Deaths",
                placeholder="Количество смертей",
                required=False,
                max_length=3,
                custom_id=f"{team_b}|{player_name}|{circle}|deaths"
            )
            self.add_item(deaths_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from storage.json_store import store

        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Турнир не найден.",
                ephemeral=True
            )
            return

        # Initialize pending_kd_data structure
        if self.match_type not in tournament.pending_kd_data:
            tournament.pending_kd_data[self.match_type] = {}
        if self.match_index not in tournament.pending_kd_data[self.match_type]:
            tournament.pending_kd_data[self.match_type][self.match_index] = {}

        # Parse K/D for both teams
        for item in self.children:
            if isinstance(item, TextInput) and item.value.strip():
                team_index, player_name, circle, data_type = item.custom_id.split('|')
                team_index = int(team_index)
                circle = int(circle)
                value = item.value.strip()

                try:
                    num_value = int(value)
                    if num_value < 0:
                        raise ValueError

                    # Store in pending_kd_data
                    if team_index not in tournament.pending_kd_data[self.match_type][self.match_index]:
                        tournament.pending_kd_data[self.match_type][self.match_index][team_index] = {}
                    if circle not in tournament.pending_kd_data[self.match_type][self.match_index][team_index]:
                        tournament.pending_kd_data[self.match_type][self.match_index][team_index][circle] = {}

                    if data_type == "kills":
                        tournament.pending_kd_data[self.match_type][self.match_index][team_index][circle] = (
                            num_value,
                            tournament.pending_kd_data[self.match_type][self.match_index][team_index][circle][1] if circle in tournament.pending_kd_data[self.match_type][self.match_index][team_index] else 0
                        )
                    else:  # deaths
                        tournament.pending_kd_data[self.match_type][self.match_index][team_index][circle] = (
                            tournament.pending_kd_data[self.match_type][self.match_index][team_index][circle][0] if circle in tournament.pending_kd_data[self.match_type][self.match_index][team_index] else 0,
                            num_value
                        )
                except (ValueError, IndexError):
                    await interaction.response.send_message(
                        f"❌ Неверное значение для {player_name}.",
                        ephemeral=True
                    )
                    return

        store.set(tournament)

        # Show confirmation view
        view = AdminConfirmView(self.guild_id, tournament, self.match_type, self.match_index)
        await interaction.response.send_message(
            "Проверьте данные перед подтверждением:",
            view=view,
            ephemeral=True
        )


class AdminConfirmView(View):
    """View for admin to confirm match statistics."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index

        # Build confirmation message
        self.confirmation_text = self._build_confirmation_text()

        # Add confirm button
        confirm_button = Button(
            label="✅ Подтвердить",
            style=discord.ButtonStyle.success,
            custom_id="confirm_stats"
        )
        confirm_button.callback = self._confirm_callback
        self.add_item(confirm_button)

        # Add edit button
        edit_button = Button(
            label="✏️ Изменить",
            style=discord.ButtonStyle.secondary,
            custom_id="edit_stats"
        )
        edit_button.callback = self._edit_callback
        self.add_item(edit_button)

    def _build_confirmation_text(self) -> str:
        """Build confirmation text with all statistics."""
        if self.match_type not in self.tournament.pending_kd_data:
            return "Нет данных для подтверждения."

        if self.match_index not in self.tournament.pending_kd_data[self.match_type]:
            return "Нет данных для подтверждения."

        lines = []
        lines.append(f"**Матч {self.match_type} #{self.match_index + 1}**\n")

        for team_index, team_data in self.tournament.pending_kd_data[self.match_type][self.match_index].items():
            team = self.tournament.teams[team_index] if team_index < len(self.tournament.teams) else {}
            team_name = self.tournament.team_names.get(team_index, team.get("captain", f"Team {team_index}"))
            lines.append(f"**{team_name}**")

            for circle, (kills, deaths) in team_data.items():
                player_name = team.get(f"circle{circle}", "Unknown")
                display_name = self.tournament.player_game_nicknames.get(player_name, player_name)
                lines.append(f"{display_name}: Kills {kills}, Deaths {deaths}")

            lines.append("")

        return "\n".join(lines)

    async def _confirm_callback(self, interaction: discord.Interaction) -> None:
        """Confirm and save statistics."""
        from storage.json_store import store

        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Турнир не найден.",
                ephemeral=True
            )
            return

        # Save statistics and confirm winner
        await self._save_statistics(tournament)

        store.set(tournament)

        await interaction.response.send_message(
            "✅ Статистика сохранена и победитель подтвержден!",
            ephemeral=True
        )

    async def _edit_callback(self, interaction: discord.Interaction) -> None:
        """Edit statistics."""
        # Re-open admin fill modal
        # Get match info
        if self.match_type == "qualifier":
            match = self.tournament.qualifier_matches[self.match_index]
        elif self.match_type == "semifinal":
            match = self.tournament.semifinal_matches[self.match_index]
        else:  # final
            match = self.tournament.final_teams

        team_a = match[0]
        team_b = match[1]

        # Get players for both teams
        team_a_players = []
        team_b_players = []

        team_a_data = self.tournament.teams[team_a] if team_a < len(self.tournament.teams) else {}
        team_b_data = self.tournament.teams[team_b] if team_b < len(self.tournament.teams) else {}

        for circle in range(1, 5):
            player_a = team_a_data.get(f"circle{circle}", "")
            if player_a:
                team_a_players.append((player_a, circle))

            player_b = team_b_data.get(f"circle{circle}", "")
            if player_b:
                team_b_players.append((player_b, circle))

        modal = AdminFillModal(
            self.guild_id,
            self.tournament,
            self.match_type,
            self.match_index,
            team_a,
            team_b,
            team_a_players,
            team_b_players
        )
        await interaction.response.send_modal(modal)

    async def _save_statistics(self, tournament: Tournament) -> None:
        """Save statistics to database and confirm winner."""
        from storage.player_stats_store import player_stats_store
        from storage.user_balance_store import user_balance_store
        from storage.bet_store import bet_store
        from models.tournament import TournamentSize

        # Get match info
        if self.match_type == "qualifier":
            match = tournament.qualifier_matches[self.match_index]
            winning_team_index = tournament.pending_winners["qualifier"][self.match_index]
        elif self.match_type == "semifinal":
            match = tournament.semifinal_matches[self.match_index]
            winning_team_index = tournament.pending_winners["semifinal"][self.match_index]
        else:  # final
            match = tournament.final_teams
            winning_team_index = tournament.pending_winners["final"][0]

        losing_team_index = match[1] if match[0] == winning_team_index else match[0]
        winning_team = tournament.teams[winning_team_index] if winning_team_index < len(tournament.teams) else {}
        losing_team = tournament.teams[losing_team_index] if losing_team_index < len(tournament.teams) else {}

        # Save K/D statistics
        if self.match_type in tournament.pending_kd_data and self.match_index in tournament.pending_kd_data[self.match_type]:
            for team_index, team_data in tournament.pending_kd_data[self.match_type][self.match_index].items():
                team = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
                for circle, (kills, deaths) in team_data.items():
                    player_name = team.get(f"circle{circle}", "")
                    if player_name:
                        user_id = tournament.player_user_ids.get(player_name, 0)
                        # Update K/D using rating calculator
                        from utils.rating_calculator import update_player_stats_from_match
                        from models.player_stats import PlayerStats
                        stats = await player_stats_store.get(tournament.guild_id, user_id)
                        if stats:
                            stats.kills += kills
                            stats.deaths += deaths
                            await player_stats_store.update_player(tournament.guild_id, user_id, player_name, result="none", count_game=False)

        # Determine result type based on match type and tournament size
        if self.match_type == "qualifier":
            winner_result = "none"
            loser_result = "qualifier_loss"
        elif self.match_type == "semifinal":
            if tournament.size == TournamentSize.SIXTEEN:
                loser_result = "semifinal_loss"
                winner_result = "none"
            else:
                loser_result = "qualifier_win_semifinal_loss"
                winner_result = "none"
        else:  # final
            if tournament.size == TournamentSize.EIGHT:
                winner_result = "final_win"
                loser_result = "final_loss"
            elif tournament.size == TournamentSize.SIXTEEN:
                winner_result = "semifinal_win_final_win"
                loser_result = "semifinal_win_final_loss"
            else:
                winner_result = "qualifier_win_semifinal_win_final_win"
                loser_result = "qualifier_win_semifinal_win_final_loss"

        # Update player statistics
        for circle in range(1, 5):
            player = winning_team.get(f"circle{circle}")
            if player:
                user_id = tournament.player_user_ids.get(player, 0)
                await player_stats_store.update_player(tournament.guild_id, user_id, player, result=winner_result, count_game=True)

        for circle in range(1, 5):
            player = losing_team.get(f"circle{circle}")
            if player:
                user_id = tournament.player_user_ids.get(player, 0)
                await player_stats_store.update_player(tournament.guild_id, user_id, player, result=loser_result, count_game=True)

        # Give rewards
        if self.match_type == "semifinal":
            for circle in range(1, 5):
                player = winning_team.get(f"circle{circle}")
                if player:
                    user_id = tournament.player_user_ids.get(player, 0)
                    await user_balance_store.add_balance(tournament.guild_id, user_id, 20)
        elif self.match_type == "final":
            for circle in range(1, 5):
                player = winning_team.get(f"circle{circle}")
                if player:
                    user_id = tournament.player_user_ids.get(player, 0)
                    await user_balance_store.add_balance(tournament.guild_id, user_id, 50)

        # Resolve betting
        winning_team_data = tournament.teams[winning_team_index] if winning_team_index < len(tournament.teams) else {}
        winning_team_name = tournament.team_names.get(winning_team_index, winning_team_data.get("captain", f"Team {winning_team_index}"))

        match_id = f"{self.match_type}_{self.match_index}"
        payouts = await bet_store.resolve_match_bets(tournament.guild_id, match_id, winning_team_name)

        for user_id, payout in payouts.items():
            await user_balance_store.add_balance(tournament.guild_id, user_id, payout)

        # Confirm winner
        if self.match_type == "qualifier":
            tournament.qualifier_winners[self.match_index] = winning_team_index
        elif self.match_type == "semifinal":
            tournament.semifinal_winners[self.match_index] = winning_team_index
        else:  # final
            tournament.winner_team_index = winning_team_index

        # Clear pending data
        if self.match_type in tournament.pending_winners:
            if self.match_index in tournament.pending_winners[self.match_type]:
                del tournament.pending_winners[self.match_type][self.match_index]

        if self.match_type in tournament.pending_kd_data:
            if self.match_index in tournament.pending_kd_data[self.match_type]:
                del tournament.pending_kd_data[self.match_type][self.match_index]
