"""Modal form for entering room ID and password."""

import discord
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from models.tournament import Tournament


class RoomModal(discord.ui.Modal, title="Комната игры"):
    """Modal for entering room ID and password."""

    room_id = discord.ui.TextInput(
        label="ID комнаты",
        placeholder="123456",
        required=True,
        max_length=20
    )

    room_password = discord.ui.TextInput(
        label="Пароль",
        placeholder="123",
        required=True,
        max_length=20
    )

    def __init__(self, match_type: str, match_index: int, team1_index: int, team2_index: int):
        super().__init__()
        self.match_type = match_type
        self.match_index = match_index
        self.team1_index = team1_index
        self.team2_index = team2_index

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Handle room ID and password submission."""
        from storage.json_store import store

        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message("❌ Нет активного турнира.", ephemeral=True)
            return

        # Validate inputs
        if not self.room_id.value or not self.room_password.value:
            await interaction.response.send_message("❌ ID и пароль комнаты не могут быть пустыми.", ephemeral=True)
            return

        # Store room data based on match type
        room_data = {"id": self.room_id.value, "password": self.room_password.value}

        if self.match_type == "qualifier":
            tournament.qualifier_rooms[self.match_index] = room_data
        elif self.match_type == "semifinal":
            tournament.semifinal_rooms[self.match_index] = room_data
        elif self.match_type == "final":
            tournament.final_room = room_data

        store.set(tournament)

        # Update tournament message
        from bot import TournamentBot
        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        # Send DM notifications to team members
        await send_room_dm_notifications(bot, tournament, self.team1_index, self.team2_index, self.room_id.value, self.room_password.value)

        await interaction.response.send_message(
            f"✅ Комната добавлена: ID={self.room_id.value}, Пароль={self.room_password.value}",
            ephemeral=True
        )


async def send_room_dm_notifications(bot: Any, tournament: Any, team1_index: int, team2_index: int, room_id: str, room_password: str) -> None:
    """Send DM notifications to team members about room info."""
    # Get team members
    team1_members = []
    team2_members = []

    if team1_index < len(tournament.teams):
        team1 = tournament.teams[team1_index]
        for circle in range(1, 5):
            player_name = team1.get(f"circle{circle}", "")
            if player_name and player_name in tournament.player_user_ids:
                team1_members.append(tournament.player_user_ids[player_name])

    if team2_index < len(tournament.teams):
        team2 = tournament.teams[team2_index]
        for circle in range(1, 5):
            player_name = team2.get(f"circle{circle}", "")
            if player_name and player_name in tournament.player_user_ids:
                team2_members.append(tournament.player_user_ids[player_name])

    # Get team names
    team1_data = tournament.teams[team1_index] if team1_index < len(tournament.teams) else {}
    team2_data = tournament.teams[team2_index] if team2_index < len(tournament.teams) else {}
    team1_name = tournament.team_names.get(team1_index, team1_data.get("captain", f"Team {team1_index}"))
    team2_name = tournament.team_names.get(team2_index, team2_data.get("captain", f"Team {team2_index}"))

    # Send DM to team1 members
    for user_id in team1_members:
        try:
            user = await bot.fetch_user(user_id)
            await user.send(
                f"<@{user_id}> 🏠 **Комната открыта!**\n\n"
                f"Команда: {team1_name}\n"
                f"ID: `{room_id}`\n"
                f"Пароль: `{room_password}`"
            )
        except Exception:
            pass  # User has DMs disabled

    # Send DM to team2 members
    for user_id in team2_members:
        try:
            user = await bot.fetch_user(user_id)
            await user.send(
                f"<@{user_id}> 🏠 **Комната открыта!**\n\n"
                f"Команда: {team2_name}\n"
                f"ID: `{room_id}`\n"
                f"Пароль: `{room_password}`"
            )
        except Exception:
            pass  # User has DMs disabled
