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

    @app_commands.command(name="bet", description="Показать статистику ставок и баланс пользователя")
    @app_commands.describe(user="Пользователь для просмотра статистики")
    async def bet_stats(self, interaction: discord.Interaction, user: discord.Member = None) -> None:
        """Показать статистику ставок и баланс пользователя."""
        target_user = user or interaction.user

        # Получаем баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, target_user.id)

        # Получаем статистику ставок
        from storage.betting_stats_store import betting_stats_store
        stats = await betting_stats_store.get_user_stats(interaction.guild_id, target_user.id)

        total_bets = stats["total_bets"]
        successful_bets = stats["successful_bets"]
        lost_bets = total_bets - successful_bets
        success_rate = stats["success_rate"]
        total_won = stats["total_won"]
        total_lost = stats["total_lost"]
        profit = total_won - total_lost

        embed = discord.Embed(
            title=f"💰 Ставки {target_user.display_name}",
            color=discord.Color.gold()
        )

        embed.add_field(name="Всего ставок", value=str(total_bets), inline=True)
        embed.add_field(name="Выигрышных", value=str(successful_bets), inline=True)
        embed.add_field(name="Проигрышных", value=str(lost_bets), inline=True)

        embed.add_field(name="Точность", value=f"{success_rate:.1f}%", inline=True)

        embed.add_field(name="Выиграно", value=f"+{total_won}", inline=True)
        embed.add_field(name="Проиграно", value=f"-{total_lost}", inline=True)

        embed.add_field(name="Баланс", value=str(balance), inline=False)

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
