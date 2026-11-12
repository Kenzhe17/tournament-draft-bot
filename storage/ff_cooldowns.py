"""Кулдаун команды /ff info — 6 часов на пользователя.

Ключ "user_id" -> unix timestamp последнего успешного запроса профиля.
Хранится в data/ff_info_cooldowns.json (атомарная запись).
"""

from __future__ import annotations

import json
import logging
import time

from config import DATA_DIR
from storage.json_atomic import atomic_write_json

logger = logging.getLogger(__name__)

DATA_FILE = DATA_DIR / "ff_info_cooldowns.json"

COOLDOWN_SECONDS = 6 * 3600  # 6 часов


class FFInfoCooldownStore:
    """JSON-хранилище времён последних /ff info по user_id."""

    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, float] = self._load()

    def _load(self) -> dict[str, float]:
        try:
            if DATA_FILE.exists():
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): float(v) for k, v in data.items()}
        except Exception:
            logger.exception("FFInfoCooldownStore: failed to load %s", DATA_FILE)
        return {}

    def _save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_json(DATA_FILE, self._data, indent=None)
        except Exception:
            logger.exception("FFInfoCooldownStore: failed to save %s", DATA_FILE)

    def seconds_left(self, user_id: int) -> int:
        """Секунд до следующего разрешённого /ff info (0 = можно)."""
        ts = self._data.get(str(user_id), 0.0)
        remaining = int(ts + COOLDOWN_SECONDS - time.time())
        return max(0, remaining)

    def mark_used(self, user_id: int) -> None:
        """Отметить успешное использование /ff info."""
        self._data[str(user_id)] = time.time()
        self._save()


ff_info_cooldowns = FFInfoCooldownStore()
