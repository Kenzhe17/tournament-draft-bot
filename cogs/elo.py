"""Elo Rating System Cog."""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from ultimatum.elo_store import elo_store

logger = logging.getLogger(__name__)


class EloCog(commands.Cog):
    """Cog for Elo rating commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ulti", description="Show your Ultimatum Elo rating")
    async def show_elo(self, interaction: discord.Interaction) -> None:
        """Show personal Elo rating."""
        player = await elo_store.get_player(interaction.user.id)

        if not player:
            await interaction.response.send_message(
                "❌ You don't have an Elo rating yet. Play a rated match to get one!",
                ephemeral=True
            )
            return

        k_factor = elo_store.calculator.get_k_factor(player['rated_matches'])

        embed = discord.Embed(
            title=f"🎮 {player['username']}'s Rating",
            color=discord.Color.gold()
        )

        embed.add_field(
            name="Текущий Elo",
            value=str(player['elo']),
            inline=True
        )

        embed.add_field(
            name="Максимальный Elo",
            value=str(player['peak_elo']),
            inline=True
        )

        embed.add_field(
            name="Рейтинговых матчей",
            value=str(player['rated_matches']),
            inline=True
        )

        embed.add_field(
            name="Победы",
            value=str(player['wins']),
            inline=True
        )

        embed.add_field(
            name="Поражения",
            value=str(player['losses']),
            inline=True
        )

        embed.add_field(
            name="K-Factor",
            value=str(k_factor),
            inline=True
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="elo_leaderboard", description="Show global Elo leaderboard")
    @app_commands.describe(page="Page number (default 1)")
    async def show_leaderboard(
        self,
        interaction: discord.Interaction,
        page: int = 1
    ) -> None:
        """Show global Elo leaderboard."""
        limit = 10
        offset = (page - 1) * limit

        players = await elo_store.get_leaderboard(limit, offset)
        total = await elo_store.get_leaderboard_count()
        max_page = (total + limit - 1) // limit

        if page > max_page:
            await interaction.response.send_message(
                f"❌ Page {page} doesn't exist. Max page: {max_page}",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🏆 Global Elo Leaderboard",
            description=f"Page {page}/{max_page} ({total} total players)",
            color=discord.Color.gold()
        )

        for idx, player in enumerate(players, start=offset + 1):
            medal = ""
            if idx == 1:
                medal = "🥇 "
            elif idx == 2:
                medal = "🥈 "
            elif idx == 3:
                medal = "🥉 "

            embed.add_field(
                name=f"{medal}#{idx} {player['username']}",
                value=f"{player['elo']} Elo",
                inline=False
            )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ulti_history", description="Show your Ultimatum Elo match history")
    async def show_history(self, interaction: discord.Interaction) -> None:
        """Show last 10 rated matches."""
        history = await elo_store.get_player_history(interaction.user.id, 10)

        if not history:
            await interaction.response.send_message(
                "❌ No rated matches found.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📊 Elo History",
            description=f"Last {len(history)} matches",
            color=discord.Color.blue()
        )

        for entry in history:
            result_emoji = "✅" if entry['result'] == 'WIN' else "❌"
            change_color = "🟢" if entry['elo_change'] > 0 else "🔴" if entry['elo_change'] < 0 else "⚪"

            opponent_name = entry.get('opponent_name', 'Unknown')

            embed.add_field(
                name=f"{result_emoji} vs {opponent_name}",
                value=f"{entry['elo_before']} → {entry['elo_after']} ({change_color} {entry['elo_change']:+d})",
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Register cog."""
    await bot.add_cog(EloCog(bot))
    logger.info("Cog 'elo' loaded")
