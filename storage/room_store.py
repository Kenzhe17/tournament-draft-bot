"""Хранилище данных игровых комнат турниров (ID + пароль Free Fire).

Отдельно от K/D и результатов: комната привязана к конкретному
matrix-ключу матча ``{tournament_id}:{phase}:{index}`` и не смешивается
между матчами. Хранение — атомарный JSON в DATA_DIR (переживает
перезапуск бота). Пароль никогда не пишется в логи.

На один матч может быть несколько комнат — каждая запись имеет
``room_no`` (по умолчанию 1).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from config import DATA_DIR
from storage.json_atomic import atomic_write_json, load_json_file

logger = logging.getLogger(__name__)

ROOM_DATA_FILE = DATA_DIR / "room_data.json"


def match_key(tournament_id: str, phase: str, index: int) -> str:
    """Стабильный ключ матча: турнир + стадия + номер матча."""
    return f"{tournament_id}:{phase}:{index}"


class RoomStore:
    """JSON-хранилище комнат: (guild, channel, match_key) -> [room]."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or ROOM_DATA_FILE
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------
    # Загрузка / сохранение
    # ------------------------------------------------------------------

    def _load(self) -> None:
        raw = load_json_file(self._path)
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            if isinstance(value, dict) and isinstance(value.get("rooms"), list):
                self._data[key] = value
        logger.info("RoomStore: загружено %d матчей с данными комнат",
                    len(self._data))

    def _save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._path, self._data)
        except Exception as exc:
            logger.error("RoomStore: ошибка сохранения: %s", exc)

    @staticmethod
    def _scope(guild_id: int, channel_id: int, key: str) -> str:
        return f"{guild_id}:{channel_id}:{key}"

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def get_rooms(self, guild_id: int, channel_id: int,
                  key: str) -> list[dict[str, Any]]:
        """Все комнаты матча (список, room_no >= 1)."""
        entry = self._data.get(self._scope(guild_id, channel_id, key))
        if not entry:
            return []
        return [dict(r) for r in entry.get("rooms", [])]

    def get_room(self, guild_id: int, channel_id: int, key: str,
                 room_no: int = 1) -> dict[str, Any] | None:
        for room in self.get_rooms(guild_id, channel_id, key):
            if int(room.get("room_no", 1)) == room_no:
                return room
        return None

    def has_room(self, guild_id: int, channel_id: int, key: str) -> bool:
        return bool(self.get_rooms(guild_id, channel_id, key))

    def upsert_room(
        self,
        guild_id: int,
        channel_id: int,
        key: str,
        room_id: str,
        password: str,
        submitted_by: int,
        room_no: int = 1,
    ) -> dict[str, Any]:
        """Создать или обновить комнату матча.

        Существующая запись обновляется на месте (автор/время
        фиксируются), новой записи на тот же room_no не создаётся.
        """
        scope = self._scope(guild_id, channel_id, key)
        entry = self._data.setdefault(scope, {"rooms": []})
        rooms: list[dict[str, Any]] = entry["rooms"]
        now = time.time()
        for room in rooms:
            if int(room.get("room_no", 1)) == room_no:
                existed = bool(room.get("room_id"))
                room.update({
                    "room_id": room_id,
                    "password": password,
                    "submitted_by": room.get("submitted_by", submitted_by),
                    "submitted_at": room.get("submitted_at", now),
                    "updated_by": submitted_by,
                    "updated_at": now,
                })
                self._save()
                return {"room": room, "created": not existed}
        room = {
            "room_no": room_no,
            "room_id": room_id,
            "password": password,
            "submitted_by": submitted_by,
            "submitted_at": now,
            "updated_by": submitted_by,
            "updated_at": now,
        }
        rooms.append(room)
        rooms.sort(key=lambda r: int(r.get("room_no", 1)))
        self._save()
        return {"room": room, "created": True}

    def rooms_for_tournament(self, guild_id: int, channel_id: int,
                             tournament_id: str) -> dict[str, list[dict[str, Any]]]:
        """Все комнаты турнира: {match_key: [rooms]}."""
        prefix = f"{guild_id}:{channel_id}:{tournament_id}:"
        result: dict[str, list[dict[str, Any]]] = {}
        for scope, entry in self._data.items():
            if not scope.startswith(prefix):
                continue
            key = scope[len(f"{guild_id}:{channel_id}:"):]
            result[key] = [dict(r) for r in entry.get("rooms", [])]
        return result

    def clear_tournament(self, guild_id: int, channel_id: int,
                         tournament_id: str) -> int:
        """Удалить все комнаты турнира (при удалении/архивации)."""
        prefix = f"{guild_id}:{channel_id}:{tournament_id}:"
        doomed = [k for k in self._data if k.startswith(prefix)]
        for k in doomed:
            del self._data[k]
        if doomed:
            self._save()
        return len(doomed)


room_store = RoomStore()
