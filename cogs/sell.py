import math
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import get_emoji
from storage.shop_ops_store import shop_ops_store
from storage.shop_store import inventory_store, shop_store
from utils.design import compose

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


async def _ensure_role_removed(
    guild: discord.Guild, user_id: int, role_id: int
) -> bool:
    """Подтвердить фактическое отсутствие Discord-роли у member.

    True — роли нет (снята или не было). False — роль всё ещё
    есть либо состояние не удалось проверить (неизвестный
    результат Discord API → повторная проверка member.roles,
    а не слепая выплата).
    """
    try:
        member = await guild.fetch_member(user_id)
    except Exception:
        return False
    role = guild.get_role(role_id)
    if role is None:
        # Роль удалена с сервера — снимать нечего, продажа безопасна.
        return True
    if not any(r.id == role_id for r in member.roles):
        return True
    try:
        from utils.role_manager import remove_role
        removed = await remove_role(guild, user_id, role_id)
    except Exception:
        removed = False
    # Повторная проверка фактического состояния — покрывает и
    # «снятие выполнилось, ответ потерялся».
    try:
        member = await guild.fetch_member(user_id)
    except Exception:
        return removed
    return not any(r.id == role_id for r in member.roles)


async def _credit_sale_once(
    guild_id: int, user_id: int, amount: int, op_id: str
) -> int:
    """Идемпотентное начисление выручки за продажу.

    Ключ привязан к долговечной операции продажи (sellop:{op_id}) —
    повторные попытки и recovery используют ОДИН И ТОТ ЖЕ ключ:
    при повторном вызове возвращается зафиксированная сумма,
    зачисление не дублируется.

    Возвращает ФАКТИЧЕСКИ зачисленную сумму (может быть меньше
    amount при достижении MAX_BALANCE).
    """
    if amount <= 0:
        return 0
    from storage.user_balance_store import user_balance_store
    key = f"sellop:{op_id}"
    try:
        return await user_balance_store.add_balance_once(
            guild_id, user_id, amount, key
        )
    except Exception:
        # Неопределённый исход: тот же ключ вернёт recorded
        # amount, если грант закоммитился.
        return await user_balance_store.add_balance_once(
            guild_id, user_id, amount, key
        )


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

from cogs.groups import economy_group, bind_commands


class SellCog(commands.Cog):
    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        bind_commands(economy_group, self, ["sell"])

    @economy_group.command(name="sell", description="Продать значок или тег за 50% от стоимости")
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
                f" Указанный предмет не найден в базе данных!",
                ephemeral=True
            )
            return

        # Check if item is sellable
        if not item.is_sellable:
            await interaction.followup.send(
                f" Этот предмет нельзя продать!",
                ephemeral=True
            )
            return

        # Цветные роли не продаются: Discord-роль — отдельная сущность,
        # её нельзя отчуждать за монеты через /economy sell.
        if item.category == "color_roles":
            await interaction.followup.send(
                " Цветные роли нельзя продать — они остаются на вашем аккаунте.",
                ephemeral=True
            )
            return

        guild_id = interaction.guild_id if interaction.guild else 0
        guild = interaction.guild

        # Сериализуем с покупками того же пользователя: продажа и
        # покупка не могут пересечься и потерять/продублировать предмет.
        from services.shop_purchase_service import _user_lock
        async with await _user_lock(guild_id, interaction.user.id):
            # Повторная проверка владения внутри блокировки
            # (двойной клик / гонка с покупкой).
            cosmetics = inventory_store.get_player_inventory(
                guild_id, interaction.user.id
            )
            cosmetic = next(
                (c for c in cosmetics if c.item_id == item.id), None
            )
            if cosmetic is None:
                await interaction.followup.send(
                    f" У вас нет этого предмета в инвентаре!",
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

                        if pet_def:  # Способности открыты с 1 возраста
                            ability = pet_def["abilities"][2]
                            if ability["name"] in ["Чёрный рынок", "Торговый нюх"]:
                                bonus_value = ability.get("max_val", 0)
                                fee_reduction = bonus_value / 100
                                base_sell_percent = base_sell_percent * (1 + fee_reduction)  # Уменьшение комиссии = увеличение цены
                                pet_bonus_info["bonus_value"] = bonus_value
                                pet_bonus_info["pet_name"] = pet.pet_name
                                pet_bonus_info["ability_name"] = ability["name"]
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Error applying pet bonus to sell: {e}")

            sell_price = math.floor(base_price * base_sell_percent)

            # 4. Долговечная операция продажи — ДО любых
            #    изменений (роль, инвентарь, баланс). crash в
            #    любой точке разрешается recover_pending по op_id.
            from storage.shop_purchase_ops_store import (
                purchase_ops_store,
            )
            from storage.user_balance_store import user_balance_store
            from storage.user_balance_store import _max_balance

            # Если по этому предмету уже есть незавершённая
            # продажа — не создаём вторую, сообщаем пользователю.
            try:
                pending_ops = await purchase_ops_store.pending()
                if any(
                    o.get("kind") == "sell"
                    and o.get("item_id") == item.id
                    and o.get("user_id") == interaction.user.id
                    and o.get("guild_id") == guild_id
                    for o in pending_ops
                ):
                    await interaction.followup.send(
                        " Предыдущая продажа этого предмета ещё "
                        "обрабатывается — дождитесь завершения.",
                        ephemeral=True,
                    )
                    return
            except Exception:  # noqa: BLE001
                # Не смогли проверить — продолжаем, op_id всё
                # равно уникален и recovery идемпотентен.
                pass

            sale_op_id = purchase_ops_store.new_op_id()
            await purchase_ops_store.create(
                sale_op_id, guild_id, interaction.user.id,
                "sell", item.id, sell_price,
                payload={
                    "item_name": item.name,
                    "item_type": item.item_type,
                    "role_id": item.role_id,
                    "cosmetic": cosmetic.to_dict(),
                },
            )

            # 4a. Проверка места до MAX_BALANCE: продажа не
            #     должна «съесть» предмет без полной выплаты.
            try:
                balance = await user_balance_store.get_balance(
                    guild_id, interaction.user.id
                )
            except Exception:  # noqa: BLE001
                balance = 0
            cap = _max_balance()
            if balance + sell_price > cap:
                await purchase_ops_store.set_status(
                    sale_op_id, "failed",
                    payload={"reason": "balance_cap",
                             "balance": balance, "cap": cap},
                )
                await interaction.followup.send(
                    f" На балансе нет места для выплаты "
                    f"({sell_price:,} монет, лимит {cap:,}). "
                    f"Сначала потратьте часть баланса.",
                    ephemeral=True,
                )
                return

            # 4c. Для role-предметов проверяем ФАКТИЧЕСКОЕ снятие
            #     Discord-роли до начисления и удаления предмета.
            #     Снятие не подтверждено → продажа не считается
            #     успешной: предмет остаётся, монеты не начисляются.
            if item.item_type == "role" and item.role_id and guild is not None:
                if not await _ensure_role_removed(
                    guild, interaction.user.id, item.role_id
                ):
                    await purchase_ops_store.set_status(
                        sale_op_id, "failed",
                        payload={"reason": "role_removal_unconfirmed"},
                    )
                    await interaction.followup.send(
                        " Не удалось подтвердить снятие Discord-роли — "
                        "продажа отменена, предмет и баланс не изменены.",
                        ephemeral=True
                    )
                    return

            # 4b. Сначала зачисляем монеты (идемпотентно по
            #     sellop:{op_id}), потом изымаем предмет.
            #     При краше между шагами recovery доводит
            #     операцию до конца в правильном порядке.
            try:
                credited = await _credit_sale_once(
                    guild_id, interaction.user.id,
                    sell_price, sale_op_id,
                )
            except Exception:  # noqa: BLE001
                # Начисление не подтверждено — предмет НЕ
                # трогаем, операция остаётся reserved и будет
                # завершена recovery (кредит → удаление).
                await interaction.followup.send(
                    " Ошибка начисления монет — продажа будет "
                    "завершена автоматически.",
                    ephemeral=True,
                )
                return

            if credited < sell_price:
                # MAX_BALANCE достигнут между проверкой и
                # начислением: откатываем частичное начисление
                # и отменяем продажу — предмет не теряется.
                try:
                    await user_balance_store.subtract_balance(
                        guild_id, interaction.user.id, credited
                    )
                except Exception:  # noqa: BLE001
                    pass
                await purchase_ops_store.set_status(
                    sale_op_id, "failed",
                    payload={"reason": "credit_shortfall",
                             "credited": credited},
                )
                await interaction.followup.send(
                    " Недостаточно места на балансе для полной "
                    "выплаты — продажа отменена.",
                    ephemeral=True,
                )
                return

            # Факт начисления зафиксирован до удаления предмета.
            await purchase_ops_store.set_status(
                sale_op_id, "charged",
                payload={"credited": credited},
            )

            # 5. Удаляем предмет. При сбое удаления начисленные
            #    монеты не откатываем — recovery доведёт
            #    удаление и завершит операцию.
            try:
                await remove_user_item(
                    guild_id, interaction.user.id, item.id
                )
            except Exception:  # noqa: BLE001
                pass
            await purchase_ops_store.set_status(sale_op_id, "done")
            shop_ops_store.record(
                guild_id=guild_id, user_id=interaction.user.id,
                kind="sell", item_id=item.id, price=sell_price,
                status="done", op_id=sale_op_id,
            )

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
            title=f"**ПРОДАЖА ПРЕДМЕТА**",
            description=(
                f" **Пользователь:** {interaction.user.mention}\n\n"
                f" **Продан предмет:** `{item.name}`\n"
                f" **Цена в магазине:** `{base_price:,}` {get_emoji('money')}\n"
                f" **Выручка (50%):** **`{sell_price:,}`** {get_emoji('money')}\n\n"
                f" *Монеты зачислены на ваш баланс.*{pet_bonus_text}"
            ),
            color=0x2ECC71
        )

        await interaction.followup.send( view=compose(embed))


async def setup(bot: "TournamentBot"):
    await bot.add_cog(SellCog(bot))
