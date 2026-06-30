"""Views for match statistics filling by captains and admins."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ui import Modal, TextInput, View, Button, button

if TYPE_CHECKING:
    from models.tournament import Tournament


class CaptainFillButton(Button):
    """Button for captains to fill match statistics."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_index: int):
        team = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
        team_name = tournament.team_names.get(team_index, team.get("captain", f"Team {team_index}"))
        super().__init__(
            label=f"✏️ {team_name}",
            style=discord.ButtonStyle.primary,
            custom_id=f"captain_fill:{guild_id}:{match_type}:{match_index}:{team_index}"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.json_store import store
        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message("❌ Турнир не найден.", ephemeral=True)
            return

        # Check if user is captain of this team
        user_name = interaction.user.display_name
        team = tournament.teams[self.team_index] if self.team_index < len(tournament.teams) else {}
        if team.get("captain") != user_name:
            await interaction.response.send_message("❌ Только капитан может заполнять статистику.", ephemeral=True)
            return

        # Check if match has pending winner
        if self.match_type == "qualifier":
            if tournament.qualifier_winners[self.match_index] is None:
                await interaction.response.send_message("❌ Матч ещё не завершён.", ephemeral=True)
                return
        elif self.match_type == "semifinal":
            if tournament.semifinal_pending_winners[self.match_index] is None:
                await interaction.response.send_message("❌ Матч ещё не завершён.", ephemeral=True)
                return
        elif self.match_type == "final":
            if tournament.final_pending_winner is None:
                await interaction.response.send_message("❌ Матч ещё не завершён.", ephemeral=True)
                return

        # Check if this team already filled stats
        match_id = f"{self.match_type}_{self.match_index}"
        if match_id in tournament.temp_match_stats:
            # Check if any player from this team has stats filled
            team_filled = False
            for player_name, circle in [(team.get(f"circle{c}"), c) for c in range(1, 5)]:
                if player_name and player_name in tournament.temp_match_stats[match_id]:
                    team_filled = True
                    break
            if team_filled:
                await interaction.response.send_message("❌ Статистика вашей команды уже заполнена.", ephemeral=True)
                return

        # Open modal for captain to fill stats
        modal = CaptainStatsModal(
            self.guild_id,
            tournament,
            self.match_type,
            self.match_index,
            self.team_index
        )
        await interaction.response.send_modal(modal)


class CaptainStatsModal(Modal, title="Статистика команды"):
    """Modal for captain to fill team statistics."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_index: int):
        super().__init__()
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index

        # Get team players
        team = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
        self.players = []
        for circle in range(1, 5):
            player = team.get(f"circle{circle}")
            if player:
                self.players.append((player, circle))

        # Create input fields for each player (format: Name: kills/deaths)
        for i, (player_name, circle) in enumerate(self.players):
            kd_input = TextInput(
                label=f"Круг {circle}",
                placeholder="Name: kills/deaths (например: Player1: 7/2)",
                required=False,
                max_length=30
            )
            setattr(self, f"kd_{i}", kd_input)
            self.add_item(kd_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from storage.json_store import store

        try:
            # Parse statistics
            match_id = f"{self.match_type}_{self.match_index}"
            stats = {}

            for i, (player_name, circle) in enumerate(self.players):
                kd_field = getattr(self, f"kd_{i}")

                if not kd_field.value:
                    kills = 0
                    deaths = 0
                else:
                    try:
                        # Parse format: "Name: kills/deaths" or just "kills/deaths"
                        value = kd_field.value.strip()
                        if ':' in value:
                            # Format: "Name: kills/deaths"
                            parts = value.split(':')
                            kd_part = parts[1].strip() if len(parts) > 1 else ""
                        else:
                            # Format: "kills/deaths"
                            kd_part = value

                        kd_parts = kd_part.split('/')
                        kills = int(kd_parts[0].strip()) if kd_parts[0].strip() else 0
                        deaths = int(kd_parts[1].strip()) if len(kd_parts) > 1 and kd_parts[1].strip() else 0
                    except (ValueError, IndexError):
                        await interaction.response.send_message(
                            f"❌ Некорректный формат для круга {circle}. Используйте формат: Name: kills/deaths (например: Player1: 7/2)",
                            ephemeral=True
                        )
                        return

                stats[player_name] = {
                    "kills": kills,
                    "deaths": deaths,
                    "circle": circle
                }

            # Store in tournament temp stats
            tournament = store.get(self.guild_id)
            if not tournament:
                await interaction.response.send_message("❌ Турнир не найден.", ephemeral=True)
                return

            if match_id not in tournament.temp_match_stats:
                tournament.temp_match_stats[match_id] = {}

            # Merge with existing stats (captain fills their team only)
            tournament.temp_match_stats[match_id].update(stats)
            store.set(tournament)

            # Update tournament message to refresh view
            from bot import TournamentBot
            bot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)

            await interaction.response.send_message(
                "✅ Данные отправлены\n\n⏳ Ожидается подтверждения администратора",
                ephemeral=True
            )
        except Exception as e:
            import logging
            logging.error(f"Error in CaptainStatsModal.on_submit: {e}", exc_info=True)
            await interaction.response.send_message("❌ Произошла ошибка при сохранении статистики.", ephemeral=True)


class AdminFillButton(Button):
    """Button for admin to fill/edit match statistics."""

    def __init__(self, guild_id: int, tournament: Tournament):
        super().__init__(
            label="✏️ Заполнить статистику (Админ)",
            style=discord.ButtonStyle.secondary,
            custom_id=f"admin_fill:{guild_id}"
        )
        self.guild_id = guild_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_admin_check

        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message("❌ Только администраторы.", ephemeral=True)
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

        # Add buttons for pending matches
        # Qualifiers (only for 32 player tournaments)
        if tournament.size.value == "32" and tournament.phase.value in ["qualifiers", "semifinals", "final", "complete"]:
            for i, winner in enumerate(tournament.qualifier_winners):
                if winner is not None:
                    match = tournament.qualifier_matches[i]
                    team_a_name = tournament.team_names.get(match[0], f"Team {match[0]}")
                    team_b_name = tournament.team_names.get(match[1], f"Team {match[1]}")
                    btn = Button(
                        label=f"Отборочный {i+1}: {team_a_name} vs {team_b_name}",
                        style=discord.ButtonStyle.primary,
                        custom_id=f"admin_match:qualifier:{i}"
                    )
                    btn.callback = self._create_callback("qualifier", i)
                    self.add_item(btn)

        # Semifinals
        if tournament.phase.value in ["semifinals", "final", "complete"]:
            for i, winner in enumerate(tournament.semifinal_pending_winners):
                if winner is not None:
                    match = tournament.semifinal_matches[i]
                    team_a_name = tournament.team_names.get(match[0], f"Team {match[0]}")
                    team_b_name = tournament.team_names.get(match[1], f"Team {match[1]}")
                    btn = Button(
                        label=f"Полуфинал {i+1}: {team_a_name} vs {team_b_name}",
                        style=discord.ButtonStyle.primary,
                        custom_id=f"admin_match:semifinal:{i}"
                    )
                    btn.callback = self._create_callback("semifinal", i)
                    self.add_item(btn)

        # Final
        if tournament.phase.value in ["final", "complete"] and tournament.final_pending_winner is not None:
            team_a = tournament.final_teams[0]
            team_b = tournament.final_teams[1]
            team_a_name = tournament.team_names.get(team_a, f"Team {team_a}")
            team_b_name = tournament.team_names.get(team_b, f"Team {team_b}")
            btn = Button(
                label=f"Финал: {team_a_name} vs {team_b_name}",
                style=discord.ButtonStyle.primary,
                custom_id=f"admin_match:final:0"
            )
            btn.callback = self._create_callback("final", 0)
            self.add_item(btn)

    def _create_callback(self, match_type: str, match_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            # Show team selection for admin
            view = AdminTeamSelectView(self.guild_id, self.tournament, match_type, match_index)
            await interaction.response.send_message(
                "Выберите команду для заполнения статистики:",
                view=view,
                ephemeral=True
            )
        return callback


class AdminTeamSelectView(View):
    """View for admin to select which team to fill stats for."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index

        # Get match teams
        if match_type == "qualifier":
            match = tournament.qualifier_matches[match_index]
        elif match_type == "semifinal":
            match = tournament.semifinal_matches[match_index]
        else:  # final
            match = (tournament.final_teams[0], tournament.final_teams[1])

        team_a_index, team_b_index = match
        team_a_data = tournament.teams[team_a_index] if team_a_index < len(tournament.teams) else {}
        team_b_data = tournament.teams[team_b_index] if team_b_index < len(tournament.teams) else {}
        team_a_name = tournament.team_names.get(team_a_index, team_a_data.get("captain", f"Team {team_a_index}"))
        team_b_name = tournament.team_names.get(team_b_index, team_b_data.get("captain", f"Team {team_b_index}"))

        # Add buttons for each team
        team_a_btn = Button(label=team_a_name, style=discord.ButtonStyle.primary)
        team_a_btn.callback = self._create_callback(team_a_index)
        self.add_item(team_a_btn)

        team_b_btn = Button(label=team_b_name, style=discord.ButtonStyle.primary)
        team_b_btn.callback = self._create_callback(team_b_index)
        self.add_item(team_b_btn)

        # Add confirm button if both teams have stats
        match_id = f"{match_type}_{match_index}"
        temp_stats = tournament.temp_match_stats.get(match_id, {})
        team_a = tournament.teams[team_a_index] if team_a_index < len(tournament.teams) else {}
        team_b = tournament.teams[team_b_index] if team_b_index < len(tournament.teams) else {}

        team_a_filled = any(team_a.get(f"circle{c}") in temp_stats for c in range(1, 5))
        team_b_filled = any(team_b.get(f"circle{c}") in temp_stats for c in range(1, 5))

        if team_a_filled and team_b_filled:
            confirm_btn = Button(label="✅ Подтвердить", style=discord.ButtonStyle.success)
            confirm_btn.callback = self._create_confirm_callback()
            self.add_item(confirm_btn)

    def _create_callback(self, team_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            modal = AdminStatsModal(self.guild_id, self.tournament, self.match_type, self.match_index, team_index)
            await interaction.response.send_modal(modal)
        return callback

    def _create_confirm_callback(self):
        async def callback(interaction: discord.Interaction) -> None:
            match_id = f"{self.match_type}_{self.match_index}"
            temp_stats = self.tournament.temp_match_stats.get(match_id, {})
            view = AdminConfirmView(self.guild_id, self.tournament, self.match_type, self.match_index, temp_stats)
            await interaction.response.send_message(
                "Проверьте статистику перед подтверждением:",
                view=view,
                ephemeral=True
            )
        return callback


class AdminStatsModal(Modal, title="Статистика команды (Админ)"):
    """Modal for admin to fill statistics for a single team."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, team_index: int):
        super().__init__()
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index

        # Get team data
        team = tournament.teams[team_index] if team_index < len(tournament.teams) else {}

        # Get existing stats from captain submissions
        match_id = f"{match_type}_{match_index}"
        temp_stats = tournament.temp_match_stats.get(match_id, {})

        # Collect players from this team
        self.players = []
        for circle in range(1, 5):
            player = team.get(f"circle{circle}")
            if player:
                self.players.append((player, circle))

        # Create input fields for each player (format: Name: kills/deaths)
        for i, (player_name, circle) in enumerate(self.players):
            # Pre-fill with captain-submitted stats if available
            existing_stat = temp_stats.get(player_name, {})
            if existing_stat:
                default_value = f"{existing_stat.get('kills', 0)}/{existing_stat.get('deaths', 0)}"
            else:
                default_value = ""

            kd_input = TextInput(
                label=f"Круг {circle}",
                placeholder="Name: kills/deaths (например: Player1: 7/2)",
                required=False,
                max_length=30,
                default=default_value
            )
            setattr(self, f"kd_{i}", kd_input)
            self.add_item(kd_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from storage.json_store import store

        # Parse statistics
        match_id = f"{self.match_type}_{self.match_index}"
        stats = {}

        for i, (player_name, circle) in enumerate(self.players):
            kd_field = getattr(self, f"kd_{i}")

            if not kd_field.value:
                kills = 0
                deaths = 0
            else:
                try:
                    # Parse format: "Name: kills/deaths" or just "kills/deaths"
                    value = kd_field.value.strip()
                    if ':' in value:
                        # Format: "Name: kills/deaths"
                        parts = value.split(':')
                        kd_part = parts[1].strip() if len(parts) > 1 else ""
                    else:
                        # Format: "kills/deaths"
                        kd_part = value

                    kd_parts = kd_part.split('/')
                    kills = int(kd_parts[0].strip()) if kd_parts[0].strip() else 0
                    deaths = int(kd_parts[1].strip()) if len(kd_parts) > 1 and kd_parts[1].strip() else 0
                except (ValueError, IndexError):
                    await interaction.response.send_message(
                        f"❌ Некорректный формат для круга {circle}. Используйте формат: Name: kills/deaths (например: Player1: 7/2)",
                        ephemeral=True
                    )
                    return

            stats[player_name] = {
                "kills": kills,
                "deaths": deaths,
                "circle": circle
            }

        # Store in tournament temp stats
        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message("❌ Турнир не найден.", ephemeral=True)
            return

        # Merge with existing stats (admin fills one team at a time)
        if match_id not in tournament.temp_match_stats:
            tournament.temp_match_stats[match_id] = {}
        tournament.temp_match_stats[match_id].update(stats)
        store.set(tournament)

        # Update tournament message to refresh view
        from bot import TournamentBot
        bot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        # Ask if admin wants to fill the other team or confirm
        # Get match teams to check if both teams have stats
        if self.match_type == "qualifier":
            match = tournament.qualifier_matches[self.match_index]
        elif self.match_type == "semifinal":
            match = tournament.semifinal_matches[self.match_index]
        else:  # final
            match = (tournament.final_teams[0], tournament.final_teams[1])

        team_a_index, team_b_index = match
        team_a = tournament.teams[team_a_index] if team_a_index < len(tournament.teams) else {}
        team_b = tournament.teams[team_b_index] if team_b_index < len(tournament.teams) else {}

        # Check if other team has stats
        other_team_index = team_b_index if self.team_index == team_a_index else team_a_index
        other_team = tournament.teams[other_team_index] if other_team_index < len(tournament.teams) else {}
        other_team_filled = any(
            other_team.get(f"circle{c}") in tournament.temp_match_stats.get(match_id, {})
            for c in range(1, 5)
        )

        if other_team_filled:
            # Both teams filled, show confirmation
            view = AdminConfirmView(self.guild_id, tournament, self.match_type, self.match_index, tournament.temp_match_stats[match_id])
            await interaction.response.send_message(
                "Проверьте статистику перед подтверждением:",
                view=view,
                ephemeral=True
            )
        else:
            # Other team not filled, ask to fill it
            other_team_name = tournament.team_names.get(other_team_index, f"Team {other_team_index}")
            await interaction.response.send_message(
                f"✅ Статистика команды сохранена.\n\nХотите заполнить статистику для команды {other_team_name}?",
                ephemeral=True
            )


class AdminConfirmView(View):
    """View for admin to confirm match statistics."""

    def __init__(self, guild_id: int, tournament: Tournament, match_type: str, match_index: int, stats: dict):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_type = match_type
        self.match_index = match_index
        self.stats = stats

        # Get match teams for display
        if match_type == "qualifier":
            match = tournament.qualifier_matches[match_index]
        elif match_type == "semifinal":
            match = tournament.semifinal_matches[match_index]
        else:  # final
            match = (tournament.final_teams[0], tournament.final_teams[1])

        self.team_a_index, self.team_b_index = match

        # Add confirm and edit buttons
        confirm_btn = Button(label="✅ Подтвердить", style=discord.ButtonStyle.success)
        confirm_btn.callback = self.confirm_callback
        self.add_item(confirm_btn)

        edit_btn = Button(label="✏️ Изменить", style=discord.ButtonStyle.secondary)
        edit_btn.callback = self.edit_callback
        self.add_item(edit_btn)

    async def confirm_callback(self, interaction: discord.Interaction) -> None:
        from storage.json_store import store
        from views.matches_view import process_match_result
        from storage.player_stats_store import player_stats_store
        from storage.bet_store import bet_store
        from storage.user_balance_store import user_balance_store

        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message("❌ Турнир не найден.", ephemeral=True)
            return

        match_id = f"{self.match_type}_{self.match_index}"
        temp_stats = tournament.temp_match_stats.get(match_id, {})

        # Process the match with statistics
        await process_match_result(self.guild_id, tournament, {
            "match_type": self.match_type,
            "match_index": self.match_index,
            "winning_team_index": self._get_winning_team_index(),
            "team1_index": self.team_a_index,
            "team2_index": self.team_b_index,
            "temp_kd_data": temp_stats
        })

        # Resolve betting
        if self.match_type == "qualifier":
            winning_team_index = tournament.qualifier_winners[self.match_index]
        elif self.match_type == "semifinal":
            winning_team_index = tournament.semifinal_pending_winners[self.match_index]
        else:  # final
            winning_team_index = tournament.final_pending_winner

        winning_team = tournament.teams[winning_team_index] if winning_team_index < len(tournament.teams) else {}
        winning_team_name = tournament.team_names.get(winning_team_index, winning_team.get("captain", f"Team {winning_team_index}"))

        payouts = await bet_store.resolve_match_bets(self.guild_id, match_id, winning_team_name)
        for user_id, payout in payouts.items():
            await user_balance_store.add_balance(self.guild_id, user_id, payout)

        # Confirm winner
        if self.match_type == "qualifier":
            tournament.confirm_qualifier_winner(self.match_index, winning_team_index)
        elif self.match_type == "semifinal":
            tournament.confirm_semifinal_winner(self.match_index, winning_team_index)
        else:  # final
            tournament.confirm_final_winner(winning_team_index)

        # Clear temp stats
        if match_id in tournament.temp_match_stats:
            del tournament.temp_match_stats[match_id]

        store.set(tournament)

        from bot import TournamentBot
        bot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message("✅ Статистика сохранена и победитель подтверждён!", ephemeral=True)

    async def edit_callback(self, interaction: discord.Interaction) -> None:
        modal = AdminStatsModal(self.guild_id, self.tournament, self.match_type, self.match_index)
        await interaction.response.send_modal(modal)

    def _get_winning_team_index(self) -> int:
        if self.match_type == "qualifier":
            return self.tournament.qualifier_winners[self.match_index]
        elif self.match_type == "semifinal":
            return self.tournament.semifinal_pending_winners[self.match_index]
        else:  # final
            return self.tournament.final_pending_winner
