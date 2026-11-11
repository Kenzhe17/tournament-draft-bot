"""Отслеживание выданной репутации (/rep).

Ключ "guild_id:giver_id" -> {"targets": [...], "day": "YYYY-MM-DD", "count": N}.

Правила:
- одна пара «кто -> кому» даёт +1 только один раз (targets);
- не более DAILY_LIMIT репутаций от одного giver в день (count/day).
"""

from __future__ import annotations

import json
import logging
from datetime import date

from config import DATA_DIR
from storage.json_atomic import atomic_write_json

logger = logging.getLogger(__name__)

DATA_FILE = DATA_DIR / "rep_given.json"

DAILY_LIMIT = 3


class RepStore:
    """JSON-хранилище фактов выдачи репутации."""

    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            if DATA_FILE.exists():
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): self._normalize(v) for k, v in data.items()}
        except Exception:
            logger.exception("RepStore: failed to load %s", DATA_FILE)
        return {}

    @staticmethod
    def _normalize(value) -> dict:
        """Привести запись к виду {targets, day, count} (поддержка старого list-формата)."""
        if isinstance(value, dict):
            targets = value.get("targets")
            return {
                "targets": [str(x) for x in targets] if isinstance(targets, list) else [],
                "day": str(value.get("day", "")),
                "count": int(value.get("count", 0) or 0),
            }
        if isinstance(value, list):
            return {"targets": [str(x) for x in value], "day": "", "count": 0}
        return {"targets": [], "day": "", "count": 0}

    def _save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_json(DATA_FILE, self._data, indent=None)
        except Exception:
            logger.exception("RepStore: failed to save %s", DATA_FILE)

    @staticmethod
    def _key(guild_id: int, giver_id: int) -> str:
        return f"{guild_id}:{giver_id}"

    def _entry(self, guild_id: int, giver_id: int) -> dict:
        key = self._key(guild_id, giver_id)
        entry = self._data.get(key)
        if entry is None:
            entry = {"targets": [], "day": "", "count": 0}
            self._data[key] = entry
        else:
            entry = self._normalize(entry)
            self._data[key] = entry
        return entry

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def has_given(self, guild_id: int, giver_id: int, target_id: int) -> bool:
        """Выдавал ли giver репутацию target ранее."""
        return str(target_id) in self._entry(guild_id, giver_id)["targets"]

    def used_today(self, guild_id: int, giver_id: int) -> int:
        """Сколько репутаций giver уже выдал сегодня."""
        entry = self._entry(guild_id, giver_id)
        if entry["day"] != self._today():
            return 0
        return entry["count"]

    def mark_given(self, guild_id: int, giver_id: int, target_id: int) -> None:
        """Отметить выдачу репутации (target + дневной счётчик)."""
        entry = self._entry(guild_id, giver_id)
        tid = str(target_id)
        if tid not in entry["targets"]:
            entry["targets"].append(tid)

        today = self._today()
        if entry["day"] != today:
            entry["day"] = today
            entry["count"] = 0
        entry["count"] += 1
        self._save()


rep_store = RepStore()
