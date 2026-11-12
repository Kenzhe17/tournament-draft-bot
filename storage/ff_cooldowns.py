"""Кулдауны команд /ff info и /ff stats — 6 часов на пользователя.

Ключ "user_id" -> unix timestamp последнего успешного запроса.
Каждая команда хранится в отдельном JSON-файле в DATA_DIR
(атомарная запись).
"""

from __future__ import annotations

import json
import logging
import time

from config import DATA_DIR
from storage.json_atomic import atomic_write_json

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 6 * 3600  # 6 часов


class FFCooldownStore:
    """JSON-хранилище времён последних вызовов FF-команды по user_id."""

    def __init__(self, filename: str) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._file = DATA_DIR / filename
        self._data: dict[str, float] = self._load()

    def _load(self) -> dict[str, float]:
        try:
            if self._file.exists():
                with open(self._file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): float(v) for k, v in data.items()}
        except Exception:
            logger.exception("FFCooldownStore: failed to load %s", self._file)
        return {}

    def _save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._file, self._data, indent=None)
        except Exception:
            logger.exception("FFCooldownStore: failed to save %s", self._file)

    def seconds_left(self, user_id: int) -> int:
        """Секунд до следующего разрешённого вызова (0 = можно)."""
        ts = self._data.get(str(user_id), 0.0)
        remaining = int(ts + COOLDOWN_SECONDS - time.time())
        return max(0, remaining)

    def mark_used(self, user_id: int) -> None:
        """Отметить успешное использование команды."""
        self._data[str(user_id)] = time.time()
        self._save()


ff_info_cooldowns = FFCooldownStore("ff_info_cooldowns.json")
ff_stats_cooldowns = FFCooldownStore("ff_stats_cooldowns.json")
