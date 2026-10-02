"""Модуль банковского сейфа - /bank команды."""

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from storage.user_balance_store import user_balance_store
from storage.user_bank_store import user_bank_store
from storage.player_stats_store import player_stats_store
from config import replace_emojis, get_emoji

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

# User locks for concurrent operations
_user_locks: dict[int, asyncio.Lock] = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    """Get or create a lock for a user."""
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]


def get_bank_limit(level: int) -> int:
    """Рассчитать лимит сейфа по уровню."""
    return 3000 + ((level - 1) // 10) * 2000


def get_next_bank_limit(level: int) -> int:
    """Рассчитать лимит следующего уровня."""
    next_level = ((level - 1) // 10 + 1) * 10 + 1
    return get_bank_limit(next_level)


class BankCog(commands.Cog):
    """Ког с командами банка."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="bank", description="Управление банковским сейфом")
    @app_commands.describe(action="Действие: status, deposit, withdraw")
    @app_commands.describe(amount="Сумма (или 'all' для всех средств)")
    async def bank(
        self,
        interaction: discord.Interaction,
        action: str,
        amount: str = None
    ) -> None:
        """Команда банка."""
        action = action.lower()

        if action == "status":
            await self.show_status(interaction)
        elif action == "deposit":
            await self.deposit(interaction, amount)
        elif action == "withdraw":
            await self.withdraw(interaction, amount)
        else:
            await interaction.response.send_message(
                f"{replace_emojis('❌')} Неверное действие! Используйте: status, deposit, withdraw",
                ephemeral=True
            )

    async def show_status(self, interaction: discord.Interaction) -> None:
        """Показать статус сейфа."""
        async with get_user_lock(interaction.user.id):
            user_id = interaction.user.id
            guild_id = interaction.guild_id

            # Получить статистику игрока для уровня
            stats = await player_stats_store.get(guild_id, user_id)
            if not stats:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Профиль не найден!",
                    ephemeral=True
                )
                return

            level = stats.level
            cash = await user_balance_store.get_balance(guild_id, user_id)
            bank = await user_bank_store.get_bank_balance(guild_id, user_id)

            # Рассчитать лимиты
            bank_limit = get_bank_limit(level)
            next_limit = get_next_bank_limit(level)
            next_level_tier = ((level - 1) // 10 + 1) * 10 + 1

            # Диапазон уровней
            level_tier = ((level - 1) // 10) * 10 + 1
            level_tier_end = level_tier + 9

            # Процент заполнения
            fill_percent = (bank / bank_limit * 100) if bank_limit > 0 else 0

            # Build embed
            embed = discord.Embed(
                title=f"{get_emoji('a_sparkle')} **БАНКОВСКИЙ СЕЙФ | /bank**",
                color=3447003  # Синий
            )
            embed.set_thumbnail(url="https://cdn.discordapp.com/embed/avatars/0.png")

            embed.add_field(
                name=f"{get_emoji('white_dot')} {get_emoji('white_arrow')} Уровень и Сейф",
                value=f"• Ваш ранг: **Уровень {level}** *(Диапазон {level_tier}–{level_tier_end} ур.)*\n• Лимит сейфа: **{bank_limit:,}** {get_emoji('money')}\n• Следующий уровень сейфа: **{next_limit:,}** {get_emoji('money')} *(на {next_level_tier} ур.)*",
                inline=False
            )

            embed.add_field(
                name=f"{get_emoji('white_dot')} {get_emoji('white_arrow')} Состояние счетов",
                value=f"• Наличные: **{cash:,}** {get_emoji('money')} *(уязвимы для /rob)*\n• В сейфе: **{bank:,} / {bank_limit:,}** {get_emoji('money')} ({fill_percent:.0f}%)\n• Защита сейфа: **100% Абсолютная**",
                inline=False
            )

            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def deposit(self, interaction: discord.Interaction, amount: str) -> None:
        """Пополнить сейф."""
        async with get_user_lock(interaction.user.id):
            user_id = interaction.user.id
            guild_id = interaction.guild_id

            # Получить статистику для уровня
            stats = await player_stats_store.get(guild_id, user_id)
            if not stats:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Профиль не найден!",
                    ephemeral=True
                )
                return

            level = stats.level
            cash = await user_balance_store.get_balance(guild_id, user_id)
            bank = await user_bank_store.get_bank_balance(guild_id, user_id)
            bank_limit = get_bank_limit(level)

            # Парсинг суммы
            if amount and amount.lower() == "all":
                deposit_amount = cash
            elif amount:
                try:
                    deposit_amount = int(amount)
                except ValueError:
                    await interaction.response.send_message(
                        f"{replace_emojis('❌')} Неверная сумма!",
                        ephemeral=True
                    )
                    return
            else:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Укажите сумму или 'all'!",
                    ephemeral=True
                )
                return

            # Проверка наличия средств
            if cash <= 0:
                await interaction.response.send_message(
                    f"{replace_emojis('⚠️')} **Недостаточно средств:** У вас на руках нет запрошенной суммы наличных!",
                    ephemeral=True
                )
                return

            if deposit_amount > cash:
                await interaction.response.send_message(
                    f"{replace_emojis('⚠️')} **Недостаточно средств:** У вас на руках нет запрошенной суммы наличных!",
                    ephemeral=True
                )
                return

            # Проверка свободного места
            space_left = bank_limit - bank
            if space_left <= 0:
                next_limit = get_next_bank_limit(level)
                next_level_tier = ((level - 1) // 10 + 1) * 10 + 1
                await interaction.response.send_message(
                    f"{replace_emojis('⚠️')} **Сейф переполнен:** На вашем ранге (**Уровень {level}**) максимальный лимит сейфа — **{bank_limit:,}** {get_emoji('money')}. Повышайте уровень персонажа, чтобы получить доступ к **{next_limit:,}** {get_emoji('money')} на {next_level_tier} уровне!",
                    ephemeral=True
                )
                return

            # Фактический взнос
            actual_deposit = min(deposit_amount, space_left)

            # Комиссия 5%
            fee = (actual_deposit * 5 + 99) // 100  # Округление вверх
            net_amount = actual_deposit - fee

            # Транзакция
            await user_balance_store.subtract_balance(guild_id, user_id, actual_deposit)
            await user_bank_store.add_to_bank(guild_id, user_id, net_amount)

            # Новые балансы
            new_cash = await user_balance_store.get_balance(guild_id, user_id)
            new_bank = await user_bank_store.get_bank_balance(guild_id, user_id)

            await interaction.response.send_message(
                f"{replace_emojis('✅')} **Сейф пополнен!** Вы внесли **{actual_deposit:,}** {get_emoji('money')}.\n• Комиссия банка (5%): **{fee:,}** {get_emoji('money')}\n• Зачислено в сейф: **{net_amount:,}** {get_emoji('money')}\n• Теперь в сейфе: **{new_bank:,} / {bank_limit:,}** {get_emoji('money')}.",
                ephemeral=True
            )

    async def withdraw(self, interaction: discord.Interaction, amount: str) -> None:
        """Снять со сейфа."""
        async with get_user_lock(interaction.user.id):
            user_id = interaction.user.id
            guild_id = interaction.guild_id

            bank = await user_bank_store.get_bank_balance(guild_id, user_id)

            # Парсинг суммы
            if amount and amount.lower() == "all":
                withdraw_amount = bank
            elif amount:
                try:
                    withdraw_amount = int(amount)
                except ValueError:
                    await interaction.response.send_message(
                        f"{replace_emojis('❌')} Неверная сумма!",
                        ephemeral=True
                    )
                    return
            else:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Укажите сумму или 'all'!",
                    ephemeral=True
                )
                return

            # Проверка наличия в сейфе
            if bank <= 0:
                await interaction.response.send_message(
                    f"{replace_emojis('⚠️')} **Недостаточно средств:** В сейфе нет средств для вывода!",
                    ephemeral=True
                )
                return

            if withdraw_amount > bank:
                await interaction.response.send_message(
                    f"{replace_emojis('⚠️')} **Недостаточно средств:** В сейфе нет запрошенной суммы!",
                    ephemeral=True
                )
                return

            # Транзакция (без комиссии)
            await user_bank_store.subtract_from_bank(guild_id, user_id, withdraw_amount)
            await user_balance_store.add_balance(guild_id, user_id, withdraw_amount)

            # Новые балансы
            new_cash = await user_balance_store.get_balance(guild_id, user_id)
            new_bank = await user_bank_store.get_bank_balance(guild_id, user_id)

            await interaction.response.send_message(
                f"{replace_emojis('✅')} **Вывод выполнен!** Вы перевели **{withdraw_amount:,}** {get_emoji('money')} из сейфа в наличные.\n• На руках: **{new_cash:,}** {get_emoji('money')} *(уязвимы)*\n• В сейфе осталось: **{new_bank:,}** {get_emoji('money')}.",
                ephemeral=True
            )


async def setup(bot: commands.Bot) -> None:
    """Загрузить ког."""
    await bot.add_cog(BankCog(bot))
