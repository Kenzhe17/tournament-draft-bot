"""Auto-moderation cog for gif-triggered warnings."""

import logging
import re

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
            logger.info(f"Auto-mod: Ignored bot message from {message.author.id}")
            return

        # Check if user is allowed to use auto-mod features
        if message.author.id not in AUTO_MOD_ALLOWED_USER_IDS:
            logger.info(f"Auto-mod: User {message.author.id} not in allowed list")
            return

        # Check if message is a reply
        if not message.reference or not message.reference.message_id:
            logger.info(f"Auto-mod: Message from {message.author.id} is not a reply")
            return

        # Check if message contains the target gif
        is_target_gif = False
        
        # Check in message content (partial match for shortened URLs)
        if AUTO_MOD_GIF_URL in message.content or "h2di-the-dictator" in message.content:
            is_target_gif = True
            logger.info(f"Auto-mod: Found target gif in message content from {message.author.id}")
        
        # Check in attachments (partial match)
        for attachment in message.attachments:
            if AUTO_MOD_GIF_URL in attachment.url or "h2di-the-dictator" in attachment.url:
                is_target_gif = True
                logger.info(f"Auto-mod: Found target gif in attachment from {message.author.id}")
                break

        # Log all URLs found in message for debugging
        if not is_target_gif:
            urls = re.findall(r'https?://[^\s<>"]+', message.content)
            attachment_urls = [a.url for a in message.attachments]
            logger.info(f"Auto-mod: URLs in message: {urls}")
            logger.info(f"Auto-mod: Attachment URLs: {attachment_urls}")
            logger.info(f"Auto-mod: Target URL: {AUTO_MOD_GIF_URL}")

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
