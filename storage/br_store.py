"""JSON-хранилище состояния BR турниров."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from config import DATA_DIR
from models.br_tournament import BRTournament

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

BR_DATA_FILE = DATA_DIR / "br_tournaments.json"


class BRStore:
    """Сохранение и загрузка BR турниров в JSON-файл."""

    def __init__(self) -> None:
        self._tournaments: dict[tuple[int, int], BRTournament] = {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить все BR турниры из файла."""
        if not BR_DATA_FILE.exists():
            self._tournaments = {}
            return

        try:
            with open(BR_DATA_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)

            # Parse tournaments
            loaded = {}
            for key, data in raw.items():
                # Format: "guild_id:channel_id"
                parts = key.split(":")
                if len(parts) == 2:
                    gid, cid = int(parts[0]), int(parts[1])
                    loaded[(gid, cid)] = BRTournament.from_dict(data)

            self._tournaments = loaded
            logger.info("Загружено %d BR турниров", len(self._tournaments))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Ошибка загрузки BR JSON: %s", exc)
            self._tournaments = {}

    def save(self) -> None:
        """Сохранить все BR турниры в файл."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = {f"{gid}:{cid}": t.to_dict() for (gid, cid), t in self._tournaments.items()}
        with open(BR_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    def get(self, guild_id: int, channel_id: int) -> BRTournament | None:
        """Получить BR турнир по guild_id и channel_id."""
        return self._tournaments.get((guild_id, channel_id))

    def set(self, br_tournament: BRTournament) -> None:
        """Сохранить или обновить BR турнир."""
        self._tournaments[(br_tournament.guild_id, br_tournament.channel_id)] = br_tournament
        self.save()

    def delete(self, guild_id: int, channel_id: int) -> None:
        """Удалить BR турнир по guild_id и channel_id."""
        self._tournaments.pop((guild_id, channel_id), None)
        self.save()

    def all(self) -> list[BRTournament]:
        """Получить все активные BR турниры."""
        return list(self._tournaments.values())

    def get_by_guild(self, guild_id: int) -> list[BRTournament]:
        """Получить все BR турниры сервера."""
        return [t for (gid, cid), t in self._tournaments.items() if gid == guild_id]


# Глобальный экземпляр хранилища
br_store = BRStore()
