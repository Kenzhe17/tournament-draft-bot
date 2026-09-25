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
