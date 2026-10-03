from config import replace_emojis
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
            await interaction.response.send_message(replace_emojis("❌ Нет активного турнира."), ephemeral=True)
            return

        # Validate inputs
        if not self.room_id.value or not self.room_password.value:
            await interaction.response.send_message(replace_emojis("❌ ID и пароль комнаты не могут быть пустыми."), ephemeral=True)
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


async def send_room_dm_notifications(bot: Any, tournament: Any, team1_index: int, team2_index: int, room_id: str, room_password: str) -> None:
    """Send room info to specific channel with all player pings."""
    # Get team members for pings
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

    # Get specific channel
    channel = bot.get_channel(1200125075156910181)
    if not channel:
        return

    # Create ping string for all players
    all_members = team1_members + team2_members
    pings = " ".join([f"<@{uid}>" for uid in all_members]) if all_members else ""

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} КОМНАТА ОТКРЫТА",
        description=f"{replace_emojis('white_arrow')} **Заходите в комнату!**\n{replace_emojis('white_dot')} **Матч:** {team1_name} vs {team2_name}",
        color=discord.Color.from_rgb(69, 233, 233)
    )
    embed.add_field(name="ID комнаты", value=f"```\n{room_id}\n```", inline=True)
    embed.add_field(name="Пароль", value=f"```\n{room_password}\n```", inline=True)
    
    await channel.send(content=pings, embed=embed)
