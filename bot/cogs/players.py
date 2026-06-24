"""Slash-команды добавления игроков."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.checks import is_admin
from bot.config import MAX_PLAYERS_PER_CIRCLE
from bot.message_manager import update_tournament_message
from bot.models import TournamentPhase
from bot.storage import storage


class PlayersCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    player_group = app_commands.Group(name="player", description="Управление игроками")

    @player_group.command(name="add", description="Добавить игроков (через запятую)")
    @app_commands.describe(names="Имена игроков: Player1,Player2,Player3")
    @is_admin()
    async def add(self, interaction: discord.Interaction, names: str) -> None:
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
                "❌ Игроков можно добавлять только на этапе настройки.",
                ephemeral=True,
            )
            return

        raw_names = [n.strip() for n in names.split(",") if n.strip()]
        if not raw_names:
            await interaction.response.send_message(
                "❌ Укажите хотя бы одного игрока.",
                ephemeral=True,
            )
            return

        existing = tournament.all_players()
        duplicates = [n for n in raw_names if n in existing]
        if duplicates:
            await interaction.response.send_message(
                f"❌ Игроки уже добавлены: {', '.join(duplicates)}",
                ephemeral=True,
            )
            return

        # Заполнение кругов: сначала 2, затем 3, затем 4
        remaining = list(raw_names)
        errors: list[str] = []

        for circle in ("2", "3", "4"):
            slot = MAX_PLAYERS_PER_CIRCLE - len(tournament.circles[circle])
            if slot <= 0:
                continue
            to_add = remaining[:slot]
            if not to_add:
                break
            tournament.circles[circle].extend(to_add)
            remaining = remaining[slot:]

        if remaining:
            errors.append(
                f"❌ Превышен лимит игроков. Не добавлено: {', '.join(remaining)}"
            )

        if errors and not any(tournament.circles[c] for c in ("2", "3", "4")):
            await interaction.response.send_message(errors[0], ephemeral=True)
            return

        storage.save(tournament)

        if errors:
            await interaction.response.send_message(errors[0], ephemeral=True)
        else:
            await interaction.response.send_message("✅ Игроки добавлены.", ephemeral=True)

        await update_tournament_message(self.bot, interaction.guild, tournament)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlayersCog(bot))
