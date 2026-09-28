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
            id="icon_letter",
            name="WW",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.BASIC,
            value="🎮",
            category="icons"
        ),
        ShopItem(
            id="icon_paw",
            name="Лапка",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.BASIC,
            value="🐾",
            category="icons"
        ),
        ShopItem(
            id="icon_bluestacks",
            name="Bluestacks",
            description="",
            price=700,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.BASIC,
            value="🔵",
            category="icons"
        ),
        # Elite (3500 монет)
        ShopItem(
            id="icon_teacup",
            name="Чашка чая",
            description="",
            price=3500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.ELITE,
            value="☕",
            category="icons"
        ),
        ShopItem(
            id="icon_ribbon",
            name="Бантик",
            description="",
            price=3500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.ELITE,
            value="🎀",
            category="icons"
        ),
        ShopItem(
            id="icon_18plus",
            name="18+",
            description="",
            price=3500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.ELITE,
            value="🔞",
            category="icons"
        ),
        ShopItem(
            id="icon_heart",
            name="Сердечко",
            description="",
            price=3500,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.ELITE,
            value="❤️",
            category="icons"
        ),
        # Premium (1750 монет)
        ShopItem(
            id="icon_v_badge",
            name="Галочка",
            description="",
            price=1750,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.PREMIUM,
            value="✅",
            category="icons"
        ),
        ShopItem(
            id="icon_cards",
            name="Карты",
            description="",
            price=1750,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.PREMIUM,
            value="🃏",
            category="icons"
        ),
        ShopItem(
            id="icon_cat_ears",
            name="Кошачьи ушки",
            description="",
            price=1750,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.PREMIUM,
            value="🐱",
            category="icons"
        ),
        # Special (5950 монет)
        ShopItem(
            id="icon_wing",
            name="Голубое крыло",
            description="",
            price=5950,
            cosmetic_type=CosmeticType.ICON,
            rarity=CosmeticRarity.SPECIAL,
            value="🪽",
            category="icons"
        ),
    ]

    # Текстовые теги и титулы (Titles & Badges)
    tags = [
        # (400 монет)
        ShopItem(
            id="tag_pro",
            name="[PRO]",
            description="",
            price=700,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.BASIC,
            value="[PRO]",
            category="tags"
        ),
        ShopItem(
            id="tag_sweet",
            name="[SWEET]",
            description="",
            price=700,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.BASIC,
            value="[SWEET]",
            category="tags"
        ),
        ShopItem(
            id="tag_vip",
            name="[VIP]",
            description="",
            price=700,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.BASIC,
            value="[VIP]",
            category="tags"
        ),
        # (1 000 монет)
        ShopItem(
            id="tag_mvp",
            name="[MVP]",
            description="",
            price=1750,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[MVP]",
            category="tags"
        ),
        ShopItem(
            id="tag_king",
            name="[KING]",
            description="",
            price=1750,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[KING]",
            category="tags"
        ),
        ShopItem(
            id="tag_boss",
            name="[BOSS]",
            description="",
            price=1750,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[BOSS]",
            category="tags"
        ),
        ShopItem(
            id="tag_love",
            name="[LOVE]",
            description="",
            price=1750,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[LOVE]",
            category="tags"
        ),
        ShopItem(
            id="tag_cry",
            name="[CRY]",
            description="",
            price=1750,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[CRY]",
            category="tags"
        ),
        ShopItem(
            id="tag_hate",
            name="[HATE]",
            description="",
            price=1750,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[HATE]",
            category="tags"
        ),
        ShopItem(
            id="tag_wow",
            name="[WOW]",
            description="",
            price=1750,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[WOW]",
            category="tags"
        ),
        ShopItem(
            id="tag_oof",
            name="[OOF]",
            description="",
            price=1750,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[OOF]",
            category="tags"
        ),
        ShopItem(
            id="tag_damn",
            name="[DAMN]",
            description="",
            price=1750,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.PREMIUM,
            value="[DAMN]",
            category="tags"
        ),
        # (2 250 монет)
        ShopItem(
            id="tag_god",
            name="[GOD]",
            description="",
            price=3500,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.ELITE,
            value="[GOD]",
            category="tags"
        ),
        ShopItem(
            id="tag_sex",
            name="[SEX]",
            description="",
            price=3500,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.ELITE,
            value="[SEX]",
            category="tags"
        ),
        ShopItem(
            id="tag_legend",
            name="[LEGEND]",
            description="",
            price=3500,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.ELITE,
            value="[LEGEND]",
            category="tags"
        ),
        # (3 750 монет)
        ShopItem(
            id="tag_404",
            name="[404]",
            description="",
            price=5950,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.SPECIAL,
            value="[404]",
            category="tags"
        ),
        ShopItem(
            id="tag_xxx",
            name="[XXX]",
            description="",
            price=5950,
            cosmetic_type=CosmeticType.TAG,
            rarity=CosmeticRarity.SPECIAL,
            value="[XXX]",
            category="tags"
        ),
        ShopItem(
            id="tag_ego",
            name="[EGO]",
            description="",
            price=5950,
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
