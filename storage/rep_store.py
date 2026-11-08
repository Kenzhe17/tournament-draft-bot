"""Отслеживание выданной репутации (/rep).

Ключ "guild_id:giver_id" -> список user_id, которым уже выдали
репутацию. Одна пара «кто -> кому» даёт +1 только один раз.
"""

from __future__ import annotations

import json
import logging

from config import DATA_DIR

logger = logging.getLogger(__name__)

DATA_FILE = DATA_DIR / "rep_given.json"


class RepStore:
    """JSON-хранилище фактов выдачи репутации."""

    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, list[str]] = self._load()

    def _load(self) -> dict[str, list[str]]:
        try:
            if DATA_FILE.exists():
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): [str(x) for x in v] for k, v in data.items() if isinstance(v, list)}
        except Exception:
            logger.exception("RepStore: failed to load %s", DATA_FILE)
        return {}

    def _save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
        except Exception:
            logger.exception("RepStore: failed to save %s", DATA_FILE)

    @staticmethod
    def _key(guild_id: int, giver_id: int) -> str:
        return f"{guild_id}:{giver_id}"

    def has_given(self, guild_id: int, giver_id: int, target_id: int) -> bool:
        """Выдавал ли giver репутацию target ранее."""
        return str(target_id) in self._data.get(self._key(guild_id, giver_id), [])

    def mark_given(self, guild_id: int, giver_id: int, target_id: int) -> None:
        """Отметить, что giver дал репутацию target."""
        key = self._key(guild_id, giver_id)
        targets = self._data.setdefault(key, [])
        tid = str(target_id)
        if tid not in targets:
            targets.append(tid)
            self._save()


rep_store = RepStore()
