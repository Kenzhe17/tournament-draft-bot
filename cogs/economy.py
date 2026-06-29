"""Slash-команды экономики."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from storage.user_balance_store import user_balance_store

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class EconomyCog(commands.Cog):
    """Ког с экономическими командами."""

    def __init__(self, bot: TournamentBot):
        self.bot = bot
        # Хранение последнего использования бонуса: "guild_id:user_id" -> datetime
        self._bonus_cooldowns: dict[str, datetime] = {}

    @app_commands.command(name="bonus", description="Получить ежедневный бонус (100 coin)")
    async def daily_bonus(self, interaction: discord.Interaction) -> None:
        """Получить ежедневный бонус."""
        key = f"{interaction.guild_id}:{interaction.user.id}"
        now = datetime.utcnow()

        # Проверяем кулдаун
        if key in self._bonus_cooldowns:
            last_used = self._bonus_cooldowns[key]
            cooldown_end = last_used + timedelta(hours=24)
            if now < cooldown_end:
                remaining = cooldown_end - now
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                await interaction.response.send_message(
                    f"❌ Бонус уже получен. Следующий бонус через {hours}ч {minutes}м.",
                    ephemeral=True
                )
                return

        # Начисляем 100 coin
        new_balance = await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, 100)

        # Записываем время использования
        self._bonus_cooldowns[key] = now

        await interaction.response.send_message(
            f"🎁 Вы получили ежедневный бонус: **100 coin**!\n"
            f"💰 Ваш баланс: {new_balance} coin",
            ephemeral=True
        )

    @app_commands.command(name="balance", description="Показать ваш баланс")
    async def balance(self, interaction: discord.Interaction) -> None:
        """Показать баланс пользователя."""
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

        embed = discord.Embed(
            title="💰 Ваш баланс",
            description=f"{balance} coin",
            color=discord.Color.gold()
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Обработка ошибок slash-команд."""
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error) or "❌ Недостаточно прав."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except discord.NotFound:
                pass
            return

        logger.exception("Ошибка команды: %s", error)
        msg = "❌ Произошла ошибка при выполнении команды."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.NotFound:
            pass


async def setup(bot: TournamentBot) -> None:
    """Загрузить ког."""
    await bot.add_cog(EconomyCog(bot))
