"""View для интерактивного магазина."""

import discord
from storage.shop_store import shop_store
from storage.user_balance_store import user_balance_store
from storage.shop_store import inventory_store
from storage.case_store import case_store
from models.shop_item import PlayerCosmetic, CosmeticRarity


class ShopMainView(discord.ui.View):
    """Главное меню магазина."""

    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ShopCategorySelect())
        self.add_item(InventoryButton())


class InventoryButton(discord.ui.Button):
    """Кнопка для перехода в инвентарь."""

    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="🎒 Мой инвентарь",
            custom_id="shop_inventory"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать инвентарь."""
        # Trigger inventory command
        bot = interaction.client
        command = bot.tree.get_command("inventory")
        if command:
            await command.callback(interaction)
        else:
            await interaction.response.send_message(
                "❌ Команда инвентаря не найдена.",
                ephemeral=True
            )


class ShopCategorySelect(discord.ui.Select):
    """Выпадающее меню выбора категории магазина."""

    def __init__(self):
        options = [
            discord.SelectOption(
                label="✨ Значки",
                value="icons",
                description="Косметические значки для профиля",
                emoji="✨"
            ),
            discord.SelectOption(
                label="🏷️ Теги",
                value="tags",
                description="Косметические теги для имени",
                emoji="🏷️"
            ),
            discord.SelectOption(
                label="👑 Discord Роли",
                value="roles",
                description="Получите специальные права на сервере",
                emoji="👑"
            ),
            discord.SelectOption(
                label="📦 Кейсы",
                value="cases",
                description="Случайные призы и бонусы",
                emoji="📦"
            ),
        ]
        super().__init__(
            placeholder="Выберите категорию товаров...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать выбор категории."""
        category = self.values[0]

        if category == "cases":
            # Показать кейсы
            await show_cases_category(interaction)
        elif category == "roles":
            # Показать роли
            await show_roles_list(interaction)
        else:
            # Показать редкость для значков/тегов
            await show_rarity_selection(interaction, category)


async def show_cases_category(interaction: discord.Interaction) -> None:
    """Показать категорию кейсов."""
    from storage.case_store import case_store

    # Получить все кейсы
    cases = case_store.get_all_cases()

    if not cases:
        await interaction.response.send_message(
            "❌ Нет доступных кейсов.",
            ephemeral=True
        )
        return

    # Создать список кейсов
    cases_list = "\n".join([
        f"{idx + 1}. 📦 **{case.name}**\n"
        f"   ├ 📝 {case.description}\n"
        f"   └ 💰 {case.price} 🪙"
        for idx, case in enumerate(cases)
    ])

    # Получить баланс
    balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

    embed = discord.Embed(
        title="📦 Кейсы | Магазин",
        description=f"Выберите кейс для открытия из списка ниже:\n\n{cases_list}",
        color=discord.Color.orange()
    )
    embed.add_field(name="💳 Ваш баланс", value=f"{balance} 🪙", inline=False)

    view = ShopBackView()
    view.add_item(CaseSelect(cases))
    await interaction.response.edit_message(embed=embed, view=view)


async def show_roles_list(interaction: discord.Interaction) -> None:
    """Показать список ролей."""
    all_items = shop_store.get_all_items()
    roles = [item for item in all_items if item.item_type == "role"]

    if not roles:
        await interaction.response.send_message(
            "❌ Нет доступных ролей.",
            ephemeral=True
        )
        return

    # Сортировать по цене
    roles.sort(key=lambda x: x.price)

    # Создать список ролей
    roles_list = "\n".join([
        f"{idx + 1}. 👑 **{role.name}**\n"
        f"   ├ 📝 {role.description}\n"
        f"   └ 💰 {role.price} 🪙"
        for idx, role in enumerate(roles)
    ])

    # Получить баланс
    balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

    embed = discord.Embed(
        title="👑 Discord Роли | Магазин",
        description=f"Выберите роль для покупки из списка ниже:\n\n{roles_list}",
        color=discord.Color.gold()
    )
    embed.add_field(name="💳 Ваш баланс", value=f"{balance} 🪙", inline=False)

    view = ShopBackView()
    view.add_item(RoleSelect(roles))
    await interaction.response.edit_message(embed=embed, view=view)


async def show_rarity_selection(interaction: discord.Interaction, category: str) -> None:
    """Показать выбор редкости."""
    view = discord.ui.View()
    view.add_item(ShopBackButton())
    view.add_item(RaritySelect(category))

    category_label = "Значки" if category == "icons" else "Теги"
    embed = discord.Embed(
        title=f"✨ {category_label} - Выберите редкость",
        description="Выберите редкость товаров для просмотра из списка ниже",
        color=discord.Color.purple()
    )

    await interaction.response.edit_message(embed=embed, view=view)


class RaritySelect(discord.ui.Select):
    """Выпадающее меню выбора редкости товара."""

    def __init__(self, category: str):
        options = [
            discord.SelectOption(
                label="⭐ Basic",
                value="basic",
                description="Базовые товары для всех"
            ),
            discord.SelectOption(
                label="💎 Premium",
                value="premium",
                description="Премиум товары для опытных"
            ),
            discord.SelectOption(
                label="👑 Elite",
                value="elite",
                description="Элитные товары для топов"
            ),
            discord.SelectOption(
                label="✨ Special",
                value="special",
                description="Специальные редкие товары"
            ),
        ]
        super().__init__(
            placeholder="Выберите редкость...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.category = category

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать товары конкретной редкости."""
        rarity_value = self.values[0]
        rarity_map = {
            "basic": CosmeticRarity.BASIC,
            "premium": CosmeticRarity.PREMIUM,
            "elite": CosmeticRarity.ELITE,
            "special": CosmeticRarity.SPECIAL,
        }
        rarity = rarity_map.get(rarity_value, CosmeticRarity.BASIC)

        # Получить товары категории и редкости
        all_items = shop_store.get_items_by_category(self.category)
        items = [item for item in all_items if item.rarity == rarity]

        if not items:
            await interaction.response.send_message(
                "❌ Нет товаров в этой категории.",
                ephemeral=True
            )
            return

        # Сортировать по цене
        items.sort(key=lambda x: x.price)

        # Создать список товаров
        items_list = "\n".join([
            f"{idx + 1}. {get_item_emoji(item.category)} **{get_item_emoji(item.category)} {item.name}**\n"
            f"   ├ 📝 {item.description}\n"
            f"   └ 💰 {item.price} 🪙"
            for idx, item in enumerate(items)
        ])

        # Получить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

        # Цвет по редкости
        color_map = {
            CosmeticRarity.BASIC: discord.Color.light_grey(),
            CosmeticRarity.PREMIUM: discord.Color.gold(),
            CosmeticRarity.ELITE: discord.Color.orange(),
            CosmeticRarity.SPECIAL: discord.Color.purple(),
        }
        embed_color = color_map.get(rarity, discord.Color.gold())

        category_label = "Значки" if self.category == "icons" else "Теги"
        embed = discord.Embed(
            title=f"✨ {category_label} - {rarity_value.capitalize()} | Магазин",
            description=f"\n{items_list}",
            color=embed_color
        )
        embed.add_field(name="💳 Ваш баланс", value=f"{balance} 🪙", inline=False)

        view = ShopBackView()
        view.add_item(CosmeticSelect(items))
        await interaction.response.edit_message(embed=embed, view=view)


def get_item_emoji(category: str) -> str:
    """Получить эмодзи для категории товара."""
    emoji_map = {
        "icons": "✨",
        "tags": "🏷️",
    }
    return emoji_map.get(category, "🛒")


class CosmeticSelect(discord.ui.Select):
    """Выпадающее меню выбора косметики."""

    def __init__(self, items):
        options = []
        for item in items:
            display_name = item.value if item.value else item.name
            options.append(
                discord.SelectOption(
                    label=display_name,
                    value=item.id,
                    description=f"Цена: {item.price} 🪙",
                    emoji="🛒"
                )
            )

        super().__init__(
            placeholder="Выберите товар...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.items = items

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать карточку товара."""
        item_id = self.values[0]
        item = shop_store.get_item(item_id)

        if not item:
            await interaction.response.send_message("❌ Товар не найден.", ephemeral=True)
            return

        await show_item_card(interaction, item)


class CaseSelect(discord.ui.Select):
    """Выпадающее меню выбора кейса."""

    def __init__(self, cases):
        options = []
        for case in cases:
            options.append(
                discord.SelectOption(
                    label=case.name,
                    value=case.id,
                    description=f"Цена: {case.price} 🪙",
                    emoji="📦"
                )
            )

        super().__init__(
            placeholder="Выберите кейс...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.cases = cases

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать карточку кейса."""
        case_id = self.values[0]
        case = case_store.get_case(case_id)

        if not case:
            await interaction.response.send_message("❌ Кейс не найден.", ephemeral=True)
            return

        await show_case_card(interaction, case)


class RoleSelect(discord.ui.Select):
    """Выпадающее меню выбора роли."""

    def __init__(self, roles):
        options = []
        for role in roles:
            level_req = f" (Lvl {role.required_level}+)" if role.required_level > 0 else ""
            options.append(
                discord.SelectOption(
                    label=role.name,
                    value=role.id,
                    description=f"{role.price} 🪙{level_req}",
                    emoji="👑"
                )
            )

        super().__init__(
            placeholder="Выберите роль...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.roles = roles

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать карточку роли."""
        role_id = self.values[0]
        item = shop_store.get_item(role_id)

        if not item:
            await interaction.response.send_message("❌ Роль не найдена.", ephemeral=True)
            return

        await show_item_card(interaction, item)


async def show_item_card(interaction: discord.Interaction, item) -> None:
    """Показать карточку товара."""
    # Получить баланс
    balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

    # Редкость
    rarity_emoji = {
        CosmeticRarity.BASIC: "⭐",
        CosmeticRarity.PREMIUM: "💎",
        CosmeticRarity.ELITE: "👑",
        CosmeticRarity.SPECIAL: "✨",
    }
    rarity_label = {
        CosmeticRarity.BASIC: "Обычный",
        CosmeticRarity.PREMIUM: "Премиум",
        CosmeticRarity.ELITE: "Элитный",
        CosmeticRarity.SPECIAL: "Специальный",
    }

    emoji = rarity_emoji.get(item.rarity, "🛒")
    label = rarity_label.get(item.rarity, "Обычный")

    # Цвет по редкости
    color_map = {
        CosmeticRarity.BASIC: discord.Color.light_grey(),
        CosmeticRarity.PREMIUM: discord.Color.gold(),
        CosmeticRarity.ELITE: discord.Color.orange(),
        CosmeticRarity.SPECIAL: discord.Color.purple(),
    }
    embed_color = color_map.get(item.rarity, discord.Color.gold())

    # Количество игроков
    if item.item_type == "role":
        player_count = "Все участники сервера"
    else:
        player_count = "Персональный предмет"

    # Имя с эмодзи
    item_emoji = get_item_emoji(item.category) if item.category != "role" else "👑"
    display_name = f"{item_emoji} {item.name}"

    embed = discord.Embed(
        title=f"{emoji} Товар: {display_name}",
        description=f"📝 Описание:\n{item.description}",
        color=embed_color
    )

    embed.add_field(name="📊 Характеристики", value=f"⏳ Срок: Навсегда\n👥 Тип: {player_count}", inline=False)
    embed.add_field(name="🏷️ Редкость", value=f"{emoji} {label}", inline=True)
    embed.add_field(name="💰 Стоимость", value=f"{item.price} 🪙", inline=True)
    embed.add_field(name="💳 Ваш баланс", value=f"{balance} 🪙", inline=True)

    # Требование уровня
    if item.required_level > 0:
        embed.add_field(name="📊 Требование", value=f"Уровень {item.required_level}+", inline=False)

    embed.add_field(name="💡", value="Для покупки нажмите кнопку ниже", inline=False)

    view = ItemCardView(item.id, item.price)
    await interaction.response.edit_message(embed=embed, view=view)


async def show_case_card(interaction: discord.Interaction, case) -> None:
    """Показать карточку кейса."""
    # Получить баланс
    balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

    embed = discord.Embed(
        title=f"📦 Кейс: {case.name}",
        description=f"📝 Описание:\n{case.description}\n\n🎲 Шанс получить редкий предмет!",
        color=discord.Color.orange()
    )

    embed.add_field(name="📊 Характеристики", value=f"⏳ Тип: Случайный приз\n🎲 Шанс легендарного: {case.legendary_chance}%", inline=False)
    embed.add_field(name="💰 Стоимость", value=f"{case.price} 🪙", inline=True)
    embed.add_field(name="💳 Ваш баланс", value=f"{balance} 🪙", inline=True)
    embed.add_field(name="💡", value="Для покупки нажмите кнопку ниже", inline=False)

    view = CaseCardView(case.id, case.price)
    await interaction.response.edit_message(embed=embed, view=view)


class ItemCardView(discord.ui.View):
    """View с кнопками для карточки товара."""

    def __init__(self, item_id: str, price: int):
        super().__init__(timeout=180)
        self.add_item(BuyButton(item_id, price))
        self.add_item(ShopBackButton())


class CaseCardView(discord.ui.View):
    """View с кнопками для карточки кейса."""

    def __init__(self, case_id: str, price: int):
        super().__init__(timeout=180)
        self.add_item(BuyCaseButton(case_id, price))
        self.add_item(ShopBackButton())


class BuyButton(discord.ui.Button):
    """Кнопка покупки товара."""

    def __init__(self, item_id: str, price: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label=f"🛒 Купить за {price} 🪙",
            custom_id=f"buy_{item_id}"
        )
        self.item_id = item_id
        self.price = price

    async def callback(self, interaction: discord.Interaction) -> None:
        """Купить товар."""
        item = shop_store.get_item(self.item_id)
        if not item:
            await interaction.response.send_message("❌ Товар не найден.", ephemeral=True)
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < self.price:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. Нужно: {self.price} 🪙, у вас: {balance} 🪙",
                ephemeral=True
            )
            return

        # Проверить требование уровня
        if item.required_level > 0:
            from storage.player_stats_store import player_stats_store
            stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
            if not stats or stats.level < item.required_level:
                await interaction.response.send_message(
                    f"❌ Требуется уровень {item.required_level} для покупки.",
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
        await user_balance_store.subtract_balance(interaction.guild_id, interaction.user.id, self.price)

        # Добавить в инвентарь
        if item.item_type == "role":
            # Купить роль
            success = await inventory_store.purchase_item(
                interaction.guild_id,
                interaction.user.id,
                self.item_id,
                interaction.guild
            )
            if success:
                await interaction.response.send_message(
                    f"✅ Вы купили **{item.name}** за {self.price} 🪙!",
                    ephemeral=True
                )
            else:
                await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, self.price)
                await interaction.response.send_message(
                    "❌ Не удалось назначить роль. Монеты возвращены.",
                    ephemeral=True
                )
        else:
            # Купить косметику
            cosmetic = PlayerCosmetic(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
                item_id=self.item_id,
                equipped=False
            )
            inventory_store.add_cosmetic(cosmetic)
            await interaction.response.send_message(
                f"✅ Вы купили **{item.name}** за {self.price} 🪙!\n\nИспользуйте `/inventory` для экипировки.",
                ephemeral=True
            )


class BuyCaseButton(discord.ui.Button):
    """Кнопка покупки кейса."""

    def __init__(self, case_id: str, price: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label=f"🛒 Купить за {price} 🪙",
            custom_id=f"buy_case_{case_id}"
        )
        self.case_id = case_id
        self.price = price

    async def callback(self, interaction: discord.Interaction) -> None:
        """Купить кейс."""
        from storage.case_store import case_store

        case = case_store.get_case(self.case_id)
        if not case:
            await interaction.response.send_message("❌ Кейс не найден.", ephemeral=True)
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < self.price:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. Нужно: {self.price} 🪙, у вас: {balance} 🪙",
                ephemeral=True
            )
            return

        # Списать монеты
        await user_balance_store.subtract_balance(interaction.guild_id, interaction.user.id, self.price)

        # Открыть кейс
        result = await case_store.open_case(interaction.guild_id, interaction.user.id, self.case_id)

        if result:
            await interaction.response.send_message(
                f"✅ Вы открыли кейс **{case.name}** и получили:\n{result}",
                ephemeral=True
            )
        else:
            await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, self.price)
            await interaction.response.send_message(
                "❌ Не удалось открыть кейс. Монеты возвращены.",
                ephemeral=True
            )


class ShopBackView(discord.ui.View):
    """View с кнопкой назад."""

    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ShopBackButton())


class ShopBackButton(discord.ui.Button):
    """Кнопка возврата в главное меню магазина."""

    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="◀ Назад в меню",
            custom_id="shop_back"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Вернуться в главное меню магазина."""
        from storage.user_balance_store import user_balance_store

        # Получить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

        # Создать главное меню
        embed = discord.Embed(
            title="🛍️ Магазин Сервера | Главный каталог",
            description="Добро пожаловать в магазин!\nВыберите категорию ниже, чтобы посмотреть товары и улучшить свой профиль.",
            color=discord.Color.gold()
        )
        embed.add_field(name="💳 Ваш баланс", value=f"{balance} 🪙", inline=False)

        view = ShopMainView()
        await interaction.response.edit_message(embed=embed, view=view)


# Старые классы для совместимости
class ShopCategoryButton(discord.ui.Button):
    """Кнопка категории магазина (устаревшая)."""

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

        # Создать View с выпадающим меню редкости
        view = discord.ui.View()
        view.add_item(ShopBackButton())
        view.add_item(RaritySelect(self.category))

        # Определить цвет embed по категории
        embed_color = discord.Color.blue() if self.category == "icons" else discord.Color.purple()

        category_label = "Значки" if self.category == "icons" else "Теги"
        embed = discord.Embed(
            title=f"🛒 {category_label} - Выберите редкость",
            description="Выберите редкость товаров для просмотра",
            color=embed_color
        )

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class ItemSelect(discord.ui.Select):
    """Выпадающее меню выбора товара (устаревшая)."""

    def __init__(self, items, category):
        options = []
        for item in items:
            display_name = item.value if item.value else item.name
            options.append(
                discord.SelectOption(
                    label=display_name,
                    value=item.id,
                    description=f"Цена: {item.price} 🪙",
                    emoji="🛒"
                )
            )

        super().__init__(
            placeholder="Выберите товар для покупки...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.items = items
        self.category = category

    async def callback(self, interaction: discord.Interaction) -> None:
        """Купить выбранный товар."""
        item_id = self.values[0]
        item = shop_store.get_item(item_id)

        if not item:
            await interaction.response.send_message("❌ Товар не найден.", ephemeral=True)
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
            if cosmetic.item_id == item_id:
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
            item_id=item_id,
            equipped=False
        )
        inventory_store.add_cosmetic(cosmetic)

        await interaction.response.send_message(
            f"✅ Вы купили **{item.name}** за {item.price} 🪙!\n\n"
            f"Используйте `/inventory` для экипировки.",
            ephemeral=True
        )


class RarityBackButton(discord.ui.Button):
    """Кнопка возврата к выбору редкости (устаревшая)."""

    def __init__(self, category: str):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="↩️ Назад",
            custom_id=f"shop_rarity_back:{category}"
        )
        self.category = category

    async def callback(self, interaction: discord.Interaction) -> None:
        """Вернуться к выбору редкости."""
        # Создать View с выпадающим меню редкости
        view = discord.ui.View()
        view.add_item(ShopBackButton())
        view.add_item(RaritySelect(self.category))

        # Определить название категории
        category_label = "Значки" if self.category == "icons" else "Теги"
        embed = discord.Embed(
            title=f"🛒 {category_label} - Выберите редкость",
            description="Выберите редкость товаров для просмотра",
            color=discord.Color.gold()
        )

        await interaction.response.edit_message(embed=embed, view=view)


class ShopBuyButton(discord.ui.Button):
    """Кнопка покупки товара (устаревшая)."""

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
    """Кнопка для показа Discord ролей (устаревшая)."""

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
    """Кнопка покупки роли (устаревшая)."""

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
