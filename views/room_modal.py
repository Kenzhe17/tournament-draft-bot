from config import replace_emojis
"""Modal form for entering room ID and password."""

import time
import discord
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from models.tournament import Tournament


# Cooldown для отправки комнаты (2 секунды)
room_submission_cooldowns = {}  # guild_id -> timestamp


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

        # Defer immediately to avoid timeout
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.DiscordServerError):
            # Interaction expired or server error, continue anyway
            pass

        # Check cooldown (2 seconds)
        guild_id = interaction.guild_id
        current_time = time.time()
        if guild_id in room_submission_cooldowns:
            elapsed = current_time - room_submission_cooldowns[guild_id]
            if elapsed < 2:
                try:
                    await interaction.followup.send(
                        f"❌ Подождите {2 - elapsed:.1f} секунд перед отправкой комнаты.",
                        ephemeral=True
                    )
                except (discord.NotFound, discord.DiscordServerError):
                    pass
                return

        tournament = store.get(interaction.guild_id)
        if not tournament:
            try:
                await interaction.followup.send(replace_emojis("❌ Нет активного турнира."), ephemeral=True)
            except (discord.NotFound, discord.DiscordServerError):
                pass
            return

        # Validate inputs
        if not self.room_id.value or not self.room_password.value:
            try:
                await interaction.followup.send(replace_emojis("❌ ID и пароль комнаты не могут быть пустыми."), ephemeral=True)
            except (discord.NotFound, discord.DiscordServerError):
                pass
            return

        # Check if room data already exists for this match
        existing_room = None
        if self.match_type == "qualifier":
            existing_room = tournament.qualifier_rooms.get(self.match_index)
        elif self.match_type == "semifinal":
            existing_room = tournament.semifinal_rooms.get(self.match_index)
        elif self.match_type == "final":
            existing_room = tournament.final_room

        if existing_room:
            try:
                await interaction.followup.send(
                    replace_emojis("❌ Данные комнаты уже были отправлены другим пользователем."),
                    ephemeral=True
                )
            except (discord.NotFound, discord.DiscordServerError):
                pass
            return

        # Check cooldown (2 seconds)
        if guild_id in room_submission_cooldowns:
            elapsed = current_time - room_submission_cooldowns[guild_id]
            if elapsed < 2:
                try:
                    await interaction.followup.send(
                        f"❌ Подождите {2 - elapsed:.1f} секунд перед отправкой комнаты.",
                        ephemeral=True
                    )
                except (discord.NotFound, discord.DiscordServerError):
                    pass
                return

        # Set cooldown BEFORE sending (to prevent simultaneous submissions)
        room_submission_cooldowns[guild_id] = time.time()

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

        # Confirm to user
        try:
            await interaction.followup.send(replace_emojis("✅ Комната создана и данные отправлены."), ephemeral=True)
        except (discord.NotFound, discord.DiscordServerError):
            pass


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
    channel = bot.get_channel(1561683314480255057)
    if not channel:
        return

    # Create ping string for all players
    all_members = team1_members + team2_members
    pings = " ".join([f"<@{uid}>" for uid in all_members]) if all_members else ""

    # Send messages separately
    # Message 1: Header
    await channel.send(content=f"**КОМНАТА ОТКРЫТА**\nМатч: {team1_name} vs {team2_name}\n\n{pings}\n\n**Данные:**")

    # Message 2: Room ID
    await channel.send(content=room_id)

    # Message 3: Password
    await channel.send(content=room_password)

    # Message 4: GIF
    await channel.send(content="https://cdn.discordapp.com/attachments/1560907251412238396/1561697607770832957/382752-purplelines.gif?ex=6ad74bbc&is=6ad5fa3c&hm=e731b1784d3a8945004feccc0b7ae13373bd92341484f8f9e3cddc3c4c8120ff&")

    # Send to general channel (1200125075156910181)
    general_channel = bot.get_channel(1200125075156910181)
    if general_channel:
        await general_channel.send(content=f"{pings}\n\n{replace_emojis('white_dot')} Комната открыта, данные {replace_emojis('white_arrow')} <#1561683314480255057>")
