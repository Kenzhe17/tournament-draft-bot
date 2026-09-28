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
    
    # Tournaments
    "winner": os.getenv("EMOJI_WINNER", ""),
    "target": os.getenv("EMOJI_TARGET", ""),
    "sword": os.getenv("EMOJI_SWORD", ""),
    
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
    
    # Emotions
    "fire": os.getenv("EMOJI_FIRE", ""),
    "ice": os.getenv("EMOJI_ICE", ""),
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
    
    # Emotions
    "fire": "🔥",
    "ice": "❄️",
}

def get_emoji(emoji_name: str) -> str:
    """Get custom emoji or fallback to standard emoji."""
    # Check rank emojis
    if emoji_name in RANK_EMOJIS:
        custom_id = RANK_EMOJIS[emoji_name]
        if custom_id:
            return f"<:{emoji_name}:{custom_id}>"
        return STANDARD_EMOJIS.get(emoji_name, emoji_name)
    
    # Check game emojis
    if emoji_name in GAME_EMOJIS:
        custom_id = GAME_EMOJIS[emoji_name]
        if custom_id:
            return f"<:{emoji_name}:{custom_id}>"
        return STANDARD_EMOJIS.get(emoji_name, emoji_name)
    
    # Fallback to standard
    return STANDARD_EMOJIS.get(emoji_name, emoji_name)
