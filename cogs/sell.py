import math
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from storage.shop_store import shop_store

if TYPE_CHECKING:
    from bot import TournamentBot


# ==========================================
# Вспомогательные функции работы с магазином
# ==========================================

async def add_balance(user_id: int, amount: int) -> None:
    """Зачисление монет пользователю."""
    if amount <= 0:
        return
    from storage.user_balance_store import user_balance_store
    # Assume guild_id = 0 for now, should be fixed
    await user_balance_store.add_balance(0, user_id, amount)


async def get_shop_item(item_id: str) -> Optional[dict]:
    """Получение информации о предмете из каталога (тег / значок)."""
    return shop_store.get_item(item_id)


async def has_user_item(user_id: int, item_id: str) -> bool:
    """Проверка наличия предмета у пользователя."""
    from storage.user_profile_store import user_profile_store
    profile = user_profile_store.get_profile(0, user_id)  # guild_id = 0
    if not profile:
        return False
    return item_id in profile.inventory


async def remove_user_item(user_id: int, item_id: str) -> None:
    """Удаление уникального предмета из инвентаря пользователя."""
    from storage.user_profile_store import user_profile_store
    profile = user_profile_store.get_profile(0, user_id)  # guild_id = 0
    if profile and item_id in profile.inventory:
        profile.inventory.remove(item_id)
        user_profile_store.set_profile(0, user_id, profile)


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
        if not await has_user_item(interaction.user.id, item["id"]):
            await interaction.followup.send(
                "<:white_dot:0000> У вас нет этого предмета в инвентаре!",
                ephemeral=True
            )
            return

        # 3. Расчёт стоимости продажи (50% от стоимости предмета в магазине)
        base_price = item["price"]
        sell_price = math.floor(base_price * 0.5)

        # 4. Удаляем предмет и начисляем монеты
        await remove_user_item(interaction.user.id, item["id"])
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
