"""Инициализация товаров магазина."""

from models.shop_item import ShopItem, CosmeticType, CosmeticRarity
from storage.shop_store import shop_store


def initialize_shop_items() -> None:
    """Инициализировать товары в магазине."""

    # Цвета удалены (Discord не поддерживает цветной текст в embed'ах)

    # Графические значки (Icons & Emblems)
    icons = [
        # (1 000 монет)
        ShopItem(
            id="icon_gamepad",
            name="Геймпад",
            description="🎮 Иконка геймпада",
            price=1000,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.BASIC,
            value="🎮",
            category="icons"
        ),
        ShopItem(
            id="icon_kiss",
            name="Поцелуй",
            description="💋 Иконка поцелуя",
            price=1000,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.BASIC,
            value="💋",
            category="icons"
        ),
        ShopItem(
            id="icon_star",
            name="Звезда",
            description="⭐ Иконка звезды",
            price=1000,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.BASIC,
            value="⭐",
            category="icons"
        ),
        # (2 500 монет)
        ShopItem(
            id="icon_fire",
            name="Огонь",
            description="🔥 Иконка огня",
            price=2500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.PREMIUM,
            value="🔥",
            category="icons"
        ),
        ShopItem(
            id="icon_wine",
            name="Вино",
            description="🍷 Иконка вина",
            price=2500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.PREMIUM,
            value="🍷",
            category="icons"
        ),
        ShopItem(
            id="icon_swords",
            name="Мечи",
            description="⚔️ Иконка мечей",
            price=2500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.PREMIUM,
            value="⚔️",
            category="icons"
        ),
        # (5 000 монет)
        ShopItem(
            id="icon_crown",
            name="Корона",
            description="👑 Иконка короны",
            price=5000,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.ELITE,
            value="👑",
            category="icons"
        ),
        ShopItem(
            id="icon_diamond",
            name="Бриллиант",
            description="💎 Иконка бриллианта",
            price=5000,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.ELITE,
            value="💎",
            category="icons"
        ),
        ShopItem(
            id="icon_chains",
            name="Цепи",
            description="⛓️ Иконка цепей",
            price=5000,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.ELITE,
            value="⛓️",
            category="icons"
        ),
        # (8 500 монет)
        ShopItem(
            id="icon_rose",
            name="Роза",
            description="🥀 Иконка розы",
            price=8500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.SPECIAL,
            value="🥀",
            category="icons"
        ),
        ShopItem(
            id="icon_dragon",
            name="Дракон",
            description="🐉 Иконка дракона",
            price=8500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.SPECIAL,
            value="🐉",
            category="icons"
        ),
        ShopItem(
            id="icon_wings",
            name="Крылья",
            description="🪽 Иконка крыльев",
            price=8500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.SPECIAL,
            value="🪽",
            category="icons"
        ),
    ]

    # Текстовые теги и титулы (Titles & Badges)
    tags = [
        # (800 монет)
        ShopItem(
            id="tag_pro",
            name="[PRO]",
            description="Тег PRO для профессионалов",
            price=800,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.BASIC,
            value="[PRO]",
            category="tags"
        ),
        ShopItem(
            id="tag_sweet",
            name="[SWEET]",
            description="Тег SWEET для милых",
            price=800,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.BASIC,
            value="[SWEET]",
            category="tags"
        ),
        ShopItem(
            id="tag_vip",
            name="[VIP]",
            description="Тег VIP для важных",
            price=800,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.BASIC,
            value="[VIP]",
            category="tags"
        ),
        # (2 000 монет)
        ShopItem(
            id="tag_mvp",
            name="[MVP]",
            description="Тег MVP для лучших",
            price=2000,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[MVP]",
            category="tags"
        ),
        ShopItem(
            id="tag_king",
            name="[KING]",
            description="Тег KING для королей",
            price=2000,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[KING]",
            category="tags"
        ),
        ShopItem(
            id="tag_boss",
            name="[BOSS]",
            description="Тег BOSS для боссов",
            price=2000,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[BOSS]",
            category="tags"
        ),
        # (4 500 монет)
        ShopItem(
            id="tag_god",
            name="[GOD]",
            description="Тег GOD для богов",
            price=4500,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.ELITE,
            value="[GOD]",
            category="tags"
        ),
        ShopItem(
            id="tag_sex",
            name="[SEX]",
            description="Тег SEX для крутых",
            price=4500,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.ELITE,
            value="[SEX]",
            category="tags"
        ),
        ShopItem(
            id="tag_legend",
            name="[LEGEND]",
            description="Тег LEGEND для легенд",
            price=4500,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.ELITE,
            value="[LEGEND]",
            category="tags"
        ),
        # (7 500 монет)
        ShopItem(
            id="tag_404",
            name="[404]",
            description="Тег 404 для хакеров",
            price=7500,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.SPECIAL,
            value="[404]",
            category="tags"
        ),
        ShopItem(
            id="tag_xxx",
            name="[XXX]",
            description="Тег XXX для взрослых",
            price=7500,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.SPECIAL,
            value="[XXX]",
            category="tags"
        ),
        ShopItem(
            id="tag_ego",
            name="[EGO]",
            description="Тег EGO для уверенных",
            price=7500,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.SPECIAL,
            value="[EGO]",
            category="tags"
        ),
    ]

    # Добавить все товары в магазин
    for item in icons + tags:
        shop_store.add_item(item)


# Инициализировать товары при импорте
initialize_shop_items()
