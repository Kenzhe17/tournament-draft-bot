"""Views for filling match statistics after match completion."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ui import Modal, TextInput, View, Button, button

if TYPE_CHECKING:
    from models.tournament import Tournament
else:
    from models.tournament import TournamentPhase


class FillStatsButton(Button):
    """Single adaptive button for captains to fill stats for their match."""

    def __init__(self, guild_id: int, tournament: Tournament):
        super().__init__(
            label="📝 Заполнить статистику",
            style=discord.ButtonStyle.primary,
            custom_id=f"fill_stats:{guild_id}"
        )
        self.guild_id = guild_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        """Check if user is a captain and show appropriate modal."""
        user_name = interaction.user.display_name

        # Find which team this user captains
        captain_team_index = None
        for i, team in enumerate(self.tournament.teams):
            if team.get("captain") == user_name:
                captain_team_index = i
                break

        if captain_team_index is None:
            await interaction.response.send_message(
                "❌ Только капитаны могут заполнять статистику.",
                ephemeral=True
            )
            return

        # Find which match this team is in and if it's completed
        match_info = None

        # Check qualifiers
        if self.tournament.phase == TournamentPhase.QUALIFIERS:
            for match_index, (team_a, team_b) in enumerate(self.tournament.qualifier_matches):
                if captain_team_index in (team_a, team_b):
                    if match_index not in self.tournament.completed_matches.get("qualifier", []):
                        await interaction.response.send_message(
                            "❌ Ваш матч еще не завершен. Дождитесь выбора победителя.",
                            ephemeral=True
                        )
                        return
                    opponent_team_index = team_b if captain_team_index == team_a else team_a
                    match_info = ("qualifier", match_index, captain_team_index, opponent_team_index)
                    break

        # Check semifinals
        elif self.tournament.phase == TournamentPhase.SEMIFINALS:
            for match_index, (team_a, team_b) in enumerate(self.tournament.semifinal_matches):
                if captain_team_index in (team_a, team_b):
                    if match_index not in self.tournament.completed_matches.get("semifinal", []):
                        await interaction.response.send_message(
                            "❌ Ваш матч еще не завершен. Дождитесь выбора победителя.",
                            ephemeral=True
                        )
                        return
                    opponent_team_index = team_b if captain_team_index == team_a else team_a
                    match_info = ("semifinal", match_index, captain_team_index, opponent_team_index)
                    break

        # Check final
        elif self.tournament.phase == TournamentPhase.FINAL:
            if captain_team_index in self.tournament.final_teams:
                if 0 not in self.tournament.completed_matches.get("final", []):
                    await interaction.response.send_message(
                        "❌ Ваш матч еще не завершен. Дождитесь выбора победителя.",
                        ephemeral=True
                    )
                    return
                opponent_team_index = self.tournament.final_teams[1] if captain_team_index == self.tournament.final_teams[0] else self.tournament.final_teams[0]
                match_info = ("final", 0, captain_team_index, opponent_team_index)

        if match_info is None:
            await interaction.response.send_message(
                "❌ Ваша команда не участвует в текущем этапе турнира.",
                ephemeral=True
            )
            return

        match_type, match_index, captain_team, opponent_team = match_info

        # Show modal with only the captain's team
        modal = CaptainStatsModal(self.guild_id, self.tournament, match_type, match_index, captain_team, opponent_team)
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
    """Modal for captains to input statistics for their team only."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, captain_team: int, opponent_team: int):
        super().__init__(title=f"Статистика вашей команды")
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.captain_team = captain_team
        self.opponent_team = opponent_team

        # Get team data for captain's team only
        team_data = tournament.teams[captain_team] if captain_team < len(tournament.teams) else {}

        # Collect players from captain's team
        self.team_players = []

        for circle in range(1, 5):
            p_name = team_data.get(f"circle{circle}", "")
            if p_name:
                display_name = tournament.player_game_nicknames.get(p_name, p_name)
                self.team_players.append((p_name, display_name))

        # Create individual input fields for each player
        for player_name, display_name in self.team_players:
            input_field = TextInput(
                label=display_name,
                placeholder="Формат: убийства смерти (например: 7 2)",
                required=True,
                max_length=10,
                custom_id=f"{player_name}|stats"
            )
            self.add_item(input_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Process stats submission."""
        from storage.json_store import store

        # Parse stats from individual player inputs
        stats_data = {"team1": {}, "team2": {}}

        # Determine which team key to use based on captain's team
        team_key = "team1" if self.captain_team == 0 or (self.match_type == "qualifier" and self.captain_team < 2) else "team2"

        for item in self.children:
            if isinstance(item, TextInput):
                player_name = item.custom_id.split('|')[0]
                value = item.value.strip()

                try:
                    # Format: kills deaths (e.g., "7 2")
                    parts = value.split()
                    if len(parts) != 2:
                        raise ValueError(f"Неверный формат для {item.label}. Используйте: убийства смерти")

                    kills = int(parts[0])
                    deaths = int(parts[1])

                    if kills < 0 or deaths < 0:
                        raise ValueError(f"Отрицательные значения для {item.label}")

                    stats_data[team_key][player_name] = {"kills": kills, "deaths": deaths}
                except (ValueError, IndexError) as e:
                    await interaction.response.send_message(
                        f"❌ {str(e)}",
                        ephemeral=True
                    )
                    return

        # Store in tournament
        if self.match_type not in self.tournament.temp_match_stats:
            self.tournament.temp_match_stats[self.match_type] = {}

        # Merge with existing stats if any
        if self.match_index in self.tournament.temp_match_stats[self.match_type]:
            existing_stats = self.tournament.temp_match_stats[self.match_type][self.match_index]
            existing_stats[team_key].update(stats_data[team_key])
            stats_data = existing_stats

        self.tournament.temp_match_stats[self.match_type][self.match_index] = stats_data
        store.set(self.tournament)

        await interaction.response.send_message(
            "✅ Статистика вашей команды отправлена\n⏳ Ожидается подтверждение администратора",
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
            try:
                modal = AdminStatsModal(self.guild_id, self.tournament, match_type, match_index, team_a, team_b)
                await interaction.response.send_modal(modal)
            except Exception as e:
                await interaction.response.send_message(
                    f"❌ Ошибка: {str(e)}",
                    ephemeral=True
                )
        return callback


class AdminStatsModal(Modal):
    """Modal for admin to input statistics (pre-filled with captain-submitted stats)."""

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

        # Load existing stats from temp_match_stats if available
        existing_stats = tournament.temp_match_stats.get(match_type, {}).get(match_index, {})

        # Build team A input with existing stats pre-filled
        team_a_lines = []
        for player_name, display_name in self.team_a_players:
            # Check if stats exist for this player
            stats = existing_stats.get("team1", {}).get(player_name, {})
            kills = stats.get("kills", 0)
            deaths = stats.get("deaths", 0)
            team_a_lines.append(f"{display_name}: {kills} {deaths}")

        team_a_default = "\n".join(team_a_lines) if team_a_lines else ""
        team_a_input = TextInput(
            label=f"Статистика команды A",
            placeholder="Формат: PlayerName: kills deaths",
            default=team_a_default,
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=500,
            custom_id="team_a_stats"
        )
        self.add_item(team_a_input)

        # Build team B input with existing stats pre-filled
        team_b_lines = []
        for player_name, display_name in self.team_b_players:
            # Check if stats exist for this player
            stats = existing_stats.get("team2", {}).get(player_name, {})
            kills = stats.get("kills", 0)
            deaths = stats.get("deaths", 0)
            team_b_lines.append(f"{display_name}: {kills} {deaths}")

        team_b_default = "\n".join(team_b_lines) if team_b_lines else ""
        team_b_input = TextInput(
            label=f"Статистика команды B",
            placeholder="Формат: PlayerName: kills deaths",
            default=team_b_default,
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=500,
            custom_id="team_b_stats"
        )
        self.add_item(team_b_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Process stats submission and show confirmation view."""
        from storage.json_store import store

        # Parse team A stats
        stats_data = {"team1": {}, "team2": {}}

        team_a_input = None
        team_b_input = None
        for item in self.children:
            if isinstance(item, TextInput):
                if item.custom_id == "team_a_stats":
                    team_a_input = item
                elif item.custom_id == "team_b_stats":
                    team_b_input = item

        # Parse team A
        if team_a_input:
            lines = team_a_input.value.strip().split('\n')
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    # Format: PlayerName: kills deaths
                    parts = line.split(':')
                    if len(parts) != 2:
                        raise ValueError(f"Неверный формат: {line}")

                    player_display = parts[0].strip()
                    kd_parts = parts[1].strip().split()
                    if len(kd_parts) != 2:
                        raise ValueError(f"Неверный формат K/D: {line}")

                    kills = int(kd_parts[0])
                    deaths = int(kd_parts[1])

                    if kills < 0 or deaths < 0:
                        raise ValueError(f"Отрицательные значения: {line}")

                    # Find player name by display name
                    player_name = None
                    for pn, pd in self.team_a_players:
                        if pd == player_display:
                            player_name = pn
                            break

                    if player_name:
                        stats_data["team1"][player_name] = {"kills": kills, "deaths": deaths}
                except (ValueError, IndexError) as e:
                    await interaction.response.send_message(
                        f"❌ Ошибка в строке: {line}. Формат: PlayerName: kills deaths",
                        ephemeral=True
                    )
                    return

        # Parse team B
        if team_b_input:
            lines = team_b_input.value.strip().split('\n')
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    # Format: PlayerName: kills deaths
                    parts = line.split(':')
                    if len(parts) != 2:
                        raise ValueError(f"Неверный формат: {line}")

                    player_display = parts[0].strip()
                    kd_parts = parts[1].strip().split()
                    if len(kd_parts) != 2:
                        raise ValueError(f"Неверный формат K/D: {line}")

                    kills = int(kd_parts[0])
                    deaths = int(kd_parts[1])

                    if kills < 0 or deaths < 0:
                        raise ValueError(f"Отрицательные значения: {line}")

                    # Find player name by display name
                    player_name = None
                    for pn, pd in self.team_b_players:
                        if pd == player_display:
                            player_name = pn
                            break

                    if player_name:
                        stats_data["team2"][player_name] = {"kills": kills, "deaths": deaths}
                except (ValueError, IndexError) as e:
                    await interaction.response.send_message(
                        f"❌ Ошибка в строке: {line}. Формат: PlayerName: kills deaths",
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
        try:
            await interaction.followup.send(
                "Проверьте статистику перед подтверждением:",
                view=confirm_view,
                ephemeral=True
            )
        except discord.NotFound:
            # Interaction expired
            pass


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

        # Clear temporary stats for this match only
        if self.match_type in self.tournament.temp_match_stats:
            if self.match_index in self.tournament.temp_match_stats[self.match_type]:
                del self.tournament.temp_match_stats[self.match_type][self.match_index]

            # If no more stats for this match type, remove the key entirely
            if not self.tournament.temp_match_stats[self.match_type]:
                del self.tournament.temp_match_stats[self.match_type]

        store.set(self.tournament)

        # Update tournament message to check if phase should advance
        from bot import TournamentBot
        bot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, self.tournament)

        await interaction.response.edit_message(
            content="✅ Статистика сохранена!",
            view=None
        )

    async def edit_callback(self, interaction: discord.Interaction):
        """Edit the stats."""
        modal = AdminStatsModal(self.guild_id, self.tournament, self.match_type, self.match_index, self.team_a, self.team_b)
        await interaction.response.send_modal(modal)
