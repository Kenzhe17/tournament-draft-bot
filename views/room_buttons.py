"""Buttons for tournament room management."""

import discord
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.tournament import Tournament


class RoomButton(discord.ui.Button):
    """Button for adding room info to a match."""

    def __init__(self, match_type: str, match_index: int, team1_index: int, team2_index: int, team1_name: str, team2_name: str):
        self.match_type = match_type
        self.match_index = match_index
        self.team1_index = team1_index
        self.team2_index = team2_index
        self.team1_name = team1_name
        self.team2_name = team2_name

        label = f"{team1_name} vs {team2_name}"
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"room_{match_type}_{match_index}"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Open modal for room info."""
        from utils.permissions import is_admin_check, is_org_check
        from storage.json_store import store

        # Check permissions
        is_admin = is_admin_check(interaction.user, interaction.guild)
        is_org = is_org_check(interaction.user, interaction.guild)

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

        if not (user_in_team or is_admin or is_org):
            await interaction.response.send_message(
                "❌ Только игроки этих команд и организаторы могут добавлять комнату.",
                ephemeral=True
            )
            return

        # Open modal
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
        from utils.permissions import is_admin_check
        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только администраторы могут редактировать комнаты.",
                ephemeral=True
            )
            return

        from storage.json_store import store
        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message("❌ Нет активного турнира.", ephemeral=True)
            return

        # Build rooms info
        rooms_info = []

        # Qualifier rooms
        for i, (team1_idx, team2_idx) in enumerate(tournament.qualifier_matches):
            room_data = tournament.qualifier_rooms.get(i, {})
            if room_data:
                rooms_info.append(f"🎯 Отбор {i+1}: ID={room_data['id']}, Пароль={room_data['password']}")

        # Semifinal rooms
        for i, (team1_idx, team2_idx) in enumerate(tournament.semifinal_matches):
            room_data = tournament.semifinal_rooms.get(i, {})
            if room_data:
                rooms_info.append(f"🏆 Полуфинал {i+1}: ID={room_data['id']}, Пароль={room_data['password']}")

        # Final room
        if tournament.final_room:
            rooms_info.append(f"👑 Финал: ID={tournament.final_room['id']}, Пароль={tournament.final_room['password']}")

        if not rooms_info:
            rooms_info = ["❌ Комнаты ещё не добавлены"]

        embed = discord.Embed(
            title="📋 Информация о комнатах",
            description="\n".join(rooms_info),
            color=discord.Color.blue()
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)
