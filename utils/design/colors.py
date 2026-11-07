"""Цветовая палитра r!z3 Minimal Dark UI.

Используется как accent_color для discord.ui.Container —
нативная цветная полоса Components V2 (замена цвету Embed).
"""

# --- Базовая палитра ---
BG = 0x232428            # основной фон
TEXT = 0xE6E6E8          # основной текст
TEXT_SECONDARY = 0xA5A5AA  # вторичный текст
TEXT_MUTED = 0x77777F    # приглушённый текст
DIVIDER = 0x414146       # разделители

# --- Цветовые категории (акценты) ---
SUCCESS = 0x2ECC71
ERROR = 0xE74C3C
WARNING = 0xF1C40F
INFO = 0x5865F2
ECONOMY = 0xF0B232
NEUTRAL = 0x747F8D

# --- Семантические алиасы для интерфейсов ---
PRIMARY = INFO
SECONDARY = NEUTRAL
DANGER = ERROR
ACCENT = ECONOMY

# Карты/темы
TOURNAMENT = 0x8E44AD      # турниры
DRAFT = 0x3498DB           # драфт
MATCH = 0xE67E22           # матчи
PROFILE = 0x1ABC9C         # профиль
RATING = 0xF39C12          # рейтинг/ELO
GAME = 0x9B59B6            # мини-игры
MODERATION = 0xC0392B      # модерация
FREEFIRE = 0xE74C3C        # Free Fire
PET = 0xF1C40F             # питомцы
CASE = 0xF0B232            # кейсы/лут


def accent_hex(value: int) -> str:
    """Форматировать accent-цвет для логов/отладки."""
    return f"#{value:06X}"
