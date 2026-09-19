"""Конфигурация бота."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Корневая директория проекта (на уровень выше пакета bot/)
BASE_DIR = Path(__file__).resolve().parent.parent

# Путь к JSON-хранилищу (можно переопределить в облаке через DATA_DIR)
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATA_FILE = DATA_DIR / "tournaments.json"

# Discord-токен из переменной окружения
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# Лимиты турнира
MAX_CAPTAINS = 4
MAX_TEAMS = 4
MAX_PLAYERS_PER_CIRCLE = 4
CIRCLES = (2, 3, 4)

# Custom ID для persistent views (переживают перезапуск)
VIEW_DRAFT_SELECT = "tournament:draft_select"
VIEW_GENERATE_MATCHES = "tournament:generate_matches"
VIEW_SEMIFINAL_PREFIX = "tournament:semi:"
VIEW_FINAL_PREFIX = "tournament:final:"
