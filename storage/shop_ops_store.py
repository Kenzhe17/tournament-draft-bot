"""Журнал операций магазина и список избранного.

Один JSON-файл `data/shop_ops.json`:
{
  "ops": [ {op_id, guild_id, user_id, kind, item_id, price, status,
            detail, created_at} ... ],          # последние OPS_KEEP
  "favorites": { "guild:user": [item_id, ...] }
}

Журнал — источник «Мои покупки» и админ-статистики. Записи
добавляются после завершения операции; статусы:
reserved → done | refunded | failed.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from config import DATA_DIR
from storage.json_atomic import atomic_write_json

OPS_FILE = DATA_DIR / "shop_ops.json"
OPS_KEEP = 5000


class ShopOpsStore:
    def __init__(self) -> None:
        self._ops: list[dict[str, Any]] = []
        self._favorites: dict[str, list[str]] = {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        self._ops = []
        self._favorites = {}
        if not OPS_FILE.exists():
            return
        try:
            data = json.loads(OPS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(data, dict):
            ops = data.get("ops")
            if isinstance(ops, list):
                self._ops = [o for o in ops if isinstance(o, dict)]
            fav = data.get("favorites")
            if isinstance(fav, dict):
                self._favorites = {
                    str(k): [str(i) for i in v]
                    for k, v in fav.items()
                    if isinstance(v, list)
                }

    def save(self) -> None:
        atomic_write_json(OPS_FILE, {
            "ops": self._ops[-OPS_KEEP:],
            "favorites": self._favorites,
        })

    # ------------------------------------------------------------------ ops

    @staticmethod
    def _key(guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"

    def new_op_id(self) -> str:
        return uuid.uuid4().hex[:16]

    def record(
        self,
        *,
        guild_id: int,
        user_id: int,
        kind: str,
        item_id: str,
        price: int,
        status: str = "done",
        detail: str = "",
        op_id: str | None = None,
    ) -> dict[str, Any]:
        op = {
            "op_id": op_id or self.new_op_id(),
            "guild_id": guild_id,
            "user_id": user_id,
            "kind": kind,
            "item_id": item_id,
            "price": price,
            "status": status,
            "detail": detail,
            "created_at": int(time.time()),
        }
        self._ops.append(op)
        self.save()
        return op

    def update_status(self, op_id: str, status: str, detail: str = "") -> None:
        for op in reversed(self._ops):
            if op.get("op_id") == op_id:
                op["status"] = status
                if detail:
                    op["detail"] = detail
                self.save()
                return

    def get_for_user(
        self, guild_id: int, user_id: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        key_ops = [
            o for o in self._ops
            if o.get("guild_id") == guild_id and o.get("user_id") == user_id
        ]
        return list(reversed(key_ops))[:limit]

    def recent(self, guild_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        ops = self._ops
        if guild_id is not None:
            ops = [o for o in ops if o.get("guild_id") == guild_id]
        return list(reversed(ops))[-limit:]

    # ------------------------------------------------------------- favorites

    def favorites(self, guild_id: int, user_id: int) -> list[str]:
        return list(self._favorites.get(self._key(guild_id, user_id), []))

    def is_favorite(self, guild_id: int, user_id: int, item_id: str) -> bool:
        return item_id in self.favorites(guild_id, user_id)

    def toggle_favorite(
        self, guild_id: int, user_id: int, item_id: str
    ) -> bool:
        """Переключить избранное. True = теперь в избранном."""
        key = self._key(guild_id, user_id)
        favs = self._favorites.setdefault(key, [])
        if item_id in favs:
            favs.remove(item_id)
            added = False
        else:
            favs.append(item_id)
            added = True
        self.save()
        return added


shop_ops_store = ShopOpsStore()
