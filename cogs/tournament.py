"""Slash-команды турнира."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from models.tournament import Tournament, TournamentPhase
from storage.json_store import store
from utils.embeds import build_setup_embed
from utils.permissions import is_admin

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


async def _delete_ephemeral_later(interaction: discord.Interaction, delay: float = 3.0) -> None:
    """Удалить ephemeral-ответ через указанное время."""
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


class TournamentCog(commands.Cog):
    """Ког с командами управления турниром."""

    def __init__(self, bot: TournamentBot):
        self.bot = bot

    tournament_group = app_commands.Group(
        name="tournament", description="Управление турниром"
    )
    captains_group = app_commands.Group(
        name="captains", description="Управление капитанами"
    )
    player_group = app_commands.Group(
        name="player", description="Управление игроками"
    )
    draft_group = app_commands.Group(
        name="draft", description="Управление драфтом"
    )

    @tournament_group.command(name="create", description="Создать новый турнир")
    @is_admin()
    async def tournament_create(self, interaction: discord.Interaction) -> None:
        """Создать турнир и отправить главное Embed-сообщение."""
        existing = store.get(interaction.guild_id)
        if existing and existing.phase != TournamentPhase.COMPLETE:
            await interaction.response.send_message(
                "❌ На сервере уже есть активный турнир.", ephemeral=True
            )
            return

        tournament = Tournament(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
        )
        embed = await build_setup_embed(tournament, interaction.guild)
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()

        tournament.message_id = message.id
        store.set(tournament)
        logger.info("Турнир создан на сервере %s", interaction.guild_id)

    @tournament_group.command(name="delete", description="Полностью удалить активный турнир")
    @is_admin()
    async def tournament_delete(self, interaction: discord.Interaction) -> None:
        """Удалить текущий турнир из хранилища и очистить состояние."""
        existing = store.get(interaction.guild_id)
        if not existing:
            await interaction.response.send_message(
                "❌ На этом сервере нет активных или созданных турниров.",
                ephemeral=True,
            )
            return

        store.delete(interaction.guild_id)

        logger.info("Турнир принудительно удален на сервере %s админом %s", interaction.guild_id, interaction.user.id)

        # 1. Делаем сообщение ephemeral=True, иначе Дискорд не разрешит боту его удалить
        await interaction.response.send_message(
            "🗑️ **Активный турнир был успешно удален.**",
            ephemeral=True
        )

        # 2. Запускаем таску на удаление через 3 секунды
        asyncio.create_task(_delete_ephemeral_later(interaction, 3.0))

    @tournament_group.command(name="replace", description="🔄 Заменить игрока в активном турнире")
    @app_commands.describe(
        old_name="Имя игрока, которого нужно заменить",
        new_name="Имя нового игрока"
    )
    @is_admin()
    async def tournament_replace(
        self,
        interaction: discord.Interaction,
        old_name: str,
        new_name: str
    ) -> None:
        """Заменить одного игрока на другого в составе сформированных команд драфта."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ На этом сервере нет активного турнира.",
                ephemeral=True,
            )
            return

        old_name = old_name.strip()
        new_name = new_name.strip()

        draft = tournament.draft
        if not draft or not draft.teams:
            await interaction.response.send_message(
                "❌ Команды ещё не сформированы. Замена на этом этапе невозможна.",
                ephemeral=True,
            )
            return

        # 1. Ищем и меняем игрока внутри сформированных команд драфта (в draft.teams)
        replaced_in_team = False
        for team_num, roster in draft.teams.items():
            if old_name in roster:
                idx = roster.index(old_name)
                roster[idx] = new_name
                replaced_in_team = True
                break

        if not replaced_in_team:
            await interaction.response.send_message(
                f"❌ Игрок с именем `{old_name}` не найден в составах команд.",
                ephemeral=True,
            )
            return

        # Сохраняем обновленные данные в store
        store.set(tournament)

        logger.info(
            "Администратор %s заменил игрока %s на %s в турнире %s",
            interaction.user.id, old_name, new_name, interaction.guild_id
        )

        # Скрытый ответ админу на 3 секунды
        await interaction.response.send_message(
            f"✅ Игрок `{old_name}` успешно заменен на `{new_name}`.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction, 3.0))

        # Перерисовываем главное сообщение турнира (сетка и списки обновятся мгновенно!)
        await self.bot.update_tournament_message(interaction.guild, tournament)

    @captains_group.command(name="add", description="Добавить 4 капитанов")
    @app_commands.describe(
        cap1="Капитан 1",
        cap2="Капитан 2",
        cap3="Капитан 3",
        cap4="Капитан 4",
    )
    @is_admin()
    async def captains_add(
        self,
        interaction: discord.Interaction,
        cap1: discord.Member,
        cap2: discord.Member,
        cap3: discord.Member,
        cap4: discord.Member,
    ) -> None:
        """Добавить капитанов по Discord Member ID."""
        tournament = store.get(interaction.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Сначала создайте турнир командой `/tournament create`.",
                ephemeral=True,
            )
            return

        captains = [cap1.id, cap2.id, cap3.id, cap4.id]
        if len(set(captains)) != 4:
            await interaction.response.send_message(
                "❌ Все 4 капитана должны быть разными пользователями.",
                ephemeral=True,
            )
            return

        tournament.captains = captains
        store.set(tournament)

        await interaction.response.send_message("Капитаны добавлены", ephemeral=True)
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    @player_group.command(name="add", description="Добавить игроков (через запятую)")
    @app_commands.describe(names="Имена игроков через запятую")
    @is_admin()
    async def player_add(
        self, interaction: discord.Interaction, names: str
    ) -> None:
        """Добавить текстовых игроков в круги 2→3→4."""
        tournament = store.get(interaction.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Сначала создайте турнир командой `/tournament create`.",
                ephemeral=True,
            )
            return

        raw_names = [n.strip() for n in names.split(",") if n.strip()]
        if not raw_names:
            await interaction.response.send_message(
                "❌ Укажите хотя бы одного игрока.", ephemeral=True
            )
            return

        added, rejected = tournament.add_players(raw_names)
        store.set(tournament)

        parts = []
        if added:
            parts.append(f"✅ Добавлено: {', '.join(added)}")
        if rejected:
            parts.append(
                f"❌ Отклонено (дубликат или лимит): {', '.join(rejected)}"
            )
        msg = "\n".join(parts) if parts else "Ничего не добавлено."

        await interaction.response.send_message(msg, ephemeral=True)
        asyncio.create_task(_delete_ephemeral_later(interaction, 5.0))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    @draft_group.command(name="start", description="Запустить драфт")
    @is_admin()
    async def draft_start(self, interaction: discord.Interaction) -> None:
        """Запустить драфт после проверки заполненности."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Сначала создайте турнир командой `/tournament create`.",
                ephemeral=True,
            )
            return

        if tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Драфт уже запущен или турнир завершён.", ephemeral=True
            )
            return

        if not tournament.is_setup_complete:
            await interaction.response.send_message(
                "❌ Турнир заполнен не полностью.", ephemeral=True
            )
            return

        tournament.start_draft()
        store.set(tournament)

        await interaction.response.send_message(
            "🎲 Драфт запущен!", ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Обработка ошибок slash-команд."""
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error) or "❌ Недостаточно прав."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return

        logger.exception("Ошибка команды: %s", error)
        msg = "❌ Произошла ошибка при выполнении команды."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: TournamentBot) -> None:
    """Загрузить ког."""
    await bot.add_cog(TournamentCog(bot))