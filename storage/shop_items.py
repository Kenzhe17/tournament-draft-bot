"""Инициализация товаров магазина."""
from config import replace_emojis


from models.shop_item import ShopItem, CosmeticType, CosmeticRarity
from storage.shop_store import shop_store


def initialize_shop_items() -> None:
    """Инициализировать товары в магазине."""

    # Очистить существующие товары перед инициализацией
    shop_store.clear_all()

    # Цвета удалены (Discord не поддерживает цветной текст в embed'ах)

    # Графические значки (Icons & Emblems)
    icons = [
        # Basic (700 монет)
        ShopItem(
            id="icon_w",
            name="WW",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.BASIC,
            value="WW",
            category="icons"
        ),
        ShopItem(
            id="icon_paw",
            name="Лапка",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.BASIC,
            value="Лапка",
            category="icons"
        ),
        ShopItem(
            id="icon_bluestacks",
            name="Bluestacks",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.BASIC,
            value="Bluestacks",
            category="icons"
        ),
        # Elite (700 монет)
        ShopItem(
            id="icon_teacup",
            name="Чашка чая",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.ELITE,
            value="Чашка чая",
            category="icons"
        ),
        ShopItem(
            id="icon_ribbon",
            name="Бантик",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.ELITE,
            value="Бантик",
            category="icons"
        ),
        ShopItem(
            id="icon_18plus",
            name="18+",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.ELITE,
            value="18+",
            category="icons"
        ),
        ShopItem(
            id="icon_heart",
            name="Сердечко",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.ELITE,
            value="Сердечко",
            category="icons"
        ),
        # Premium (700 монет)
        ShopItem(
            id="icon_v_badge",
            name="Галочка",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.PREMIUM,
            value="Галочка",
            category="icons"
        ),
        ShopItem(
            id="icon_cards",
            name="Карты",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.PREMIUM,
            value="Карты",
            category="icons"
        ),
        ShopItem(
            id="icon_cat_ears",
            name="Кошачьи ушки",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.PREMIUM,
            value="Кошачьи ушки",
            category="icons"
        ),
        # Special (700 монет)
        ShopItem(
            id="icon_wing",
            name="Голубое крыло",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.SPECIAL,
            value="Голубое крыло",
            category="icons"
        ),
    ]

    # Текстовые теги и титулы (Titles & Badges)
    tags = []

    # Добавить все товары в магазин
    for item in icons + tags:
        shop_store.add_item(item)


# Инициализировать товары при импорте
initialize_shop_items()
