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
        base_sell_percent = 0.5

        # Бонусы питомцев для отслеживания
        pet_bonus_info = {"bonus_value": 0, "pet_name": None, "ability_name": None}

        # Применить бонус питомца (Чёрный рынок / Торговый нюх)
        try:
            from storage.pet_store import pet_store
            pet = await pet_store.get_user_pet(guild_id, interaction.user.id)

            if pet and pet.fullness > 0:
                # Списать энергию за использование способности
                energy_consumed = await pet_store.consume_energy_for_ability(guild_id, interaction.user.id, "sell")

                if energy_consumed:
                    from storage.pet_store import pet_catalog
                    pet_def = pet_catalog.get_pet(pet.pet_type.value)

                    if pet_def and pet.level >= 7:  # Уровень разблокировки Чёрного рынка
                        ability = pet_def["abilities"][2]
                        if ability["name"] in ["Чёрный рынок", "Торговый нюх"]:
                            effectiveness = pet.get_ability_effectiveness()
                            min_val = ability.get("min_val", 0)
                            max_val = ability.get("max_val", 0)
                            bonus_value = min_val + (max_val - min_val) * effectiveness
                            fee_reduction = bonus_value / 100
                            base_sell_percent = base_sell_percent * (1 - fee_reduction)
                            pet_bonus_info["bonus_value"] = bonus_value
                            pet_bonus_info["pet_name"] = pet.pet_name
                            pet_bonus_info["ability_name"] = ability["name"]
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error applying pet bonus to sell: {e}")

        sell_price = math.floor(base_price * base_sell_percent)

        # 4. Удаляем предмет и начисляем монеты
        await remove_user_item(guild_id, interaction.user.id, item.id)
        await add_balance(guild_id, interaction.user.id, sell_price)

        # 5. Формировать текст бонуса питомца
        pet_bonus_text = ""
        if pet_bonus_info["bonus_value"] > 0:
            pet_name = pet_bonus_info["pet_name"]
            ability_name = pet_bonus_info["ability_name"]
            bonus_value = pet_bonus_info["bonus_value"]

            if ability_name == "Чёрный рынок":
                pet_bonus_text = f"\n\n🐾 **Енот-грабитель ({pet_name}):** Комиссия Чёрного рынка снижена на **-{bonus_value:.1f}%**!"
            elif ability_name == "Торговый нюх":
                pet_bonus_text = f"\n\n🐾 **Мимик-собиратель ({pet_name}):** Торговый сбор уменьшен на **-{bonus_value:.1f}%**!"

        # 6. Красивый Embed ответа
        embed = discord.Embed(
            title=f"{get_emoji('a_star')} **ПРОДАЖА ПРЕДМЕТА**",
            description=(
                f"{get_emoji('a_sparkle')} **Пользователь:** {interaction.user.mention}\n\n"
                f"{get_emoji('white_arrow')} **Продан предмет:** `{item.name}`\n"
                f"{get_emoji('white_arrow')} **Цена в магазине:** `{base_price:,}` {get_emoji('money')}\n"
                f"{get_emoji('white_arrow')} **Выручка (50%):** **`{sell_price:,}`** {get_emoji('money')}\n\n"
                f"{get_emoji('white_dot')} *Монеты зачислены на ваш баланс.*{pet_bonus_text}"
            ),
            color=0x2ECC71
        )

        await interaction.followup.send(embed=embed)


async def setup(bot: "TournamentBot"):
    await bot.add_cog(SellCog(bot))
