"""Bot settings management commands."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID, replace_emojis
from storage.json_store import store

logger = logging.getLogger(__name__)


class BotSettingsCog(commands.Cog):
    """Команды для управления настройками бота."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("BotSettingsCog initialized")

    @app_commands.group(name="off", description="Управление функциями бота")
    async def off(self, interaction: discord.Interaction):
        """Root command for bot settings."""
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Только владелец бота может использовать эту команду.",
                ephemeral=True
            )
            return
        
        await interaction.response.send_message(
            "Используйте `/off toggle` для включения/выключения функций.",
            ephemeral=True
        )

    @off.command(name="toggle", description="Включить/выключить функции бота")
    @app_commands.describe(welcome="Welcome message для новых участников", coin_drop="Спонтанные ивенты с монетами")
    async def toggle(
        self,
        interaction: discord.Interaction,
        welcome: bool = None,
        coin_drop: bool = None
    ):
        """Включить или выключить функции бота."""
        logger.info(f"/off toggle called by user {interaction.user.id}, welcome={welcome}, coin_drop={coin_drop}")
        
        # Check if user is bot owner
        if interaction.user.id != BOT_OWNER_ID:
            logger.warning(f"User {interaction.user.id} is not bot owner (BOT_OWNER_ID={BOT_OWNER_ID})")
            await interaction.response.send_message(
                "❌ Только владелец бота может использовать эту команду.",
                ephemeral=True
            )
            return
        
        logger.info(f"User {interaction.user.id} is bot owner, proceeding")
        
        # Load settings from store
        settings = store.get_settings() or {}
        logger.info(f"Current settings: {settings}")
        
        response_messages = []
        
        if welcome is not None:
            settings["welcome_enabled"] = welcome
            status = "включены" if welcome else "отключены"
            response_messages.append(f"{replace_emojis('✅')} Welcome сообщения {status}.")
            logger.info(f"Welcome enabled set to {welcome}")
        
        if coin_drop is not None:
            settings["coin_drop_enabled"] = coin_drop
            status = "включены" if coin_drop else "отключены"
            response_messages.append(f"{replace_emojis('✅')} Спонтанные ивенты {status}.")
            logger.info(f"Coin drop enabled set to {coin_drop}")
        
        if welcome is None and coin_drop is None:
            logger.warning("No options specified")
            await interaction.response.send_message(
                f"{replace_emojis('❌')} Укажите хотя бы одну опцию для изменения.",
                ephemeral=True
            )
            return
        
        # Save settings
        store.set_settings(settings)
        logger.info(f"Settings saved: {settings}")
        
        await interaction.response.send_message(
            "\n".join(response_messages),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    """Setup the bot settings cog."""
    await bot.add_cog(BotSettingsCog(bot))
