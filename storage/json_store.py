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
        self._tournaments: dict[str, Tournament] = {}  # Changed from int to str key (tournament.id)
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
                tid: Tournament.from_dict(data)
                for tid, data in raw.items()
            }
            logger.info("Загружено %d турниров", len(self._tournaments))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Ошибка загрузки JSON: %s", exc)
            self._tournaments = {}

    def save(self) -> None:
        """Сохранить все турниры в файл."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = {tid: t.to_dict() for tid, t in self._tournaments.items()}
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    def get(self, guild_id: int) -> Tournament | None:
        """Получить турнир сервера (legacy - returns first tournament for guild)."""
        for t in self._tournaments.values():
            if t.guild_id == guild_id:
                return t
        return None

    def get_by_id(self, tournament_id: str) -> Tournament | None:
        """Получить турнир по ID."""
        return self._tournaments.get(tournament_id)

    def get_all_for_guild(self, guild_id: int) -> list[Tournament]:
        """Получить все турниры сервера."""
        return [t for t in self._tournaments.values() if t.guild_id == guild_id]

    def set(self, tournament: Tournament) -> None:
        """Сохранить или обновить турнир."""
        self._tournaments[tournament.id] = tournament
        self.save()

    def delete(self, guild_id: int) -> None:
        """Удалить турнир сервера (legacy - deletes all tournaments for guild)."""
        to_delete = [tid for tid, t in self._tournaments.items() if t.guild_id == guild_id]
        for tid in to_delete:
            self._tournaments.pop(tid, None)
        self.save()

    def delete_by_id(self, tournament_id: str) -> None:
        """Удалить турнир по ID."""
        self._tournaments.pop(tournament_id, None)
        self.save()

    def all(self) -> list[Tournament]:
        """Получить все активные турниры."""
        return list(self._tournaments.values())


# Глобальный экземпляр хранилища
store = JsonStore()
