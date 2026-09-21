"""Buttons for tournament room management."""

import discord
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.tournament import Tournament


class RoomButton(discord.ui.Button):
    """Button for adding/editing room info to a match."""

    def __init__(self, match_type: str, match_index: int, team1_index: int, team2_index: int, team1_name: str, team2_name: str, is_admin: bool = False):
        self.match_type = match_type
        self.match_index = match_index
        self.team1_index = team1_index
        self.team2_index = team2_index
        self.team1_name = team1_name
        self.team2_name = team2_name
        self.is_admin = is_admin

        label = f"{team1_name} vs {team2_name}"
        style = discord.ButtonStyle.danger if is_admin else discord.ButtonStyle.primary
        super().__init__(
            style=style,
            label=label,
            custom_id=f"room_{match_type}_{match_index}"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Open modal for room info."""
        from utils.permissions import is_org_check
        from storage.json_store import store

        # Check permissions (org role has same access as admin)
        is_admin = is_org_check(interaction.user, interaction.guild)

        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message("❌ Нет активного турнира.", ephemeral=True)
            return

        # Get team members
        team1_members = set()
        team2_members = set()

        if self.team1_index < len(tournament.teams):
            team1 = tournament.teams[self.team1_index]
            for circle in range(1, 5):
                player_name = team1.get(f"circle{circle}", "")
                if player_name and player_name in tournament.player_user_ids:
                    team1_members.add(tournament.player_user_ids[player_name])

        if self.team2_index < len(tournament.teams):
            team2 = tournament.teams[self.team2_index]
            for circle in range(1, 5):
                player_name = team2.get(f"circle{circle}", "")
                if player_name and player_name in tournament.player_user_ids:
                    team2_members.add(tournament.player_user_ids[player_name])

        # Check if user is in one of the teams or is admin/org
        user_in_team = interaction.user.id in team1_members or interaction.user.id in team2_members

        if not (user_in_team or is_admin):
            await interaction.response.send_message(
                "❌ Только игроки этих команд и организаторы могут добавлять комнату.",
                ephemeral=True
            )
            return

        # Open modal (for both adding and editing)
        from views.room_modal import RoomModal
        modal = RoomModal(self.match_type, self.match_index, self.team1_index, self.team2_index)
        await interaction.response.send_modal(modal)


class AdminRoomsButton(discord.ui.Button):
    """Button for admins to edit all rooms."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="📋 Комнаты",
            custom_id=f"admin_rooms:{guild_id}"
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Show all rooms for editing."""
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только администраторы или организаторы (роль 'org') могут редактировать комнаты.",
                ephemeral=True
            )
            return

        from storage.json_store import store
        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message("❌ Нет активного турнира.", ephemeral=True)
            return

        # Create view with edit buttons for each room
        view = discord.ui.View()

        # Add edit buttons for qualifier rooms
        for i, (team_a, team_b) in enumerate(tournament.qualifier_matches):
            team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
            team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
            captain_a = team_a_data.get("captain", f"П{team_a + 1}")
            captain_b = team_b_data.get("captain", f"П{team_b + 1}")
            name_a = tournament.team_names.get(team_a, captain_a)
            name_b = tournament.team_names.get(team_b, captain_b)

            view.add_item(RoomButton("qualifier", i, team_a, team_b, name_a, name_b, is_admin=True))

        # Add edit buttons for semifinal rooms
        for i, (team_a, team_b) in enumerate(tournament.semifinal_matches):
            team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
            team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
            captain_a = team_a_data.get("captain", f"П{team_a + 1}")
            captain_b = team_b_data.get("captain", f"П{team_b + 1}")
            name_a = tournament.team_names.get(team_a, captain_a)
            name_b = tournament.team_names.get(team_b, captain_b)

            view.add_item(RoomButton("semifinal", i, team_a, team_b, name_a, name_b, is_admin=True))

        # Add edit button for final room
        if tournament.final_teams:
            team_a = tournament.final_teams[0]
            team_b = tournament.final_teams[1]
            team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
            team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
            captain_a = team_a_data.get("captain", f"П{team_a + 1}")
            captain_b = team_b_data.get("captain", f"П{team_b + 1}")
            name_a = tournament.team_names.get(team_a, captain_a)
            name_b = tournament.team_names.get(team_b, captain_b)

            view.add_item(RoomButton("final", 0, team_a, team_b, name_a, name_b, is_admin=True))

        embed = discord.Embed(
            title="📋 Редактирование комнат",
            description="Нажмите на кнопку матча для редактирования комнаты",
            color=discord.Color.blue()
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
