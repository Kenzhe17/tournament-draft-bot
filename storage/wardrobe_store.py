"""Хранилище гардероба игроков (предметы из каталога Free Fire).

JSON-файл data/wardrobes.json:
{
  "guild_id:user_id": {
    "<item_id>": {"equipped": false, "obtained_at": "iso"},
    ...
  }
}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from config import DATA_DIR  # noqa: E402 — единый путь из config
FILE = DATA_DIR / "wardrobes.json"


class WardrobeStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._mtime: float | None = None
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    # ------------------------------------------------------------------
    def _key(self, guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"

    def _file_mtime(self) -> float | None:
        try:
            return FILE.stat().st_mtime
        except OSError:
            return None

    def _refresh_if_changed(self) -> None:
        """Подхватить внешние изменения файла (CLI-сброс и т.п.)."""
        if self._file_mtime() != self._mtime:
            self._load()

    def _load(self) -> None:
        self._data = {}
        self._mtime = self._file_mtime()
        if self._mtime is None:
            return
        try:
            self._data = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Не удалось загрузить wardrobes.json")
            self._data = {}

    def _save(self) -> None:
        try:
            FILE.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._mtime = self._file_mtime()
        except Exception:
            logger.exception("Не удалось сохранить wardrobes.json")

    # ------------------------------------------------------------------
    def is_owned(self, guild_id: int, user_id: int, item_id: int) -> bool:
        self._refresh_if_changed()
        return str(item_id) in self._data.get(self._key(guild_id, user_id), {})

    def add(self, guild_id: int, user_id: int, item_id: int) -> bool:
        """Добавить предмет. False — уже есть."""
        self._refresh_if_changed()
        key = self._key(guild_id, user_id)
        wardrobe = self._data.setdefault(key, {})
        sid = str(item_id)
        if sid in wardrobe:
            return False
        wardrobe[sid] = {
            "equipped": False,
            "obtained_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()
        return True

    def get_owned_ids(self, guild_id: int, user_id: int) -> list[int]:
        self._refresh_if_changed()
        wardrobe = self._data.get(self._key(guild_id, user_id), {})
        ids = []
        for sid in wardrobe:
            try:
                ids.append(int(sid))
            except (TypeError, ValueError):
                continue
        return ids

    def count(self, guild_id: int, user_id: int) -> int:
        self._refresh_if_changed()
        return len(self._data.get(self._key(guild_id, user_id), {}))

    def get_equipped_id(self, guild_id: int, user_id: int) -> int | None:
        self._refresh_if_changed()
        wardrobe = self._data.get(self._key(guild_id, user_id), {})
        for sid, meta in wardrobe.items():
            if meta.get("equipped"):
                try:
                    return int(sid)
                except (TypeError, ValueError):
                    continue
        return None

    def set_equipped(self, guild_id: int, user_id: int, item_id: int | None) -> bool:
        """Экипировать предмет как 'образ' (None — снять)."""
        self._refresh_if_changed()
        key = self._key(guild_id, user_id)
        wardrobe = self._data.get(key)
        if wardrobe is None:
            return False
        sid = str(item_id) if item_id is not None else None
        found = False
        for iid, meta in wardrobe.items():
            meta["equipped"] = iid == sid
            if iid == sid:
                found = True
        if item_id is not None and not found:
            return False
        self._save()
        return True

    def remove(self, guild_id: int, user_id: int, item_id: int) -> bool:
        self._refresh_if_changed()
        wardrobe = self._data.get(self._key(guild_id, user_id), {})
        if str(item_id) in wardrobe:
            del wardrobe[str(item_id)]
            self._save()
            return True
        return False


wardrobe_store = WardrobeStore()
