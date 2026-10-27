"""Bot settings management commands."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID, replace_emojis
from storage.json_store import store


class BotSettingsCog(commands.Cog):
    """Команды для управления настройками бота."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    settings_group = app_commands.Group(name="off", description="Управление функциями бота")

    @settings_group.command(name="toggle", description="Включить/выключить функции бота")
    @app_commands.describe(welcome="Welcome message для новых участников", coin_drop="Спонтанные ивенты с монетами")
    async def toggle(
        self,
        interaction: discord.Interaction,
        welcome: bool = None,
        coin_drop: bool = None
    ):
        """Включить или выключить функции бота."""
        # Check if user is bot owner
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Только владелец бота может использовать эту команду.",
                ephemeral=True
            )
            return
        # Load settings from store
        settings = store.get_settings() or {}
        
        if welcome is not None:
            settings["welcome_enabled"] = welcome
            status = "включены" if welcome else "отключены"
            await interaction.response.send_message(
                f"{replace_emojis('✅')} Welcome сообщения {status}.",
                ephemeral=True
            )
        
        if coin_drop is not None:
            settings["coin_drop_enabled"] = coin_drop
            status = "включены" if coin_drop else "отключены"
            await interaction.response.send_message(
                f"{replace_emojis('✅')} Спонтанные ивенты {status}.",
                ephemeral=True
            )
        
        if welcome is None and coin_drop is None:
            await interaction.response.send_message(
                f"{replace_emojis('❌')} Укажите хотя бы одну опцию для изменения.",
                ephemeral=True
            )
            return
        
        # Save settings
        store.set_settings(settings)


async def setup(bot: commands.Bot):
    """Setup the bot settings cog."""
    await bot.add_cog(BotSettingsCog(bot))
