"""Thread management cog - deletes game threads."""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from cogs.games.game_state import clear_user_games
from utils.permissions import is_org

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bot import TournamentBot


class ThreadCog(commands.Cog):
    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="delete", description="Удалить текущий игровой тред")
    async def thread_delete(self, interaction: discord.Interaction):
        """Delete the current game thread."""
        user_id = interaction.user.id

        # Check if user is in a thread
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "❌ Эта команда работает только внутри треда.",
                ephemeral=True
            )
            return

        thread = interaction.channel

        # Clear all active games for this user
        clear_user_games(user_id)

        await interaction.response.send_message("🗑️ Тред будет удалён через 2 секунды...", ephemeral=True)
        import asyncio
        await asyncio.sleep(2)
        await thread.delete()


async def setup(bot: "TournamentBot"):
    await bot.add_cog(ThreadCog(bot))
