"""Modal for Ultimatum room ID and password."""

import time
import discord
from config import replace_emojis

# Cooldown для отправки комнаты (1 секунда)
ultimatum_room_cooldowns = {}  # (guild_id, match_id) -> timestamp


class UltimatumRoomModal(discord.ui.Modal, title="Комната игры"):
    """Modal for entering room ID and password for Ultimatum match."""

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

    def __init__(self, match_id: int, tournament_id: str):
        super().__init__()
        self.match_id = match_id
        self.tournament_id = tournament_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Handle room ID and password submission."""
        from ultimatum.store import ultimatum_store

        # Defer immediately to avoid timeout
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.DiscordServerError):
            pass

        # Check cooldown (1 second)
        cooldown_key = (interaction.guild_id, self.match_id)
        current_time = time.time()
        if cooldown_key in ultimatum_room_cooldowns:
            elapsed = current_time - ultimatum_room_cooldowns[cooldown_key]
            if elapsed < 1:
                try:
                    await interaction.followup.send(
                        f"❌ Подождите {1 - elapsed:.1f} секунд перед отправкой комнаты.",
                        ephemeral=True
                    )
                except (discord.NotFound, discord.DiscordServerError):
                    pass
                return

        # Get match details
        match = await ultimatum_store.get_match_by_id(self.match_id)
        if not match:
            try:
                await interaction.followup.send("❌ Матч не найден.", ephemeral=True)
            except (discord.NotFound, discord.DiscordServerError):
                pass
            return

        # Check if room already exists
        if match.room_id:
            try:
                await interaction.followup.send(
                    "❌ Комната уже была создана другим игроком.",
                    ephemeral=True
                )
            except (discord.NotFound, discord.DiscordServerError):
                pass
            return

        # Validate inputs
        if not self.room_id.value or not self.room_password.value:
            try:
                await interaction.followup.send(
                    "❌ ID и пароль комнаты не могут быть пустыми.",
                    ephemeral=True
                )
            except (discord.NotFound, discord.DiscordServerError):
                pass
            return

        # Set cooldown BEFORE sending
        ultimatum_room_cooldowns[cooldown_key] = time.time()

        # Update match with room data
        await ultimatum_store.update_match_room(
            self.match_id,
            self.room_id.value,
            self.room_password.value
        )

        # Send room notifications to players
        await send_ultimatum_room_notifications(
            interaction.client,
            match.player1_id,
            match.player2_id,
            self.room_id.value,
            self.room_password.value
        )

        # Confirm to user
        try:
            await interaction.followup.send("✅ Комната создана и данные отправлены.", ephemeral=True)
        except (discord.NotFound, discord.DiscordServerError):
            pass


async def send_ultimatum_room_notifications(
    bot,
    player1_id: int,
    player2_id: int,
    room_id: str,
    room_password: str
) -> None:
    """Send room info to specific channel for Ultimatum match."""
    # Get specific channel
    channel = bot.get_channel(1561683314480255057)
    if not channel:
        return

    # Get player names
    p1 = bot.get_user(player1_id)
    p2 = bot.get_user(player2_id)
    p1_name = p1.display_name if p1 else f"<@{player1_id}>"
    p2_name = p2.display_name if p2 else f"<@{player2_id}>"

    # Create ping string
    pings = f"<@{player1_id}> <@{player2_id}>"

    # Send messages
    await channel.send(content=f"**КОМНАТА ОТКРЫТА**\nМатч: {p1_name} vs {p2_name}\n\n{pings}\n\n**Данные:**")
    await channel.send(content=room_id)
    await channel.send(content=room_password)
    await channel.send(content="https://cdn.discordapp.com/attachments/1560907251412238396/1561697607770832957/382752-purplelines.gif?ex=6ad74bbc&is=6ad5fa3c&hm=e731b1784d4a8945004feccc0b7ae13373bd92341484f8f9e3cddc3c4c8120ff&")

    # Send to general channel
    general_channel = bot.get_channel(1200125075156910181)
    if general_channel:
        await general_channel.send(content=f"{pings}\n\n{replace_emojis('white_dot')} Комната открыта, данные {replace_emojis('white_arrow')} <#1561683314480255057>")
