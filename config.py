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

# Дополнительные пользовательские ID с правами администратора/организатора
# и доступа к GIF-модерации (env EXTRA_ADMIN_USER_IDS, csv).
# Продакшен: "1032544122600423427,582145013358657549,575937439147753472"
EXTRA_ADMIN_USER_IDS = {
    int(x) for x in os.getenv("EXTRA_ADMIN_USER_IDS", "").split(",")
    if x.strip().isdigit()
}
ADMIN_USER_IDS = ({BOT_OWNER_ID} if BOT_OWNER_ID else set()) | EXTRA_ADMIN_USER_IDS

# Альянсированные гильдии: если задано — бот обрабатывает события
# только этих серверов и покидает чужие при вступлении.
# Пусто = все гильдии (поведение production по умолчанию).
ALLOWED_GUILD_IDS = {
    int(x) for x in os.getenv("ALLOWED_GUILD_IDS", "").split(",")
    if x.strip().isdigit()
}


def guild_allowed(guild_id: int | None) -> bool:
    """True — гильдия разрешена для обработки событий этим ботом."""
    if not ALLOWED_GUILD_IDS:
        return True
    return guild_id in ALLOWED_GUILD_IDS

# PostgreSQL Database
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Redis Cache
REDIS_URL = os.getenv("REDIS_URL", "")

# Organizer role ID
ORG_ROLE_ID = int(os.getenv("ORG_ROLE_ID", "0"))

# Manager role ID (for channel organizer management)
MANAGER_ROLE_ID = int(os.getenv("MANAGER_ROLE_ID", "0"))

# Tournament organizer roles (one per channel)
ORG_1_ROLE_ID = int(os.getenv("ORG_1_ROLE_ID", "0"))
ORG_2_ROLE_ID = int(os.getenv("ORG_2_ROLE_ID", "0"))
ORG_3_ROLE_ID = int(os.getenv("ORG_3_ROLE_ID", "0"))


def _parse_channel_role_map(raw: str) -> dict[int, int]:
    """'cid:rid,cid:rid,...' → {channel_id: role_id}."""
    out: dict[int, int] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        c, _, r = part.partition(":")
        try:
            out[int(c.strip())] = int(r.strip())
        except ValueError:
            continue
    return out


# Соответствие канал организаторов → роль (env ORG_ROLE_MAP, csv "cid:rid").
# Production: "1549809898643001484:1569740403522142258,..."
ORG_ROLE_MAP = _parse_channel_role_map(os.getenv("ORG_ROLE_MAP", ""))

# Пул постоянных каналов MM-матчей: вместо create/delete на каждый
# матч канал берётся из пула, чистится и переназначается.
# Переопределяется env MM_MATCH_CHANNEL_POOL (csv id); без env —
# ровно два постоянных канала r!z3.
_DEFAULT_MM_MATCH_CHANNEL_POOL = (1569282744909824061, 1569282781270245417)
MM_MATCH_CHANNEL_POOL = [
    int(x) for x in os.getenv("MM_MATCH_CHANNEL_POOL", "").split(",")
    if x.strip()
] or list(_DEFAULT_MM_MATCH_CHANNEL_POOL)

# Notification role ID for tournament announcements
NOTIFICATION_ROLE_ID = int(os.getenv("NOTIFICATION_ROLE_ID", "0"))

# Notification channel ID for tournament announcements
NOTIFICATION_CHANNEL_ID = int(os.getenv("NOTIFICATION_CHANNEL_ID", "0"))

# Tournament channels for organizer management (comma-separated list)
TOURNAMENT_CHANNELS_RAW = os.getenv("TOURNAMENT_CHANNELS", "")
TOURNAMENT_CHANNELS = [int(x.strip()) for x in TOURNAMENT_CHANNELS_RAW.split(",") if x.strip()]

# Единый канал карточек «НАЙДЕН МАТЧ» (Ready Check) для Ranked MM.
# По умолчанию — первый турнирный канал, если не задан явно.
MATCH_FOUND_CHANNEL_ID = int(os.getenv("MATCH_FOUND_CHANNEL_ID", "0")) or (
    TOURNAMENT_CHANNELS[0] if TOURNAMENT_CHANNELS else 0
)

# Moderation logging webhook — СЕКРЕТ, только через env.
# Пустое значение = mod-log отключён (send_mod_log no-op).
MOD_LOG_WEBHOOK_URL = os.getenv("MOD_LOG_WEBHOOK_URL", "")
MOD_LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID", "0"))

# Battle Royale settings
BR_ROLE_ID = int(os.getenv("BR_ROLE_ID", "0"))
BR_ROOM_CHANNEL_ID = int(os.getenv("BR_ROOM_CHANNEL_ID", "0"))
# Канал данных комнат BR и канал создания команд
BR_DATA_CHANNEL_ID = int(os.getenv("BR_DATA_CHANNEL_ID", "0"))
BR_TEAM_CHANNEL_ID = int(os.getenv("BR_TEAM_CHANNEL_ID", "0"))

# Welcome banner URL
WELCOME_BANNER_URL = os.getenv("WELCOME_BANNER_URL", "https://cdn.discordapp.com/attachments/1492103238348967988/1554430577132961823/image.png?ex=6abcdbc9&is=6abb8a49&hm=668a5650450e51905a99ec6ed624eb0cc51c019d91c786d228e5e2f821fc5260&")

# Organizer thumbnail URL for tournament embeds
ORGANIZER_THUMBNAIL_URL = os.getenv("ORGANIZER_THUMBNAIL_URL", "https://cdn.discordapp.com/attachments/1492103238348967988/1554430580442005564/image.png?ex=6abcdbca&is=6abb8a4a&hm=47d04c7fdccae7a3dd3e9b23f84596f471986b77077848b8c29ed9045ce81dc1&")

# GIF-based moderation settings
MUTE_ROLE_ID = int(os.getenv("MUTE_ROLE_ID", "0"))
# GIF URLs for temporary mute/unmute via gif replies
WARN_GIF_URL = os.getenv("WARN_GIF_URL", "https://klipy.com/gifs/h2di-the-dictator")
UNWARN_GIF_URL = os.getenv("UNWARN_GIF_URL", "https://klipy.com/gifs/slice-execute-4")

# Guild ID сервера (server tag checking, guild sync, welcome-канал).
# 0 = не задан (тестовый бот без привязки к конкретному серверу).
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# Канал приветствий новым участникам (0 = welcome отключён)
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0"))

# Ultimatum: роль участников и канал данных комнат
ULT_ROLE_ID = int(os.getenv("ULT_ROLE_ID", "0"))
ULT_ROOM_CHANNEL_ID = int(os.getenv("ULT_ROOM_CHANNEL_ID", "0"))

# Прочие серверные каналы
PET_HUNGER_CHANNEL_ID = int(os.getenv("PET_HUNGER_CHANNEL_ID", "0"))
# Канал публикации карточки «🔓 Комната открыта» для обычных
# турниров (/tournament create). MM-матчи шлют в свой канал матча.
ROOM_DATA_CHANNEL_ID = int(
    os.getenv("ROOM_DATA_CHANNEL_ID") or "1561683314480255057"
)
GAMES_CHANNEL_ID = int(os.getenv("GAMES_CHANNEL_ID", "0"))

# Ready Check («Найден матч») для Ranked Matchmaking
RANKED_READY_TIMEOUT_SECONDS = int(os.getenv("RANKED_READY_TIMEOUT_SECONDS", "90"))

# Reputation System 2.0
REP_MANUAL_AMOUNT = int(os.getenv("REP_MANUAL_AMOUNT", "1"))
REP_MANUAL_COOLDOWN_HOURS = int(os.getenv("REP_MANUAL_COOLDOWN_HOURS", "24"))
REP_INVITE_AMOUNT = int(os.getenv("REP_INVITE_AMOUNT", "5"))
REP_INVITE_VERIFICATION_HOURS = int(os.getenv("REP_INVITE_VERIFICATION_HOURS", "72"))
REP_BOOST_AMOUNT = int(os.getenv("REP_BOOST_AMOUNT", "10"))
REP_BOOST_VERIFICATION_DAYS = int(os.getenv("REP_BOOST_VERIFICATION_DAYS", "7"))
REP_SERVER_TAG_AMOUNT = int(os.getenv("REP_SERVER_TAG_AMOUNT", "5"))
REP_ONE_INVITE_REWARD_PER_INVITED_USER = os.getenv("REP_ONE_INVITE_REWARD_PER_INVITED_USER", "true").lower() in ("1", "true", "yes")
REP_ONE_BOOST_REWARD_PER_USER = os.getenv("REP_ONE_BOOST_REWARD_PER_USER", "true").lower() in ("1", "true", "yes")
REP_ONE_MANUAL_REP_PER_PAIR = os.getenv("REP_ONE_MANUAL_REP_PER_PAIR", "true").lower() in ("1", "true", "yes")

# Публичный канал логов репутации (создаётся автоматически при отсутствии)
REP_LOG_CHANNEL_ID = int(os.getenv("REP_LOG_CHANNEL_ID", "0"))
REP_LOG_CHANNEL_NAME = os.getenv("REP_LOG_CHANNEL_NAME", "репутация-логи")

# Информационный центр: канал по умолчанию для /info publish
# (если 0 — ищется канал с именем info/информация или задаётся параметром)
INFO_CHANNEL_ID = int(os.getenv("INFO_CHANNEL_ID", "0"))

# Economy limits
MAX_BALANCE = 100000  # 100k монет максимум
MIN_BET = 10  # Минимальная ставка
MAX_BET = 1000  # Максимальная ставка (для обычных игр)

# Лимиты турнира
MAX_CAPTAINS = 4
MAX_PLAYERS_PER_CIRCLE = 4
CIRCLES = (2, 3, 4)

# Smart Balance (r!z3 Ranked 4×4): веса критериев и допуски.
# SR в диапазоне 0–10, команды по 4 игрока.
MM_BALANCE_WEIGHTS = {
    "sum": float(os.getenv("MM_BALANCE_W_SUM", "0.40")),
    "top": float(os.getenv("MM_BALANCE_W_TOP", "0.30")),
    "dist": float(os.getenv("MM_BALANCE_W_DIST", "0.20")),
    "spread": float(os.getenv("MM_BALANCE_W_SPREAD", "0.10")),
}
# Допустимое превышение минимальной разницы суммарного SR
# на первом этапе отбора комбинаций.
MM_BALANCE_SUM_TOLERANCE = float(os.getenv("MM_BALANCE_SUM_TOLERANCE", "1.0"))
# Порог «практически равного» balance_score для случайного выбора.
MM_BALANCE_EPSILON = float(os.getenv("MM_BALANCE_EPSILON", "0.001"))

# Custom emoji IDs for ranks (get these from Discord server settings)
# Leave empty to use standard emojis: 👑 🔱 🎯 💎 🌪️ 🥇 🥈 🥉
RANK_EMOJIS = {
    "Bronze": os.getenv("EMOJI_BRONZE", ""),
    "Silver": os.getenv("EMOJI_SILVER", ""),
    "Gold": os.getenv("EMOJI_GOLD", ""),
    "Platinum": os.getenv("EMOJI_PLATINUM", ""),
    "Diamond": os.getenv("EMOJI_DIAMOND", ""),
    "Heroic": os.getenv("EMOJI_HEROIC", os.getenv("EMOJI_ELITE", "")),
    "Elite Heroic": os.getenv(
        "EMOJI_ELITE_HEROIC", os.getenv("EMOJI_ELITE", "")
    ),
    "Master": os.getenv("EMOJI_MASTER", ""),
    "Elite Master": os.getenv(
        "EMOJI_ELITE_MASTER", os.getenv("EMOJI_MASTER", "")
    ),
    "Grandmaster": os.getenv(
        "EMOJI_GRANDMASTER", os.getenv("EMOJI_LEGEND", "")
    ),
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
    
    # Shop & UI
    "tag": os.getenv("EMOJI_TAG", ""),
    "guild_tag": os.getenv("EMOJI_GUILD_TAG", ""),
    "settings": os.getenv("EMOJI_SETTINGS", ""),
    "eye": os.getenv("EMOJI_EYE", ""),
    "arrow_left": os.getenv("EMOJI_ARROW_LEFT", ""),
    "arrow_right": os.getenv("EMOJI_ARROW_RIGHT", ""),
    "room": os.getenv("EMOJI_ROOM", ""),
    
    # Special UI emojis
    "white_dot": os.getenv("EMOJI_WHITE_DOT", ""),
    "sub_directory": os.getenv("EMOJI_SUB_DIRECTORY", ""),
    "sub_middle": os.getenv("EMOJI_SUB_MIDDLE", ""),
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

    "eye": os.getenv("EMOJI_EYE", ""),


    "room": os.getenv("EMOJI_ROOM", ""),
    "winner": os.getenv("EMOJI_WINNER", ""),

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

    # Case name emojis (for display before case names)
    "emoji_basic_case": os.getenv("EMOJI_BASIC_CASE", ""),
    "emoji_premium_case": os.getenv("EMOJI_PREMIUM_CASE", ""),
    "emoji_elite_case": os.getenv("EMOJI_ELITE_CASE", ""),
    "emoji_special_case": os.getenv("EMOJI_SPECIAL_CASE", ""),

    # Medal emojis
    "gold_medal": os.getenv("EMOJI_GOLD_MEDAL", ""),
    "silver_medal": os.getenv("EMOJI_SILVER_MEDAL", ""),
    "bronze_medal": os.getenv("EMOJI_BRONZE_MEDAL", ""),

    # Number emojis
    "num_1": os.getenv("EMOJI_NUM_1", ""),
    "num_2": os.getenv("EMOJI_NUM_2", ""),
    "num_3": os.getenv("EMOJI_NUM_3", ""),
    "num_4": os.getenv("EMOJI_NUM_4", ""),
    "num_5": os.getenv("EMOJI_NUM_5", ""),
    "num_6": os.getenv("EMOJI_NUM_6", ""),
    "num_7": os.getenv("EMOJI_NUM_7", ""),
    "num_8": os.getenv("EMOJI_NUM_8", ""),
    "num_9": os.getenv("EMOJI_NUM_9", ""),
    "num_ten": os.getenv("EMOJI_NUM_TEN", ""),
    "num_eleven": os.getenv("EMOJI_NUM_ELEVEN", ""),
    "num_twelve": os.getenv("EMOJI_NUM_TWELVE", ""),

    # Icon emojis
    "icon_letter": os.getenv("EMOJI_ICON_LETTER", ""),
    "icon_paw": os.getenv("EMOJI_ICON_PAW", ""),
    "icon_bluestacks": os.getenv("EMOJI_ICON_BLUESTACKS", ""),
    "icon_brazil_flag": os.getenv("EMOJI_ICON_BRAZIL_FLAG", ""),
    "icon_japan_flag": os.getenv("EMOJI_ICON_JAPAN_FLAG", ""),
    "icon_germany_flag": os.getenv("EMOJI_ICON_GERMANY_FLAG", ""),
    "icon_spain_flag": os.getenv("EMOJI_ICON_SPAIN_FLAG", ""),
    "icon_gray_cat": os.getenv("EMOJI_ICON_GRAY_CAT", ""),
    "icon_teacup": os.getenv("EMOJI_ICON_TEACUP", ""),
    "icon_ribbon": os.getenv("EMOJI_ICON_RIBBON", ""),
    "icon_18plus": os.getenv("EMOJI_ICON_18PLUS", ""),
    "icon_heart": os.getenv("EMOJI_ICON_HEART", ""),
    "icon_white_sword": os.getenv("EMOJI_ICON_WHITE_SWORD", ""),
    "icon_v_badge": os.getenv("EMOJI_ICON_V_BADGE", ""),
    "icon_cards": os.getenv("EMOJI_ICON_CARDS", ""),
    "icon_cat_ears": os.getenv("EMOJI_ICON_CAT_EARS", ""),
    "icon_bunny": os.getenv("EMOJI_ICON_BUNNY", ""),
    "icon_wing": os.getenv("EMOJI_ICON_WING", ""),
    "icon_black_rose": os.getenv("EMOJI_ICON_BLACK_ROSE", ""),
    "icon_fuck_you": os.getenv("EMOJI_ICON_FUCK_YOU", ""),
    "icon_zzz": os.getenv("EMOJI_ICON_ZZZ", ""),
}

# Custom emoji IDs for icons/profile elements
ICON_EMOJIS = {
    "icon_letter": os.getenv("EMOJI_ICON_LETTER", ""),
    "icon_paw": os.getenv("EMOJI_ICON_PAW", ""),
    "icon_bluestacks": os.getenv("EMOJI_ICON_BLUESTACKS", ""),
    "icon_brazil_flag": os.getenv("EMOJI_ICON_BRAZIL_FLAG", ""),
    "icon_japan_flag": os.getenv("EMOJI_ICON_JAPAN_FLAG", ""),
    "icon_germany_flag": os.getenv("EMOJI_ICON_GERMANY_FLAG", ""),
    "icon_spain_flag": os.getenv("EMOJI_ICON_SPAIN_FLAG", ""),
    "icon_gray_cat": os.getenv("EMOJI_ICON_GRAY_CAT", ""),
    "icon_teacup": os.getenv("EMOJI_ICON_TEACUP", ""),
    "icon_ribbon": os.getenv("EMOJI_ICON_RIBBON", ""),
    "icon_18plus": os.getenv("EMOJI_ICON_18PLUS", ""),
    "icon_heart": os.getenv("EMOJI_ICON_HEART", ""),
    "icon_white_sword": os.getenv("EMOJI_ICON_WHITE_SWORD", ""),
    "icon_v_badge": os.getenv("EMOJI_ICON_V_BADGE", ""),
    "icon_cards": os.getenv("EMOJI_ICON_CARDS", ""),
    "icon_cat_ears": os.getenv("EMOJI_ICON_CAT_EARS", ""),
    "icon_bunny": os.getenv("EMOJI_ICON_BUNNY", ""),
    "icon_wing": os.getenv("EMOJI_ICON_WING", ""),
    "icon_black_rose": os.getenv("EMOJI_ICON_BLACK_ROSE", ""),
    "icon_fuck_you": os.getenv("EMOJI_ICON_FUCK_YOU", ""),
    "icon_zzz": os.getenv("EMOJI_ICON_ZZZ", ""),
}

# Standard emojis as fallback when custom emojis are not set
STANDARD_EMOJIS = {
    # Ranks (тиры; поддивизионы наследуют эмодзи тира)
    "Bronze": "🥉",
    "Silver": "🥈",
    "Gold": "🥇",
    "Platinum": "💠",
    "Diamond": "🔷",
    "Heroic": "⚔️",
    "Elite Heroic": "🔥",
    "Master": "🎖️",
    "Elite Master": "🏅",
    "Grandmaster": "👑",
    
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
    "arrow_left": "⬅️",
    "arrow_right": "➡️",
    "check": "✅",
    "cross": "❌",
    "settings": "⚙️",
    "room": "🚪",

    # Medals
    "gold_medal": "🥇",
    "silver_medal": "🥈",
    "bronze_medal": "🥉",

    # Numbers
    "num_1": "1️⃣",
    "num_2": "2️⃣",
    "num_3": "3️⃣",
    "num_4": "4️⃣",
    "num_5": "5️⃣",
    "num_6": "6️⃣",
    "num_7": "7️⃣",
    "num_8": "8️⃣",
    "num_9": "9️⃣",
    "num_ten": "🔟",
    "num_eleven": "1️⃣1️⃣",
    "num_twelve": "1️⃣2️⃣",

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
    "tag": "🏷️",
    "eye": "👁️",
    "arrow_left": "⬅️",
    "arrow_right": "➡️",
    
    # Emotions
    "fire": "🔥",
    "ice": "❄️",

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
    "icon_letter": "",
    "icon_paw": "",
    "icon_bluestacks": "",
    "icon_brazil_flag": "🇧🇷",
    "icon_japan_flag": "🇯🇵",
    "icon_germany_flag": "🇩🇪",
    "icon_spain_flag": "🇪🇸",
    "icon_gray_cat": "🐱",
    "icon_teacup": "",
    "icon_ribbon": "",
    "icon_18plus": "",
    "icon_heart": "",
    "icon_white_sword": "⚔️",
    "icon_v_badge": "",
    "icon_cards": "",
    "icon_cat_ears": "",
    "icon_bunny": "🐰",
    "icon_wing": "",
    "icon_black_rose": "🌹",
    "icon_fuck_you": "🖕",
    "icon_zzz": "💤",

    # Case name emojis (for display before case names)
    "emoji_basic_case": "",
    "emoji_premium_case": "",
    "emoji_elite_case": "",
    "emoji_special_case": "",

    # Shop & UI
    "tag": "🏷️",
    "guild_tag": "🏷️",
    "settings": "⚙️",
    "eye": "👁️",
    "arrow_left": "⬅️",
    "arrow_right": "➡️",
    "room": "🚪",
    "winner": "🏆",
    
    # Special UI emojis
    "white_dot": "•",
    "sub_directory": "└",
    "sub_middle": "├",
    "white_arrow": "→",
    "a_sparkle": "✨",
    "a_star": "⭐",
    "a_triple_dots": "⋮",
    "a_dot_smaller": "⋮",
}

# Эмодзи, используемые только для украшения интерфейса.
# Всегда возвращают пустую строку, чтобы не попадать в CV2-карточки.
# Смысловые эмодзи (валюта, рейтинги, игровые иконки, редкости) сюда не входят.
DECORATIVE_EMOJI_KEYS = {
    # Навигация и стрелки
    "arrow_left", "arrow_right", "white_arrow", "white_dot",
    "sub_directory", "sub_middle",
    "a_sparkle", "a_star", "a_triple_dots", "a_dot_smaller",
    "back", "forward", "refresh", "dropdown", "settings", "eye",
    # Статусы и уведомления
    "success", "error", "warning", "info", "loading", "check", "cross",
    "question", "link",
    # Коммуникация и модерация
    "pin", "megaphone", "new", "comment", "announce",
    "delete", "block",
    # Игры и разделы (декоративные иконки меню)
    "game", "dice", "slots", "roulette", "rps", "brain",
    "draft", "match", "screenshot", "stats", "damage",
    "freefire", "pet", "sparkle", "star", "clock",
    "skull", "target", "sword", "team", "profile",
    "shop", "gift", "daily", "bet", "bank", "tournament",
    "room", "tag",
    "case_basic", "case_premium", "case_elite", "case_special",
    # Assets 2.0 декоративные
    "arcade", "boost", "favorite", "home", "inventory", "moderation",
    "music", "partners", "rules", "security", "streams", "support",
}


def _emoji_tag(emoji_name: str, custom_id: str) -> str:
    """Discord custom emoji tag. Имена эмодзи не могут содержать пробелы."""
    safe_name = emoji_name.replace(" ", "_")
    return f"<:{safe_name}:{custom_id}>"


def get_emoji(emoji_name: str) -> str:
    """Get custom emoji with ID or fallback to standard emoji."""

    if emoji_name in DECORATIVE_EMOJI_KEYS:
        return ""

    # Check rank emojis
    if emoji_name in RANK_EMOJIS:
        custom_id = RANK_EMOJIS[emoji_name]
        if custom_id:
            return _emoji_tag(emoji_name, custom_id)
        result = STANDARD_EMOJIS.get(emoji_name, emoji_name)
        return result

    # Check icon emojis
    if emoji_name in ICON_EMOJIS:
        custom_id = ICON_EMOJIS[emoji_name]
        if custom_id:
            return _emoji_tag(emoji_name, custom_id)
        result = STANDARD_EMOJIS.get(emoji_name, emoji_name)
        return result

    # Check game emojis
    if emoji_name in GAME_EMOJIS:
        custom_id = GAME_EMOJIS[emoji_name]
        if custom_id:
            return _emoji_tag(emoji_name, custom_id)
        # Don't fallback to standard emoji for custom emoji names like 'money'
        # Return empty string if custom ID is not set
        return ""

    # Fallback to standard emoji
    result = STANDARD_EMOJIS.get(emoji_name, emoji_name)
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
        "🔽": "dropdown",
        "🆕": "new",
        "➡️": "arrow_right",
        "→": "white_arrow",
        "▶": "forward",
        "◀": "back",
        "⬅️": "arrow_left",

        # Decorative status markers
        "❓": "question",
        "⏳": "loading",
        "ℹ️": "info",
        "🟢": "success",
        "🔵": "info",
        "🟡": "warning",
        "🔒": "security",

        # Communication
        "💬": "comment",
        "👥": "team",
        "📌": "pin",
        "📢": "announce",
        "👁️": "eye",
        "🏷️": "tag",
        "🚪": "room",

        # Emotions
        "🔥": "fire",
        "❄️": "ice",

        # Tournaments
        "🏆": "winner",
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
        "👁️": "eye",

        # Special
        "🏠": "room",

        # Additional symbols for replacement
        "⋯": "a_triple_dots",

        # Case emojis
        "📦": "case_basic",
        "💎": "case_premium",
        "👑": "case_elite",

        # Rare emojis
        "💫": "rare_premium",
        "🌟": "rare_elite",

        # Medal emojis
        "🥇": "gold_medal",
        "🥈": "silver_medal",
        "🥉": "bronze_medal",

        # Number emojis
        "1️⃣": "num_1",
        "2️⃣": "num_2",
        "3️⃣": "num_3",
        "4️⃣": "num_4",
        "5️⃣": "num_5",
        "6️⃣": "num_6",
        "7️⃣": "num_7",
        "8️⃣": "num_8",
        "9️⃣": "num_9",
        "🔟": "num_ten",
        "1️⃣1️⃣": "num_eleven",
        "1️⃣2️⃣": "num_twelve",
    }

    # Direct emoji name mapping for custom emoji names passed directly
    direct_emoji_names = [
        "money", "white_dot", "sub_directory", "sub_middle", "room", "white_arrow",
        "a_sparkle", "a_star", "a_triple_dots", "a_dot_smaller",
        "case_basic", "case_premium", "case_elite", "case_special",
        "rare_basic", "rare_premium", "rare_elite", "rare_special",
        "tag",
        "icon_letter", "icon_paw", "icon_bluestacks", "icon_brazil_flag", "icon_japan_flag",
        "icon_germany_flag", "icon_spain_flag", "icon_gray_cat", "icon_teacup", "icon_ribbon",
        "icon_18plus", "icon_heart", "icon_white_sword", "icon_v_badge", "icon_cards", "icon_cat_ears", "icon_bunny",
        "icon_wing", "icon_black_rose", "icon_fuck_you", "icon_zzz",
        "check", "cross",
        "gold_medal", "silver_medal", "bronze_medal",
        "num_1", "num_2", "num_3", "num_4", "num_5", "num_6", "num_7", "num_8", "num_9", "num_ten", "num_eleven", "num_twelve",
        "room",
        "winner",
        "emoji_basic_case", "emoji_premium_case", "emoji_elite_case", "emoji_special_case",
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

    # Special handling for num emojis to avoid substring replacement issues
    # Replace in reverse order (longest first) to avoid num_11 becoming num_1
    for emoji_name in ["num_twelve", "num_eleven", "num_ten", "num_9", "num_8", "num_7", "num_6", "num_5", "num_4", "num_3", "num_2", "num_1"]:
        if emoji_name in result:
            custom_emoji = get_emoji(emoji_name)
            result = result.replace(emoji_name, custom_emoji)

    return result
