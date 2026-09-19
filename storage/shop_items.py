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
            description="",
            price=1000,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.BASIC,
            value="🎮",
            category="icons"
        ),
        ShopItem(
            id="icon_kiss",
            name="Поцелуй",
            description="",
            price=1000,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.BASIC,
            value="💋",
            category="icons"
        ),
        ShopItem(
            id="icon_star",
            name="Звезда",
            description="",
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
            description="",
            price=2500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.PREMIUM,
            value="🔥",
            category="icons"
        ),
        ShopItem(
            id="icon_wine",
            name="Вино",
            description="",
            price=2500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.PREMIUM,
            value="🍷",
            category="icons"
        ),
        ShopItem(
            id="icon_swords",
            name="Мечи",
            description="",
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
            description="",
            price=5000,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.ELITE,
            value="👑",
            category="icons"
        ),
        ShopItem(
            id="icon_diamond",
            name="Бриллиант",
            description="",
            price=5000,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.ELITE,
            value="💎",
            category="icons"
        ),
        ShopItem(
            id="icon_chains",
            name="Цепи",
            description="",
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
            description="",
            price=8500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.SPECIAL,
            value="🥀",
            category="icons"
        ),
        ShopItem(
            id="icon_dragon",
            name="Дракон",
            description="",
            price=8500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.SPECIAL,
            value="🐉",
            category="icons"
        ),
        ShopItem(
            id="icon_wings",
            name="Крылья",
            description="",
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
            description="",
            price=800,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.BASIC,
            value="[PRO]",
            category="tags"
        ),
        ShopItem(
            id="tag_sweet",
            name="[SWEET]",
            description="",
            price=800,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.BASIC,
            value="[SWEET]",
            category="tags"
        ),
        ShopItem(
            id="tag_vip",
            name="[VIP]",
            description="",
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
            description="",
            price=2000,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[MVP]",
            category="tags"
        ),
        ShopItem(
            id="tag_king",
            name="[KING]",
            description="",
            price=2000,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[KING]",
            category="tags"
        ),
        ShopItem(
            id="tag_boss",
            name="[BOSS]",
            description="",
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
            description="",
            price=4500,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.ELITE,
            value="[GOD]",
            category="tags"
        ),
        ShopItem(
            id="tag_sex",
            name="[SEX]",
            description="",
            price=4500,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.ELITE,
            value="[SEX]",
            category="tags"
        ),
        ShopItem(
            id="tag_legend",
            name="[LEGEND]",
            description="",
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
            description="",
            price=7500,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.SPECIAL,
            value="[404]",
            category="tags"
        ),
        ShopItem(
            id="tag_xxx",
            name="[XXX]",
            description="",
            price=7500,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.SPECIAL,
            value="[XXX]",
            category="tags"
        ),
        ShopItem(
            id="tag_ego",
            name="[EGO]",
            description="",
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
