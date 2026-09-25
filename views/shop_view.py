"""View для интерактивного магазина."""

import discord
from storage.shop_store import shop_store
from storage.user_balance_store import user_balance_store
from storage.shop_store import inventory_store
from models.shop_item import PlayerCosmetic, CosmeticRarity


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
        """Показать подкатегории по редкости."""
        await interaction.response.defer(ephemeral=True)

        # Создать View с кнопками редкости
        view = discord.ui.View()
        view.add_item(ShopBackButton())

        # Добавить кнопки для каждой редкости
        rarities = [
            (CosmeticRarity.BASIC, "Basic", "⭐"),
            (CosmeticRarity.PREMIUM, "Premium", "💎"),
            (CosmeticRarity.ELITE, "Elite", "👑"),
            (CosmeticRarity.SPECIAL, "Special", "✨"),
        ]

        for rarity, label, emoji in rarities:
            view.add_item(RarityButton(self.category, rarity, label, emoji))

        # Определить цвет embed по категории
        embed_color = discord.Color.blue() if self.category == "icons" else discord.Color.purple()

        embed = discord.Embed(
            title=f"🛒 {self.label} - Выберите редкость",
            description="Выберите редкость товаров для просмотра",
            color=embed_color
        )

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class RarityButton(discord.ui.Button):
    """Кнопка редкости товара."""

    def __init__(self, category: str, rarity: CosmeticRarity, label: str, emoji: str):
        # Определить стиль кнопки по редкости
        style_map = {
            CosmeticRarity.BASIC: discord.ButtonStyle.secondary,
            CosmeticRarity.PREMIUM: discord.ButtonStyle.primary,
            CosmeticRarity.ELITE: discord.ButtonStyle.success,
            CosmeticRarity.SPECIAL: discord.ButtonStyle.danger,
        }
        style = style_map.get(rarity, discord.ButtonStyle.secondary)

        # Использовать emoji в label для более красивого вида
        label = f"{emoji} {label}"

        super().__init__(
            style=style,
            label=label,
            custom_id=f"shop_rarity:{category}:{rarity.value}"
        )
        self.category = category
        self.rarity = rarity

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать товары конкретной редкости."""
        await interaction.response.defer(ephemeral=True)

        # Получить товары категории и редкости
        all_items = shop_store.get_items_by_category(self.category)
        items = [item for item in all_items if item.rarity == self.rarity]

        if not items:
            await interaction.followup.send(
                "❌ Нет товаров в этой категории."
            )
            return

        # Сортировать по цене (от дешёвого к дорогому)
        items.sort(key=lambda x: x.price)

        # Определить цвет embed по редкости
        color_map = {
            CosmeticRarity.BASIC: discord.Color.light_grey(),
            CosmeticRarity.PREMIUM: discord.Color.gold(),
            CosmeticRarity.ELITE: discord.Color.orange(),
            CosmeticRarity.SPECIAL: discord.Color.purple(),
        }
        embed_color = color_map.get(self.rarity, discord.Color.gold())

        # Определить описание редкости
        rarity_descriptions = {
            CosmeticRarity.BASIC: "Базовые товары для всех",
            CosmeticRarity.PREMIUM: "Премиум товары для опытных",
            CosmeticRarity.ELITE: "Элитные товары для топов",
            CosmeticRarity.SPECIAL: "Специальные редкие товары",
        }
        description = rarity_descriptions.get(self.rarity, "")

        # Создать embed с товарами
        embed = discord.Embed(
            title=f"🛒 {self.label}",
            description=description,
            color=embed_color
        )

        # Создать View с кнопками товаров
        view = discord.ui.View()
        view.add_item(RarityBackButton(self.category))

        # Добавить кнопки для каждого товара (по 2 в ряд для мобильных)
        for i, item in enumerate(items):
            # Показывать значок/тег вместо названия
            display_name = item.value if item.value else item.name
            label = f"{display_name} - {item.price} 🪙"
            button = ShopBuyButton(item.id, label)
            # Не используем row для простоты
            view.add_item(button)

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class RarityBackButton(discord.ui.Button):
    """Кнопка возврата к выбору редкости."""

    def __init__(self, category: str):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="↩️ Назад",
            custom_id=f"shop_rarity_back:{category}"
        )
        self.category = category

    async def callback(self, interaction: discord.Interaction) -> None:
        """Вернуться к выбору редкости."""
        # Создать View с кнопками редкости
        view = discord.ui.View()
        view.add_item(ShopBackButton())

        # Добавить кнопки для каждой редкости
        rarities = [
            (CosmeticRarity.BASIC, "Basic", "⭐"),
            (CosmeticRarity.PREMIUM, "Premium", "💎"),
            (CosmeticRarity.ELITE, "Elite", "👑"),
            (CosmeticRarity.SPECIAL, "Special", "✨"),
        ]

        for rarity, label, emoji in rarities:
            view.add_item(RarityButton(self.category, rarity, label, emoji))

        # Определить название категории
        category_label = "Значки" if self.category == "icons" else "Теги"
        embed = discord.Embed(
            title=f"🛒 {category_label} - Выберите редкость",
            color=discord.Color.gold()
        )

        await interaction.response.edit_message(embed=embed, view=view)


class ShopBackButton(discord.ui.Button):
    """Кнопка возврата в главное меню магазина."""

    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="↩️ В меню",
            custom_id="shop_back"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Вернуться в главное меню магазина."""
        from storage.user_balance_store import user_balance_store

        # Получить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

        # Создать главное меню
        embed = discord.Embed(
            title="🛒 Магазин",
            description=f"💰 {balance} 🪙",
            color=discord.Color.gold()
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

        # Добавить в инвентарь (не экипировать автоматически)
        cosmetic = PlayerCosmetic(
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
            item_id=self.item_id,
            equipped=False
        )
        inventory_store.add_cosmetic(cosmetic)

        await interaction.response.send_message(
            f"✅ Вы купили **{item.name}** за {item.price} 🪙!\n\n"
            f"Используйте `/inventory` для экипировки.",
            ephemeral=True
        )


class InventoryEquipButton(discord.ui.Button):
    """Кнопка экипировки предмета."""

    def __init__(self, item_id: str, label: str):
        super().__init__(
            style=discord.ButtonStyle.success,
            label=label,
            custom_id=f"inv_equip:{item_id}"
        )
        self.item_id = item_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Экипировать предмет."""
        # Экипировать
        success = inventory_store.equip_cosmetic(interaction.guild_id, interaction.user.id, self.item_id)
        if success:
            item = shop_store.get_item(self.item_id)
            item_name = item.name if item else self.item_id
            await interaction.response.send_message(
                f"✅ **{item_name}** экипирован!",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ Не удалось экипировать предмет.",
                ephemeral=True
            )


class InventoryUnequipButton(discord.ui.Button):
    """Кнопка снятия предмета."""

    def __init__(self, item_id: str, label: str):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=label,
            custom_id=f"inv_unequip:{item_id}"
        )
        self.item_id = item_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Снять предмет."""
        # Снять
        success = inventory_store.unequip_cosmetic(interaction.guild_id, interaction.user.id, self.item_id)
        if success:
            item = shop_store.get_item(self.item_id)
            item_name = item.name if item else self.item_id
            await interaction.response.send_message(
                f"✅ **{item_name}** снят.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ Не удалось снять предмет.",
                ephemeral=True
            )


class RolesButton(discord.ui.Button):
    """Кнопка для показа Discord ролей."""

    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Роли",
            emoji="👑",
            custom_id="shop_roles"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать все доступные роли."""
        await interaction.response.defer(ephemeral=True)

        # Получить все роли из магазина
        all_items = shop_store.get_all_items()
        roles = [item for item in all_items if item.item_type == "role"]

        if not roles:
            await interaction.followup.send(
                "❌ Нет доступных ролей."
            )
            return

        # Сортировать по цене (от дешёвого к дорогому)
        roles.sort(key=lambda x: x.price)

        # Создать embed с ролями
        embed = discord.Embed(
            title="👑 Discord Роли",
            description="Купите роль для получения специальных прав",
            color=discord.Color.gold()
        )

        # Создать View с кнопками покупки
        view = discord.ui.View()
        view.add_item(ShopBackButton())

        for role in roles:
            # Получить уровень игрока для проверки требования
            from storage.player_stats_store import player_stats_store
            stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
            user_level = stats.level if stats else 1

            # Проверить требование уровня
            can_buy = user_level >= role.required_level
            level_req = f" (Lvl {role.required_level}+)" if role.required_level > 0 else ""

            label = f"{role.name} - {role.price} 🪙{level_req}"
            button = RoleBuyButton(role.id, label, can_buy)
            view.add_item(button)

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class RoleBuyButton(discord.ui.Button):
    """Кнопка покупки роли."""

    def __init__(self, item_id: str, label: str, can_buy: bool):
        style = discord.ButtonStyle.success if can_buy else discord.ButtonStyle.secondary
        super().__init__(
            style=style,
            label=label,
            custom_id=f"shop_buy_role:{item_id}",
            disabled=not can_buy
        )
        self.item_id = item_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Купить роль."""
        await interaction.response.defer(ephemeral=True)

        item = shop_store.get_item(self.item_id)
        if not item:
            await interaction.followup.send("❌ Предмет не найден.")
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < item.price:
            await interaction.followup.send(
                f"❌ Недостаточно монет. Нужно: {item.price} 🪙, у вас: {balance} 🪙"
            )
            return

        # Проверить требование уровня
        if item.required_level > 0:
            from storage.player_stats_store import player_stats_store
            stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
            if not stats or stats.level < item.required_level:
                await interaction.followup.send(
                    f"❌ Требуется уровень {item.required_level} для покупки этой роли."
                )
                return

        # Снять монеты
        await user_balance_store.subtract_balance(interaction.guild_id, interaction.user.id, item.price)

        # Купить предмет (назначить роль)
        success = await inventory_store.purchase_item(
            interaction.guild_id,
            interaction.user.id,
            self.item_id,
            interaction.guild
        )

        if success:
            await interaction.followup.send(
                f"✅ Вы купили **{item.name}** за {item.price} 🪙!"
            )
        else:
            # Возврат монет при ошибке
            await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, item.price)
            await interaction.followup.send(
                "❌ Не удалось назначить роль. Монеты возвращены."
            )


class ShopMainView(discord.ui.View):
    """Главное меню магазина."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ShopCategoryButton("icons", "Значки", "✨"))
        self.add_item(ShopCategoryButton("tags", "Теги", "🏷️"))
        self.add_item(RolesButton())
