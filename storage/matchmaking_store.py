"""JSON-хранилище состояния matchmaking."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from config import DATA_DIR
from models.matchmaking import Matchmaking

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

MATCHMAKING_FILE = DATA_DIR / "matchmaking.json"


class MatchmakingStore:
    """Сохранение и загрузка matchmaking в JSON-файл."""

    def __init__(self) -> None:
        self._matchmaking: dict[int, Matchmaking] = {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить все matchmaking из файла."""
        if not MATCHMAKING_FILE.exists():
            self._matchmaking = {}
            return

        try:
            with open(MATCHMAKING_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._matchmaking = {
                int(gid): Matchmaking.from_dict(data)
                for gid, data in raw.items()
            }
            logger.info("Загружено %d matchmaking сессий", len(self._matchmaking))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Ошибка загрузки JSON: %s", exc)
            self._matchmaking = {}

    def save(self) -> None:
        """Сохранить все matchmaking в файл."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = {str(gid): m.to_dict() for gid, m in self._matchmaking.items()}
        with open(MATCHMAKING_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    def get(self, guild_id: int) -> Matchmaking | None:
        """Получить matchmaking сервера."""
        return self._matchmaking.get(guild_id)

    def set(self, matchmaking: Matchmaking) -> None:
        """Сохранить или обновить matchmaking."""
        self._matchmaking[matchmaking.guild_id] = matchmaking
        self.save()

    def delete(self, guild_id: int) -> None:
        """Удалить matchmaking сервера."""
        self._matchmaking.pop(guild_id, None)
        self.save()

    def all(self) -> list[Matchmaking]:
        """Получить все активные matchmaking."""
        return list(self._matchmaking.values())


# Глобальный экземпляр хранилища
matchmaking_store = MatchmakingStore()
