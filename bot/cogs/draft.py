"""Slash-команды запуска драфта."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.checks import is_admin
from bot.draft_engine import init_draft
from bot.message_manager import update_tournament_message
from bot.models import TournamentPhase
from bot.storage import storage


class DraftCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    draft_group = app_commands.Group(name="draft", description="Управление драфтом")

    @draft_group.command(name="start", description="Запустить драфт")
    @is_admin()
    async def start(self, interaction: discord.Interaction) -> None:
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
                "❌ Драфт уже был запущен или турнир завершён.",
                ephemeral=True,
            )
            return

        if not tournament.is_setup_complete():
            await interaction.response.send_message(
                "❌ Турнир заполнен не полностью.",
                ephemeral=True,
            )
            return

        tournament.draft = init_draft(tournament.captains)
        tournament.phase = TournamentPhase.DRAFT
        tournament.bracket = None
        storage.save(tournament)

        await interaction.response.send_message("🎲 Драфт начался!", ephemeral=True)
        await update_tournament_message(self.bot, interaction.guild, tournament)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DraftCog(bot))
