"""Архив завершённых турниров.

Завершённый турнир (TournamentPhase.COMPLETE) нельзя удалить —
результаты, ELO, статистика и история матчей сохраняются.
При создании нового турнира в том же канале завершённая запись
архивируется сюда и слот канала освобождается.

Хранилище: DATA_DIR/tournament_archive.json — список снапшотов
{"tournament_id", "guild_id", "channel_id", "archived_at", "data"}.
Запись идемпотентна по tournament_id: повторное архивирование
заменяет запись, а не дублирует её.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATA_DIR
from storage.json_atomic import atomic_write_text, load_json_file

logger = logging.getLogger(__name__)

ARCHIVE_FILE = DATA_DIR / "tournament_archive.json"


class TournamentArchive:
    """Потокобезопасный архив завершённых турниров (single-file JSON)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
        self._loaded = False

    # ------------------------------------------------------------------
    # Загрузка/сохранение
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        raw = load_json_file(ARCHIVE_FILE)
        if isinstance(raw, list):
            self._entries = [e for e in raw if isinstance(e, dict)]
        elif isinstance(raw, dict) and isinstance(raw.get("entries"), list):
            self._entries = [e for e in raw["entries"] if isinstance(e, dict)]

    def _save_unlocked(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"entries": self._entries}, ensure_ascii=False, indent=1
        )
        atomic_write_text(ARCHIVE_FILE, payload)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def archive(self, tournament) -> bool:
        """Сохранить завершённый турнир в архив (идемпотентно).

        Возвращает True, если запись добавлена/обновлена.
        """
        try:
            data = tournament.to_dict()
        except Exception as exc:
            logger.error("archive: to_dict failed for %s: %s",
                         getattr(tournament, "id", "?"), exc)
            return False
        entry = {
            "tournament_id": str(getattr(tournament, "id", "")),
            "guild_id": int(getattr(tournament, "guild_id", 0)),
            "channel_id": int(getattr(tournament, "channel_id", 0)),
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        with self._lock:
            self._ensure_loaded()
            tid = entry["tournament_id"]
            self._entries = [
                e for e in self._entries
                if str(e.get("tournament_id", "")) != tid
            ]
            self._entries.append(entry)
            try:
                self._save_unlocked()
            except Exception as exc:
                logger.error("archive: save failed: %s", exc)
                return False
        logger.info(
            "Турнир %s (%s:%s) заархивирован",
            tid, entry["guild_id"], entry["channel_id"],
        )
        return True

    def list(self, guild_id: int | None = None) -> list[dict[str, Any]]:
        """Все записи архива (или только гильдии)."""
        with self._lock:
            self._ensure_loaded()
            entries = list(self._entries)
        if guild_id is not None:
            entries = [e for e in entries if e.get("guild_id") == guild_id]
        return entries

    def get(self, tournament_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_loaded()
            for e in self._entries:
                if str(e.get("tournament_id", "")) == str(tournament_id):
                    return e
        return None

    def is_archived(self, tournament_id: str) -> bool:
        return self.get(tournament_id) is not None


tournament_archive = TournamentArchive()
