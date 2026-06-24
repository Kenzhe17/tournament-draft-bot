"""Slash-команды управления турниром."""

from __future__ import annotations

import asyncio
import logging
import discord
from discord import app_commands
from discord.ext import commands

from bot.checks import is_admin
from bot.embeds import build_setup_embed
from bot.models import Tournament, TournamentPhase
from bot.storage import storage
from bot.services.message_manager import update_tournament_message

logger = logging.getLogger(__name__)


async def _delete_ephemeral_later(interaction: discord.Interaction, delay: float = 3.0) -> None:
    """Удалить ephemeral-ответ через указанное время."""
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


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

    @tournament_group.command(name="replace", description="Заменить игрока в активном турнире")
    @app_commands.describe(
        old_name="Имя игрока, которого нужно заменить",
        new_name="Имя нового игрока"
    )
    @is_admin()
    async def player_replace(
        self,
        interaction: discord.Interaction,
        old_name: str,
        new_name: str
    ) -> None:
        """Заменить одного игрока на другого на любом этапе турнира."""
        if not interaction.guild_id:
            return

        tournament = storage.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ На этом сервере нет активного турнира.",
                ephemeral=True,
            )
            return

        old_name = old_name.strip()
        new_name = new_name.strip()

        # 1. Находим и меняем игрока в командах
        replaced_in_team = False
        for team in tournament.teams:
            if old_name in team.players:
                idx = team.players.index(old_name)
                team.players[idx] = new_name
                replaced_in_team = True
                break

        # 2. Меняем в общем списке участников, если он есть в модели
        if hasattr(tournament, "players") and old_name in tournament.players:
            idx = tournament.players.index(old_name)
            tournament.players[idx] = new_name

        if not replaced_in_team:
            await interaction.response.send_message(
                f"❌ Игрок с именем `{old_name}` не найден ни в одной из команд.",
                ephemeral=True,
            )
            return

        # Сохраняем состояние в твой storage
        storage.save(tournament)

        logger.info(
            "Администратор %s заменил игрока %s на %s в турнире %s",
            interaction.user.id, old_name, new_name, interaction.guild_id
        )

        # Отвечаем админу скрытым сообщением на 3 секунды
        await interaction.response.send_message(
            f"✅ Игрок `{old_name}` успешно заменен на `{new_name}`.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction, 3.0))

        # Перерисовываем главное сообщение турнира — сетка обновится «на лету»
        await update_tournament_message(self.bot, interaction.guild, tournament)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TournamentCog(bot))