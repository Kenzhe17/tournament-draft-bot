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
        self._tournaments: dict[tuple[int, int], Tournament] = {}
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
            
            # Migration: convert old format {guild_id: data} to new format {(guild_id, channel_id): data}
            migrated = {}
            for key, data in raw.items():
                if isinstance(key, str) and key.isdigit():
                    # Old format: single guild_id
                    gid = int(key)
                    tournament = Tournament.from_dict(data)
                    # Use tournament's channel_id for migration
                    migrated[(gid, tournament.channel_id)] = tournament
                else:
                    # New format: "guild_id:channel_id"
                    parts = key.split(":")
                    if len(parts) == 2:
                        gid, cid = int(parts[0]), int(parts[1])
                        migrated[(gid, cid)] = Tournament.from_dict(data)
            
            self._tournaments = migrated
            logger.info("Загружено %d турниров", len(self._tournaments))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Ошибка загрузки JSON: %s", exc)
            self._tournaments = {}

    def save(self) -> None:
        """Сохранить все турниры в файл."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = {f"{gid}:{cid}": t.to_dict() for (gid, cid), t in self._tournaments.items()}
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    def get(self, guild_id: int, channel_id: int) -> Tournament | None:
        """Получить турнир по guild_id и channel_id."""
        return self._tournaments.get((guild_id, channel_id))

    def set(self, tournament: Tournament) -> None:
        """Сохранить или обновить турнир."""
        self._tournaments[(tournament.guild_id, tournament.channel_id)] = tournament
        self.save()

    def delete(self, guild_id: int, channel_id: int) -> None:
        """Удалить турнир по guild_id и channel_id."""
        self._tournaments.pop((guild_id, channel_id), None)
        self.save()

    def all(self) -> list[Tournament]:
        """Получить все активные турниры."""
        return list(self._tournaments.values())

    def get_by_guild(self, guild_id: int) -> list[Tournament]:
        """Получить все турниры сервера (для проверок конфликтов)."""
        return [t for (gid, cid), t in self._tournaments.items() if gid == guild_id]

    def get_settings(self) -> dict:
        """Получить настройки бота."""
        SETTINGS_FILE = DATA_DIR / "settings.json"
        if not SETTINGS_FILE.exists():
            return {}
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError, TypeError):
            return {}

    def set_settings(self, settings: dict) -> None:
        """Сохранить настройки бота."""
        SETTINGS_FILE = DATA_DIR / "settings.json"
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)


# Глобальный экземпляр хранилища
store = JsonStore()
