"""Конфигурация бота."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Корневая директория проекта
BASE_DIR = Path(__file__).parent

# Путь к JSON-файлу состояния (DATA_DIR для облачного volume)
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATA_FILE = DATA_DIR / "tournaments.json"

# Токен Discord-бота
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# ID владельца бота (для команды /reset)
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))

# PostgreSQL Database
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Redis Cache
REDIS_URL = os.getenv("REDIS_URL", "")

# Economy limits
MAX_BALANCE = 100000  # 100k монет максимум
MIN_BET = 10  # Минимальная ставка
MAX_BET = 1000  # Максимальная ставка (для обычных игр)

# Лимиты турнира
MAX_CAPTAINS = 4
MAX_PLAYERS_PER_CIRCLE = 4
CIRCLES = (2, 3, 4)

# Custom emoji IDs for ranks (get these from Discord server settings)
# Leave empty to use standard emojis: 👑 🔱 🎯 💎 🌪️ 🥇 🥈 🥉
RANK_EMOJIS = {
    "Radiant": os.getenv("EMOJI_RADIANT", ""),
    "Immortal": os.getenv("EMOJI_IMMORTAL", ""),
    "Ascendant": os.getenv("EMOJI_ASCENDANT", ""),
    "Diamond": os.getenv("EMOJI_DIAMOND", ""),
    "Platinum": os.getenv("EMOJI_PLATINUM", ""),
    "Gold": os.getenv("EMOJI_GOLD", ""),
    "Silver": os.getenv("EMOJI_SILVER", ""),
    "Bronze": os.getenv("EMOJI_BRONZE", ""),
}

# Custom emoji IDs for other game elements
# Leave empty to use standard emojis
GAME_EMOJIS = {
    # Games
    "game": os.getenv("EMOJI_GAME", ""),
    "dice": os.getenv("EMOJI_DICE", ""),
    "slots": os.getenv("EMOJI_SLOTS", ""),
    "roulette": os.getenv("EMOJI_ROULETTE", ""),
    "rps": os.getenv("EMOJI_RPS", ""),
    "brain": os.getenv("EMOJI_BRAIN", ""),
    
    # Tournaments
    "winner": os.getenv("EMOJI_WINNER", ""),
    "target": os.getenv("EMOJI_TARGET", ""),
    "sword": os.getenv("EMOJI_SWORD", ""),
    "gold_medal": os.getenv("EMOJI_GOLD_MEDAL", ""),
    "silver_medal": os.getenv("EMOJI_SILVER_MEDAL", ""),
    "bronze_medal": os.getenv("EMOJI_BRONZE_MEDAL", ""),
    
    # Stats
    "grow": os.getenv("EMOJI_GROW", ""),
    "kill_death": os.getenv("EMOJI_KILL_DEATH", ""),
    "star": os.getenv("EMOJI_STAR", ""),
    "notes": os.getenv("EMOJI_NOTES", ""),
    
    # System
    "clock": os.getenv("EMOJI_CLOCK", ""),
    "book": os.getenv("EMOJI_BOOK", ""),
    "light_bulb": os.getenv("EMOJI_LIGHT_BULB", ""),
    "flash": os.getenv("EMOJI_FLASH", ""),
    
    # Economy
    "money": os.getenv("EMOJI_MONEY", ""),
    "shop": os.getenv("EMOJI_SHOP", ""),
    "gift": os.getenv("EMOJI_GIFT", ""),
    
    # Interface
    "success": os.getenv("EMOJI_SUCCESS", ""),
    "error": os.getenv("EMOJI_ERROR", ""),
    "warning": os.getenv("EMOJI_WARNING", ""),
    "delete": os.getenv("EMOJI_DELETE", ""),
    "block": os.getenv("EMOJI_BLOCK", ""),
    "back": os.getenv("EMOJI_BACK", ""),
    "forward": os.getenv("EMOJI_FORWARD", ""),
    "refresh": os.getenv("EMOJI_REFRESH", ""),
    "dropdown": os.getenv("EMOJI_DROPDOWN", ""),
    "new": os.getenv("EMOJI_NEW", ""),
    "comment": os.getenv("EMOJI_COMMENT", ""),
    "team": os.getenv("EMOJI_TEAM", ""),
    "profile": os.getenv("EMOJI_PROFILE", ""),
    "pin": os.getenv("EMOJI_PIN", ""),
    "announce": os.getenv("EMOJI_ANNOUNCE", ""),
    "check": os.getenv("EMOJI_CHECK", ""),
    "cross": os.getenv("EMOJI_CROSS", ""),

    # Emotions
    "fire": os.getenv("EMOJI_FIRE", ""),
    "ice": os.getenv("EMOJI_ICE", ""),
    
    # Shop & UI
    "tag": os.getenv("EMOJI_TAG", ""),
    "settings": os.getenv("EMOJI_SETTINGS", ""),
    "eye": os.getenv("EMOJI_EYE", ""),

    # Numbers
    "num_1": os.getenv("EMOJI_NUM_1", ""),
    "num_2": os.getenv("EMOJI_NUM_2", ""),
    "num_3": os.getenv("EMOJI_NUM_3", ""),
    "num_4": os.getenv("EMOJI_NUM_4", ""),
    "num_5": os.getenv("EMOJI_NUM_5", ""),
    "num_6": os.getenv("EMOJI_NUM_6", ""),
    "num_7": os.getenv("EMOJI_NUM_7", ""),
    "num_8": os.getenv("EMOJI_NUM_8", ""),

    # Special symbols
    "white_dot": os.getenv("EMOJI_WHITE_DOT", ""),
    "sub_directory": os.getenv("EMOJI_SUB_DIRECTORY", ""),
    "sub_middle": os.getenv("EMOJI_SUB_MIDDLE", ""),
    "room": os.getenv("EMOJI_ROOM", ""),
    "white_arrow": os.getenv("EMOJI_WHITE_ARROW", ""),
    "a_sparkle": os.getenv("EMOJI_A_SPARKLE", ""),
    "a_star": os.getenv("EMOJI_A_STAR", ""),
    "a_triple_dots": os.getenv("EMOJI_A_TRIPLE_DOTS", ""),
    "a_dot_smaller": os.getenv("EMOJI_A_DOT_SMALLER", ""),

    # Case emojis
    "case_basic": os.getenv("EMOJI_CASE_BASIC", ""),
    "case_premium": os.getenv("EMOJI_CASE_PREMIUM", ""),
    "case_elite": os.getenv("EMOJI_CASE_ELITE", ""),
    "case_special": os.getenv("EMOJI_CASE_SPECIAL", ""),

    # Rare emojis
    "rare_basic": os.getenv("EMOJI_RARE_BASIC", ""),
    "rare_premium": os.getenv("EMOJI_RARE_PREMIUM", ""),
    "rare_elite": os.getenv("EMOJI_RARE_ELITE", ""),
    "rare_special": os.getenv("EMOJI_RARE_SPECIAL", ""),

    # Icon emojis
    "icon_w": os.getenv("EMOJI_ICON_W", ""),
    "icon_paw": os.getenv("EMOJI_ICON_PAW", ""),
    "icon_bluestacks": os.getenv("EMOJI_ICON_BLUESTACKS", ""),
    "icon_teacup": os.getenv("EMOJI_ICON_TEACUP", ""),
    "icon_ribbon": os.getenv("EMOJI_ICON_RIBBON", ""),
    "icon_18plus": os.getenv("EMOJI_ICON_18PLUS", ""),
    "icon_heart": os.getenv("EMOJI_ICON_HEART", ""),
    "icon_v_badge": os.getenv("EMOJI_ICON_V_BADGE", ""),
    "icon_cards": os.getenv("EMOJI_ICON_CARDS", ""),
    "icon_cat_ears": os.getenv("EMOJI_ICON_CAT_EARS", ""),
    "icon_wing": os.getenv("EMOJI_ICON_WING", ""),
}

# Standard emojis as fallback when custom emojis are not set
STANDARD_EMOJIS = {
    # Ranks
    "Radiant": "👑",
    "Immortal": "🔱",
    "Ascendant": "🎯",
    "Diamond": "💎",
    "Platinum": "🌪️",
    "Gold": "🥇",
    "Silver": "🥈",
    "Bronze": "🥉",
    
    # Games
    "game": "🎮",
    "dice": "🎲",
    "slots": "🎰",
    "roulette": "🎰",
    "rps": "🎲",
    
    # Tournaments
    "winner": "🏆",
    "target": "🎯",
    "sword": "⚔️",
    
    # Stats
    "grow": "📈",
    "kill_death": "📊",
    "star": "⭐",
    "notes": "📝",
    
    # System
    "clock": "⏰",
    "book": "📖",
    "light_bulb": "💡",
    "flash": "⚡",
    
    # Economy
    "money": "💰",
    "shop": "🛍️",
    "gift": "🎁",
    
    # Interface
    "success": "✅",
    "error": "❌",
    "warning": "⚠️",
    "delete": "🗑️",
    "block": "🚫",
    "back": "🔙",
    "forward": "➡️",
    "refresh": "🔄",
    "dropdown": "🔽",
    "new": "🆕",
    "comment": "💬",
    "team": "👥",
    "profile": "👤",
    "pin": "📌",
    "announce": "📢",
    "check": "✅",
    "cross": "❌",
    
    # Emotions
    "fire": "🔥",
    "ice": "❄️",

    # Numbers
    "num_1": "1️⃣",
    "num_2": "2️⃣",
    "num_3": "3️⃣",
    "num_4": "4️⃣",
    "num_5": "5️⃣",
    "num_6": "6️⃣",
    "num_7": "7️⃣",
    "num_8": "8️⃣",

    # Special
    "white_dot": "",
    "sub_directory": "",
    "sub_middle": "",
    "room": "",
    "white_arrow": "",
    "a_sparkle": "",
    "a_star": "",
    "a_triple_dots": "",
    "a_dot_smaller": "",

    # Case emojis
    "case_basic": "",
    "case_premium": "",
    "case_elite": "",
    "case_special": "",

    # Rare emojis
    "rare_basic": "",
    "rare_premium": "",
    "rare_elite": "",
    "rare_special": "",

    # Icon emojis
    "icon_w": "",
    "icon_paw": "",
    "icon_bluestacks": "",
    "icon_teacup": "",
    "icon_ribbon": "",
    "icon_18plus": "",
    "icon_heart": "",
    "icon_v_badge": "",
    "icon_cards": "",
    "icon_cat_ears": "",
    "icon_wing": "",
}

def get_emoji(emoji_name: str) -> str:
    """Get custom emoji with ID or fallback to standard emoji."""
    import logging
    logger = logging.getLogger(__name__)
    
    # Check rank emojis
    if emoji_name in RANK_EMOJIS:
        custom_id = RANK_EMOJIS[emoji_name]
        logger.info(f"get_emoji: {emoji_name} -> custom_id={custom_id}")
        if custom_id:
            return f"<:{emoji_name}:{custom_id}>"
        result = STANDARD_EMOJIS.get(emoji_name, emoji_name)
        logger.info(f"get_emoji: {emoji_name} -> fallback to {result}")
        return result
    
    # Check game emojis
    if emoji_name in GAME_EMOJIS:
        custom_id = GAME_EMOJIS[emoji_name]
        logger.info(f"get_emoji: {emoji_name} -> custom_id={custom_id}")
        if custom_id:
            return f"<:{emoji_name}:{custom_id}>"
        result = STANDARD_EMOJIS.get(emoji_name, emoji_name)
        logger.info(f"get_emoji: {emoji_name} -> fallback to {result}")
        return result
    
    # Fallback to standard emoji
    result = STANDARD_EMOJIS.get(emoji_name, emoji_name)
    logger.info(f"get_emoji: {emoji_name} -> final fallback to {result}")
    return result


def replace_emojis(text: str) -> str:
    """Replace all standard emojis in text with custom emojis if IDs are available."""
    if not text:
        return text

    # Emoji mapping: standard emoji -> custom emoji name
    emoji_map = {
        # Profile & User
        "👤": "profile",
        "👛": "money",

        # Economy
        "💰": "money",
        "🪙": "money",
        "💵": "money",
        "💳": "profile",
        "🛍️": "shop",
        "🎁": "gift",

        # Status
        "✅": "check",
        "❌": "cross",
        "⚠️": "warning",
        "🗑️": "delete",
        "🚫": "block",

        # Navigation
        "🔄": "refresh",
        "🔙": "back",
        "➡️": "forward",
        "🔽": "dropdown",
        "🆕": "new",

        # Communication
        "💬": "comment",
        "👥": "team",
        "📌": "pin",
        "📢": "announce",

        # Emotions
        "🔥": "fire",
        "❄️": "ice",

        # Tournaments
        "🏆": "winner",
        "🥇": "gold_medal",
        "🥈": "silver_medal",
        "🥉": "bronze_medal",
        "🎯": "target",
        "⚔️": "sword",

        # Stats
        "📊": "kill_death",
        "📈": "grow",
        "⭐": "star",
        "📝": "notes",

        # System
        "⏰": "clock",
        "📖": "book",
        "💡": "light_bulb",
        "⚡": "flash",

        # Games
        "🎮": "game",
        "🎲": "dice",
        "🎰": "slots",
        "🧠": "brain",

        # Shop & UI
        "✨": "star",
        "🏷️": "tag",
        "👑": "winner",
        "📦": "gift",
        "⚙️": "settings",
        "👁️": "eye",

        # Numbers
        "1️⃣": "num_1",
        "2️⃣": "num_2",
        "3️⃣": "num_3",
        "4️⃣": "num_4",
        "5️⃣": "num_5",
        "6️⃣": "num_6",
        "7️⃣": "num_7",
        "8️⃣": "num_8",

        # Special
        "•": "white_dot",
        "└": "sub_directory",
        "🏠": "room",

        # Additional symbols for replacement
        "⚪": "white_dot",
        "➡️": "white_arrow",
        "✨": "a_sparkle",
        "⭐": "a_star",
        "⋯": "a_triple_dots",

        # Case emojis
        "📦": "case_basic",
        "💎": "case_premium",
        "👑": "case_elite",

        # Rare emojis
        "💫": "rare_premium",
        "🌟": "rare_elite",
    }

    # Direct emoji name mapping for custom emoji names passed directly
    direct_emoji_names = [
        "money", "white_dot", "sub_directory", "sub_middle", "room", "white_arrow",
        "a_sparkle", "a_star", "a_triple_dots", "a_dot_smaller",
        "case_basic", "case_premium", "case_elite", "case_special",
        "rare_basic", "rare_premium", "rare_elite", "rare_special",
        "tag",
        "icon_w", "icon_paw", "icon_bluestacks", "icon_teacup", "icon_ribbon",
        "icon_18plus", "icon_heart", "icon_v_badge", "icon_cards", "icon_cat_ears", "icon_wing",
        "check", "cross",
    ]

    result = text

    # First, replace direct emoji names
    for emoji_name in direct_emoji_names:
        if emoji_name in result:
            custom_emoji = get_emoji(emoji_name)
            result = result.replace(emoji_name, custom_emoji)

    # Then, replace standard emojis
    for standard, custom_name in emoji_map.items():
        if standard in result:
            custom_emoji = get_emoji(custom_name)
            # Only replace if custom emoji is different from standard
            if custom_emoji != standard:
                result = result.replace(standard, custom_emoji)

    return result
