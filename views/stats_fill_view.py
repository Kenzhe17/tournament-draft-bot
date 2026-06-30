"""Views for filling match statistics after match completion."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ui import Modal, TextInput, View, Button, button

if TYPE_CHECKING:
    from models.tournament import Tournament


class FillStatsButton(Button):
    """Button for captains to fill stats for their completed match."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_a: int, team_b: int):
        super().__init__(
            label="📝 Заполнить",
            style=discord.ButtonStyle.primary,
            custom_id=f"fill_stats:{guild_id}:{match_type}:{match_index}"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_a = team_a
        self.team_b = team_b

    async def callback(self, interaction: discord.Interaction) -> None:
        """Check if user is a captain of this match and show modal."""
        # Get team data
        team_a_data = self.tournament.teams[self.team_a] if self.team_a < len(self.tournament.teams) else {}
        team_b_data = self.tournament.teams[self.team_b] if self.team_b < len(self.tournament.teams) else {}

        captain_a = team_a_data.get("captain", "")
        captain_b = team_b_data.get("captain", "")

        # Check if user is a captain of either team
        user_name = interaction.user.display_name
        if user_name != captain_a and user_name != captain_b:
            await interaction.response.send_message(
                "❌ Только капитаны команд этого матча могут заполнять статистику.",
                ephemeral=True
            )
            return

        # Show modal
        modal = CaptainStatsModal(self.guild_id, self.tournament, self.match_type, self.match_index, self.team_a, self.team_b)
        await interaction.response.send_modal(modal)


class AdminFillStatsButton(Button):
    """Button for admin to fill stats for any match."""

    def __init__(self, guild_id: int, tournament: Tournament):
        super().__init__(
            label="📝 Заполнить статистику",
            style=discord.ButtonStyle.secondary,
            custom_id=f"admin_fill_stats:{guild_id}"
        )
        self.guild_id = guild_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        """Check if user is admin and show match selection view."""
        from utils.permissions import is_admin_check

        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только администраторы могут заполнять статистику.",
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


class CaptainStatsModal(Modal):
    """Modal for captains to input statistics for both teams in their match."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_a: int, team_b: int):
        super().__init__(title=f"Статистика матча #{match_index + 1}")
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_a = team_a
        self.team_b = team_b

        # Get team data
        team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
        team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}

        # Collect players from both teams
        self.team_a_players = []
        self.team_b_players = []

        for circle in range(1, 5):
            p_name = team_a_data.get(f"circle{circle}", "")
            if p_name:
                display_name = tournament.player_game_nicknames.get(p_name, p_name)
                self.team_a_players.append((p_name, display_name))

            p_name = team_b_data.get(f"circle{circle}", "")
            if p_name:
                display_name = tournament.player_game_nicknames.get(p_name, p_name)
                self.team_b_players.append((p_name, display_name))

        # Create input fields for each player
        for player_name, display_name in self.team_a_players:
            kills_input = TextInput(
                label=f"{display_name} (Team A) - Kills",
                placeholder="Количество убийств",
                required=True,
                max_length=3,
                custom_id=f"{player_name}|team_a|kills"
            )
            self.add_item(kills_input)

            deaths_input = TextInput(
                label=f"{display_name} (Team A) - Deaths",
                placeholder="Количество смертей",
                required=True,
                max_length=3,
                custom_id=f"{player_name}|team_a|deaths"
            )
            self.add_item(deaths_input)

        for player_name, display_name in self.team_b_players:
            kills_input = TextInput(
                label=f"{display_name} (Team B) - Kills",
                placeholder="Количество убийств",
                required=True,
                max_length=3,
                custom_id=f"{player_name}|team_b|kills"
            )
            self.add_item(kills_input)

            deaths_input = TextInput(
                label=f"{display_name} (Team B) - Deaths",
                placeholder="Количество смертей",
                required=True,
                max_length=3,
                custom_id=f"{player_name}|team_b|deaths"
            )
            self.add_item(deaths_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Process stats submission."""
        from storage.json_store import store

        # Parse all inputs
        stats_data = {"team1": {}, "team2": {}}

        for item in self.children:
            if isinstance(item, TextInput):
                player_name, team, stat_type = item.custom_id.split('|')

                try:
                    value = int(item.value.strip())
                    if value < 0:
                        raise ValueError

                    # Map team_a/team_b to team1/team2 for consistency
                    team_key = "team1" if team == "team_a" else "team2"

                    if player_name not in stats_data[team_key]:
                        stats_data[team_key][player_name] = {}

                    stats_data[team_key][player_name][stat_type] = value
                except ValueError:
                    await interaction.response.send_message(
                        f"❌ Неверное значение для {item.label}",
                        ephemeral=True
                    )
                    return

        # Store in tournament
        if self.match_type not in self.tournament.temp_match_stats:
            self.tournament.temp_match_stats[self.match_type] = {}

        self.tournament.temp_match_stats[self.match_type][self.match_index] = stats_data
        store.set(self.tournament)

        await interaction.response.send_message(
            "✅ Данные отправлены\n⏳ Ожидается подтверждение администратора",
            ephemeral=True
        )


class AdminMatchSelectView(View):
    """View for admin to select which match to fill stats for."""

    def __init__(self, guild_id: int, tournament: Tournament):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament

        # Add buttons for qualifier matches
        for i, (team_a, team_b) in enumerate(tournament.qualifier_matches):
            team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
            team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
            captain_a = team_a_data.get("captain", f"П{team_a + 1}")
            captain_b = team_b_data.get("captain", f"П{team_b + 1}")
            name_a = tournament.team_names.get(team_a, captain_a)
            name_b = tournament.team_names.get(team_b, captain_b)

            is_completed = i in tournament.completed_matches.get("qualifier", [])
            label = f"Отбор #{i + 1}: {name_a} vs {name_b} {'✅' if is_completed else ''}"

            btn = Button(
                label=label,
                style=discord.ButtonStyle.primary if is_completed else discord.ButtonStyle.secondary,
                custom_id=f"admin_fill_qualifier_{i}"
            )
            btn.callback = self._create_callback("qualifier", i, team_a, team_b)
            self.add_item(btn)

        # Add buttons for semifinal matches
        for i, (team_a, team_b) in enumerate(tournament.semifinal_matches):
            team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
            team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
            captain_a = team_a_data.get("captain", f"П{team_a + 1}")
            captain_b = team_b_data.get("captain", f"П{team_b + 1}")
            name_a = tournament.team_names.get(team_a, captain_a)
            name_b = tournament.team_names.get(team_b, captain_b)

            is_completed = i in tournament.completed_matches.get("semifinal", [])
            label = f"Полуфинал #{i + 1}: {name_a} vs {name_b} {'✅' if is_completed else ''}"

            btn = Button(
                label=label,
                style=discord.ButtonStyle.primary if is_completed else discord.ButtonStyle.secondary,
                custom_id=f"admin_fill_semifinal_{i}"
            )
            btn.callback = self._create_callback("semifinal", i, team_a, team_b)
            self.add_item(btn)

        # Add button for final match
        if tournament.final_teams:
            team_a = tournament.final_teams[0]
            team_b = tournament.final_teams[1]
            team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
            team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
            captain_a = team_a_data.get("captain", f"П{team_a + 1}")
            captain_b = team_b_data.get("captain", f"П{team_b + 1}")
            name_a = tournament.team_names.get(team_a, captain_a)
            name_b = tournament.team_names.get(team_b, captain_b)

            is_completed = 0 in tournament.completed_matches.get("final", [])
            label = f"Финал: {name_a} vs {name_b} {'✅' if is_completed else ''}"

            btn = Button(
                label=label,
                style=discord.ButtonStyle.primary if is_completed else discord.ButtonStyle.secondary,
                custom_id=f"admin_fill_final_0"
            )
            btn.callback = self._create_callback("final", 0, team_a, team_b)
            self.add_item(btn)

    def _create_callback(self, match_type: str, match_index: int, team_a: int, team_b: int):
        async def callback(interaction: discord.Interaction):
            modal = AdminStatsModal(self.guild_id, self.tournament, match_type, match_index, team_a, team_b)
            await interaction.response.send_modal(modal)
        return callback


class AdminStatsModal(Modal):
    """Modal for admin to input statistics (same as captain modal)."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_a: int, team_b: int):
        super().__init__(title=f"Статистика матча #{match_index + 1}")
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_a = team_a
        self.team_b = team_b

        # Get team data
        team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
        team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}

        # Collect players from both teams
        self.team_a_players = []
        self.team_b_players = []

        for circle in range(1, 5):
            p_name = team_a_data.get(f"circle{circle}", "")
            if p_name:
                display_name = tournament.player_game_nicknames.get(p_name, p_name)
                self.team_a_players.append((p_name, display_name))

            p_name = team_b_data.get(f"circle{circle}", "")
            if p_name:
                display_name = tournament.player_game_nicknames.get(p_name, p_name)
                self.team_b_players.append((p_name, display_name))

        # Create input fields for each player
        for player_name, display_name in self.team_a_players:
            kills_input = TextInput(
                label=f"{display_name} (Team A) - Kills",
                placeholder="Количество убийств",
                required=True,
                max_length=3,
                custom_id=f"{player_name}|team_a|kills"
            )
            self.add_item(kills_input)

            deaths_input = TextInput(
                label=f"{display_name} (Team A) - Deaths",
                placeholder="Количество смертей",
                required=True,
                max_length=3,
                custom_id=f"{player_name}|team_a|deaths"
            )
            self.add_item(deaths_input)

        for player_name, display_name in self.team_b_players:
            kills_input = TextInput(
                label=f"{display_name} (Team B) - Kills",
                placeholder="Количество убийств",
                required=True,
                max_length=3,
                custom_id=f"{player_name}|team_b|kills"
            )
            self.add_item(kills_input)

            deaths_input = TextInput(
                label=f"{display_name} (Team B) - Deaths",
                placeholder="Количество смертей",
                required=True,
                max_length=3,
                custom_id=f"{player_name}|team_b|deaths"
            )
            self.add_item(deaths_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Process stats submission and show confirmation view."""
        from storage.json_store import store

        # Parse all inputs
        stats_data = {"team1": {}, "team2": {}}

        for item in self.children:
            if isinstance(item, TextInput):
                player_name, team, stat_type = item.custom_id.split('|')

                try:
                    value = int(item.value.strip())
                    if value < 0:
                        raise ValueError

                    # Map team_a/team_b to team1/team2 for consistency
                    team_key = "team1" if team == "team_a" else "team2"

                    if player_name not in stats_data[team_key]:
                        stats_data[team_key][player_name] = {}

                    stats_data[team_key][player_name][stat_type] = value
                except ValueError:
                    await interaction.response.send_message(
                        f"❌ Неверное значение для {item.label}",
                        ephemeral=True
                    )
                    return

        # Store in tournament temporarily
        if self.match_type not in self.tournament.temp_match_stats:
            self.tournament.temp_match_stats[self.match_type] = {}

        self.tournament.temp_match_stats[self.match_type][self.match_index] = stats_data
        store.set(self.tournament)

        # Show confirmation view
        confirm_view = AdminConfirmView(self.guild_id, self.tournament, self.match_type, self.match_index, self.team_a, self.team_b)
        await interaction.response.send_message(
            "Проверьте статистику перед подтверждением:",
            view=confirm_view,
            ephemeral=True
        )


class AdminConfirmView(View):
    """View for admin to review and confirm stats."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_a: int, team_b: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_a = team_a
        self.team_b = team_b

        # Get team data
        team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
        team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
        captain_a = team_a_data.get("captain", f"П{team_a + 1}")
        captain_b = team_b_data.get("captain", f"П{team_b + 1}")
        self.name_a = tournament.team_names.get(team_a, captain_a)
        self.name_b = tournament.team_names.get(team_b, captain_b)

        # Get stats data
        self.stats_data = tournament.temp_match_stats.get(match_type, {}).get(match_index, {})

        # Add confirm button
        confirm_btn = Button(
            label="✅ Подтвердить",
            style=discord.ButtonStyle.success,
            custom_id="confirm_stats"
        )
        confirm_btn.callback = self.confirm_callback
        self.add_item(confirm_btn)

        # Add edit button
        edit_btn = Button(
            label="✏️ Изменить",
            style=discord.ButtonStyle.secondary,
            custom_id="edit_stats"
        )
        edit_btn.callback = self.edit_callback
        self.add_item(edit_btn)

    def get_stats_display(self) -> str:
        """Generate display text for stats."""
        lines = [f"**Матч #{self.match_index + 1}: {self.name_a} vs {self.name_b}**\n"]

        # Team 1
        lines.append(f"**{self.name_a}**")
        team1_stats = self.stats_data.get("team1", {})
        for player_name, stats in team1_stats.items():
            display_name = self.tournament.player_game_nicknames.get(player_name, player_name)
            kills = stats.get("kills", 0)
            deaths = stats.get("deaths", 0)
            lines.append(f"{display_name}: {kills}K / {deaths}D")

        lines.append("")

        # Team 2
        lines.append(f"**{self.name_b}**")
        team2_stats = self.stats_data.get("team2", {})
        for player_name, stats in team2_stats.items():
            display_name = self.tournament.player_game_nicknames.get(player_name, player_name)
            kills = stats.get("kills", 0)
            deaths = stats.get("deaths", 0)
            lines.append(f"{display_name}: {kills}K / {deaths}D")

        return "\n".join(lines)

    async def confirm_callback(self, interaction: discord.Interaction):
        """Confirm and save stats."""
        from storage.json_store import store
        from storage.player_stats_store import player_stats_store

        # Save stats to database
        stats_data = self.tournament.temp_match_stats.get(self.match_type, {}).get(self.match_index, {})

        # Process team 1
        for player_name, stats in stats_data.get("team1", {}).items():
            user_id = self.tournament.player_user_ids.get(player_name, 0)
            kills = stats.get("kills", 0)
            deaths = stats.get("deaths", 0)

            # Update player stats
            await player_stats_store.update_player(
                self.tournament.guild_id,
                user_id,
                player_name,
                kills=kills,
                deaths=deaths
            )

        # Process team 2
        for player_name, stats in stats_data.get("team2", {}).items():
            user_id = self.tournament.player_user_ids.get(player_name, 0)
            kills = stats.get("kills", 0)
            deaths = stats.get("deaths", 0)

            # Update player stats
            await player_stats_store.update_player(
                self.tournament.guild_id,
                user_id,
                player_name,
                kills=kills,
                deaths=deaths
            )

        # Clear temporary stats
        if self.match_type in self.tournament.temp_match_stats:
            if self.match_index in self.tournament.temp_match_stats[self.match_type]:
                del self.tournament.temp_match_stats[self.match_type][self.match_index]

        store.set(self.tournament)

        await interaction.response.edit_message(
            content="✅ Статистика сохранена!",
            view=None
        )

    async def edit_callback(self, interaction: discord.Interaction):
        """Edit the stats."""
        modal = AdminStatsModal(self.guild_id, self.tournament, self.match_type, self.match_index, self.team_a, self.team_b)
        await interaction.response.send_modal(modal)
