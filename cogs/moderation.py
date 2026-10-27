"""Warning and moderation system cog."""

import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import MUTE_ROLE_ID, WARN_GIF_URL, UNWARN_GIF_URL
from storage.warn_store import warn_store

logger = logging.getLogger(__name__)


# Mute duration scale
MUTE_SCALE = {
    1: None,
    2: None,
    3: None,
    4: timedelta(hours=1),
    5: timedelta(hours=2),
    6: timedelta(hours=4),
    7: timedelta(hours=8),
    8: timedelta(hours=12),
    9: timedelta(days=1),
    10: timedelta(days=1),
}


class ModerationCog(commands.Cog):
    """Warning and moderation system."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mute_check_task.start()
        logger.info("ModerationCog initialized")
    
    def cog_unload(self):
        """Clean up when cog is unloaded."""
        self.mute_check_task.cancel()
    
    @tasks.loop(minutes=1)
    async def mute_check_task(self):
        """Check for expired mutes and warnings every minute."""
        try:
            # Clear expired mutes
            expired_mutes = await warn_store.clear_expired_mutes()

            for user_id, guild_id in expired_mutes:
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    continue

                member = guild.get_member(user_id)
                if not member:
                    continue

                # Remove mute role
                mute_role = guild.get_role(MUTE_ROLE_ID)
                if mute_role and mute_role in member.roles:
                    await member.remove_roles(mute_role, reason="Мут истек")
                    logger.info(f"Removed mute role from {user_id} in guild {guild_id}")

            # Clear expired warnings
            expired_warns = await warn_store.clear_expired_warns()

            for user_id, guild_id in expired_warns:
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    continue

                member = guild.get_member(user_id)
                if not member:
                    continue

                # Check if user still has active warnings
                active_warns = await warn_store.get_active_warns(user_id, guild_id)
                if len(active_warns) < 4:
                    # If user has less than 4 active warnings, remove mute role
                    mute_role = guild.get_role(MUTE_ROLE_ID)
                    if mute_role and mute_role in member.roles:
                        await member.remove_roles(mute_role, reason="Предупреждение истекло")
                        logger.info(f"Removed mute role from {user_id} due to expired warning in guild {guild_id}")

        except Exception as e:
            logger.error(f"Error in mute check task: {e}", exc_info=True)
    
    @mute_check_task.before_loop
    async def before_mute_check(self):
        """Wait for bot to be ready before starting task."""
        await self.bot.wait_until_ready()
    
    async def apply_punishment(self, guild: discord.Guild, member: discord.Member, warn_count: int):
        """Apply punishment based on warning count."""
        duration = MUTE_SCALE.get(warn_count)
        
        if duration:
            mute_role = guild.get_role(MUTE_ROLE_ID)
            if mute_role:
                mute_until = datetime.utcnow() + duration
                await member.add_roles(mute_role, reason=f"Мут за {warn_count} предупреждений")
                await warn_store.set_mute_until(member.id, guild.id, mute_until)
                logger.info(f"Muted {member.id} for {duration}")
                return duration
        
        return None
    
    async def _warn_impl(self, interaction: discord.Interaction, участник: discord.Member, причина: str = "Без причины"):
        """Implementation of warn command."""
        # Check if user has moderator permissions
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ У вас нет прав для выдачи предупреждений.", ephemeral=True)
            return
        
        # Add warning
        warn = await warn_store.add_warn(участник.id, interaction.guild.id, interaction.user.id, причина)
        
        if not warn:
            await interaction.response.send_message("❌ Ошибка при добавлении предупреждения.", ephemeral=True)
            return
        
        # Get active warnings count
        active_warns = await warn_store.get_active_warns(участник.id, interaction.guild.id)
        warn_count = len(active_warns)
        
        # Apply punishment
        duration = await self.apply_punishment(interaction.guild, участник, warn_count)
        
        # Build response message
        if duration:
            # Format duration for display
            if duration.days >= 1:
                duration_text = f"{duration.days} дн"
            elif duration.seconds >= 3600:
                hours = duration.seconds // 3600
                duration_text = f"{hours} ч"
            else:
                duration_text = f"{duration.seconds // 60} мин"
            
            message = f"✅Участник {участник.mention} получил предупреждение `#{warn.warn_id}` (случай `#{warn.warn_id}`) и был замьючен. Он сможет говорить снова через {duration_text}"
        else:
            message = f"✅Участник {участник.mention} получил предупреждение `#{warn.warn_id}` (случай `#{warn.warn_id}`)"
        
        embed = discord.Embed(
            description=message,
            color=discord.Color(0xFFAA00)
        )
        
        await interaction.response.send_message(embed=embed)
        
        # Delete after 5 seconds
        await asyncio.sleep(5)
        msg = await interaction.original_response()
        await msg.delete()

    @app_commands.command(name="казнить", description="Выдать предупреждение")
    @app_commands.describe(участник="Пользователь для предупреждения", причина="Причина предупреждения")
    async def warn(self, interaction: discord.Interaction, участник: discord.Member, причина: str = "Без причины"):
        """Issue a warning to a user."""
        await self._warn_impl(interaction, участник, причина)
    
    @app_commands.command(name="пред", description="Выдать предупреждение")
    @app_commands.describe(участник="Пользователь для предупреждения", причина="Причина предупреждения")
    async def warn_alias(self, interaction: discord.Interaction, участник: discord.Member, причина: str = "Без причины"):
        """Alias for /казнить."""
        await self._warn_impl(interaction, участник, причина)
    
    async def _unwarn_impl(self, interaction: discord.Interaction, участник: discord.Member):
        """Implementation of unwarn command."""
        # Check if user has moderator permissions
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ У вас нет прав для снятия предупреждений.", ephemeral=True)
            return
        
        # Remove last warning
        warn = await warn_store.remove_last_warn(участник.id, interaction.guild.id)
        
        if not warn:
            await interaction.response.send_message("❌ У пользователя нет активных предупреждений.", ephemeral=True)
            return
        
        # Get remaining warnings
        active_warns = await warn_store.get_active_warns(участник.id, interaction.guild.id)
        warn_count = len(active_warns)
        
        # Check if mute should be reduced/removed
        if warn_count < 4:
            mute_role = interaction.guild.get_role(MUTE_ROLE_ID)
            if mute_role and mute_role in участник.roles:
                await участник.remove_roles(mute_role, reason="Снятие предупреждения")
        
        message = f"✅Предупреждение участника {участник.mention} #{warn.warn_id} снято"
        
        embed = discord.Embed(
            description=message,
            color=discord.Color(0xFFAA00)
        )
        
        await interaction.response.send_message(embed=embed)
        
        # Delete after 5 seconds
        await asyncio.sleep(5)
        msg = await interaction.original_response()
        await msg.delete()

    @app_commands.command(name="пощадить", description="Снять последнее предупреждение")
    @app_commands.describe(участник="Пользователь для снятия предупреждения")
    async def unwarn(self, interaction: discord.Interaction, участник: discord.Member):
        """Remove the most recent warning from a user."""
        await self._unwarn_impl(interaction, участник)
    
    @app_commands.command(name="снятьпред", description="Снять последнее предупреждение")
    @app_commands.describe(участник="Пользователь для снятия предупреждения")
    async def unwarn_alias(self, interaction: discord.Interaction, участник: discord.Member):
        """Alias for /пощадить."""
        await self._unwarn_impl(interaction, участник)
    
    async def _warns_impl(self, interaction: discord.Interaction, участник: discord.Member):
        """Implementation of warns command."""
        # Check if user has moderator permissions
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ У вас нет прав для просмотра предупреждений.", ephemeral=True)
            return
        
        # Get all warnings
        all_warns = await warn_store.get_all_warns(участник.id, interaction.guild.id)
        active_warns = await warn_store.get_active_warns(участник.id, interaction.guild.id)
        
        if not all_warns:
            await interaction.response.send_message(f"У пользователя {участник.mention} нет предупреждений.", ephemeral=True)
            return
        
        # Build embed
        embed = discord.Embed(
            title="Список предупреждений",
            description=f"Предупреждения участника {участник.mention}",
            color=discord.Color(0xFFAA00)
        )
        embed.set_thumbnail(url=f"https://cdn.discordapp.com/embed/avatars/{участник.id % 5}.png")
        
        # Add warning fields
        for warn in all_warns[:10]:  # Show last 10
            date_str = warn.timestamp.strftime('%d %B %Y г. %H:%M')
            status = "✅" if warn.active else "❌"
            field_name = f"{status} Случай `#{warn.warn_id}` **{date_str}**"

            # Get moderator info
            moderator_id = getattr(warn, 'moderator_id', interaction.user.id)
            moderator = interaction.guild.get_member(moderator_id)
            if moderator:
                moderator_mention = moderator.mention
            else:
                moderator_mention = f"<@{moderator_id}>"

            field_value = f"**Модератор:** {moderator_mention} (ID: `{moderator_id}`)\n"

            # Add expiration if exists
            if warn.expires_at:
                expires_str = warn.expires_at.strftime('%d %B %Y г. %H:%M')
                if warn.active:
                    # Calculate time remaining
                    remaining = warn.expires_at - datetime.utcnow()
                    days = remaining.days
                    hours = remaining.seconds // 3600
                    if days > 0:
                        remaining_text = f"*(через {days} дн)*"
                    elif hours > 0:
                        remaining_text = f"*(через {hours} ч)*"
                    else:
                        remaining_text = "*(скоро)*"
                    field_value += f"**Истекает:** `{expires_str}` {remaining_text}\n"
                else:
                    field_value += f"**Истекло:** `{expires_str}`\n"

            # Add mute_until if exists
            mute_until = await warn_store.get_mute_until(участник.id, interaction.guild.id)
            if mute_until:
                mute_until_str = mute_until.strftime('%d %B %Y г. %H:%M')
                # Calculate time remaining
                remaining = mute_until - datetime.utcnow()
                days = remaining.days
                hours = remaining.seconds // 3600
                if days > 0:
                    remaining_text = f"*(через {days} дн)*"
                elif hours > 0:
                    remaining_text = f"*(через {hours} ч)*"
                else:
                    remaining_text = "*(скоро)*"
                field_value += f"**Мут до:** `{mute_until_str}` {remaining_text}\n"

            # Add reason if exists
            if warn.reason and warn.reason != "Без причины":
                field_value += f"**Причина:** {warn.reason}\n"

            field_value += "───────────────────────────────────"
            embed.add_field(name=field_name, value=field_value, inline=False)
        
        # Footer
        total_warns = len(all_warns)
        embed.set_footer(text=f"Страница 1 из 1 — Всего предупреждений: {total_warns} • Запросил {interaction.user.display_name}")
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="преды", description="Просмотреть предупреждения пользователя")
    @app_commands.describe(участник="Пользователь для просмотра")
    async def warns(self, interaction: discord.Interaction, участник: discord.Member):
        """View all warnings for a user."""
        await self._warns_impl(interaction, участник)
    
    @app_commands.command(name="допускания", description="Просмотреть предупреждения пользователя")
    @app_commands.describe(участник="Пользователь для просмотра")
    async def warns_alias(self, interaction: discord.Interaction, участник: discord.Member):
        """Alias for /преды."""
        await self._warns_impl(interaction, участник)
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listen for gif triggers."""
        # Ignore messages from bots
        if message.author.bot:
            return
        
        # Check if message is a reply
        if not message.reference or not message.reference.message_id:
            return
        
        # Check if user has moderator permissions
        if not message.author.guild_permissions.moderate_members:
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
        
        if is_warn_gif:
            # Add warning
            warn = await warn_store.add_warn(target_user.id, message.guild.id, message.author.id, "Гифка-триггер")
            
            if warn:
                active_warns = await warn_store.get_active_warns(target_user.id, message.guild.id)
                warn_count = len(active_warns)
                duration = await self.apply_punishment(message.guild, target_user, warn_count)
                
                if duration:
                    # Format duration for display
                    if duration.days >= 1:
                        duration_text = f"{duration.days} дн"
                    elif duration.seconds >= 3600:
                        hours = duration.seconds // 3600
                        duration_text = f"{hours} ч"
                    else:
                        duration_text = f"{duration.seconds // 60} мин"
                    
                    warn_msg = f"✅Участник {target_user.mention} получил предупреждение `#{warn.warn_id}` (случай `#{warn.warn_id}`) и был замьючен. Он сможет говорить снова через {duration_text}"
                else:
                    warn_msg = f"✅Участник {target_user.mention} получил предупреждение `#{warn.warn_id}` (случай `#{warn.warn_id}`)"
                
                embed = discord.Embed(
                    description=warn_msg,
                    color=discord.Color(0xFFAA00)
                )
                
                msg = await channel.send(embed=embed)
                
                # Delete after 5 seconds
                await asyncio.sleep(5)
                await msg.delete()
        
        elif is_unwarn_gif:
            # Remove warning
            warn = await warn_store.remove_last_warn(target_user.id, message.guild.id)
            
            if warn:
                active_warns = await warn_store.get_active_warns(target_user.id, message.guild.id)
                warn_count = len(active_warns)
                
                if warn_count < 4:
                    mute_role = message.guild.get_role(MUTE_ROLE_ID)
                    if mute_role and mute_role in target_user.roles:
                        await target_user.remove_roles(mute_role, reason="Снятие через гифку")
                
                unwarn_msg = f"✅Предупреждение участника {target_user.mention} #{warn.warn_id} снято"
                
                embed = discord.Embed(
                    description=unwarn_msg,
                    color=discord.Color(0xFFAA00)
                )
                
                msg = await channel.send(embed=embed)
                
                # Delete after 5 seconds
                await asyncio.sleep(5)
                await msg.delete()


async def setup(bot: commands.Bot):
    """Setup the moderation cog."""
    await bot.add_cog(ModerationCog(bot))
