"""GIF-based moderation cog for temporary mutes."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from config import MUTE_ROLE_ID, WARN_GIF_URL, UNWARN_GIF_URL, MOD_LOG_WEBHOOK_URL

logger = logging.getLogger(__name__)

# Timezone offset for GMT+5
TIMEZONE_OFFSET = timezone(timedelta(hours=5))

def format_timestamp(dt: datetime) -> str:
    """Format datetime to GMT+5 timestamp string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(TIMEZONE_OFFSET)
    return dt.strftime('%d.%m.%Y %H:%M:%S')

def format_date(dt: datetime) -> str:
    """Format datetime to GMT+5 date string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(TIMEZONE_OFFSET)
    return dt.strftime('%d.%m.%Y %H:%M')


# Authorized user IDs for GIF triggers
AUTHORIZED_GIF_USERS = {
    1032544122600423427,
    582145013358657549,
    575937439147753472
}


class ModerationCog(commands.Cog):
    """Moderation cog for GIF-based temporary mutes."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def send_mod_log(self, embed: discord.Embed):
        """Send log to webhook."""
        try:
            webhook = discord.Webhook.from_url(MOD_LOG_WEBHOOK_URL, client=self.bot)
            await webhook.send(embed=embed, username="Модератор")
        except Exception as e:
            logger.error(f"Failed to send mod log: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listen for gif triggers."""
        # Ignore messages from bots
        if message.author.bot:
            return

        # Check if message is a reply
        if not message.reference or not message.reference.message_id:
            return

        # Check if user is authorized for GIF triggers
        if message.author.id not in AUTHORIZED_GIF_USERS:
            return

        # Check for warn gif
        is_warn_gif = WARN_GIF_URL in message.content or "h2di-the-dictator" in message.content
        for attachment in message.attachments:
            if WARN_GIF_URL in attachment.url or "h2di-the-dictator" in attachment.url:
                is_warn_gif = True
                break

        # Check for unwarn gif
        is_unwarn_gif = UNWARN_GIF_URL in message.content or "slice-execute-4" in message.content
        for attachment in message.attachments:
            if UNWARN_GIF_URL in attachment.url or "slice-execute-4" in attachment.url:
                is_unwarn_gif = True
                break

        if not (is_warn_gif or is_unwarn_gif):
            return

        # Get the replied message
        channel = message.channel
        replied_message = await channel.fetch_message(message.reference.message_id)
        target_user = replied_message.author

        # Check if target has higher role than moderator
        if target_user.top_role.position > message.author.top_role.position:
            return

        if is_warn_gif:
            # Give 1 minute mute
            mute_role = message.guild.get_role(MUTE_ROLE_ID)
            if mute_role:
                await target_user.add_roles(mute_role, reason="Гифка мьют")

                # Use custom message from /молчать
                mute_msg = f"По воле диктатора участник {target_user.mention} будет молчать 1 мин"

                embed = discord.Embed(
                    description=mute_msg,
                    color=discord.Color(0xFFAA00)
                )

                msg = await channel.send(embed=embed)

                # Delete after 10 seconds
                await asyncio.sleep(10)
                await msg.delete()

                # Send detailed log to webhook
                current_timestamp = format_timestamp(datetime.now())
                expire_date = format_date(datetime.now() + timedelta(minutes=1))
                log_embed = discord.Embed(
                    description=(
                        f"**Участник {target_user.mention} ({target_user.name}) был замьючен на всём сервере**\n\n"
                        f"**Модератор:** {message.author.mention} ({message.author.name})\n"
                        f"**Канал:** <#{channel.id}>\n"
                        f"**Длительность:** 1 мин ({expire_date})\n\n"
                        f"`ID участника: {target_user.id}` • `{current_timestamp}`"
                    ),
                    color=discord.Color.orange()
                )
                await self.send_mod_log(log_embed)

        elif is_unwarn_gif:
            # Check if trying to remove mute from self
            if target_user.id == message.author.id:
                return

            # Check if target has higher role than moderator
            if target_user.top_role.position > message.author.top_role.position:
                return

            # Remove mute role
            mute_role = message.guild.get_role(MUTE_ROLE_ID)
            if mute_role and mute_role in target_user.roles:
                await target_user.remove_roles(mute_role, reason="Гифка размьют")

                # Use custom message from /говори
                unmute_msg = f"Участнику {target_user.mention} было даровано право говорить по воле Диктатора"

                embed = discord.Embed(
                    description=unmute_msg,
                    color=discord.Color(0xFFAA00)
                )

                msg = await channel.send(embed=embed)

                # Delete after 10 seconds
                await asyncio.sleep(10)
                await msg.delete()

                # Send detailed log to webhook
                current_timestamp = format_timestamp(datetime.now())
                log_embed = discord.Embed(
                    description=(
                        f"**Участник {target_user.mention} ({target_user.name}) был размьючен**\n\n"
                        f"**Модератор:** {message.author.mention} ({message.author.name})\n"
                        f"**Канал:** <#{channel.id}>\n\n"
                        f"`ID участника: {target_user.id}` • `{current_timestamp}`"
                    ),
                    color=discord.Color.green()
                )
                await self.send_mod_log(log_embed)


async def setup(bot: commands.Bot):
    """Setup the moderation cog."""
    await bot.add_cog(ModerationCog(bot))
