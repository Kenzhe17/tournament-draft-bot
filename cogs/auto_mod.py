"""Auto-moderation cog for gif-triggered warnings."""

import logging

import discord
from discord.ext import commands

from config import AUTO_MOD_GIF_URL, AUTO_MOD_ALLOWED_USER_IDS

logger = logging.getLogger(__name__)


class AutoModCog(commands.Cog):
    """Auto-moderation for gif-triggered warnings."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Check for gif trigger and issue warning via JuniperBot."""
        # Ignore messages from bots
        if message.author.bot:
            return

        # Check if user is allowed to use auto-mod features
        if message.author.id not in AUTO_MOD_ALLOWED_USER_IDS:
            return

        # Check if message is a reply
        if not message.reference or not message.reference.message_id:
            return

        # Check if message contains the target gif
        is_target_gif = False
        
        # Check in message content
        if AUTO_MOD_GIF_URL in message.content:
            is_target_gif = True
        
        # Check in attachments
        for attachment in message.attachments:
            if attachment.url == AUTO_MOD_GIF_URL:
                is_target_gif = True
                break

        # If the target gif is found
        if is_target_gif:
            try:
                # Get the original message that was replied to
                channel = message.channel
                replied_message = await channel.fetch_message(message.reference.message_id)
                
                # The author of the original message is the violator
                violator = replied_message.author

                # Format the command for JuniperBot
                punish_command = f"/пред участник:<@{violator.id}> длительность:7д Выдача наказания гифкой"

                # Send the command to the channel (JuniperBot will process it)
                await channel.send(punish_command)
                
                logger.info(f"Auto-mod: Issued warning to {violator.id} ({violator.display_name}) via gif trigger by {message.author.id}")
            except Exception as e:
                logger.error(f"Error in auto-mod gif trigger: {e}", exc_info=True)


async def setup(bot: commands.Bot):
    """Setup the auto-mod cog."""
    await bot.add_cog(AutoModCog(bot))
