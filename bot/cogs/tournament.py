"""Slash-команды управления турниром."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.checks import is_admin
from bot.embeds import build_setup_embed
from bot.models import Tournament, TournamentPhase
from bot.storage import storage


class TournamentCog(commands.Cog):
    """Создание и управление турниром."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    tournament_group = app_commands.Group(name="tournament", description="Управление турниром")

    @tournament_group.command(name="create", description="Создать новый турнир")
    @is_admin()
    async def create(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Команда доступна только в текстовом канале.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id

        # Новый турнир заменяет предыдущий в этом guild
        tournament = Tournament(
            guild_id=guild_id,
            channel_id=interaction.channel.id,
            phase=TournamentPhase.SETUP,
        )

        embed = await build_setup_embed(interaction.guild, tournament)
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()

        tournament.message_id = message.id
        storage.save(tournament)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TournamentCog(bot))
