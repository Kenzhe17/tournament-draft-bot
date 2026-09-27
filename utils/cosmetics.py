"""Утилиты для форматирования имен с косметикой."""

import re
from storage.shop_store import inventory_store, shop_store


def clean_nickname(text: str) -> str:
    """Очистить никнейм от Unicode эмодзи и кастомных эмодзи Discord.
    
    Args:
        text: Исходный текст
        
    Returns:
        Текст без эмодзи
    """
    # Unicode emoji ranges
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"  # supplemental symbols and pictographs
        "\U0001FA00-\U0001FA6F"  # chess symbols
        "\U0001FA70-\U0001FAFF"  # symbols and pictographs extended-A
        "\U00002600-\U000027BF"  # miscellaneous symbols
        "\U0000FE00-\U0000FE0F"  # variation selectors
        "\U0001F000-\U0001F0FF"  # mahjong tiles
        "\U0001F0A0-\U0001F0FF"  # playing cards
        "]+",
        flags=re.UNICODE
    )
    
    # Discord custom emoji pattern: <:name:id> or <a:name:id>
    custom_emoji_pattern = re.compile(r'<a?:\w+:\d+>')
    
    # Remove both types of emojis
    text = emoji_pattern.sub('', text)
    text = custom_emoji_pattern.sub('', text)
    
    # Remove extra whitespace
    text = ' '.join(text.split())
    
    return text


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
