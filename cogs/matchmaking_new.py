"""Slash-команды matchmaking."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from models.matchmaking import Matchmaking, MatchmakingPhase
from storage.matchmaking_store import matchmaking_store
from utils.matchmaking_embeds import build_matchmaking_embed
from views.matchmaking_setup_view import build_matchmaking_setup_view

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class NewMatchmakingCog(commands.Cog):
    """Ког с командами matchmaking."""

    def __init__(self, bot: TournamentBot):
        self.bot = bot

    @app_commands.command(name="mm_create", description="Создать matchmaking")
    async def mm_create(self, interaction: discord.Interaction) -> None:
        """Создать новый matchmaking."""
        # Check if matchmaking already exists
        existing = matchmaking_store.get(interaction.guild_id)
        if existing:
            await interaction.response.send_message(
                "❌ Matchmaking уже существует на этом сервере.",
                ephemeral=True
            )
            return

        # Create new matchmaking
        matchmaking = Matchmaking(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
        )

        matchmaking_store.set(matchmaking)

        # Send initial message
        embed = await build_matchmaking_embed(matchmaking, interaction.guild)
        view = build_matchmaking_setup_view(matchmaking)

        message = await interaction.channel.send(embed=embed, view=view)

        # Save message_id
        matchmaking.message_id = message.id
        matchmaking_store.set(matchmaking)

        await interaction.response.send_message(
            "✅ Matchmaking создан!",
            ephemeral=True
        )

    @app_commands.command(name="mm_delete", description="Удалить matchmaking")
    async def mm_delete(self, interaction: discord.Interaction) -> None:
        """Удалить matchmaking."""
        matchmaking = matchmaking_store.get(interaction.guild_id)
        if not matchmaking:
            await interaction.response.send_message(
                "❌ Matchmaking не найден.",
                ephemeral=True
            )
            return

        matchmaking_store.delete(interaction.guild_id)

        await interaction.response.send_message(
            "✅ Matchmaking удален.",
            ephemeral=True
        )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Обработка ошибок slash-команд."""
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error) or "❌ Недостаточно прав."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except discord.NotFound:
                pass
            return

        logger.exception("Ошибка команды: %s", error)
        msg = "❌ Произошла ошибка при выполнении команды."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.NotFound:
            pass


async def setup(bot: TournamentBot) -> None:
    """Загрузить ког."""
    await bot.add_cog(NewMatchmakingCog(bot))
