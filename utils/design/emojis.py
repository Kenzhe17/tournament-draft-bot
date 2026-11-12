"""Централизованная библиотека эмодзи дизайн-системы.

EMOJI_IDS — словарь «имя -> ID кастомного эмодзи Discord».
Пользователь заполняет ID после загрузки эмодзи на сервер.
Пока ID не задан — используется unicode-fallback из FALLBACKS.

Формат значения:
- "1234567890123456789" — ID, имя эмодзи на сервере = ключу
- "emoji_name:1234567890123456789" — ID + имя эмодзи на сервере
  отличается от ключа (например PNG assets/emojis/coins.png
  загружен как эмодзи "coins", а ключ остаётся "coin").

Эмодзи должен быть доступен на сервере бота.
"""

# Заполнить после загрузки эмодзи на Discord-сервер.
# Пример: "coin": "1234567890123456789",
# Пример с другим именем: "coin": "coins:1234567890123456789",
EMOJI_IDS: dict[str, str] = {
    # --- Экономика ---
    "coin": "",
    "money": "",
    "bank": "",
    "shop": "",
    "gift": "",
    "daily": "",
    "bet": "",
    # --- Профиль / рейтинг ---
    "profile": "",
    "rating": "",
    "trophy": "",
    "medal": "",
    "target": "",
    "crown": "",
    # --- Турнир ---
    "tournament": "",
    "sword": "",
    "team": "",
    "room": "",
    "winner": "",
    "skull": "",
    # --- Статус / уведомления ---
    "success": "",
    "error": "",
    "warning": "",
    "info": "",
    "loading": "",
    "check": "",
    "cross": "",
    "pin": "",
    "megaphone": "",
    # --- Навигация ---
    "back": "",
    "forward": "",
    "refresh": "",
    "dropdown": "",
    "arrow_left": "",
    "arrow_right": "",
    "settings": "",
    "eye": "",
    # --- Игры ---
    "game": "",
    "dice": "",
    "slots": "",
    "roulette": "",
    "rps": "",
    "brain": "",
    # --- Драфт / матч ---
    "draft": "",
    "match": "",
    "screenshot": "",
    "stats": "",
    "damage": "",
    # --- Free Fire ---
    "freefire": "",
    # --- Кейсы / питомцы ---
    "case_basic": "",
    "case_premium": "",
    "case_elite": "",
    "pet": "",
    # --- Прочее ---
    "sparkle": "",
    "star": "",
    "clock": "",
    "fire": "",
    "ice": "",
    "comment": "",
    "announce": "",
    "new": "",
    "delete": "",
    "block": "",
    "question": "",
    "link": "",
    # --- Assets 2.0 (PNG в assets/emojis/, см. EMOJI_ASSETS_V2.md) ---
    "arcade": "",
    "boost": "",
    "favorite": "",
    "home": "",
    "inventory": "",
    "moderation": "",
    "music": "",
    "partners": "",
    "rules": "",
    "security": "",
    "streams": "",
    "support": "",
}

# Unicode-fallback на случай, если кастомный эмодзи не загружен.
FALLBACKS: dict[str, str] = {
    "coin": "🪙",
    "money": "💰",
    "bank": "🏦",
    "shop": "🛒",
    "gift": "🎁",
    "daily": "📅",
    "bet": "🎲",
    "profile": "👤",
    "rating": "📈",
    "trophy": "🏆",
    "medal": "🏅",
    "target": "🎯",
    "crown": "👑",
    "tournament": "🏆",
    "sword": "⚔️",
    "team": "👥",
    "room": "🚪",
    "winner": "🥇",
    "skull": "💀",
    "success": "✅",
    "error": "❌",
    "warning": "⚠️",
    "info": "ℹ️",
    "loading": "⏳",
    "check": "☑️",
    "cross": "❎",
    "pin": "📌",
    "megaphone": "📢",
    "back": "◀️",
    "forward": "▶️",
    "refresh": "🔄",
    "dropdown": "🔽",
    "arrow_left": "⬅️",
    "arrow_right": "➡️",
    "settings": "⚙️",
    "eye": "👁️",
    "game": "🎮",
    "dice": "🎲",
    "slots": "🎰",
    "roulette": "🎡",
    "rps": "✊",
    "brain": "🧠",
    "draft": "📝",
    "match": "⚔️",
    "screenshot": "📸",
    "stats": "📊",
    "damage": "💥",
    "freefire": "🔥",
    "case_basic": "📦",
    "case_premium": "🎁",
    "case_elite": "💎",
    "pet": "🐾",
    "sparkle": "✨",
    "star": "⭐",
    "clock": "🕐",
    "fire": "🔥",
    "ice": "❄️",
    "comment": "💬",
    "announce": "📣",
    "new": "🆕",
    "delete": "🗑️",
    "block": "🚫",
    "question": "❓",
    "link": "🔗",
    # --- Assets 2.0 ---
    "arcade": "🕹️",
    "boost": "⬆️",
    "favorite": "❤️",
    "home": "🏠",
    "inventory": "🎒",
    "moderation": "🛡️",
    "music": "🎵",
    "partners": "🤝",
    "rules": "📜",
    "security": "🔒",
    "streams": "📺",
    "support": "🛟",
}


def em(name: str) -> str:
    """Получить строку эмодзи: кастомный <:`name`:id> или unicode-fallback.

    EMOJI_IDS[name] может быть "id" (имя эмодзи = ключу) или
    "discord_name:id", если имя загруженного эмодзи отличается.
    """
    raw = EMOJI_IDS.get(name, "")
    if raw:
        emoji_name, sep, emoji_id = raw.partition(":")
        if not sep:
            emoji_name, emoji_id = name, raw
        if emoji_id:
            return f"<:{emoji_name}:{emoji_id}>"
    # Мост к config.py: там ID берутся из EMOJI_* env-переменных
    try:
        from config import get_emoji
        result = get_emoji(name)
        if result and result != name:
            return result
    except Exception:
        pass
    return FALLBACKS.get(name, name)
