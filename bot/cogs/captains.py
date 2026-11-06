"""Slash-команды добавления капитанов."""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from bot.checks import is_admin
from bot.config import MAX_CAPTAINS
from bot.message_manager import update_tournament_message
from bot.models import TournamentPhase
from bot.storage import storage


async def _delete_ephemeral_later(interaction: discord.Interaction, delay: float = 3.0) -> None:
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


class CaptainsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    captains_group = app_commands.Group(name="captains", description="Управление капитанами")

    @captains_group.command(name="add", description="Добавить 4 капитанов")
    @app_commands.describe(
        cap1="Капитан 1",
        cap2="Капитан 2",
        cap3="Капитан 3",
        cap4="Капитан 4",
    )
    @is_admin()
    async def add(
        self,
        interaction: discord.Interaction,
        cap1: discord.Member,
        cap2: discord.Member,
        cap3: discord.Member,
        cap4: discord.Member,
    ) -> None:
        if not interaction.guild:
            return

        tournament = storage.get(interaction.guild.id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Сначала создайте турнир: /tournament create",
                ephemeral=True,
            )
            return

        if tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Капитанов можно добавлять только на этапе настройки.",
                ephemeral=True,
            )
            return

        captains = [cap1.id, cap2.id, cap3.id, cap4.id]
        if len(set(captains)) != MAX_CAPTAINS:
            await interaction.response.send_message(
                "❌ Все 4 капитана должны быть разными.",
                ephemeral=True,
            )
            return

        tournament.captains = captains
        storage.save(tournament)

        await interaction.response.send_message("Капитаны добавлены", ephemeral=True)
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await update_tournament_message(self.bot, interaction.guild, tournament)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CaptainsCog(bot))
