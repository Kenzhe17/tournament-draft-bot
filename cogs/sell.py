import math
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from storage.shop_store import shop_store, inventory_store
from config import get_emoji

if TYPE_CHECKING:
    from bot import TournamentBot


# ==========================================
# Вспомогательные функции работы с магазином
# ==========================================

async def add_balance(guild_id: int, user_id: int, amount: int) -> None:
    """Зачисление монет пользователю."""
    if amount <= 0:
        return
    from storage.user_balance_store import user_balance_store
    await user_balance_store.add_balance(guild_id, user_id, amount)


async def get_shop_item(item_id: str) -> Optional[dict]:
    """Получение информации о предмете из каталога (тег / значок)."""
    return shop_store.get_item(item_id)


async def has_user_item(guild_id: int, user_id: int, item_id: str) -> bool:
    """Проверка наличия предмета у пользователя."""
    inventory = inventory_store.get_player_inventory(guild_id, user_id)
    return any(cosmetic.item_id == item_id for cosmetic in inventory)


async def remove_user_item(guild_id: int, user_id: int, item_id: str) -> None:
    """Удаление уникального предмета из инвентаря пользователя."""
    inventory_store.remove_cosmetic(guild_id, user_id, item_id)


# ==========================================
# Cog модуль продажи предмета
# ==========================================

class SellCog(commands.Cog):
    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="sell", description="Продать значок или тег за 50% от стоимости")
    @app_commands.describe(
        item_id="ID или название продаваемого предмета (значка/тега)"
    )
    async def sell_item(self, interaction: discord.Interaction, item_id: str):
        await interaction.response.defer()

        guild_id = interaction.guild_id if interaction.guild else 0

        # 1. Проверяем существование предмета в каталоге
        item = await get_shop_item(item_id.lower())
        if not item:
            await interaction.followup.send(
                f"{get_emoji('white_dot')} Указанный предмет не найден в базе данных!",
                ephemeral=True
            )
            return

        # Check if item is sellable
        if not item.is_sellable:
            await interaction.followup.send(
                f"{get_emoji('white_dot')} Этот предмет нельзя продать!",
                ephemeral=True
            )
            return

        # 2. Проверяем наличие предмета в инвентаре у игрока
        if not await has_user_item(guild_id, interaction.user.id, item.id):
            await interaction.followup.send(
                f"{get_emoji('white_dot')} У вас нет этого предмета в инвентаре!",
                ephemeral=True
            )
            return

        # 3. Расчёт стоимости продажи (50% от стоимости предмета в магазине)
        base_price = item.price
        sell_price = math.floor(base_price * 0.5)

        # 4. Удаляем предмет и начисляем монеты
        await remove_user_item(guild_id, interaction.user.id, item.id)
        await add_balance(guild_id, interaction.user.id, sell_price)

        # 5. Красивый Embed ответа
        embed = discord.Embed(
            title=f"{get_emoji('a_star')} **ПРОДАЖА ПРЕДМЕТА**",
            description=(
                f"{get_emoji('a_sparkle')} **Пользователь:** {interaction.user.mention}\n\n"
                f"{get_emoji('white_arrow')} **Продан предмет:** `{item.name}`\n"
                f"{get_emoji('white_arrow')} **Цена в магазине:** `{base_price:,}` {get_emoji('money')}\n"
                f"{get_emoji('white_arrow')} **Выручка (50%):** **`{sell_price:,}`** {get_emoji('money')}\n\n"
                f"{get_emoji('white_dot')} *Монеты зачислены на ваш баланс.*"
            ),
            color=0x2ECC71
        )

        await interaction.followup.send(embed=embed)


async def setup(bot: "TournamentBot"):
    await bot.add_cog(SellCog(bot))
