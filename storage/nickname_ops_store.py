"""Хранилище операций платной смены ника (JSON + atomic writes).

Каждая покупка услуги ``nickname_change`` — отдельная операция
с уникальным ``op_id`` и явным статусом:

- ``reserved``  — монеты зарезервированы (списаны), Discord edit
  ещё не подтверждён или результат неизвестен;
- ``applied``   — ник успешно изменён, списание финально;
- ``refunded``  — операция отменена/не удалась, монеты возвращены;
- ``unknown``   — исход обращения к Discord неизвестен (сетевой
  сбой); разрешается проверкой фактического ника или recovery
  при перезапуске.

Статус меняется только вперёд и атомарно (load→modify→atomic
write под asyncio.Lock) — двойной возврат/списание исключены
на уровне одной записи. Возврат монет дополнительно защищён
идемпотентным ``add_balance_once`` по ключу ``nickref:{op_id}``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from config import DATA_DIR
from storage.json_atomic import atomic_write_json, load_json_file

OPS_FILE = DATA_DIR / "nickname_ops.json"

STATUS_RESERVED = "reserved"
STATUS_APPLIED = "applied"
STATUS_REFUNDED = "refunded"
STATUS_UNKNOWN = "unknown"

# Статусы, при которых операция ещё может быть «в полёте».
ACTIVE_STATUSES = {STATUS_RESERVED, STATUS_UNKNOWN}


class NicknameOpsStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._ops: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------- io

    def _load(self) -> None:
        raw = load_json_file(OPS_FILE)
        if isinstance(raw, dict):
            self._ops = {
                str(k): v for k, v in raw.items()
                if isinstance(v, dict)
            }
        else:
            self._ops = {}

    def _save(self) -> None:
        atomic_write_json(OPS_FILE, self._ops)

    # ---------------------------------------------------------- queries

    def get(self, op_id: str) -> dict[str, Any] | None:
        return self._ops.get(str(op_id))

    def active_op_for_user(
        self, guild_id: int, user_id: int
    ) -> dict[str, Any] | None:
        """Незавершённая операция пользователя (reserved/unknown)."""
        for op in self._ops.values():
            if (
                op.get("guild_id") == guild_id
                and op.get("user_id") == user_id
                and op.get("status") in ACTIVE_STATUSES
            ):
                return op
        return None

    def unresolved_ops(self) -> list[dict[str, Any]]:
        """Все операции, требующие восстановления после рестарта."""
        return [
            op for op in self._ops.values()
            if op.get("status") in ACTIVE_STATUSES
        ]

    # ----------------------------------------------------------- mutate

    async def create(
        self,
        *,
        guild_id: int,
        user_id: int,
        old_nick: str,
        new_nick: str,
        price: int,
    ) -> dict[str, Any]:
        """Создать операцию в статусе reserved. Возвращает op dict."""
        async with self._lock:
            op_id = uuid.uuid4().hex
            now = time.time()
            op = {
                "op_id": op_id,
                "guild_id": guild_id,
                "user_id": user_id,
                "old_nick": old_nick,
                "new_nick": new_nick,
                "price": price,
                "status": STATUS_RESERVED,
                # Флаг «монеты списаны»: выставляется сразу после
                # успешного subtract_balance. При recovery операции
                # без флага трактуются консервативно (возврат), чтобы
                # пользователь никогда не потерял монеты.
                "charged": False,
                "created_at": now,
                "updated_at": now,
            }
            self._ops[op_id] = op
            self._save()
            return dict(op)

    async def mark_charged(self, op_id: str) -> None:
        """Отметить, что резервирование монет подтверждено."""
        async with self._lock:
            op = self._ops.get(str(op_id))
            if op is not None and op.get("status") in ACTIVE_STATUSES:
                op["charged"] = True
                op["updated_at"] = time.time()
                self._save()

    async def set_status(
        self, op_id: str, status: str, **fields: Any
    ) -> dict[str, Any] | None:
        """Перевести операцию в новый статус (с дополнительными полями).

        Переходы разрешены только из ACTIVE_STATUSES — повторный
        перевод завершённой операции игнорируется (защита от
        двойного возврата/подтверждения).
        """
        async with self._lock:
            op = self._ops.get(str(op_id))
            if op is None:
                return None
            if op.get("status") not in ACTIVE_STATUSES:
                return dict(op)
            op["status"] = status
            op["updated_at"] = time.time()
            op.update(fields)
            self._save()
            return dict(op)

    async def mark_unknown(self, op_id: str, **fields: Any) -> None:
        await self.set_status(op_id, STATUS_UNKNOWN, **fields)


nickname_ops_store = NicknameOpsStore()
