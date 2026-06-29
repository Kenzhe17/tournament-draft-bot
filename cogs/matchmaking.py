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

        embed = discord.Embed(
            title="🎮 Matchmaking",
            description="Поиск игры:\n0/8 игроков",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Players:",
            value="1.\n2.\n3.\n4.\n5.\n6.\n7.\n8.",
            inline=False
        )

        view = MatchmakingView(interaction.guild_id)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="mm_leave", description="Покинуть matchmaking")
    async def leave_matchmaking(self, interaction: discord.Interaction) -> None:
        """Покинуть текущую сессию matchmaking."""
        success = matchmaking_manager.remove_player(interaction.guild_id, interaction.user.id)

        if success:
            await interaction.response.send_message("✅ Вы покинули Matchmaking", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Вы не находитесь в Matchmaking", ephemeral=True)

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
