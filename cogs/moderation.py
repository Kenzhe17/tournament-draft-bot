"""Warning and moderation system cog."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

from config import MUTE_ROLE_ID, WARN_GIF_URL, UNWARN_GIF_URL, MOD_LOG_WEBHOOK_URL
from storage.warn_store import warn_store

logger = logging.getLogger(__name__)

# Timezone offset for GMT+5
TIMEZONE_OFFSET = timezone(timedelta(hours=5))

def format_timestamp(dt: datetime) -> str:
    """Format datetime to GMT+5 timestamp string."""
    if dt.tzinfo is None:
        # Convert naive datetime to timezone-aware
        dt = dt.replace(tzinfo=timezone.utc).astimezone(TIMEZONE_OFFSET)
    return dt.strftime('%d.%m.%Y %H:%M:%S')

def format_date(dt: datetime) -> str:
    """Format datetime to GMT+5 date string."""
    if dt.tzinfo is None:
        # Convert naive datetime to timezone-aware
        dt = dt.replace(tzinfo=timezone.utc).astimezone(TIMEZONE_OFFSET)
    return dt.strftime('%d.%m.%Y %H:%M')


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

# Authorized user IDs for GIF triggers
AUTHORIZED_GIF_USERS = {
    1032544122600423427,
    582145013358657549,
    575937439147753472
}


class WarnPaginationView(ui.View):
    """Pagination view for warnings list."""

    def __init__(self, warnings: list, user: discord.Member, interaction_user: discord.Member, guild_id: int):
        super().__init__(timeout=None)
        self.warnings = warnings
        self.user = user
        self.interaction_user = interaction_user
        self.guild_id = guild_id
        self.current_page = 0
        self.per_page = 5
        self.total_pages = (len(warnings) + self.per_page - 1) // self.per_page
        self.update_buttons()

        # Update button custom_ids to be unique
        self.prev_button.custom_id = f"warn_prev_{user.id}"
        self.next_button.custom_id = f"warn_next_{user.id}"

    def update_buttons(self):
        """Update button states based on current page."""
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1

    async def build_embed(self) -> discord.Embed:
        """Build embed for current page."""
        start_idx = self.current_page * self.per_page
        end_idx = start_idx + self.per_page
        page_warns = self.warnings[start_idx:end_idx]

        embed = discord.Embed(
            title="Список предупреждений",
            description=f"Предупреждения участника {self.user.mention}",
            color=discord.Color(0xFFAA00)
        )
        embed.set_thumbnail(url=f"https://cdn.discordapp.com/embed/avatars/{self.user.id % 5}.png")

        for warn in page_warns:
            date_str = format_date(warn.timestamp)

            # Get moderator info
            moderator_id = getattr(warn, 'moderator_id', self.interaction_user.id)
            moderator_mention = f"<@{moderator_id}>"

            # Build field value
            field_value = f"**Модератор:** {moderator_mention} (ID: `{moderator_id}`)\n"

            # Add expiration (warning duration) if exists
            if warn.expires_at:
                expires_str = format_date(warn.expires_at)
                if warn.active:
                    # Calculate time remaining
                    remaining = warn.expires_at - datetime.now()
                    days = remaining.days
                    hours = remaining.seconds // 3600
                    if days > 0:
                        remaining_text = f"(`через {days} дн`)"
                    elif hours > 0:
                        remaining_text = f"(`через {hours} ч`)"
                    else:
                        remaining_text = "(`скоро`)"
                    field_value += f"**Истекает:** `{expires_str}` {remaining_text}\n"
                else:
                    field_value += f"**Истекло:** `{expires_str}`\n"

            # Add mute_until if exists
            mute_until = await warn_store.get_mute_until(self.user.id, self.guild_id)
            if mute_until:
                mute_until_str = format_date(mute_until)
                # Calculate time remaining
                remaining = mute_until - datetime.now()
                days = remaining.days
                hours = remaining.seconds // 3600
                if days > 0:
                    remaining_text = f"(`через {days} дн`)"
                elif hours > 0:
                    remaining_text = f"(`через {hours} ч`)"
                else:
                    remaining_text = "(`скоро`)"
                field_value += f"**Мут до:** `{mute_until_str}` {remaining_text}\n"

            # Add reason if exists
            if warn.reason and warn.reason != "Без причины":
                field_value += f"**Причина:** {warn.reason}\n"

            field_value += "───────────────────────────────────"

            # Add field with case number and date in name (no emojis)
            case_id = getattr(warn, 'case_id', warn.warn_id)
            warn_number = getattr(warn, 'warn_number', warn.warn_id)
            field_name = f"Случай `#{case_id}` **{date_str}**"
            embed.add_field(name=field_name, value=field_value, inline=False)

        # Footer
        total_warns = len(self.warnings)
        embed.set_footer(text=f"Страница {self.current_page + 1} из {self.total_pages} — Всего предупреждений: {total_warns} • Запросил {self.interaction_user.display_name}")

        return embed

    @ui.button(label="◀ Предыдущая", style=discord.ButtonStyle.secondary, custom_id="warn_prev")
    async def prev_button(self, interaction: discord.Interaction, button: ui.Button):
        """Previous page button."""
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @ui.button(label="Следующая ▶", style=discord.ButtonStyle.secondary, custom_id="warn_next")
    async def next_button(self, interaction: discord.Interaction, button: ui.Button):
        """Next page button."""
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=await self.build_embed(), view=self)


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
            # Clear expired mutes (mute_until in DB is already cleared by clear_expired_mutes)
            expired_mutes = await warn_store.clear_expired_mutes()

            for user_id, guild_id in expired_mutes:
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    continue

                member = guild.get_member(user_id)
                if not member:
                    continue

                # Send log for expired mute
                current_timestamp = format_timestamp(datetime.now())
                log_embed = discord.Embed(
                    description=(
                        f"**Срок мьюта участника {member.mention} ({member.name}) истек**\n\n"
                        f"**Случай:** `#Авто`\n\n"
                        f"`ID участника: {member.id}` • `{current_timestamp}`"
                    ),
                    color=discord.Color.green()
                )
                await self.send_mod_log(log_embed)

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

                # Send log for expired warning
                current_timestamp = format_timestamp(datetime.now())
                log_embed = discord.Embed(
                    description=(
                        f"**Предупреждение участника {member.mention} ({member.name}) истекло**\n\n"
                        f"**Модератор:** Бот (Автоматически)\n"
                        f"**Случай:** `#Авто`\n\n"
                        f"`ID участника: {member.id}` • `{current_timestamp}`"
                    ),
                    color=discord.Color.green()
                )
                await self.send_mod_log(log_embed)

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

    async def send_mod_log(self, embed: discord.Embed):
        """Send moderation log to webhook."""
        if not MOD_LOG_WEBHOOK_URL:
            logger.warning("MOD_LOG_WEBHOOK_URL not set, skipping log")
            return
        try:
            webhook = discord.Webhook.from_url(MOD_LOG_WEBHOOK_URL, client=self.bot)
            await webhook.send(embed=embed, username="Модератор")
        except Exception as e:
            logger.error(f"Failed to send mod log: {e}", exc_info=True)
    
    async def apply_punishment(self, guild: discord.Guild, member: discord.Member, warn_count: int):
        """Apply punishment based on warning count."""
        duration = MUTE_SCALE.get(warn_count)

        if duration:
            mute_role = guild.get_role(MUTE_ROLE_ID)
            if mute_role:
                mute_until = datetime.now() + duration
                await member.add_roles(mute_role, reason=f"Мут за {warn_count} предупреждений")
                await warn_store.set_mute_until(member.id, guild.id, mute_until)
                logger.info(f"Muted {member.id} for {duration}")
                return duration

        return None
    
    async def _warn_impl(self, interaction: discord.Interaction, участник: discord.Member, причина: str = "Без причины", custom: bool = False):
        """Implementation of warn command."""
        # Check if user has admin permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ У вас нет прав для выдачи предупреждений.", ephemeral=True)
            return

        # Check if target has higher role than moderator
        if участник.top_role.position > interaction.user.top_role.position:
            await interaction.response.send_message("❌ Нельзя выдавать предупреждения пользователям с более высокой ролью.", ephemeral=True)
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

        # Build response message based on command type
        if custom:
            # Custom format for /казнить
            message = f"**Приговор исполнен.** На участника `{участник.global_name or участник.name}` клеймом наложено предупреждение `#{warn.warn_number}` (дело `#{warn.case_id}`)."
        else:
            # Standard format for /пред
            if duration:
                # Format duration for display
                if duration.days >= 1:
                    duration_text = f"{duration.days} дн"
                elif duration.seconds >= 3600:
                    hours = duration.seconds // 3600
                    duration_text = f"{hours} ч"
                else:
                    duration_text = f"{duration.seconds // 60} мин"

                message = f"Участник {участник.mention} получил предупреждение #{warn.warn_number} случай #{warn.case_id}!\n**Модератор:** {interaction.user.mention}\n**Срок мьюта:** {duration_text}"
            else:
                message = f"Участник {участник.mention} получил предупреждение #{warn.warn_number} случай #{warn.case_id}!\n**Модератор:** {interaction.user.mention}\n**Срок мьюта:** нет"

        embed = discord.Embed(
            description=message,
            color=discord.Color(0xFFAA00)
        )

        await interaction.response.send_message(embed=embed)

        # Delete after 10 seconds
        await asyncio.sleep(10)
        msg = await interaction.original_response()
        await msg.delete()

        # Send detailed log to webhook
        current_timestamp = format_timestamp(datetime.now())
        expires_str = format_date(warn.expires_at)
        log_embed = discord.Embed(
            description=(
                f"**Участник {участник.mention} ({участник.name}) получил предупреждение**\n\n"
                f"**Модератор:** {interaction.user.mention} ({interaction.user.name})\n"
                f"**Канал:** <#{interaction.channel.id}>\n"
                f"**Предупреждение:** `#{warn.warn_number}`\n"
                f"**Случай:** `#{warn.case_id}`\n"
                f"**Длительность:** 7д ({expires_str})\n"
                f"────────────────────────\n"
                f"**Причина**\n"
                f"{причина}\n\n"
                f"`ID участника: {участник.id}` • `{current_timestamp}`"
            ),
            color=discord.Color.orange()
        )
        await self.send_mod_log(log_embed)

    @app_commands.command(name="казнить", description="Выдать предупреждение")
    @app_commands.describe(участник="Пользователь для предупреждения", причина="Причина предупреждения")
    async def warn(self, interaction: discord.Interaction, участник: discord.Member, причина: str = "Без причины"):
        """Issue a warning to a user."""
        await self._warn_impl(interaction, участник, причина, custom=True)
    
    @app_commands.command(name="пред", description="Выдать предупреждение")
    @app_commands.describe(участник="Пользователь для предупреждения", причина="Причина предупреждения")
    async def warn_alias(self, interaction: discord.Interaction, участник: discord.Member, причина: str = "Без причины"):
        """Alias for /казнить."""
        await self._warn_impl(interaction, участник, причина)
    
    async def _unwarn_impl(self, interaction: discord.Interaction, участник: discord.Member, custom: bool = False):
        """Implementation of unwarn command."""
        # Check if user has admin permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ У вас нет прав для снятия предупреждений.", ephemeral=True)
            return

        # Check if trying to remove warning from self
        if участник.id == interaction.user.id:
            await interaction.response.send_message("❌ Нельзя снимать предупреждения с самого себя.", ephemeral=True)
            return

        # Check if target has higher role than moderator
        if участник.top_role.position > interaction.user.top_role.position:
            await interaction.response.send_message("❌ Нельзя снимать предупреждения с пользователей с более высокой ролью.", ephemeral=True)
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

        # Build response message based on command type
        if custom:
            # Custom format for /пощадить
            message = f"**Высочайшая милость.** Предупреждение `#{warn.warn_number}` участника `{участник.global_name or участник.name}` аннулировано."
        else:
            # Standard format for /снятьпред
            message = f"✅ С участника {участник.mention} снята предупреждение #{warn.warn_number} (случай #{warn.case_id})!"

        embed = discord.Embed(
            description=message,
            color=discord.Color(0xFFAA00)
        )

        await interaction.response.send_message(embed=embed)

        # Delete after 10 seconds
        await asyncio.sleep(10)
        msg = await interaction.original_response()
        await msg.delete()

        # Send detailed log to webhook
        current_timestamp = format_timestamp(datetime.now())
        log_embed = discord.Embed(
            description=(
                f"**Предупреждение снято с участника {участник.mention} ({участник.name})**\n\n"
                f"**Модератор:** {interaction.user.mention} ({interaction.user.name})\n"
                f"**Канал:** <#{interaction.channel.id}>\n"
                f"**Случай:** `#{warn.case_id}`\n\n"
                f"`ID участника: {участник.id}` • `{current_timestamp}`"
            ),
            color=discord.Color.green()
        )
        await self.send_mod_log(log_embed)

    @app_commands.command(name="пощадить", description="Снять последнее предупреждение")
    @app_commands.describe(участник="Пользователь для снятия предупреждения")
    async def unwarn(self, interaction: discord.Interaction, участник: discord.Member):
        """Remove the most recent warning from a user."""
        await self._unwarn_impl(interaction, участник, custom=True)

    @app_commands.command(name="снятьпред", description="Снять последнее предупреждение")
    @app_commands.describe(участник="Пользователь для снятия предупреждения")
    async def unwarn_alias(self, interaction: discord.Interaction, участник: discord.Member):
        """Alias for /пощадить."""
        await self._unwarn_impl(interaction, участник, custom=False)
    
    async def _warns_impl(self, interaction: discord.Interaction, участник: discord.Member):
        """Implementation of warns command."""
        # Check if user has admin permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ У вас нет прав для просмотра предупреждений.", ephemeral=True)
            return

        # Get all warnings
        all_warns = await warn_store.get_all_warns(участник.id, interaction.guild.id)

        if not all_warns:
            await interaction.response.send_message(f"У пользователя {участник.mention} нет предупреждений.", ephemeral=True)
            return

        # Create pagination view
        view = WarnPaginationView(all_warns, участник, interaction.user, interaction.guild.id)
        embed = await view.build_embed()

        if len(all_warns) > 5:
            # Only show buttons if there are more than 5 warnings
            await interaction.response.send_message(embed=embed, view=view)
        else:
            # No pagination needed
            view = None
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

    async def _mute_impl(self, interaction: discord.Interaction, участник: discord.Member, длительность: str, custom: bool = False):
        """Implementation of mute command."""
        # Check if user has admin permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ У вас нет прав для выдачи мута.", ephemeral=True)
            return

        # Check if target has higher role than moderator
        if участник.top_role.position > interaction.user.top_role.position:
            await interaction.response.send_message("❌ Нельзя выдавать мут пользователям с более высокой ролью.", ephemeral=True)
            return

        # Parse duration
        duration = self.parse_duration(длительность)
        if not duration:
            await interaction.response.send_message("❌ Неверный формат длительности. Используйте: 1ч, 30м, 1д", ephemeral=True)
            return

        # Give mute role
        mute_role = interaction.guild.get_role(MUTE_ROLE_ID)
        if not mute_role:
            await interaction.response.send_message("❌ Роль мута не найдена.", ephemeral=True)
            return

        await участник.add_roles(mute_role, reason=f"Мут от {interaction.user.display_name}")

        # Set mute_until in database
        mute_until = datetime.now() + duration
        await warn_store.set_mute_until(участник.id, interaction.guild.id, mute_until)

        # Format duration for display
        if duration.days >= 1:
            duration_text = f"{duration.days} дн"
        elif duration.seconds >= 3600:
            hours = duration.seconds // 3600
            duration_text = f"{hours} ч"
        else:
            duration_text = f"{duration.seconds // 60} мин"

        # Use custom message for /молчать, standard for /мьют
        if custom:
            message = f"По воле диктатора участник {участник.mention} будет молчать {duration_text}"
        else:
            message = f"✅ Участник {участник.mention} замьючен!\n**Модератор:** {interaction.user.mention}\n**Срок мьюта:** {duration_text}"

        embed = discord.Embed(
            description=message,
            color=discord.Color(0xFFAA00)
        )

        await interaction.response.send_message(embed=embed)

        # Delete after 10 seconds
        await asyncio.sleep(10)
        msg = await interaction.original_response()
        await msg.delete()

        # Send detailed log to webhook
        current_timestamp = format_timestamp(datetime.now())
        expire_date = format_date(datetime.now() + duration)
        log_embed = discord.Embed(
            description=(
                f"**Участник {участник.mention} ({участник.name}) был замьючен на всём сервере**\n\n"
                f"**Модератор:** {interaction.user.mention} ({interaction.user.name})\n"
                f"**Канал:** <#{interaction.channel.id}>\n"
                f"**Длительность:** {duration_text} ({expire_date})\n\n"
                f"`ID участника: {участник.id}` • `{current_timestamp}`"
            ),
            color=discord.Color.orange()
        )
        await self.send_mod_log(log_embed)

    def parse_duration(self, duration_str: str) -> timedelta:
        """Parse duration string like '1ч', '30м', '1д'."""
        duration_str = duration_str.lower().strip()
        if not duration_str:
            return None

        # Extract number and unit
        unit = duration_str[-1]
        try:
            num = int(duration_str[:-1])
        except ValueError:
            return None

        if unit == 'ч' or unit == 'h':
            return timedelta(hours=num)
        elif unit == 'м' or unit == 'm':
            return timedelta(minutes=num)
        elif unit == 'д' or unit == 'd':
            return timedelta(days=num)
        else:
            return None

    async def _unmute_impl(self, interaction: discord.Interaction, участник: discord.Member, custom: bool = False):
        """Implementation of unmute command."""
        # Check if user has admin permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ У вас нет прав для снятия мута.", ephemeral=True)
            return

        # Check if trying to unmute self
        if участник.id == interaction.user.id:
            await interaction.response.send_message("❌ Нельзя снимать мут с самого себя.", ephemeral=True)
            return

        # Check if target has higher role than moderator
        if участник.top_role.position > interaction.user.top_role.position:
            await interaction.response.send_message("❌ Нельзя снимать мут с пользователей с более высокой ролью.", ephemeral=True)
            return

        # Remove mute role
        mute_role = interaction.guild.get_role(MUTE_ROLE_ID)
        if not mute_role:
            await interaction.response.send_message("❌ Роль мута не найдена.", ephemeral=True)
            return

        if mute_role not in участник.roles:
            await interaction.response.send_message("❌ У пользователя нет роли мута.", ephemeral=True)
            return

        await участник.remove_roles(mute_role, reason=f"Размут от {interaction.user.display_name}")

        # Clear mute_until in database
        await warn_store.clear_mute_until(участник.id, interaction.guild.id)

        # Use custom message for /говори, standard for /размьют
        if custom:
            message = f"Участнику {участник.mention} было даровано право говорить по воле Диктатора"
        else:
            message = f"✅ Участник {участник.mention} был размьючен!"

        embed = discord.Embed(
            description=message,
            color=discord.Color(0xFFAA00)
        )

        await interaction.response.send_message(embed=embed)

        # Delete after 10 seconds
        await asyncio.sleep(10)
        msg = await interaction.original_response()
        await msg.delete()

        # Send detailed log to webhook
        current_timestamp = format_timestamp(datetime.now())
        log_embed = discord.Embed(
            description=(
                f"**Участник {участник.mention} ({участник.name}) был размьючен**\n\n"
                f"**Модератор:** {interaction.user.mention} ({interaction.user.name})\n"
                f"**Канал:** <#{interaction.channel.id}>\n\n"
                f"`ID участника: {участник.id}` • `{current_timestamp}`"
            ),
            color=discord.Color.green()
        )
        await self.send_mod_log(log_embed)

    @app_commands.command(name="мьют", description="Выдать мут пользователю")
    @app_commands.describe(участник="Пользователь для мута", длительность="Длительность мута (например: 1ч, 30м, 1д)")
    async def mute(self, interaction: discord.Interaction, участник: discord.Member, длительность: str):
        """Mute a user for a specified duration."""
        await self._mute_impl(interaction, участник, длительность, custom=False)

    @app_commands.command(name="молчать", description="Молчать")
    @app_commands.describe(участник="Пользователь для мута", длительность="Длительность мута (например: 1ч, 30м, 1д)")
    async def mute_custom(self, interaction: discord.Interaction, участник: discord.Member, длительность: str):
        """Custom mute command with dictator-style message."""
        await self._mute_impl(interaction, участник, длительность, custom=True)

    @app_commands.command(name="запретить", description="Молчать")
    @app_commands.describe(участник="Пользователь для мута", длительность="Длительность мута (например: 1ч, 30м, 1д)")
    async def mute_alias(self, interaction: discord.Interaction, участник: discord.Member, длительность: str):
        """Alias for /молчать."""
        await self._mute_impl(interaction, участник, длительность, custom=True)

    @app_commands.command(name="размьют", description="Снять мут с пользователя")
    @app_commands.describe(участник="Пользователь для снятия мута")
    async def unmute(self, interaction: discord.Interaction, участник: discord.Member):
        """Unmute a user."""
        await self._unmute_impl(interaction, участник, custom=False)

    @app_commands.command(name="говори", description="Говори")
    @app_commands.describe(участник="Пользователь для снятия мута")
    async def unmute_custom(self, interaction: discord.Interaction, участник: discord.Member):
        """Custom unmute command with dictator-style message."""
        await self._unmute_impl(interaction, участник, custom=True)

    @app_commands.command(name="разрешить", description="Говори")
    @app_commands.describe(участник="Пользователь для снятия мута")
    async def unmute_alias(self, interaction: discord.Interaction, участник: discord.Member):
        """Alias for /говори."""
        await self._unmute_impl(interaction, участник, custom=True)
    
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
            # Add warning
            warn = await warn_store.add_warn(target_user.id, message.guild.id, message.author.id, "Воля Диктатора")

            if warn:
                active_warns = await warn_store.get_active_warns(target_user.id, message.guild.id)
                warn_count = len(active_warns)
                duration = await self.apply_punishment(message.guild, target_user, warn_count)

                # Use custom format for GIF (same as /казнить)
                warn_msg = f"**Приговор исполнен.** На участника `{target_user.global_name or target_user.name}` клеймом наложено предупреждение `#{warn.warn_number}` (дело `#{warn.case_id}`)."

                embed = discord.Embed(
                    description=warn_msg,
                    color=discord.Color(0xFFAA00)
                )

                msg = await channel.send(embed=embed)

                # Delete after 10 seconds
                await asyncio.sleep(10)
                await msg.delete()

                # Send detailed log to webhook
                current_timestamp = format_timestamp(datetime.now())
                expires_str = format_date(warn.expires_at)
                log_embed = discord.Embed(
                    description=(
                        f"**Участник {target_user.mention} ({target_user.name}) получил предупреждение**\n\n"
                        f"**Модератор:** {message.author.mention} ({message.author.name})\n"
                        f"**Канал:** <#{channel.id}>\n"
                        f"**Предупреждение:** `#{warn.warn_number}`\n"
                        f"**Случай:** `#{warn.case_id}`\n"
                        f"**Длительность:** 7д ({expires_str})\n"
                        f"────────────────────────\n"
                        f"**Причина**\n"
                        f"Воля Диктатора\n\n"
                        f"`ID участника: {target_user.id}` • `{current_timestamp}`"
                    ),
                    color=discord.Color.orange()
                )
                await self.send_mod_log(log_embed)
        
        elif is_unwarn_gif:
            # Check if trying to remove warning from self
            if target_user.id == message.author.id:
                return

            # Check if target has higher role than moderator
            if target_user.top_role.position > message.author.top_role.position:
                return

            # Remove warning
            warn = await warn_store.remove_last_warn(target_user.id, message.guild.id)
            
            if warn:
                active_warns = await warn_store.get_active_warns(target_user.id, message.guild.id)
                warn_count = len(active_warns)
                
                if warn_count < 4:
                    mute_role = message.guild.get_role(MUTE_ROLE_ID)
                    if mute_role and mute_role in target_user.roles:
                        await target_user.remove_roles(mute_role, reason="Снятие через гифку")

                unwarn_msg = f"**Высочайшая милость.** Предупреждение `#{warn.warn_number}` участника `{target_user.global_name or target_user.name}` аннулировано."

                embed = discord.Embed(
                    description=unwarn_msg,
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
                        f"**Предупреждение снято с участника {target_user.mention} ({target_user.name})**\n\n"
                        f"**Модератор:** {message.author.mention} ({message.author.name})\n"
                        f"**Канал:** <#{channel.id}>\n"
                        f"**Случай:** `#{warn.case_id}`\n\n"
                        f"`ID участника: {target_user.id}` • `{current_timestamp}`"
                    ),
                    color=discord.Color.green()
                )
                await self.send_mod_log(log_embed)


async def setup(bot: commands.Bot):
    """Setup the moderation cog."""
    await bot.add_cog(ModerationCog(bot))
