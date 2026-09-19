"""View для интерактивного магазина."""

import discord
from storage.shop_store import shop_store
from storage.user_balance_store import user_balance_store
from storage.shop_store import inventory_store
from models.shop_item import PlayerCosmetic


class ShopCategoryButton(discord.ui.Button):
    """Кнопка категории магазина."""

    def __init__(self, category: str, label: str, emoji: str):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            emoji=emoji,
            custom_id=f"shop_category:{category}"
        )
        self.category = category

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать товары категории."""
        # Получить товары категории
        items = shop_store.get_items_by_category(self.category)

        if not items:
            await interaction.response.send_message(
                "❌ Нет товаров в этой категории.",
                ephemeral=True
            )
            return

        # Создать embed с товарами
        embed = discord.Embed(
            title=f"🛒 {self.label}",
            color=discord.Color.gold()
        )

        # Создать View с кнопками товаров
        view = discord.ui.View()
        view.add_item(ShopBackButton())

        # Добавить кнопки для каждого товара
        for item in items:
            rarity_emoji = {
                "basic": "⚪",
                "premium": "🔵",
                "elite": "🟡",
                "special": "🟣"
            }.get(item.rarity.value, "⚪")

            label = f"{rarity_emoji} {item.name} - {item.price} 🪙"
            view.add_item(ShopBuyButton(item.id, label))

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class ShopBackButton(discord.ui.Button):
    """Кнопка возврата в главное меню магазина."""

    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="↩️ Назад",
            custom_id="shop_back"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Вернуться в главное меню магазина."""
        from storage.user_balance_store import user_balance_store

        # Получить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

        # Создать главное меню
        embed = discord.Embed(
            title="🛒 Магазин косметики",
            description=f"Ваш баланс: {balance} 🪙",
            color=discord.Color.gold()
        )

        embed.add_field(
            name="📖 Как купить",
            value="Выберите категорию ниже, затем используйте `/buy <item_id>` для покупки.",
            inline=False
        )

        view = ShopMainView()
        await interaction.response.edit_message(embed=embed, view=view)


class ShopBuyButton(discord.ui.Button):
    """Кнопка покупки товара."""

    def __init__(self, item_id: str, label: str):
        super().__init__(
            style=discord.ButtonStyle.success,
            label=label,
            custom_id=f"shop_buy:{item_id}"
        )
        self.item_id = item_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Купить товар."""
        # Получить товар
        item = shop_store.get_item(self.item_id)
        if not item:
            await interaction.response.send_message(
                "❌ Товар не найден.",
                ephemeral=True
            )
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < item.price:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. Нужно: {item.price} 🪙, у вас: {balance} 🪙",
                ephemeral=True
            )
            return

        # Проверить есть ли уже
        inventory = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
        for cosmetic in inventory:
            if cosmetic.item_id == self.item_id:
                await interaction.response.send_message(
                    f"❌ У вас уже есть этот товар!",
                    ephemeral=True
                )
                return

        # Списать монеты
        await user_balance_store.subtract_balance(interaction.guild_id, interaction.user.id, item.price)

        # Добавить в инвентарь и экипировать
        cosmetic = PlayerCosmetic(
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
            item_id=self.item_id,
            equipped=True  # Автоматически экипировать
        )
        inventory_store.add_cosmetic(cosmetic)

        await interaction.response.send_message(
            f"✅ Вы купили **{item.name}** за {item.price} 🪙!\n\n"
            f"Товар автоматически экипирован.",
            ephemeral=True
        )


class ShopMainView(discord.ui.View):
    """Главное меню магазина."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ShopCategoryButton("icons", "Значки", "✨"))
        self.add_item(ShopCategoryButton("tags", "Теги", "🏷️"))
