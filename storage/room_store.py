"""Хранилище данных игровых комнат турниров (ID + пароль Free Fire).

Отдельно от K/D и результатов: комната привязана к конкретному
matrix-ключу матча ``{tournament_id}:{phase}:{index}`` и не смешивается
между матчами. Хранение — атомарный JSON в DATA_DIR (переживает
перезапуск бота). Пароль никогда не пишется в логи.

Модель данных:
- На один ``match_key`` — ОДНА актуальная запись ID/пароля
  (``get_active_room``). Исторические записи старого формата
  (несколько ``room_no`` на матч) сохраняются нетронутыми —
  актуальной считается запись с максимальным ``updated_at``
  (fallback ``submitted_at``/``created_at``).
- Актуальная запись хранит: ``tournament_id``, ``match_key``,
  ``room_id``, ``password``, ``submitted_by``,
  ``data_message_id`` (карточка в канале данных),
  ``notification_message_id`` (уведомление в общем чате),
  ``created_at``, ``updated_at``.
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

    @staticmethod
    def _record_ts(room: dict[str, Any]) -> float:
        """Отметка актуальности записи (новые → старые поля)."""
        for field in ("updated_at", "created_at", "submitted_at"):
            try:
                return float(room.get(field) or 0)
            except (TypeError, ValueError):
                continue
        return 0.0

    def get_active_room(self, guild_id: int, channel_id: int,
                        key: str) -> dict[str, Any] | None:
        """Актуальная запись комнаты матча (последняя по времени).

        Все старые записи сохраняются в get_rooms() — выбор
        актуальной детерминирован: max(updated_at|created_at|
        submitted_at). Записи разных матчей не смешиваются —
        scope включает tournament_id:phase:index.
        """
        rooms = self.get_rooms(guild_id, channel_id, key)
        if not rooms:
            return None
        return max(rooms, key=self._record_ts)

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
        *,
        tournament_id: str = "",
    ) -> dict[str, Any]:
        """Создать или обновить АКТУАЛЬНУЮ комнату матча.

        Одна запись на match_key: обновляется запись, признанная
        актуальной (max timestamp); исторические записи старого
        формата (room_no>1 и т.п.) не трогаем — они остаются
        в get_rooms() для восстановления, но не являются
        актуальными. Поля публикации (data_message_id,
        notification_message_id, card_pending, notify_required)
        сохраняются при обновлении — карточка редактируется,
        а не пересоздаётся.
        """
        scope = self._scope(guild_id, channel_id, key)
        entry = self._data.setdefault(scope, {"rooms": []})
        rooms: list[dict[str, Any]] = entry["rooms"]
        now = time.time()

        # Актуальная запись — с максимальной отметкой времени.
        target = None
        if rooms:
            target = max(rooms, key=self._record_ts)

        if target is not None:
            existed = bool(target.get("room_id"))
            # Поля публикации сохраняем — сообщение карточки
            # будет отредактировано, а не создано заново.
            publish_fields = {
                k: target[k] for k in (
                    "data_message_id", "notification_message_id",
                    "card_pending", "notify_required",
                ) if k in target
            }
            target.update({
                "room_no": int(target.get("room_no", room_no)),
                "tournament_id": tournament_id or target.get(
                    "tournament_id", ""),
                "match_key": key,
                "room_id": room_id,
                "password": password,
                "submitted_by": target.get("submitted_by", submitted_by),
                "submitted_at": target.get("submitted_at", now),
                "created_at": target.get(
                    "created_at", target.get("submitted_at", now)),
                "updated_by": submitted_by,
                "updated_at": now,
                **publish_fields,
            })
            self._save()
            return {"room": target, "created": not existed}

        room = {
            "room_no": room_no,
            "tournament_id": tournament_id,
            "match_key": key,
            "room_id": room_id,
            "password": password,
            "submitted_by": submitted_by,
            "submitted_at": now,
            "created_at": now,
            "updated_by": submitted_by,
            "updated_at": now,
            # Первая публикация требует карточку + уведомление.
            "notify_required": True,
        }
        rooms.append(room)
        self._save()
        return {"room": room, "created": True}

    def set_publish_state(
        self,
        guild_id: int,
        channel_id: int,
        key: str,
        *,
        data_message_id: int | None = None,
        notification_message_id: int | None = None,
        card_pending: bool | None = None,
        notify_required: bool | None = None,
    ) -> None:
        """Обновить поля публикации актуальной записи матча."""
        room = self.get_active_room(guild_id, channel_id, key)
        if room is None:
            return
        scope = self._scope(guild_id, channel_id, key)
        entry = self._data.get(scope)
        if entry is None:
            return
        # Изменяем сам объект записи в хранилище (get_rooms
        # возвращает копии — ищем по идентичности полей).
        stored = max(
            entry.get("rooms", []), key=self._record_ts)
        if data_message_id is not None:
            stored["data_message_id"] = int(data_message_id)
        if notification_message_id is not None:
            stored["notification_message_id"] = int(
                notification_message_id)
        if card_pending is not None:
            if card_pending:
                stored["card_pending"] = True
            else:
                stored.pop("card_pending", None)
        if notify_required is not None:
            if notify_required:
                stored["notify_required"] = True
            else:
                stored.pop("notify_required", None)
        self._save()

    def pending_rooms(self) -> list[dict[str, Any]]:
        """Все записи с card_pending — для recovery после рестарта.

        Возвращает dict с guild_id/channel_id/key/room —
        вызывающий код поднимает турнир из store по scope.
        """
        out: list[dict[str, Any]] = []
        for scope, entry in self._data.items():
            parts = scope.split(":", 2)
            if len(parts) != 3:
                continue
            try:
                gid, cid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            key = parts[2]
            room = self.get_active_room(gid, cid, key)
            if room and room.get("card_pending"):
                out.append({
                    "guild_id": gid,
                    "channel_id": cid,
                    "key": key,
                    "room": room,
                })
        return out

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
