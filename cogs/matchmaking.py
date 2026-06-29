"""Slash-команды для matchmaking."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from matchmaking.manager import matchmaking_manager
from models.match import MatchPhase, MatchStatus
from storage.player_stats_store import player_stats_store
from storage.user_balance_store import user_balance_store
from utils.permissions import is_admin

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class MatchmakingCog(commands.Cog):
    """Ког с командами управления matchmaking."""

    def __init__(self, bot: TournamentBot):
        self.bot = bot

    @app_commands.command(name="matchmaking", description="Создать matchmaking embed")
    @is_admin()
    async def create_matchmaking(self, interaction: discord.Interaction) -> None:
        """Создать embed для matchmaking в канале."""
        from views.matchmaking_view import MatchmakingView

        # Создаем сессию если её нет
        session = matchmaking_manager.get_session(interaction.guild_id)
        if not session:
            session = matchmaking_manager.create_session(interaction.guild_id, interaction.channel_id)

        embed = discord.Embed(
            title="🎮 Matchmaking Lobby",
            description="Поиск игры:\n0/8 игроков",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Players:",
            value="1.\n2.\n3.\n4.\n5.\n6.\n7.\n8.",
            inline=False
        )

        view = MatchmakingView(interaction.guild_id)
        message = await interaction.response.send_message(embed=embed, view=view)

        # Сохраняем message_id в сессии
        session.match.main_message_id = message.id
        session.match.main_channel_id = interaction.channel_id

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
    await bot.add_cog(MatchmakingCog(bot))
