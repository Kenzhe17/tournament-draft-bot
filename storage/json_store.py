"""JSON-хранилище состояния турниров."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from config import DATA_DIR, DATA_FILE
from models.tournament import Tournament

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class JsonStore:
    """Сохранение и загрузка турниров в JSON-файл."""

    def __init__(self) -> None:
        self._tournaments: dict[int, Tournament] = {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить все турниры из файла."""
        if not DATA_FILE.exists():
            self._tournaments = {}
            return

        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._tournaments = {
                int(gid): Tournament.from_dict(data)
                for gid, data in raw.items()
            }
            logger.info("Загружено %d турниров", len(self._tournaments))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Ошибка загрузки JSON: %s", exc)
            self._tournaments = {}

    def save(self) -> None:
        """Сохранить все турниры в файл."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = {str(gid): t.to_dict() for gid, t in self._tournaments.items()}
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    def get(self, guild_id: int) -> Tournament | None:
        """Получить турнир сервера."""
        return self._tournaments.get(guild_id)

    def set(self, tournament: Tournament) -> None:
        """Сохранить или обновить турнир."""
        self._tournaments[tournament.guild_id] = tournament
        self.save()

    def delete(self, guild_id: int) -> None:
        """Удалить турнир сервера."""
        self._tournaments.pop(guild_id, None)
        self.save()

    def all(self) -> list[Tournament]:
        """Получить все активные турниры."""
        return list(self._tournaments.values())


# Глобальный экземпляр хранилища
store = JsonStore()
