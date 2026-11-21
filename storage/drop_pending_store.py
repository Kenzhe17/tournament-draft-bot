"""Долговечная файловая очередь обязательств Coin Drop.

Используется ТОЛЬКО когда PostgreSQL недоступен в момент
фиксации победителя: атомарная JSON-запись (tmp+fsync+replace)
гарантирует, что обязательство переживает рестарт процесса —
в отличие от in-memory очереди.

Семантика:
- add(): первый победитель по grant_key сохраняется, повторные
  вызовы с тем же ключом возвращают УЖЕ сохранённого владельца —
  ложное сообщение о победе исключено.
- remove(): вызывается после успешной записи в PG.
- Worker крутит drain: file → drop_reward_store.record → remove.

Это НЕ замена PG-таблице drop_coin_rewards: файл — временный
мост на время недоступности БД. Идемпотентность обеспечивается
grant_key на обоих уровнях.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from config import DATA_DIR
from storage.json_atomic import atomic_write_json, load_json_file

logger = logging.getLogger(__name__)

PENDING_JSON = DATA_DIR / "drop_pending_rewards.json"


class DropPendingStore:
    """Атомарная JSON-очередь pending-наград (thread/loop-safe:
    все операции синхронны, состояние — dict в памяти)."""

    def __init__(self, path=PENDING_JSON) -> None:
        self._path = path
        self._items: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        data = load_json_file(self._path)
        if isinstance(data, dict):
            self._items = {
                k: v for k, v in data.items() if isinstance(v, dict)
            }
        elif isinstance(data, list):  # защита от старого формата
            self._items = {
                str(v.get("grant_key")): v
                for v in data if isinstance(v, dict) and v.get("grant_key")
            }

    def _save(self) -> None:
        atomic_write_json(
            self._path,
            {k: v for k, v in self._items.items()},
        )

    def add(self, item: dict[str, Any]) -> dict[str, Any]:
        """Зафиксировать обязательство. Возвращает фактически
        сохранённого владельца: при повторном grant_key —
        ПЕРВОГО (повторная выплата невозможна)."""
        key = str(item["grant_key"])
        with self._lock:
            self._ensure_loaded()
            existing = self._items.get(key)
            if existing is not None:
                return existing
            stored = dict(item)
            stored.setdefault("queued_at", time.time())
            self._items[key] = stored
            try:
                self._save()
            except OSError:
                # Не смогли сохранить на диск — не оставляем
                # в памяти «фантомное» обязательство.
                self._items.pop(key, None)
                raise
            return stored

    def remove(self, grant_key: str) -> None:
        with self._lock:
            self._ensure_loaded()
            if self._items.pop(str(grant_key), None) is not None:
                try:
                    self._save()
                except OSError:
                    logger.error(
                        "drop_pending remove failed key=%s — "
                        "row will be re-recorded (idempotent)", grant_key)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            return [dict(v) for v in self._items.values()]

    def __len__(self) -> int:
        with self._lock:
            self._ensure_loaded()
            return len(self._items)


drop_pending_store = DropPendingStore()
