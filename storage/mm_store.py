"""JSON-хранилище состояния Matchmaking турниров."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from config import DATA_DIR
from models.mm_tournament import MMTournament

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

MM_DATA_FILE = DATA_DIR / "mm_tournaments.json"


class MMStore:
    """Сохранение и загрузка MM турниров в JSON-файл."""

    def __init__(self) -> None:
        self._tournaments: dict[tuple[int, int], MMTournament] = {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить все MM турниры из файла."""
        if not MM_DATA_FILE.exists():
            self._tournaments = {}
            return

        try:
            with open(MM_DATA_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)

            # Parse tournaments
            loaded = {}
            for key, data in raw.items():
                # Format: "guild_id:channel_id"
                parts = key.split(":")
                if len(parts) == 2:
                    gid, cid = int(parts[0]), int(parts[1])
                    loaded[(gid, cid)] = MMTournament.from_dict(data)

            self._tournaments = loaded
            logger.info("Загружено %d MM турниров", len(self._tournaments))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Ошибка загрузки MM JSON: %s", exc)
            self._tournaments = {}

    def save(self) -> None:
        """Сохранить все MM турниры в файл."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = {f"{gid}:{cid}": t.to_dict() for (gid, cid), t in self._tournaments.items()}
        with open(MM_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    def get(self, guild_id: int, channel_id: int) -> MMTournament | None:
        """Получить MM турнир по guild_id и channel_id."""
        return self._tournaments.get((guild_id, channel_id))

    def set(self, mm_tournament: MMTournament) -> None:
        """Сохранить или обновить MM турнир."""
        self._tournaments[(mm_tournament.guild_id, mm_tournament.channel_id)] = mm_tournament
        self.save()

    def delete(self, guild_id: int, channel_id: int) -> None:
        """Удалить MM турнир по guild_id и channel_id."""
        self._tournaments.pop((guild_id, channel_id), None)
        self.save()

    def all(self) -> list[MMTournament]:
        """Получить все активные MM турниры."""
        return list(self._tournaments.values())

    def get_by_guild(self, guild_id: int) -> list[MMTournament]:
        """Получить все MM турниры сервера."""
        return [t for (gid, cid), t in self._tournaments.items() if gid == guild_id]


# Глобальный экземпляр хранилища
mm_store = MMStore()
