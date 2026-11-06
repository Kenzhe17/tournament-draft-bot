"""Точка входа — Discord-бот для турнирного драфта."""

from __future__ import annotations

import logging
import sys

import discord
from discord.ext import commands

from config import DATABASE_URL, DISCORD_TOKEN
from storage.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class TournamentBot(commands.Bot):
    """Основной класс бота."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            max_ratelimit_timeout=30.0,
            max_ratelimit_retries=5
        )

    async def setup_hook(self) -> None:
        """Синхронизация slash-команд и инициализация."""
        # Initialize database if DATABASE_URL is set
        if DATABASE_URL:
            try:
                await init_db()
                logger.info("Database initialized")
            except Exception as e:
                logger.error("Failed to initialize database: %s", e)

        # Load cogs
        await self.load_extension("cogs.tournament")
        await self.load_extension("cogs.mm")
        await self.load_extension("cogs.ultimatum")

        # Sync commands globally
        try:
            synced = await self.tree.sync()
            logger.info(f"Slash-команды синхронизированы глобально: {len(synced)} команд")
        except Exception as e:
            logger.error(f"Ошибка синхронизации команд: {e}")

        # Force sync for main guild
        MAIN_GUILD_ID = 1196165488565289012
        try:
            guild_synced = await self.tree.sync(guild=discord.Object(id=MAIN_GUILD_ID))
            logger.info(f"Синхронизировано {len(guild_synced)} команд для основного сервера {MAIN_GUILD_ID}")
        except Exception as e:
            logger.error(f"Ошибка синхронизации для основного сервера: {e}")

        # Set up error handler
        self.tree.on_error = self.on_app_command_error

    async def on_app_command_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """Handle app command errors."""
        logger.error(f"App command error: {error}", exc_info=True)

        # Log to Discord
        if interaction.guild and interaction.user:
            from utils.logging import log_command_error
            await log_command_error(self, interaction.guild, interaction.user, error)

        # Send error message to user
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Произошла ошибка при выполнении команды.", ephemeral=True)
            else:
                await interaction.response.send_message("Произошла ошибка при выполнении команды.", ephemeral=True)
        except Exception:
            pass

    async def on_ready(self) -> None:
        logger.info("Бот запущен как %s (ID: %s)", self.user, self.user.id)

        # Sync commands with Discord
        try:
            synced = await self.tree.sync()
            logger.info(f"Синхронизировано {len(synced)} slash-команд")
        except Exception as e:
            logger.error(f"Ошибка синхронизации команд: {e}")

        # Debug: Log all registered commands
        commands = list(self.tree.walk_commands())
        logger.info(f"Зарегистрировано {len(commands)} команд:")
        for cmd in commands:
            cmd_type = cmd.type.__name__ if hasattr(cmd, 'type') else 'Group'
            logger.info(f"  - /{cmd.name} (type: {cmd_type})")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Log when bot is added to a server."""
        from utils.logging import log_guild_join
        await log_guild_join(self, guild)

    async def on_member_join(self, member: discord.Member) -> None:
        """Send welcome message when a member joins the server."""
        # Only send for the specific server
        if member.guild.id != 1196165488565289012:
            return

        WELCOME_CHANNEL_ID = 1200125075156910181

        # Get the welcome channel
        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if not channel:
            try:
                channel = await member.guild.fetch_channel(WELCOME_CHANNEL_ID)
            except discord.HTTPException:
                logger.error(f"Welcome channel {WELCOME_CHANNEL_ID} not found")
                return

        # Calculate member count
        member_count = member.guild.member_count + 1

        # Send welcome message
        await channel.send(
            f"₊˚⊹♡ **Добро пожаловать на сервер**, {member.mention}!\n\n"
            f"ты стал(а) нашим **{member_count:,}-м** участником ♡\n"
            f"чувствуй себя как дома, поприветствуй всех и заглядывай в чат!\n\n"
            f"୨୧ надеемся, тебе у нас понравится ୨୧"
        )


def main() -> None:
    """Запуск бота."""
    if not DISCORD_TOKEN:
        logger.error(
            "DISCORD_TOKEN не задан. Скопируйте .env.example в .env и укажите токен."
        )
        sys.exit(1)

    bot = TournamentBot()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
