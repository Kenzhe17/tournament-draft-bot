"""Slash-команды управления турниром."""

from __future__ import annotations

import asyncio
import logging
import discord
from discord import app_commands
from discord.ext import commands

from bot.checks import is_admin
from bot.embeds import build_tournament_embed, build_setup_embed
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
        """Создать турнир и отправить главное Embed-сообщение."""
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Команда доступна только в текстовом канале.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id

        # Проверяем, есть ли уже незавершенный турнир
        existing = storage.get(guild_id)
        if existing and existing.phase != TournamentPhase.COMPLETE:
            await interaction.response.send_message(
                "❌ На сервере уже есть активный турнир. Сначала удалите его с помощью `/tournament delete`.",
                ephemeral=True
            )
            return

        # Создаем чистый турнир
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
        logger.info("Турнир успешно создан на сервере %s", guild_id)

    @tournament_group.command(name="delete", description="Полностью удалить активный турнир")
    @is_admin()
    async def tournament_delete(self, interaction: discord.Interaction) -> None:
        """Удалить текущий турнир из хранилища."""
        if not interaction.guild_id:
            return

        existing = storage.get(interaction.guild_id)
        if not existing:
            await interaction.response.send_message(
                "❌ На этом сервере нет активных турниров для удаления.",
                ephemeral=True,
            )
            return

        storage.delete(interaction.guild_id)
        logger.info("Турнир принудительно удален администратором %s", interaction.user.id)

        await interaction.response.send_message(
            "🗑️ **Активный турнир был успешно удален.**",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction, 3.0))

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
        replaced_in_team = False

        # 1. Проверяем активный ДРАФТ (Вариант фазы драфта)
        if hasattr(tournament, "draft") and tournament.draft and hasattr(tournament.draft, "teams") and tournament.draft.teams:
            for team_num, roster in tournament.draft.teams.items():
                if isinstance(roster, list) and old_name in roster:
                    idx = roster.index(old_name)
                    roster[idx] = new_name
                    replaced_in_team = True
                    logger.info("Игрок %s заменен на %s в драфте команды %s", old_name, new_name, team_num)
                    break

        # 2. Проверяем основные КОМАНДЫ (Если драфт прошел и фаза сменилась на TEAMS/SEMIFINALS/etc.)
        if not replaced_in_team and hasattr(tournament, "teams") and tournament.teams:
            for team in tournament.teams:
                if hasattr(team, "players") and old_name in team.players:
                    idx = team.players.index(old_name)
                    team.players[idx] = new_name
                    replaced_in_team = True
                    break
                elif isinstance(team, dict) and old_name in team.get("players", []):
                    idx = team["players"].index(old_name)
                    team["players"][idx] = new_name
                    replaced_in_team = True
                    break

        # 3. Меняем в общих списках (кругах регистрации), если игрок заменяется в самом начале
        for circle_attr in ("circle2", "circle3", "circle4"):
            if hasattr(tournament, circle_attr):
                circle_list = getattr(tournament, circle_attr)
                if isinstance(circle_list, list) and old_name in circle_list:
                    idx = circle_list.index(old_name)
                    circle_list[idx] = new_name
                    logger.info("Игрок заменен в списке %s", circle_attr)

        if hasattr(tournament, "players") and tournament.players and old_name in tournament.players:
            idx = tournament.players.index(old_name)
            tournament.players[idx] = new_name

        # Если нигде не нашли совпадений, сообщаем об этом админу
        if not replaced_in_team:
            await interaction.response.send_message(
                f"❌ Игрок с именем `{old_name}` не найден в текущих составах команд.",
                ephemeral=True,
            )
            return

        # Сохраняем измененное состояние в storage
        storage.save(tournament)

        logger.info(
            "Администратор %s заменил игрока %s на %s в турнире %s",
            interaction.user.id, old_name, new_name, interaction.guild_id
        )

        # Отвечаем скрытым сообщением
        await interaction.response.send_message(
            f"✅ Игрок `{old_name}` успешно заменен на `{new_name}`.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction, 3.0))

        # Моментально перерисовываем главное сообщение во всех фазах
        await update_tournament_message(self.bot, interaction.guild, tournament)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TournamentCog(bot))