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


from cogs.groups import admin_group, bind_commands


class ThreadCog(commands.Cog):
    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        bind_commands(admin_group, self, ["delete_thread"])

    @admin_group.command(name="delete_thread", description="Удалить текущий игровой тред")
    async def thread_delete(self, interaction: discord.Interaction):
        """Delete the current game thread."""
        user_id = interaction.user.id

        # Check if user is in a thread
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Эта команда работает только внутри треда.",
                ephemeral=True
            )
            return

        thread = interaction.channel

        # Clear all active games for this user
        clear_user_games(user_id)

        from datetime import datetime
        await interaction.response.send_message(f"🗑️ Тред будет удалён <t:{int(datetime.now().timestamp()) + 2}:R>...", ephemeral=True)
        import asyncio
        await asyncio.sleep(2)
        await thread.delete()


async def setup(bot: "TournamentBot"):
    await bot.add_cog(ThreadCog(bot))
