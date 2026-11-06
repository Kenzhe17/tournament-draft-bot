"""Хранилище для matchmaking поисков."""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from config import DATA_DIR
from models.mm_search import MMSearch

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

MM_SEARCH_FILE = DATA_DIR / "mm_search.json"


class MMSearchStore:
    """Сохранение и загрузка MM поисков в JSON-файл."""

    def __init__(self) -> None:
        self._searches: dict[tuple[int, int], MMSearch] = {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить все поиски из файла."""
        if not MM_SEARCH_FILE.exists():
            self._searches = {}
            return

        try:
            with open(MM_SEARCH_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)

            for key, data in raw.items():
                parts = key.split(":")
                if len(parts) == 2:
                    gid, cid = int(parts[0]), int(parts[1])
                    self._searches[(gid, cid)] = MMSearch.from_dict(data)

            logger.info("Загружено %d MM поисков", len(self._searches))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Ошибка загрузки MM JSON: %s", exc)
            self._searches = {}

    def save(self) -> None:
        """Сохранить все поиски в файл."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = {f"{gid}:{cid}": s.to_dict() for (gid, cid), s in self._searches.items()}
        with open(MM_SEARCH_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    def get(self, guild_id: int, channel_id: int) -> MMSearch | None:
        """Получить поиск по guild_id и channel_id."""
        return self._searches.get((guild_id, channel_id))

    def set(self, search: MMSearch) -> None:
        """Сохранить или обновить поиск."""
        self._searches[(search.guild_id, search.channel_id)] = search
        self.save()

    def delete(self, guild_id: int, channel_id: int) -> None:
        """Удалить поиск по guild_id и channel_id."""
        self._searches.pop((guild_id, channel_id), None)
        self.save()

    def all(self) -> list[MMSearch]:
        """Получить все активные поиски."""
        return list(self._searches.values())

    def get_by_guild(self, guild_id: int) -> list[MMSearch]:
        """Получить все поиски сервера."""
        return [s for (gid, cid), s in self._searches.items() if gid == guild_id]


# Глобальный экземпляр хранилища
mm_search_store = MMSearchStore()
