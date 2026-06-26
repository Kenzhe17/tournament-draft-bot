"""Slash-команды турнира."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from models.tournament import (
    RegistrationState,
    Tournament,
    TournamentPhase,
    TournamentSize,
)
from storage.json_store import store
from utils.embeds import build_setup_embed
from utils.permissions import is_admin

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


async def _delete_ephemeral_later(interaction: discord.Interaction, delay: float = 4.0) -> None:
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

    @app_commands.command(name="tournament", description="Создать новый турнир")
    @app_commands.describe(size="Размер турнира: 8, 16 или 32 игрока")
    @is_admin()
    async def tournament_create(
        self, interaction: discord.Interaction, size: str
    ) -> None:
        """Создать турнир с указанным размером."""
        existing = store.get(interaction.guild_id)
        if existing and existing.phase != TournamentPhase.COMPLETE:
            await interaction.response.send_message(
                "❌ На сервере уже есть активный турнир.", ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Validate size
        try:
            tournament_size = TournamentSize(size)
        except ValueError:
            await interaction.response.send_message(
                "❌ Неверный размер. Используйте: 8, 16 или 32.", ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament = Tournament(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            size=tournament_size,
        )
        embed = await build_setup_embed(tournament, interaction.guild)
        view = self.bot.build_view_for_tournament(tournament)
        self.bot._register_view(view)
        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()

        tournament.message_id = message.id
        store.set(tournament)
        logger.info("Турнир создан на сервере %s с размером %s", interaction.guild_id, size)

    @app_commands.command(name="delete", description="Удалить активный турнир")
    @is_admin()
    async def tournament_delete(self, interaction: discord.Interaction) -> None:
        """Удалить текущий турнир."""
        existing = store.get(interaction.guild_id)
        if not existing:
            await interaction.response.send_message(
                "❌ На этом сервере нет активного турнира.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        store.delete(interaction.guild_id)
        logger.info("Турнир удален на сервере %s", interaction.guild_id)

        await interaction.response.send_message(
            "🗑️ **Турнир успешно удален.**",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

    @app_commands.command(name="start", description="Запустить драфт")
    @is_admin()
    async def start_draft(self, interaction: discord.Interaction) -> None:
        """Запустить драфт после заполнения игроков."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Сначала создайте турнир командой `/tournament`.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                f"❌ Турнир не в фазе настройки. Текущая фаза: {tournament.phase.value}",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if not tournament.is_setup_complete:
            captain_count = tournament.captain_count
            msg = f"❌ Турнир заполнен не полностью. Нужно {captain_count} капитан(а/ов), ровно {captain_count} игрок(а/ов) в круге 2, ровно {captain_count} игрок(а/ов) в круге 3 и минимум {captain_count} игрок(а/ов) в круге 4."
            await interaction.response.send_message(msg, ephemeral=True)
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament.start_draft()
        store.set(tournament)

        await interaction.response.send_message(
            "🎲 Драфт запущен!", ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    @app_commands.command(name="close", description="Закрыть регистрацию (только админ добавляет)")
    @is_admin()
    async def close_registration(self, interaction: discord.Interaction) -> None:
        """Закрыть регистрацию игроков."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Нет активного турнира.", ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament.registration = RegistrationState.CLOSED
        store.set(tournament)

        await self.bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            "🔒 Регистрация закрыта. Только админ может добавлять игроков.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

    @app_commands.command(name="open", description="Открыть регистрацию (игроки добавляются сами)")
    @is_admin()
    async def open_registration(self, interaction: discord.Interaction) -> None:
        """Открыть регистрацию игроков."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Нет активного турнира.", ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament.registration = RegistrationState.OPEN
        store.set(tournament)

        await self.bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            "🔓 Регистрация открыта! Игроки могут добавляться через кнопки.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

    @app_commands.command(name="test", description="Тестовый запуск (заполнить турнир фиктивными именами)")
    @is_admin()
    async def test_start(self, interaction: discord.Interaction) -> None:
        """Заполнить турнир тестовыми данными и запустить драфт."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Сначала создайте турнир командой `/tournament`.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Турнир уже запущен.", ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Fill with test data based on tournament size
        tournament.is_test = True
        captain_count = tournament.captain_count
        tournament.captains = [f"Cap{i+1}" for i in range(captain_count)]
        
        # Fill circles based on tournament size
        tournament.circle1.extend(tournament.captains)
        tournament.circle2.extend([f"P2-{i}" for i in range(captain_count)])
        tournament.circle3.extend([f"P3-{i}" for i in range(captain_count)])
        tournament.circle4.extend([f"P4-{i}" for i in range(captain_count + 2)])  # captain_count + 2 players in circle4
        
        tournament.start_draft()
        store.set(tournament)

        await interaction.response.send_message(
            "🧪 Тестовый режим активирован! Турнир заполнен и драфт запущен.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    @app_commands.command(name="leaderboard", description="Показать таблицу лидеров")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        """Показать глобальную таблицу лидеров."""
        from utils.embeds import build_leaderboard_embed
        from views.leaderboard_view import LeaderboardView
        
        embed = build_leaderboard_embed(page=1)
        view = LeaderboardView(interaction.guild_id, page=1)
        
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="replace", description="Заменить игрока")
    @app_commands.describe(
        old_name="Имя игрока которого нужно заменить",
        new_name="Имя нового игрока"
    )
    @is_admin()
    async def replace_player(
        self,
        interaction: discord.Interaction,
        old_name: str,
        new_name: str
    ) -> None:
        """Заменить игрока в турнире."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Нет активного турнира.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        old_name = old_name.strip()
        new_name = new_name.strip()

        if tournament.phase == TournamentPhase.SETUP or tournament.phase == TournamentPhase.DRAFT:
            # Replace in circles
            if old_name not in tournament.all_players:
                await interaction.response.send_message(
                    f"❌ Игрок `{old_name}` не найден.",
                    ephemeral=True,
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

            for circle in range(1, 5):
                circle_list = getattr(tournament, f"circle{circle}")
                if old_name in circle_list:
                    idx = circle_list.index(old_name)
                    circle_list[idx] = new_name
                    break
        elif tournament.phase == TournamentPhase.FINAL:
            # Replace in final teams
            found = False
            for team_idx in range(len(tournament.final_teams)):
                if tournament.final_teams[team_idx] == old_name:
                    tournament.final_teams[team_idx] = new_name
                    found = True
                    break
            
            if not found:
                await interaction.response.send_message(
                    f"❌ Игрок `{old_name}` не найден в финальных командах.",
                    ephemeral=True,
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return
        else:
            # Replace in teams (TEAMS, QUALIFIERS, SEMIFINALS)
            found = False
            for team in tournament.teams:
                for key, value in team.items():
                    if value == old_name:
                        team[key] = new_name
                        found = True
                        break
                if found:
                    break

            if not found:
                await interaction.response.send_message(
                    f"❌ Игрок `{old_name}` не найден в командах.",
                    ephemeral=True,
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

        store.set(tournament)

        await interaction.response.send_message(
            f"✅ Игрок `{old_name}` заменен на `{new_name}`.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    @app_commands.command(name="delete_player", description="Удалить игрока из турнира")
    @app_commands.describe(name="Имя игрока которого нужно удалить")
    @is_admin()
    async def delete_player(
        self,
        interaction: discord.Interaction,
        name: str
    ) -> None:
        """Удалить игрока из турнира."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Нет активного турнира.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        name = name.strip()

        if tournament.phase == TournamentPhase.SETUP:
            if not tournament.remove_player(name):
                await interaction.response.send_message(
                    f"❌ Игрок `{name}` не найден.",
                    ephemeral=True,
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return
        else:
            await interaction.response.send_message(
                "❌ Можно удалять игроков только на этапе настройки.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        store.set(tournament)

        await interaction.response.send_message(
            f"✅ Игрок `{name}` удален.",
            ephemeral=True
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
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        logger.exception("Ошибка команды: %s", error)
        msg = "❌ Произошла ошибка при выполнении команды."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        asyncio.create_task(_delete_ephemeral_later(interaction))


async def setup(bot: TournamentBot) -> None:
    """Загрузить ког."""
    await bot.add_cog(TournamentCog(bot))