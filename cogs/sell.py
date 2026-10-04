import math
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from storage.db import get_pool

if TYPE_CHECKING:
    from bot import TournamentBot


# ==========================================
# Вспомогательные функции работы с БД
# ==========================================

async def add_balance(user_id: int, amount: int) -> None:
    """Зачисление монет пользователю."""
    if amount <= 0:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET cash = cash + $1 WHERE user_id = $2",
            amount, user_id
        )


async def get_shop_item(item_id: str) -> Optional[dict]:
    """Получение информации о предмете из каталога (тег / значок)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT item_id, name, price, is_sellable FROM shop_items WHERE item_id = $1",
            item_id
        )
        return dict(row) if row else None


async def has_user_item(user_id: int, item_id: str) -> bool:
    """Проверка наличия предмета у пользователя."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM user_inventory WHERE user_id = $1 AND item_id = $2",
            user_id, item_id
        )
        return row is not None


async def remove_user_item(user_id: int, item_id: str) -> None:
    """Удаление уникального предмета из инвентаря пользователя."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_inventory WHERE user_id = $1 AND item_id = $2",
            user_id, item_id
        )


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

        # 1. Проверяем существование предмета в каталоге
        item = await get_shop_item(item_id.lower())
        if not item:
            await interaction.followup.send(
                "<:white_dot:0000> Указанный предмет не найден в базе данных!",
                ephemeral=True
            )
            return

        if not item.get("is_sellable", True):
            await interaction.followup.send(
                "<:white_dot:0000> Этот предмет нельзя продать!",
                ephemeral=True
            )
            return

        # 2. Проверяем наличие предмета в инвентаре у игрока
        if not await has_user_item(interaction.user.id, item["item_id"]):
            await interaction.followup.send(
                "<:white_dot:0000> У вас нет этого предмета в инвентаре!",
                ephemeral=True
            )
            return

        # 3. Расчёт стоимости продажи (50% от стоимости предмета в магазине)
        base_price = item["price"]
        sell_price = math.floor(base_price * 0.5)

        # 4. Удаляем предмет и начисляем монеты
        await remove_user_item(interaction.user.id, item["item_id"])
        await add_balance(interaction.user.id, sell_price)

        # 5. Красивый Embed ответа
        embed = discord.Embed(
            title="<a:a_star:0000> **ПРОДАЖА ПРЕДМЕТА | /sell**",
            description=(
                f"<a:a_sparkle:0000> **Пользователь:** {interaction.user.mention}\n\n"
                f"<:white_arrow:0000> **Продан предмет:** `{item['name']}`\n"
                f"<:white_arrow:0000> **Цена в магазине:** `{base_price:,}` монет\n"
                f"<:white_arrow:0000> **Выручка (50%):** **`{sell_price:,}`** монет\n\n"
                f"<:white_dot:0000> *Монеты зачислены на ваш баланс.*"
            ),
            color=0x2ECC71
        )

        await interaction.followup.send(embed=embed)


async def setup(bot: "TournamentBot"):
    await bot.add_cog(SellCog(bot))
