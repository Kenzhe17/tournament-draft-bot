"""Auto-unwarn cog for automatic warning removal via JuniperBot."""

import asyncio
import logging
import re

import discord
from discord.ext import commands

from config import AUTO_MOD_ALLOWED_USER_IDS

logger = logging.getLogger(__name__)


class UnwarnCog(commands.Cog):
    """Auto-unwarn functionality via JuniperBot."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Store pending unwarn operations: (channel_id, user_id) -> timestamp
        self.pending_unwarns = {}

    @commands.command(name="снятьпред", description="Автоматически снять последнее предупреждение через JuniperBot")
    async def unwarn(self, ctx: commands.Context, user: str):
        """Automatically remove the latest warning via JuniperBot."""
        # Check if user is allowed to use auto-mod features
        if ctx.author.id not in AUTO_MOD_ALLOWED_USER_IDS:
            await ctx.send("❌ У вас нет прав для использования этой команды.")
            return

        # Parse user ID from mention or raw ID
        user_id = None
        if "<@" in user:
            # Mention format: <@1234567890>
            match = re.search(r"<@!?(\d+)>", user)
            if match:
                user_id = int(match.group(1))
        else:
            # Raw ID
            try:
                user_id = int(user)
            except ValueError:
                await ctx.send("❌ Неверный формат пользователя. Используйте тег или ID.")
                return

        if not user_id:
            await ctx.send("❌ Не удалось определить ID пользователя.")
            return

        # Store the pending unwarn request with timestamp
        key = (ctx.channel.id, user_id)
        self.pending_unwarns[key] = asyncio.get_event_loop().time()

        # Send command to get user's warnings
        await ctx.send(f"/преды участник:<@{user_id}>")

        logger.info(f"Unwarn: Requested warnings for user {user_id} in channel {ctx.channel.id}")

        # Wait and check for response (non-blocking)
        # The actual processing happens in on_message

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listen for JuniperBot responses to /преды."""
        # Only process messages from bots
        if not message.author.bot:
            return

        # Look for embed that contains warning information
        if not message.embeds:
            return

        # Check if embed is from JuniperBot (usually contains warning info)
        for embed in message.embeds:
            # Look for warning case numbers in the embed
            # JuniperBot typically shows warnings like "Пред #123"
            embed_text = embed.description or ""
            embed_text += " ".join(field.value for field in embed.fields)

            # Try to find warning case numbers
            # Pattern: "Случай #123" or "Предупреждение #123"
            case_matches = re.findall(r"Случай\s*#(\d+)", embed_text, re.IGNORECASE)
            
            if case_matches:
                # Get the latest (highest) case number
                latest_case = max(int(num) for num in case_matches)
                
                # Check if this channel has a pending unwarn request
                for (channel_id, pending_user_id), timestamp in list(self.pending_unwarns.items()):
                    if channel_id == message.channel.id:
                        # Found pending request in this channel
                        user_id = pending_user_id if pending_user_id else None
                        
                        # Try to extract user ID from embed if not specified
                        if not user_id:
                            user_match = re.search(r"<@!?(\d+)>", embed_text)
                            if user_match:
                                user_id = int(user_match.group(1))
                        
                        if user_id:
                            # Execute unwarn
                            await self.execute_unwarn(message.channel, latest_case, user_id)
                            # Remove from pending
                            del self.pending_unwarns[(channel_id, pending_user_id)]
                            logger.info(f"Unwarn: Executed unwarn for case {latest_case}, user {user_id}")
                            break

    async def execute_unwarn(self, channel: discord.TextChannel, case_number: int, user_id: int):
        """Execute the unwarn command with delay."""
        # Wait a moment to ensure the /преды response is visible
        await asyncio.sleep(1.5)
        
        # Send the unwarn command
        await channel.send(f"/снятьпред номер_случая:{case_number}")
        logger.info(f"Unwarn: Executed unwarn for case {case_number}, user {user_id}")


async def setup(bot: commands.Bot):
    """Setup the unwarn cog."""
    await bot.add_cog(UnwarnCog(bot))
