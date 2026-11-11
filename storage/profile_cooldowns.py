"""Кулдауны редактирования полей профиля.

День рождения и описание можно менять не чаще раза в COOLDOWN_SECONDS.
Ключ "guild_id:user_id:field" -> unix timestamp последнего изменения.
"""

from __future__ import annotations

import json
import logging
import time

from config import DATA_DIR
from storage.json_atomic import atomic_write_json

logger = logging.getLogger(__name__)

DATA_FILE = DATA_DIR / "profile_cooldowns.json"

COOLDOWN_SECONDS = 7 * 24 * 3600  # 7 дней

LIMITED_FIELDS = ("birthday", "description")


class ProfileCooldownStore:
    """JSON-хранилище времён последних изменений полей профиля."""

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
            logger.exception("ProfileCooldownStore: failed to load %s", DATA_FILE)
        return {}

    def _save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_json(DATA_FILE, self._data, indent=None)
        except Exception:
            logger.exception("ProfileCooldownStore: failed to save %s", DATA_FILE)

    @staticmethod
    def _key(guild_id: int, user_id: int, field: str) -> str:
        return f"{guild_id}:{user_id}:{field}"

    def seconds_left(self, guild_id: int, user_id: int, field: str) -> int:
        """Секунд до следующего разрешённого изменения (0 = можно)."""
        ts = self._data.get(self._key(guild_id, user_id, field), 0.0)
        remaining = int(ts + COOLDOWN_SECONDS - time.time())
        return max(0, remaining)

    def mark_changed(self, guild_id: int, user_id: int, field: str) -> None:
        """Отметить, что поле было изменено."""
        self._data[self._key(guild_id, user_id, field)] = time.time()
        self._save()


profile_cooldowns = ProfileCooldownStore()


def format_remaining(seconds: int) -> str:
    """Человекочитаемый остаток кулдауна."""
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    if days > 0:
        return f"{days} дн. {hours} ч."
    minutes = (seconds % 3600) // 60
    return f"{hours} ч. {minutes} мин."
