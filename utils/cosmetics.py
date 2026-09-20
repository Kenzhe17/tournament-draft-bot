"""Утилиты для форматирования имен с косметикой."""

from storage.shop_store import inventory_store, shop_store


def format_player_name(guild_id: int, user_id: int, base_name: str) -> str:
    """Форматировать имя игрока с учётом косметики.

    Формат: **[TAG]** Name ICON (жирный тег)
    """
    # Получить экипированную косметику
    cosmetics = inventory_store.get_equipped_cosmetics(guild_id, user_id)

    if not cosmetics:
        return base_name

    # Сгруппировать по типам
    tag = ""
    icon = ""

    for cosmetic in cosmetics:
        item = shop_store.get_item(cosmetic.item_id)
        if not item:
            continue

        if item.cosmetic_type.value == "tag":
            tag = item.value
        elif item.cosmetic_type.value == "icon":
            icon = item.value

    # Форматировать: **[TAG]** Name ICON
    formatted = base_name
    if tag:
        formatted = f"**{tag}** {formatted}"
    if icon:
        formatted = f"{formatted} {icon}"

    return formatted


def format_player_name_async(guild_id: int, user_id: int, base_name: str) -> str:
    """Асинхронная версия форматирования имени (для совместимости)."""
    return format_player_name(guild_id, user_id, base_name)
