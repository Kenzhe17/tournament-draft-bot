"""Thread management cog - creates and deletes game threads."""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from cogs.games.game_state import add_active_thread, remove_active_thread, has_active_thread, clear_user_games

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bot import TournamentBot


class ThreadCog(commands.Cog):
    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    thread = app_commands.Group(name="thread", description="Управление игровыми тредами")

    @thread.command(name="open", description="Открыть игровой тред (1 тред на пользователя)")
    @app_commands.describe(name="Название треда (опционально)")
    async def thread_open(self, interaction: discord.Interaction, name: str = None):
        """Create a game thread for the user."""
        user_id = interaction.user.id

        # Check if user already has an active thread
        if has_active_thread(user_id):
            await interaction.response.send_message(
                "❌ У вас уже есть открытый игровой тред. Закройте его с помощью `/thread delete` перед созданием нового.",
                ephemeral=True
            )
            return

        # Generate thread name if not provided
        if name is None:
            name = f"🎮 Игровой тред - {interaction.user.display_name}"

        # Check if already in a thread
        if isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "❌ Вы уже находитесь в треде. Используйте `/thread delete` для удаления текущего треда.",
                ephemeral=True
            )
            return

        # Create the thread
        await interaction.response.send_message(f"{interaction.user.mention} создаёт игровой тред...", ephemeral=False)
        original_message = await interaction.original_response()

        try:
            thread = await original_message.create_thread(
                name=name,
                auto_archive_duration=60
            )

            # Track this thread for the user
            add_active_thread(user_id, thread.id)

            await thread.send(f"🎮 Игровой тред создан! Теперь вы можете запускать игры: `/coin_flip`, `/rps`, `/mathquiz`")
        except discord.HTTPException as e:
            logger.error(f"Failed to create thread: {e}")
            await original_message.edit(content="❌ Не удалось создать тред. Попробуйте позже.")

    @thread.command(name="delete", description="Удалить текущий игровой тред")
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

        # Remove from active threads
        remove_active_thread(user_id)

        await interaction.response.send_message("🗑️ Тред будет удалён через 2 секунды...", ephemeral=True)
        import asyncio
        await asyncio.sleep(2)
        await thread.delete()


async def setup(bot: "TournamentBot"):
    await bot.add_cog(ThreadCog(bot))
