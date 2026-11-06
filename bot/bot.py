"""Точка входа Discord-бота."""
from discord import app_commands
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands  # Добавили импорт для работы с ошибками дерева
from discord.ext import commands

from bot.config import DISCORD_TOKEN
from bot.message_manager import get_view_for_tournament
from bot.models import TournamentPhase
from bot.storage import storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

COGS = (
    "bot.cogs.tournament",
    "bot.cogs.captains",
    "bot.cogs.players",
    "bot.cogs.draft",
)


class TournamentBot(commands.Bot):
    """Бот с восстановлением persistent views после перезапуска."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True  # Изменили на True, чтобы убрать предупреждение в логах
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        # Подключаем коги
        for cog in COGS:
            await self.load_extension(cog)
            
        # Регистрируем глобальный обработчик ошибок для слэш-команд
        @self.tree.error
        async def on_app_command_error(
            interaction: discord.Interaction, 
            error: app_commands.AppCommandError
        ) -> None:
            # Если команда не прошла проверку прав (например, администратора)
            if isinstance(error, app_commands.CheckFailure):
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ У вас недостаточно прав для выполнения этой команды.",
                        ephemeral=True
                    )
                return
            
            # Логируем остальные критические ошибки, если они возникнут
            logger.error("Произошла непредвиденная ошибка в слэш-команде: %s", error, exc_info=error)

        await self.tree.sync()
        self._restore_views()

    def _restore_views(self) -> None:
        """Восстановить кнопки и Select Menu из JSON."""
        for tournament in storage.all_tournaments():
            view = get_view_for_tournament(tournament)
            if view:
                self.add_view(view)
                logger.info(
                    "View восстановлен для guild %s (phase=%s)",
                    tournament.guild_id,
                    tournament.phase.value,
                )


async def setup_hook(self) -> None:
        for cog in COGS:
            await self.load_extension(cog)

        # Наш обработчик ошибок
        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            if isinstance(error, app_commands.CheckFailure):
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ У вас недостаточно прав для выполнения этой команды.",
                        ephemeral=True
                    )
                return
            logger.error("Ошибка в слэш-команде: %s", error)

        await self.tree.sync()
        self._restore_views()

if __name__ == "__main__":
    asyncio.run(main())