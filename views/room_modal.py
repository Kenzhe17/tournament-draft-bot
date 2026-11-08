from config import replace_emojis, get_emoji
"""Modal form for entering room ID and password."""

import time
import discord
from utils.design import compose, text_card
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from models.tournament import Tournament


# Cooldown для отправки комнаты (1 секунда)
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

        # Check cooldown (1 second)
        guild_id = interaction.guild_id
        current_time = time.time()
        if guild_id in room_submission_cooldowns:
            elapsed = current_time - room_submission_cooldowns[guild_id]
            if elapsed < 1:
                try:
                    await interaction.followup.send(
                        f"❌ Подождите {1 - elapsed:.1f} секунд перед отправкой комнаты.",
                        ephemeral=True
                    )
                except (discord.NotFound, discord.DiscordServerError):
                    pass
                return

        tournament = store.get(interaction.guild_id, interaction.channel_id)
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

        # Check cooldown (1 second)
        if guild_id in room_submission_cooldowns:
            elapsed = current_time - room_submission_cooldowns[guild_id]
            if elapsed < 1:
                try:
                    await interaction.followup.send(
                        f"❌ Подождите {1 - elapsed:.1f} секунд перед отправкой комнаты.",
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


def _team_roster(tournament: Any, team_data: dict) -> list[str]:
    """Имена состава: players[] или circle1-4."""
    players = [p for p in team_data.get("players", []) if p]
    if players:
        return players
    return [
        team_data.get(f"circle{c}") for c in range(1, 5)
        if team_data.get(f"circle{c}")
    ]


def _team_line(num_emoji: str, tournament: Any, team_data: dict) -> str:
    """Строка вида 'num_X <@p1> <@p2> <@p3> <@p4>'."""
    parts = []
    for name in _team_roster(tournament, team_data):
        uid = tournament.player_user_ids.get(name)
        parts.append(f"<@{uid}>" if uid else name)
    return f"{num_emoji} {' '.join(parts)}" if parts else f"{num_emoji} *"


async def send_room_dm_notifications(bot: Any, tournament: Any, team1_index: int, team2_index: int, room_id: str, room_password: str) -> None:
    """Send room info card to results channel + announcement to general channel."""
    team1_data = tournament.teams[team1_index] if team1_index < len(tournament.teams) else {}
    team2_data = tournament.teams[team2_index] if team2_index < len(tournament.teams) else {}

    # Название команды = имя капитана
    name1 = team1_data.get("captain", f"Team {team1_index}")
    name2 = team2_data.get("captain", f"Team {team2_index}")

    line1 = _team_line(get_emoji("num_1"), tournament, team1_data)
    line2 = _team_line(get_emoji("num_2"), tournament, team2_data)

    # Get specific channel
    channel = bot.get_channel(1561683314480255057)
    if channel:
        card = (
            f"**КОМНАТА ОТКРЫТА**\n"
            f"Матч: {name1} vs {name2}\n\n"
            f"{line1}\n{line2}\n\n"
            f"**Данные:**\n{room_id}\n{room_password}"
        )
        await channel.send( view=text_card(card))

    # Send to general channel (1200125075156910181)
    general_channel = bot.get_channel(1200125075156910181)
    if general_channel:
        await general_channel.send(
            view=text_card(f"{line1}\n{line2}\n\n Комната открыта, данные  <#1561683314480255057>")
        )