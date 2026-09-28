"""View для интерактивного магазина."""
from config import replace_emojis

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
                label=replace_emojis("✨ Значки"),
                value="icons",
                description="Косметические иконы профиля",
                emoji="✨"
            ),
            discord.SelectOption(
                label=replace_emojis("🏷️ Теги"),
                value="tags",
                description="Префиксы для никнейма в чате",
                emoji="🏷️"
            ),
            discord.SelectOption(
                label=replace_emojis("👑 Discord Роли & Доступы"),
                value="roles",
                description="Роли и права",
                emoji="👑"
            ),
            discord.SelectOption(
                label=replace_emojis("📦 Кейсы"),
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
            replace_emojis("❌ Нет доступных кейсов."),
            ephemeral=True
        )
        return

    # Создать список кейсов
    cases_parts = []
    for case in cases:
        case_emoji = "case_basic"  # default
        rare_emoji = "rare_basic"  # default
        sub_emoji = "sub_middle"  # default
        sparkle = ""
        if "Basic" in case.name:
            case_emoji = "case_basic"
            rare_emoji = "rare_basic"
            sub_emoji = "sub_middle"
        elif "Premium" in case.name:
            case_emoji = "case_premium"
            rare_emoji = "rare_premium"
            sub_emoji = "sub_middle"
        elif "Elite" in case.name:
            case_emoji = "case_elite"
            rare_emoji = "rare_elite"
            sub_emoji = "sub_middle"
        elif "Special" in case.name:
            case_emoji = "case_special"
            rare_emoji = "rare_special"
            sub_emoji = "sub_directory"
            sparkle = f" {replace_emojis('a_star')}"

        cases_parts.append(
            f"{replace_emojis(sub_emoji)} {replace_emojis(case_emoji)} **{case.name}** • {case.price} {replace_emojis('money')} {replace_emojis(rare_emoji)}{sparkle}"
        )

    cases_list = "\n".join(cases_parts)

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
        title=replace_emojis("КАТАЛОГ | Кейсы"),
        description=f"Выберите кейс из списка ниже для открытия:\n\n{replace_emojis('⚪')} **Доступные кейсы:**\n{cases_list}\n\n{replace_emojis('⚪')} **Ваш профиль:**\n{replace_emojis('sub_middle')} Баланс: {balance:,} {replace_emojis('money')}\n{replace_emojis('sub_middle')} Ранг: {rank}\n{replace_emojis('sub_directory')} Инвентарь: {inventory_count}/{max_inventory}\n\n{replace_emojis('a_dot_smaller')} Выберите кейс в меню ниже",
        color=discord.Color.from_rgb(69, 69, 69)
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

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
            replace_emojis("❌ Нет доступных ролей."),
            ephemeral=True
        )
        return

    # Сортировать по цене
    roles.sort(key=lambda x: x.price)

    # Создать список ролей
    roles_list = "\n\n".join([
        f"{item.value if item.value else ''} **{item.name}**\n"
        f"{replace_emojis('sub_middle')} {item.description}\n"
        f"{replace_emojis('sub_directory')} **Цена:** {item.price} {replace_emojis('money')}"
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
        title=replace_emojis("КАТАЛОГ | Discord Роли"),
        description=f"Выберите роль из списка ниже для покупки:\n\n{roles_list}",
        color=discord.Color.from_rgb(69, 69, 69)
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    # Профиль
    embed.add_field(
        name=replace_emojis("⚪ ВАШ ПРОФИЛЬ"),
        value=f"{replace_emojis('sub_middle')} Баланс: {balance:,} {replace_emojis('money')}\n"
              f"{replace_emojis('sub_middle')} Ранг: {rank}\n"
              f"{replace_emojis('sub_directory')} Мест в инвентаре: {inventory_count}/{max_inventory}",
        inline=False
    )

    embed.add_field(
        name=replace_emojis("a_dot_smaller Выберите роль в выпадающем меню для покупки"),
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
    category_emoji = "a_sparkle" if category == "icons" else "tag"

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
        title=f"{replace_emojis(category_emoji)} КАТАЛОГ | {category_label}",
        description=f"Выберите уровень товаров из списка ниже для просмотра доступных предметов и цен:\n\n{replace_emojis('⚪')} **Доступные категории:**\n{replace_emojis('sub_middle')} {replace_emojis('rare_basic')} **Basic** • Базовые товары\n{replace_emojis('sub_middle')} {replace_emojis('rare_premium')} **Premium** • Премиум товары\n{replace_emojis('sub_middle')} {replace_emojis('rare_elite')} **Elite** • Элитные товары\n{replace_emojis('sub_directory')} {replace_emojis('rare_special')} **Special** • Специальные редкие товары {replace_emojis('a_star')}\n\n{replace_emojis('⚪')} **Ваш профиль:**\n{replace_emojis('sub_middle')} Баланс: {balance:,} {replace_emojis('money')}\n{replace_emojis('sub_middle')} Ранг: {rank}\n{replace_emojis('sub_directory')} Инвентарь: {inventory_count}/{max_inventory}\n\n{replace_emojis('a_dot_smaller')} Для перехода выберите подкатегорию в меню",
        color=discord.Color.from_rgb(69, 69, 69)
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    view = discord.ui.View()
    view.add_item(ShopBackButton())
    view.add_item(RaritySelect(category))
    await interaction.response.edit_message(embed=embed, view=view)


class RaritySelect(discord.ui.Select):
    """Выпадающее меню выбора редкости товара."""

    def __init__(self, category: str):
        options = [
            discord.SelectOption(
                label=f"{replace_emojis('rare_basic')} Basic",
                value="basic",
                description="Базовые товары для всех"
            ),
            discord.SelectOption(
                label=f"{replace_emojis('rare_premium')} Premium",
                value="premium",
                description="Премиум товары для опытных"
            ),
            discord.SelectOption(
                label=f"{replace_emojis('rare_elite')} Elite",
                value="elite",
                description="Элитные товары для топов"
            ),
            discord.SelectOption(
                label=f"{replace_emojis('rare_special')} Special",
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
                replace_emojis("❌ Нет товаров в этой категории."),
                ephemeral=True
            )
            return

        # Сортировать по цене
        items.sort(key=lambda x: x.price)

        # Создать список товаров
        category_label = "Значки" if self.category == "icons" else "Теги"
        category_emoji = replace_emojis("✨") if self.category == "icons" else replace_emojis("🏷️")

        if self.category == "tags":
            items_list = "\n\n".join([
                f"{replace_emojis('🏷️')} -  {item.value}\n"
                f"└ {replace_emojis('💰')} **Цена:** {item.price} {replace_emojis('🪙')}"
                for item in items
            ])
        else:
            items_list = "\n\n".join([
                f"{item.value} **{item.name}**\n"
                f"└ {replace_emojis('💰')} **Цена:** {item.price} {replace_emojis('🪙')}"
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
        embed_color = discord.Color.from_rgb(69, 69, 69)

        # Маппинг icon эмодзи по значению
        icon_map = {
            "WW": "icon_w",
            "Лапка": "icon_paw",
            "Bluestacks": "icon_bluestacks",
            "Чашка чая": "icon_teacup",
            "Бантик": "icon_ribbon",
            "18+": "icon_18plus",
            "Сердечко": "icon_heart",
            "Галочка": "icon_v_badge",
            "Карты": "icon_cards",
            "Кошачьи ушки": "icon_cat_ears",
            "Голубое крыло": "icon_wing",
        }

        # Маппинг rare эмодзи по редкости
        rare_map = {
            CosmeticRarity.BASIC: "rare_basic",
            CosmeticRarity.PREMIUM: "rare_premium",
            CosmeticRarity.ELITE: "rare_elite",
            CosmeticRarity.SPECIAL: "rare_special",
        }

        rare_emoji = rare_map.get(rarity, "rare_basic")

        # Создать список товаров с icon эмодзи
        items_parts = []
        for i, item in enumerate(items):
            icon_emoji = icon_map.get(item.value, "")
            sub_emoji = "sub_middle" if i < len(items) - 1 else "sub_directory"
            items_parts.append(
                f"{replace_emojis(sub_emoji)} {replace_emojis(icon_emoji)} **{item.name}** • {item.price} {replace_emojis('money')} {replace_emojis(rare_emoji)}"
            )

        items_list = "\n".join(items_parts)

        # Создать embed
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} КАТАЛОГ | {category_label} — {rarity_value.capitalize()}",
            description=f"Выберите {category_label.lower()} из списка ниже для покупки:\n\n{replace_emojis('⚪')} **Доступные товары:**\n{items_list}\n\n{replace_emojis('⚪')} **Ваш профиль:**\n{replace_emojis('sub_middle')} Баланс: {balance:,} {replace_emojis('money')}\n{replace_emojis('sub_middle')} Ранг: {rank}\n{replace_emojis('sub_directory')} Инвентарь: {inventory_count}/{max_inventory}\n\n{replace_emojis('a_dot_smaller')} Выберите предмет в выпадающем меню для покупки",
            color=embed_color
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        view = ShopBackView()
        view.add_item(CosmeticSelect(items))
        await interaction.response.edit_message(embed=embed, view=view)


def get_item_emoji(category: str) -> str:
    """Получить эмодзи для категории товара."""
    emoji_map = {
        "icons": replace_emojis("✨"),
        "tags": replace_emojis("🏷️"),
    }
    return emoji_map.get(category, replace_emojis("🛒"))


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
                    description=f"Цена: {item.price} {replace_emojis('🪙')}",
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
            await interaction.response.send_message(replace_emojis("❌ Товар не найден."), ephemeral=True)
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
            await interaction.response.send_message(replace_emojis("❌ Кейс не найден."), ephemeral=True)
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
                    description=f"{role.price} {replace_emojis('🪙')}{level_req}",
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
            await interaction.response.send_message(replace_emojis("❌ Роль не найдена."), ephemeral=True)
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
    rare_map = {
        CosmeticRarity.BASIC: "rare_basic",
        CosmeticRarity.PREMIUM: "rare_premium",
        CosmeticRarity.ELITE: "rare_elite",
        CosmeticRarity.SPECIAL: "rare_special",
    }
    rarity_label = {
        CosmeticRarity.BASIC: "Basic",
        CosmeticRarity.PREMIUM: "Premium",
        CosmeticRarity.ELITE: "Elite",
        CosmeticRarity.SPECIAL: "Special",
    }

    rare_emoji = rare_map.get(item.rarity, "rare_basic")
    label = rarity_label.get(item.rarity, "Basic")

    # Цвет
    embed_color = discord.Color.from_rgb(69, 69, 69)

    # Категория
    category_label = "Значки" if item.category == "icons" else "Теги"

    # Icon emoji
    icon_map = {
        "WW": "icon_w",
        "Лапка": "icon_paw",
        "Bluestacks": "icon_bluestacks",
        "Чашка чая": "icon_teacup",
        "Бантик": "icon_ribbon",
        "18+": "icon_18plus",
        "Сердечко": "icon_heart",
        "Галочка": "icon_v_badge",
        "Карты": "icon_cards",
        "Кошачьи ушки": "icon_cat_ears",
        "Голубое крыло": "icon_wing",
    }
    icon_emoji = icon_map.get(item.value, "")

    # Создать embed
    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} ПОКУПКА ЭМОДЗИ | {item.name}",
        description=f"Вы действительно хотите приобрести данный предмет?\n\n{replace_emojis('⚪')} **Информация:**\n{replace_emojis('sub_middle')} {replace_emojis(icon_emoji)} Тип: {category_label} • {label} {replace_emojis(rare_emoji)}\n{replace_emojis('sub_directory')} Стоимость: {item.price} {replace_emojis('money')}\n\n{replace_emojis('⚪')} **Ваш профиль:**\n{replace_emojis('sub_middle')} Баланс: {balance:,} {replace_emojis('money')}\n{replace_emojis('sub_middle')} Ранг: {rank}\n{replace_emojis('sub_directory')} Инвентарь: {inventory_count}/{max_inventory}\n\n{replace_emojis('a_dot_smaller')} Подтвердите покупку кнопкой ниже",
        color=embed_color
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

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
    rare_map = {
        CosmeticRarity.BASIC: "rare_basic",
        CosmeticRarity.PREMIUM: "rare_premium",
        CosmeticRarity.ELITE: "rare_elite",
        CosmeticRarity.SPECIAL: "rare_special",
    }
    rarity_label = {
        CosmeticRarity.BASIC: "Basic",
        CosmeticRarity.PREMIUM: "Premium",
        CosmeticRarity.ELITE: "Elite",
        CosmeticRarity.SPECIAL: "Special",
    }

    rare_emoji = rare_map.get(item.rarity, "rare_basic")
    label = rarity_label.get(item.rarity, "Basic")

    # Цвет
    embed_color = discord.Color.from_rgb(69, 69, 69)

    # Создать embed
    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} ПОКУПКА ТЕГА | {item.name}",
        description=f"Вы действительно хотите приобрести данный тег?\n\n{replace_emojis('⚪')} **Информация:**\n{replace_emojis('sub_middle')} Категория: Теги • {label} {replace_emojis(rare_emoji)}\n{replace_emojis('sub_directory')} Стоимость: {item.price} {replace_emojis('money')}\n\n{replace_emojis('⚪')} **Предпросмотр:**\n{replace_emojis('sub_directory')} **{item.value}** {interaction.user.display_name}\n\n{replace_emojis('⚪')} **Ваш профиль:**\n{replace_emojis('sub_middle')} Баланс: {balance:,} {replace_emojis('money')}\n{replace_emojis('sub_middle')} Ранг: {rank}\n{replace_emojis('sub_directory')} Инвентарь: {inventory_count}/{max_inventory}\n\n{replace_emojis('a_dot_smaller')} Подтвердите покупку кнопкой ниже",
        color=embed_color
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

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
        title=f"{replace_emojis('👑')} ПОКУПКА РОЛИ | {item.name}",
        description="Вы действительно хотите приобрести эту роль?",
        color=discord.Color.gold()
    )

    # Информация о товаре
    embed.add_field(
        name=replace_emojis("📌 **Информация о товаре:**"),
        value=f"├ {replace_emojis('🏷️')} **Категория:** Discord Роли\n"
              f"├ {replace_emojis('📝')} **Описание:** {item.description}\n"
              f"├ {replace_emojis('⚡')} **Привилегии:** {item.description}\n"
              f"└ {replace_emojis('💰')} **Стоимость:** {item.price} {replace_emojis('🪙')}",
        inline=False
    )

    # Отображение в профиле
    embed.add_field(
        name=replace_emojis("🎨 **Отображение в профиле:**"),
        value=f"└ {replace_emojis('🏷️')} Роль: <@&{item.role_id}>",
        inline=False
    )

    # Профиль
    embed.add_field(
        name=replace_emojis("⚪ ВАШ ПРОФИЛЬ"),
        value=f"{replace_emojis('sub_middle')} Баланс: {balance:,} {replace_emojis('money')}\n"
              f"{replace_emojis('sub_middle')} Ранг: {rank}\n"
              f"{replace_emojis('sub_directory')} Мест в инвентаре: {inventory_count}/{max_inventory}",
        inline=False
    )

    embed.add_field(
        name=replace_emojis("💡 Подтвердите покупку кнопкой ниже"),
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

    # Определить эмодзи для кейса
    case_emoji = "case_basic"
    rare_emoji = "rare_basic"
    sub_emoji = "sub_middle"
    sparkle = ""
    if "Basic" in case.name:
        case_emoji = "case_basic"
        rare_emoji = "rare_basic"
        sub_emoji = "sub_middle"
    elif "Premium" in case.name:
        case_emoji = "case_premium"
        rare_emoji = "rare_premium"
        sub_emoji = "sub_middle"
    elif "Elite" in case.name:
        case_emoji = "case_elite"
        rare_emoji = "rare_elite"
        sub_emoji = "sub_middle"
    elif "Special" in case.name:
        case_emoji = "case_special"
        rare_emoji = "rare_special"
        sub_emoji = "sub_directory"
        sparkle = f" {replace_emojis('a_star')}"

    # Создать embed
    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} ПОКУПКА КЕЙСА | {case.name}",
        description=f"Вы действительно хотите открыть этот кейс?\n\n{replace_emojis('⚪')} **Информация:**\n{replace_emojis('sub_middle')} {replace_emojis(case_emoji)} Категория: Кейсы • {replace_emojis(rare_emoji)}{sparkle}\n{replace_emojis('sub_directory')} Стоимость: {case.price} {replace_emojis('money')}\n\n{replace_emojis('⚪')} **Шансы выпадения:**\n{replace_emojis('sub_middle')} Монеты: 50%\n{replace_emojis('sub_middle')} Предмет: 20%\n{replace_emojis('sub_directory')} Ничего: 30%\n\n{replace_emojis('⚪')} **Ваш профиль:**\n{replace_emojis('sub_middle')} Баланс: {balance:,} {replace_emojis('money')}\n{replace_emojis('sub_middle')} Ранг: {rank}\n{replace_emojis('sub_directory')} Инвентарь: {inventory_count}/{max_inventory}\n\n{replace_emojis('a_dot_smaller')} Подтвердите покупку и открытие кнопкой ниже",
        color=discord.Color.from_rgb(69, 69, 69)
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

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
        label_text = f"{replace_emojis('✅')} Купить за {price} {replace_emojis('🪙')}"
        if item_type == "tag":
            label_text = f"{replace_emojis('✅')} Примерить и купить за {price} {replace_emojis('🪙')}"
        elif item_type == "role":
            label_text = f"{replace_emojis('✅')} Купить роль за {price} {replace_emojis('🪙')}"
        
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
            await interaction.response.send_message(replace_emojis("❌ Товар не найден."), ephemeral=True)
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < self.price:
            await interaction.response.send_message(
                f"{replace_emojis('❌')} Недостаточно монет. Нужно: {self.price} {replace_emojis('🪙')}, у вас: {balance} {replace_emojis('🪙')}",
                ephemeral=True
            )
            return

        # Проверить требование уровня
        if item.required_level > 0:
            from storage.player_stats_store import player_stats_store
            stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
            if not stats or stats.level < item.required_level:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Требуется уровень {item.required_level} для покупки.",
                    ephemeral=True
                )
                return

        # Проверить есть ли уже
        inventory = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
        for cosmetic in inventory:
            if cosmetic.item_id == self.item_id:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} У вас уже есть этот товар!",
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
                embed = discord.Embed(
                    title=f"{replace_emojis('👑')} ПОКУПКА РОЛИ | {item.name}",
                    color=discord.Color.gold()
                )
                embed.add_field(
                    name=replace_emojis("📌 **ТОВАР:**"),
                    value=f"├ {replace_emojis('🏷️')} **Роль:** {item.name}\n"
                          f"├ {replace_emojis('⭐')} **Права:** {item.description}\n"
                          f"└ {replace_emojis('💰')} **Цена:** {self.price} {replace_emojis('🪙')}",
                    inline=False
                )
                embed.add_field(
                    name=replace_emojis("👤 **ПОКУПАТЕЛЬ:**"),
                    value=f"{interaction.user.mention} купил за {self.price} {replace_emojis('🪙')}",
                    inline=False
                )
                await interaction.response.send_message(embed=embed)
            else:
                await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, self.price)
                await interaction.response.send_message(
                    replace_emojis("❌ Не удалось назначить роль. Монеты возвращены."),
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

            # Определить тип для отображения
            rarity_map = {
                "basic": f"{replace_emojis('⭐')} Basic",
                "premium": f"{replace_emojis('💎')} Premium",
                "elite": f"{replace_emojis('👑')} Elite",
                "special": f"{replace_emojis('✨')} Special",
            }
            rarity_display = rarity_map.get(item.rarity.value, f"{replace_emojis('⭐')} Basic")

            if item.category == "tags":
                embed = discord.Embed(
                    title=f"{replace_emojis('🏷️')} ПОКУПКА ТЕГА | {item.name}",
                    color=discord.Color.purple()
                )
                embed.add_field(
                    name=replace_emojis("📌 **ТОВАР:**"),
                    value=f"├ {replace_emojis('🏷️')} **Префикс:** {item.value}\n"
                          f"├ {replace_emojis('⭐')} **Превью:** {item.value} {interaction.user.display_name}\n"
                          f"└ {replace_emojis('💰')} **Цена:** {self.price} {replace_emojis('🪙')}",
                    inline=False
                )
                embed.add_field(
                    name=replace_emojis("👤 **ПОКУПАТЕЛЬ:**"),
                    value=f"{interaction.user.mention} купил за {self.price} {replace_emojis('🪙')}",
                    inline=False
                )
            else:
                embed = discord.Embed(
                    title=f"{replace_emojis('✨')} ПОКУПКА ЗНАЧКА | {item.name}",
                    color=discord.Color.blue()
                )
                embed.add_field(
                    name=replace_emojis("📌 **ТОВАР:**"),
                    value=f"├ {replace_emojis('🏷️')} **Предмет:** {item.value}\n"
                          f"├ {replace_emojis('⭐')} **Редкость:** {rarity_display}\n"
                          f"└ {replace_emojis('💰')} **Цена:** {self.price} {replace_emojis('🪙')}",
                    inline=False
                )
                embed.add_field(
                    name=replace_emojis("👤 **ПОКУПАТЕЛЬ:**"),
                    value=f"{interaction.user.mention} купил за {self.price} {replace_emojis('🪙')}",
                    inline=False
                )

            await interaction.response.send_message(embed=embed)


class BuyCaseButton(discord.ui.Button):
    """Кнопка покупки кейса."""

    def __init__(self, case_id: str, price: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label=f"{replace_emojis('🎲')} Открыть кейс за {price} {replace_emojis('🪙')}",
            custom_id=f"buy_case_{case_id}"
        )
        self.case_id = case_id
        self.price = price

    async def callback(self, interaction: discord.Interaction) -> None:
        """Купить кейс."""
        from storage.case_store import case_store

        case = case_store.get_case(self.case_id)
        if not case:
            await interaction.response.send_message(replace_emojis("❌ Кейс не найден."), ephemeral=True)
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < self.price:
            await interaction.response.send_message(
                f"{replace_emojis('❌')} Недостаточно монет. Нужно: {self.price} {replace_emojis('🪙')}, у вас: {balance} {replace_emojis('🪙')}",
                ephemeral=True
            )
            return

        # Открыть кейс
        result = await case_store.open_case(interaction.guild_id, interaction.user.id, self.case_id, interaction.guild)

        if result:
            # Определить иконку и текст результата
            if result["type"] == "coins":
                status_icon = replace_emojis("💰")
                result_text = f"{result['value']} {replace_emojis('🪙')}"
                # Показать стандартный шанс монет из описания
                chance_text = "50%"
            elif result["type"] == "item":
                status_icon = replace_emojis("🎁")
                result_text = result['value']  # Now it's just the name string
                # Показать стандартный шанс предмета из описания
                chance_text = "20%"
            else:
                status_icon = replace_emojis("❌")
                result_text = "Ничего"
                # Показать стандартный шанс ничего из описания
                chance_text = "30%"

            embed = discord.Embed(
                title=f"{replace_emojis('📦')} ОТКРЫТИЕ КЕЙСА | {case.name}",
                color=discord.Color.orange()
            )
            embed.add_field(
                name=f"{status_icon} **НАГРАДА:**",
                value=f"├ {replace_emojis('🏷️')} **Выигрыш:** {result_text}\n"
                      f"└ {replace_emojis('⭐')} **С Шансом:** {chance_text}",
                inline=False
            )
            embed.add_field(
                name=replace_emojis("👤 **ОТКРЫЛ:**"),
                value=f"{interaction.user.mention} открыл за {self.price} {replace_emojis('🪙')}",
                inline=False
            )
            await interaction.response.send_message(embed=embed)
        else:
            await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, self.price)
            await interaction.response.send_message(
                replace_emojis("❌ Не удалось открыть кейс. Монеты возвращены."),
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
            label=replace_emojis("◀ Назад в главное меню"),
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
            title=replace_emojis("🛍️ МАГАЗИН СЕРВЕРА | Главное меню"),
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
            name=replace_emojis("⚪ ВАШ ПРОФИЛЬ"),
            value=f"{replace_emojis('sub_middle')} Баланс: {balance:,} {replace_emojis('money')}\n"
                  f"{replace_emojis('sub_middle')} Ранг: {rank}\n"
                  f"{replace_emojis('sub_directory')} Мест в инвентаре: {inventory_count}/{max_inventory}",
            inline=False
        )

        embed.add_field(
            name=replace_emojis("💡 Для навигации используйте компоненты ниже"),
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
            label=replace_emojis("◀ Назад к списку"),
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
            title=replace_emojis("🛍️ Магазин Сервера | Главный каталог"),
            description="Добро пожаловать в магазин!\nВыберите категорию ниже, чтобы посмотреть товары и улучшить свой профиль.",
            color=discord.Color.gold()
        )
        embed.add_field(name=replace_emojis("💳 Ваш баланс"), value=f"{balance} {replace_emojis('🪙')}", inline=False)

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
            title=f"{replace_emojis('🛒')} {category_label} - Выберите редкость",
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
                    description=f"Цена: {item.price} {replace_emojis('🪙')}",
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
            await interaction.response.send_message(replace_emojis("❌ Товар не найден."), ephemeral=True)
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < item.price:
            await interaction.response.send_message(
                f"{replace_emojis('❌')} Недостаточно монет. Нужно: {item.price} {replace_emojis('🪙')}, у вас: {balance} {replace_emojis('🪙')}",
                ephemeral=True
            )
            return

        # Проверить есть ли уже
        inventory = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
        for cosmetic in inventory:
            if cosmetic.item_id == item_id:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} У вас уже есть этот товар!",
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
            f"{replace_emojis('✅')} Вы купили **{item.name}** за {item.price} {replace_emojis('🪙')}!\n\n"
            f"Используйте `/inventory` для экипировки.",
            ephemeral=True
        )


class RarityBackButton(discord.ui.Button):
    """Кнопка возврата к выбору редкости (устаревшая)."""

    def __init__(self, category: str):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=replace_emojis("↩️ Назад"),
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
            title=f"{replace_emojis('🛒')} {category_label} - Выберите редкость",
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
                replace_emojis("❌ Товар не найден."),
                ephemeral=True
            )
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < item.price:
            await interaction.response.send_message(
                f"{replace_emojis('❌')} Недостаточно монет. Нужно: {item.price} {replace_emojis('🪙')}, у вас: {balance} {replace_emojis('🪙')}",
                ephemeral=True
            )
            return

        # Проверить есть ли уже
        inventory = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
        for cosmetic in inventory:
            if cosmetic.item_id == self.item_id:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} У вас уже есть этот товар!",
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
            f"{replace_emojis('✅')} Вы купили **{item.name}** за {item.price} {replace_emojis('🪙')}!\n\n"
            f"Используйте `/inventory` для экипировки.",
            ephemeral=True
        )


class InventoryEquipSelect(discord.ui.Select):
    """Select menu для экипировки предмета."""

    def __init__(self, items):
        options = []
        for item_id, item_name in items:
            options.append(
                discord.SelectOption(
                    label=f"Экипировать {item_name}",
                    value=item_id,
                    emoji="✅"
                )
            )

        super().__init__(
            placeholder="Выберите предмет для экипировки...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.items = items

    async def callback(self, interaction: discord.Interaction) -> None:
        """Экипировать предмет."""
        item_id = self.values[0]
        success = inventory_store.equip_cosmetic(interaction.guild_id, interaction.user.id, item_id)
        if success:
            item = shop_store.get_item(item_id)
            item_name = item.name if item else item_id
            await interaction.response.send_message(
                f"{replace_emojis('✅')} **{item_name}** экипирован!",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                replace_emojis("❌ Не удалось экипировать предмет."),
                ephemeral=True
            )


class InventoryUnequipSelect(discord.ui.Select):
    """Select menu для снятия предмета."""

    def __init__(self, items):
        options = []
        for item_id, item_name in items:
            options.append(
                discord.SelectOption(
                    label=f"Снять {item_name}",
                    value=item_id,
                    emoji="❌"
                )
            )

        super().__init__(
            placeholder="Выберите предмет для снятия...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.items = items

    async def callback(self, interaction: discord.Interaction) -> None:
        """Снять предмет."""
        item_id = self.values[0]
        success = inventory_store.unequip_cosmetic(interaction.guild_id, interaction.user.id, item_id)
        if success:
            item = shop_store.get_item(item_id)
            item_name = item.name if item else item_id
            await interaction.response.send_message(
                f"{replace_emojis('✅')} **{item_name}** снят.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                replace_emojis("❌ Не удалось снять предмет."),
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
                f"{replace_emojis('✅')} **{item_name}** экипирован!",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                replace_emojis("❌ Не удалось экипировать предмет."),
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
                f"{replace_emojis('✅')} **{item_name}** снят.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                replace_emojis("❌ Не удалось снять предмет."),
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
                replace_emojis("❌ Нет доступных ролей.")
            )
            return

        # Сортировать по цене (от дешёвого к дорогому)
        roles.sort(key=lambda x: x.price)

        # Создать embed с ролями
        embed = discord.Embed(
            title=replace_emojis("👑 Discord Роли"),
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

            label = f"{role.name} - {role.price} {replace_emojis('🪙')}{level_req}"
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
            await interaction.followup.send(replace_emojis("❌ Предмет не найден."))
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < item.price:
            await interaction.followup.send(
                f"{replace_emojis('❌')} Недостаточно монет. Нужно: {item.price} {replace_emojis('🪙')}, у вас: {balance} {replace_emojis('🪙')}"
            )
            return

        # Проверить требование уровня
        if item.required_level > 0:
            from storage.player_stats_store import player_stats_store
            stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
            if not stats or stats.level < item.required_level:
                await interaction.followup.send(
                    f"{replace_emojis('❌')} Требуется уровень {item.required_level} для покупки этой роли."
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
                f"{replace_emojis('✅')} Вы купили **{item.name}** за {item.price} {replace_emojis('🪙')}!"
            )
        else:
            # Возврат монет при ошибке
            await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, item.price)
            await interaction.followup.send(
                replace_emojis("❌ Не удалось назначить роль. Монеты возвращены.")
            )
