"""JSON-хранилище состояния турниров."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from bot.config import DATA_DIR, DATA_FILE
from bot.models import Tournament


class TournamentStorage:
    """Потокобезопасное хранение турниров в JSON."""

    def __init__(self, path: Path = DATA_FILE) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._ensure_data_dir()

    def _ensure_data_dir(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write_raw({"tournaments": {}})

    def _read_raw(self) -> dict:
        try:
            with self._path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {"tournaments": {}}

    def _write_raw(self, data: dict) -> None:
        with self._path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _key(self, guild_id: int) -> str:
        return str(guild_id)

    def get(self, guild_id: int) -> Tournament | None:
        with self._lock:
            raw = self._read_raw()
            entry = raw.get("tournaments", {}).get(self._key(guild_id))
            if not entry:
                return None
            return Tournament.from_dict(entry)

    def save(self, tournament: Tournament) -> None:
        with self._lock:
            raw = self._read_raw()
            raw.setdefault("tournaments", {})[self._key(tournament.guild_id)] = tournament.to_dict()
            self._write_raw(raw)

    def delete(self, guild_id: int) -> None:
        with self._lock:
            raw = self._read_raw()
            raw.get("tournaments", {}).pop(self._key(guild_id), None)
            self._write_raw(raw)

    def all_tournaments(self) -> list[Tournament]:
        with self._lock:
            raw = self._read_raw()
            return [Tournament.from_dict(v) for v in raw.get("tournaments", {}).values()]

    def update(self, guild_id: int, mutator: Callable[[Tournament], None]) -> Tournament | None:
        """Атомарное обновление турнира через callback."""
        with self._lock:
            raw = self._read_raw()
            key = self._key(guild_id)
            entry = raw.get("tournaments", {}).get(key)
            if not entry:
                return None
            tournament = Tournament.from_dict(entry)
            mutator(tournament)
            raw["tournaments"][key] = tournament.to_dict()
            self._write_raw(raw)
            return tournament


# Глобальный экземпляр хранилища
storage = TournamentStorage()
