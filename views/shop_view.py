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


class ShopCategorySelect(discord.ui.Select):
    """Выпадающее меню выбора категории магазина."""

    def __init__(self):
        options = [
            discord.SelectOption(
                label="✨ Значки",
                value="icons",
                description="Косметические иконы профиля",
                emoji="✨"
            ),
            discord.SelectOption(
                label="🏷️ Теги",
                value="tags",
                description="Префиксы для никнейма в чате",
                emoji="🏷️"
            ),
            discord.SelectOption(
                label="👑 Discord Роли & Доступы",
                value="roles",
                description="Роли и права",
                emoji="👑"
            ),
            discord.SelectOption(
                label="📦 Кейсы",
                value="cases",
                description="Награды и удача",
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
    """Показать категорию кейсов с новым шаблоном."""
    from storage.player_stats_store import player_stats_store
    from storage.shop_store import inventory_store
    from cogs.tournament import get_rank_emoji

    # Получить все кейсы
    cases = case_store.get_all_cases()

    if not cases:
        await interaction.response.send_message(
            "❌ Нет доступных кейсов.",
            ephemeral=True
        )
        return

    # Создать список кейсов
    cases_list = "\n\n".join([
        f"⭐ **{case.name}**\n"
        f"├ 📝 {case.description}\n"
        f"└ 💰 **Цена:** {case.price} 🪙"
        for case in cases
    ])

    # Получить данные профиля
    balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
    cosmetics = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
    inventory_count = len(cosmetics)
    max_inventory = 20

    stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
    rank = "Без ранга"
    if stats:
        rank = get_rank_emoji(stats.level)

    # Создать embed
    embed = discord.Embed(
        title="📦 КАТАЛОГ | Кейсы",
        description=f"Выберите кейс из списка ниже для открытия:\n\n{cases_list}",
        color=discord.Color.orange()
    )

    # Профиль
    embed.add_field(
        name="💳 ВАШ ПРОФИЛЬ",
        value=f"├ 👛 **Баланс:** {balance:,} 🪙\n"
              f"├ 🏆 **Ранг:** {rank}\n"
              f"└ 🎒 **Мест в инвентаре:** {inventory_count}/{max_inventory}",
        inline=False
    )

    embed.add_field(
        name="💡 Выберите кейс в выпадающем меню для открытия",
        value="",
        inline=False
    )

    view = ShopBackView()
    view.add_item(CaseSelect(cases))
    await interaction.response.edit_message(embed=embed, view=view)


async def show_roles_list(interaction: discord.Interaction) -> None:
    """Показать список ролей с новым шаблоном."""
    from storage.player_stats_store import player_stats_store
    from storage.shop_store import inventory_store
    from cogs.tournament import get_rank_emoji

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
    roles_list = "\n\n".join([
        f"{item.value if item.value else '�'} **{item.name}**\n"
        f"├ 📝 {item.description}\n"
        f"└ 💰 **Цена:** {item.price} 🪙"
        for item in roles
    ])

    # Получить данные профиля
    balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
    cosmetics = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
    inventory_count = len(cosmetics)
    max_inventory = 20

    stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
    rank = "Без ранга"
    if stats:
        rank = get_rank_emoji(stats.level)

    # Создать embed
    embed = discord.Embed(
        title="👑 КАТАЛОГ | Discord Роли",
        description=f"Выберите роль из списка ниже для покупки:\n\n{roles_list}",
        color=discord.Color.gold()
    )

    # Профиль
    embed.add_field(
        name="💳 ВАШ ПРОФИЛЬ",
        value=f"├ 👛 **Баланс:** {balance:,} 🪙\n"
              f"├ 🏆 **Ранг:** {rank}\n"
              f"└ 🎒 **Мест в инвентаре:** {inventory_count}/{max_inventory}",
        inline=False
    )

    embed.add_field(
        name="💡 Выберите роль в выпадающем меню для покупки",
        value="",
        inline=False
    )

    view = ShopBackView()
    view.add_item(RoleSelect(roles))
    await interaction.response.edit_message(embed=embed, view=view)


async def show_rarity_selection(interaction: discord.Interaction, category: str) -> None:
    """Показать выбор редкости с новым шаблоном."""
    from storage.player_stats_store import player_stats_store
    from storage.shop_store import inventory_store
    from cogs.tournament import get_rank_emoji

    category_label = "Значки" if category == "icons" else "Теги"
    category_emoji = "✨" if category == "icons" else "🏷️"

    # Получить данные профиля
    balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
    cosmetics = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
    inventory_count = len(cosmetics)
    max_inventory = 20

    stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
    rank = "Без ранга"
    if stats:
        rank = get_rank_emoji(stats.level)

    # Создать embed
    embed = discord.Embed(
        title=f"{category_emoji} КАТАЛОГ | {category_label}",
        description="Выберите уровень товаров из списка ниже для просмотра доступных предметов и цен:",
        color=discord.Color.purple()
    )

    # Список уровней
    embed.add_field(
        name="",
        value="⭐ **Basic**    • Базовые товары для всех\n"
              "💎 **Premium**  • Премиум товары для опытных\n"
              "👑 **Elite**    • Элитные товары для топов\n"
              "✨ **Special**  • Специальные редкие товары",
        inline=False
    )

    # Профиль
    embed.add_field(
        name="💳 ВАШ ПРОФИЛЬ",
        value=f"├ 👛 **Баланс:** {balance:,} 🪙\n"
              f"├ 🏆 **Ранг:** {rank}\n"
              f"└ 🎒 **Мест в инвентаре:** {inventory_count}/{max_inventory}",
        inline=False
    )

    embed.add_field(
        name="💡 Для перехода выберите подкатегорию в меню",
        value="",
        inline=False
    )

    view = discord.ui.View()
    view.add_item(ShopBackButton())
    view.add_item(RaritySelect(category))
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
            placeholder="Выберите подкатегорию...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.category = category

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать товары конкретной редкости с новым шаблоном."""
        from storage.player_stats_store import player_stats_store
        from storage.shop_store import inventory_store
        from cogs.tournament import get_rank_emoji

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
        category_label = "Значки" if self.category == "icons" else "Теги"
        category_emoji = "✨" if self.category == "icons" else "🏷️"
        
        if self.category == "tags":
            items_list = "\n\n".join([
                f"🏷️ -  {item.value}\n"
                f"└ 💰 **Цена:** {item.price} 🪙"
                for item in items
            ])
        else:
            items_list = "\n\n".join([
                f"{item.value} **{item.name}**\n"
                f"└ 💰 **Цена:** {item.price} 🪙"
                for item in items
            ])

        # Получить данные профиля
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        cosmetics = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
        inventory_count = len(cosmetics)
        max_inventory = 20

        stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
        rank = "Без ранга"
        if stats:
            rank = get_rank_emoji(stats.level)

        # Цвет по редкости
        color_map = {
            CosmeticRarity.BASIC: discord.Color.light_grey(),
            CosmeticRarity.PREMIUM: discord.Color.gold(),
            CosmeticRarity.ELITE: discord.Color.orange(),
            CosmeticRarity.SPECIAL: discord.Color.purple(),
        }
        embed_color = color_map.get(rarity, discord.Color.gold())

        # Создать embed
        embed = discord.Embed(
            title=f"{category_emoji} КАТАЛОГ | {category_label} — {rarity_value.capitalize()}",
            description=f"Выберите {category_label.lower()} из списка ниже для покупки:\n\n{items_list}",
            color=embed_color
        )

        # Профиль
        embed.add_field(
            name="💳 ВАШ ПРОФИЛЬ",
            value=f"├ 👛 **Баланс:** {balance:,} 🪙\n"
                  f"├ 🏆 **Ранг:** {rank}\n"
                  f"└ 🎒 **Мест в инвентаре:** {inventory_count}/{max_inventory}",
            inline=False
        )

        embed.add_field(
            name="💡 Выберите предмет в выпадающем меню для покупки",
            value="",
            inline=False
        )

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
            placeholder="Выберите значок для покупки...",
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

        # Для тегов используем отдельный шаблон
        if item.category == "tags":
            await show_tag_card(interaction, item)
        else:
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
            placeholder="Выберите роль для покупки...",
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

        await show_role_card(interaction, item)


async def show_item_card(interaction: discord.Interaction, item) -> None:
    """Показать карточку товара (эмодзи) с новым шаблоном."""
    from storage.player_stats_store import player_stats_store
    from storage.shop_store import inventory_store
    from cogs.tournament import get_rank_emoji

    # Получить баланс
    balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
    cosmetics = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
    inventory_count = len(cosmetics)
    max_inventory = 20

    stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
    rank = "Без ранга"
    if stats:
        rank = get_rank_emoji(stats.level)

    # Редкость
    rarity_emoji = {
        CosmeticRarity.BASIC: "⭐",
        CosmeticRarity.PREMIUM: "💎",
        CosmeticRarity.ELITE: "👑",
        CosmeticRarity.SPECIAL: "✨",
    }
    rarity_label = {
        CosmeticRarity.BASIC: "Basic",
        CosmeticRarity.PREMIUM: "Premium",
        CosmeticRarity.ELITE: "Elite",
        CosmeticRarity.SPECIAL: "Special",
    }

    emoji = rarity_emoji.get(item.rarity, "⭐")
    label = rarity_label.get(item.rarity, "Basic")

    # Цвет по редкости
    color_map = {
        CosmeticRarity.BASIC: discord.Color.light_grey(),
        CosmeticRarity.PREMIUM: discord.Color.gold(),
        CosmeticRarity.ELITE: discord.Color.orange(),
        CosmeticRarity.SPECIAL: discord.Color.purple(),
    }
    embed_color = color_map.get(item.rarity, discord.Color.gold())

    # Категория
    category_label = "Значки" if item.category == "icons" else "Теги"

    # Создать embed
    embed = discord.Embed(
        title=f"{emoji} ПОКУПКА ЭМОДЗИ | {item.name}",
        description="Вы действительно хотите приобрести данный предмет?",
        color=embed_color
    )

    # Информация о товаре
    embed.add_field(
        name="� **Информация о товаре:**",
        value=f"├ 🏷️ **Тип:** {category_label} ({label})\n"
              f"├ 📝 **Описание:** {item.description}\n"
              f"└ 💰 **Стоимость:** {item.price} 🪙",
        inline=False
    )

    # Профиль
    embed.add_field(
        name="💳 ВАШ ПРОФИЛЬ",
        value=f"├ 👛 **Баланс:** {balance:,} 🪙\n"
              f"├ 🏆 **Ранг:** {rank}\n"
              f"└ 🎒 **Мест в инвентаре:** {inventory_count}/{max_inventory}",
        inline=False
    )

    embed.add_field(
        name="💡 Подтвердите покупку кнопкой ниже",
        value="",
        inline=False
    )

    view = ItemCardView(item.id, item.price, "icon")
    await interaction.response.edit_message(embed=embed, view=view)


async def show_tag_card(interaction: discord.Interaction, item) -> None:
    """Показать карточку тега с новым шаблоном."""
    from storage.player_stats_store import player_stats_store
    from storage.shop_store import inventory_store
    from cogs.tournament import get_rank_emoji

    # Получить баланс
    balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
    cosmetics = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
    inventory_count = len(cosmetics)
    max_inventory = 20

    stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
    rank = "Без ранга"
    if stats:
        rank = get_rank_emoji(stats.level)

    # Редкость
    rarity_emoji = {
        CosmeticRarity.BASIC: "⭐",
        CosmeticRarity.PREMIUM: "💎",
        CosmeticRarity.ELITE: "👑",
        CosmeticRarity.SPECIAL: "✨",
    }
    rarity_label = {
        CosmeticRarity.BASIC: "Basic",
        CosmeticRarity.PREMIUM: "Premium",
        CosmeticRarity.ELITE: "Elite",
        CosmeticRarity.SPECIAL: "Special",
    }

    emoji = rarity_emoji.get(item.rarity, "⭐")
    label = rarity_label.get(item.rarity, "Basic")

    # Цвет по редкости
    color_map = {
        CosmeticRarity.BASIC: discord.Color.light_grey(),
        CosmeticRarity.PREMIUM: discord.Color.gold(),
        CosmeticRarity.ELITE: discord.Color.orange(),
        CosmeticRarity.SPECIAL: discord.Color.purple(),
    }
    embed_color = color_map.get(item.rarity, discord.Color.gold())

    # Создать embed
    embed = discord.Embed(
        title=f"🏷️ ПОКУПКА ТЕГА | {item.name}",
        description="Вы действительно хотите приобрести данный тег?",
        color=embed_color
    )

    # Информация о товаре
    embed.add_field(
        name="📌 **Информация о товаре:**",
        value=f"├ 🏷️ **Категория:** Теги ({label})\n"
              f"├ 📝 **Описание:** {item.description}\n"
              f"└ 💰 **Стоимость:** {item.price} 🪙",
        inline=False
    )

    # Предпросмотр
    embed.add_field(
        name="👁️ **Предпросмотр:**",
        value=f"└ **{item.value}** {interaction.user.display_name}",
        inline=False
    )

    # Профиль
    embed.add_field(
        name="💳 ВАШ ПРОФИЛЬ",
        value=f"├ 👛 **Баланс:** {balance:,} 🪙\n"
              f"├ 🏆 **Ранг:** {rank}\n"
              f"└ 🎒 **Мест в инвентаре:** {inventory_count}/{max_inventory}",
        inline=False
    )

    embed.add_field(
        name="💡 Подтвердите покупку кнопкой ниже",
        value="",
        inline=False
    )

    view = ItemCardView(item.id, item.price, "tag")
    await interaction.response.edit_message(embed=embed, view=view)


async def show_role_card(interaction: discord.Interaction, item) -> None:
    """Показать карточку роли с новым шаблоном."""
    from storage.player_stats_store import player_stats_store
    from storage.shop_store import inventory_store
    from cogs.tournament import get_rank_emoji

    # Получить баланс
    balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
    cosmetics = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
    inventory_count = len(cosmetics)
    max_inventory = 20

    stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
    rank = "Без ранга"
    if stats:
        rank = get_rank_emoji(stats.level)

    # Создать embed
    embed = discord.Embed(
        title=f"👑 ПОКУПКА РОЛИ | {item.name}",
        description="Вы действительно хотите приобрести эту роль?",
        color=discord.Color.gold()
    )

    # Информация о товаре
    embed.add_field(
        name="📌 **Информация о товаре:**",
        value=f"├ 🏷️ **Категория:** Discord Роли\n"
              f"├ 📝 **Описание:** {item.description}\n"
              f"├ ⚡ **Привилегии:** {item.description}\n"
              f"└ 💰 **Стоимость:** {item.price} 🪙",
        inline=False
    )

    # Отображение в профиле
    embed.add_field(
        name="🎨 **Отображение в профиле:**",
        value=f"└ 🏷️ Роль: <@&{item.role_id}>",
        inline=False
    )

    # Профиль
    embed.add_field(
        name="💳 ВАШ ПРОФИЛЬ",
        value=f"├ 👛 **Баланс:** {balance:,} 🪙\n"
              f"├ 🏆 **Ранг:** {rank}\n"
              f"└ 🎒 **Мест в инвентаре:** {inventory_count}/{max_inventory}",
        inline=False
    )

    embed.add_field(
        name="💡 Подтвердите покупку кнопкой ниже",
        value="",
        inline=False
    )

    view = ItemCardView(item.id, item.price, "role")
    await interaction.response.edit_message(embed=embed, view=view)


async def show_case_card(interaction: discord.Interaction, case) -> None:
    """Показать карточку кейса с новым шаблоном."""
    from storage.player_stats_store import player_stats_store
    from storage.shop_store import inventory_store
    from cogs.tournament import get_rank_emoji

    # Получить баланс
    balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
    cosmetics = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
    inventory_count = len(cosmetics)
    max_inventory = 20

    stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
    rank = "Без ранга"
    if stats:
        rank = get_rank_emoji(stats.level)

    # Создать embed
    embed = discord.Embed(
        title=f"📦 ПОКУПКА КЕЙСА | {case.name}",
        description="Вы действительно хотите открыть этот кейс?",
        color=discord.Color.orange()
    )

    # Информация о товаре
    embed.add_field(
        name="� **Информация о товаре:**",
        value=f"├ 🏷️ **Категория:** Кейсы\n"
              f"├ 📝 **Описание:** {case.description}\n"
              f"└ 💰 **Стоимость:** {case.price} 🪙",
        inline=False
    )

    # Шансы выпадения
    coins_chance = sum(rate for drop_type, rate in case.drop_rates.items() if drop_type.startswith("coins_"))
    item_chance = sum(rate for drop_type, rate in case.drop_rates.items() if drop_type.startswith("item_"))
    nothing_chance = case.drop_rates.get("nothing", 0.0)

    embed.add_field(
        name="🎲 **Шансы выпадения:**",
        value=f"├ 🪙 **Монеты:** {coins_chance * 100:.0f}%\n"
              f"├ 🎁 **Предмет:** {item_chance * 100:.0f}%\n"
              f"└ ❌ **Ничего:** {nothing_chance * 100:.0f}%",
        inline=False
    )

    # Профиль
    embed.add_field(
        name="💳 ВАШ ПРОФИЛЬ",
        value=f"├ 👛 **Баланс:** {balance:,} 🪙\n"
              f"├ 🏆 **Ранг:** {rank}\n"
              f"└ 🎒 **Мест в инвентаре:** {inventory_count}/{max_inventory}",
        inline=False
    )

    embed.add_field(
        name="💡 Подтвердите покупку и открытие кнопкой ниже",
        value="",
        inline=False
    )

    view = CaseCardView(case.id, case.price)
    await interaction.response.edit_message(embed=embed, view=view)


class ItemCardView(discord.ui.View):
    """View с кнопками для карточки товара."""

    def __init__(self, item_id: str, price: int, item_type: str = "icon"):
        super().__init__(timeout=180)
        self.add_item(BuyButton(item_id, price, item_type))
        self.add_item(ShopBackToListButton())


class CaseCardView(discord.ui.View):
    """View с кнопками для карточки кейса."""

    def __init__(self, case_id: str, price: int):
        super().__init__(timeout=180)
        self.add_item(BuyCaseButton(case_id, price))
        self.add_item(ShopBackToListButton())


class BuyButton(discord.ui.Button):
    """Кнопка покупки товара."""

    def __init__(self, item_id: str, price: int, item_type: str = "icon"):
        label_text = f"✅ Купить за {price} 🪙"
        if item_type == "tag":
            label_text = f"✅ Примерить и купить за {price} 🪙"
        elif item_type == "role":
            label_text = f"✅ Купить роль за {price} 🪙"
        
        super().__init__(
            style=discord.ButtonStyle.success,
            label=label_text,
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
            label=f"🎲 Открыть кейс за {price} 🪙",
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
        result = await case_store.open_case(interaction.guild_id, interaction.user.id, self.case_id, interaction.guild)

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
            label="◀ Назад в главное меню",
            custom_id="shop_back"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Вернуться в главное меню магазина."""
        from storage.user_balance_store import user_balance_store
        from storage.shop_store import inventory_store
        from storage.player_stats_store import player_stats_store
        from cogs.tournament import get_rank_emoji

        # Получить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

        # Получить инвентарь
        cosmetics = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
        inventory_count = len(cosmetics)
        max_inventory = 20

        # Получить ранг
        stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
        rank = "Без ранга"
        if stats:
            rank = get_rank_emoji(stats.level)

        # Создать embed в новом формате
        embed = discord.Embed(
            title="🛍️ МАГАЗИН СЕРВЕРА | Главное меню",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url=interaction.user.avatar.url if interaction.user.avatar else interaction.user.default_avatar.url)
        embed.description = (
            "Добро пожаловать в игровой магазин!\n"
            "Выберите нужный раздел в выпадающем меню ниже,\n"
            "чтобы посмотреть доступные товары."
        )

        # Профиль пользователя
        embed.add_field(
            name="💳 ВАШ ПРОФИЛЬ",
            value=f"├ 👛 Баланс: {balance:,} 🪙\n"
                  f"├ 🏆 Ранг: {rank}\n"
                  f"└ 🎒 Мест в инвентаре: {inventory_count}/{max_inventory}",
            inline=False
        )

        embed.add_field(
            name="💡 Для навигации используйте компоненты ниже",
            value="",
            inline=False
        )

        # Создать View с выпадающим меню категорий
        view = ShopMainView()

        await interaction.response.edit_message(embed=embed, view=view)


class ShopBackToListButton(discord.ui.Button):
    """Кнопка возврата к списку товаров."""

    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="◀ Назад к списку",
            custom_id="shop_back_list"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Вернуться к списку товаров (заглушка)."""
        # Для простоты - вернуться в главное меню
        # В будущем можно реализовать возврат к категории
        from storage.user_balance_store import user_balance_store
        from cogs.tournament import TournamentCog

        cog = interaction.client.get_cog("TournamentCog")
        if cog:
            await cog.shop(interaction)

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
