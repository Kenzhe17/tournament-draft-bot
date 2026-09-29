from config import replace_emojis
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
            await interaction.response.send_message(replace_emojis("❌ Нет активного турнира."), ephemeral=True)
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
                replace_emojis("❌ Только игроки этих команд и организаторы могут добавлять комнату."),
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
        self.tournament = None
        self.match_type = None

    async def callback(self, interaction: discord.Interaction) -> None:
        """Show all rooms for editing."""
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                replace_emojis("❌ Только администраторы или организаторы (роль 'org') могут редактировать комнаты."),
                ephemeral=True
            )
            return

        from storage.json_store import store
        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message(replace_emojis("❌ Нет активного турнира."), ephemeral=True)
            return

        # Determine which rooms to show based on match_type
        if self.match_type == "qualifier":
            rooms_to_show = [(i, match, "qualifier") for i, match in enumerate(tournament.qualifier_matches)]
        elif self.match_type == "semifinal":
            rooms_to_show = [(i, match, "semifinal") for i, match in enumerate(tournament.semifinal_matches)]
        elif self.match_type == "final":
            rooms_to_show = [(0, tournament.final_teams, "final")] if tournament.final_teams else []
        else:
            # Show all rooms
            rooms_to_show = []
            for i, match in enumerate(tournament.qualifier_matches):
                rooms_to_show.append((i, match, "qualifier"))
            for i, match in enumerate(tournament.semifinal_matches):
                rooms_to_show.append((i, match, "semifinal"))
            if tournament.final_teams:
                rooms_to_show.append((0, tournament.final_teams, "final"))

        # Create view with edit buttons
        view = discord.ui.View()

        for match_index, match, match_type in rooms_to_show:
            team_a = match[0]
            team_b = match[1]
            team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
            team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
            captain_a = team_a_data.get("captain", f"П{team_a + 1}")
            captain_b = team_b_data.get("captain", f"П{team_b + 1}")
            name_a = tournament.team_names.get(team_a, captain_a)
            name_b = tournament.team_names.get(team_b, captain_b)

            view.add_item(RoomButton(match_type, match_index, team_a, team_b, name_a, name_b, is_admin=True))

        embed = discord.Embed(
            title="📋 Редактирование комнат",
            description="Нажмите на кнопку матча для редактирования комнаты",
            color=discord.Color.blue()
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
