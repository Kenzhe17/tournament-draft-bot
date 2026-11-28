"""JSON-хранилище состояния BR турниров.

Активные турниры хранятся по ключу "guild_id:channel_id".
Завершённые/отменённые архивируются в history — остаются доступны
для Hall of Fame и истории, но не занимают канал.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from config import DATA_DIR
from models.br_tournament import BRTournament, BRTournamentPhase
from storage.json_atomic import atomic_write_json

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

BR_DATA_FILE = DATA_DIR / "br_tournaments.json"

# Фазы, после которых турнир уходит в архив.
ARCHIVE_PHASES = {
    BRTournamentPhase.RESULTS,
    BRTournamentPhase.COMPLETE,
    BRTournamentPhase.CANCELLED,
}


class BRStore:
    """Сохранение и загрузка BR турниров в JSON-файл."""

    def __init__(self) -> None:
        self._tournaments: dict[tuple[int, int], BRTournament] = {}
        self._history: list[BRTournament] = []
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить все BR турниры из файла."""
        if not BR_DATA_FILE.exists():
            self._tournaments = {}
            self._history = []
            return

        try:
            with open(BR_DATA_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)

            active_raw: dict
            history_raw: list
            if isinstance(raw, dict) and ("active" in raw or "history" in raw):
                active_raw = raw.get("active", {}) or {}
                history_raw = raw.get("history", []) or []
            else:
                # legacy-плоский формат {"gid:cid": {...}}
                active_raw = raw if isinstance(raw, dict) else {}
                history_raw = []

            loaded: dict[tuple[int, int], BRTournament] = {}
            for key, data in active_raw.items():
                parts = str(key).split(":")
                if len(parts) == 2:
                    gid, cid = int(parts[0]), int(parts[1])
                    loaded[(gid, cid)] = BRTournament.from_dict(data)
            self._tournaments = loaded

            history: list[BRTournament] = []
            for data in history_raw:
                if isinstance(data, dict):
                    history.append(BRTournament.from_dict(data))
            # legacy-турниры в архивных фазах тоже уходят в history
            for (gid, cid), t in list(self._tournaments.items()):
                if t.phase in ARCHIVE_PHASES:
                    history.append(t)
                    del self._tournaments[(gid, cid)]
            self._history = history
            logger.info(
                "Загружено %d активных и %d архивных BR турниров",
                len(self._tournaments), len(self._history),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Ошибка загрузки BR JSON: %s", exc)
            self._tournaments = {}
            self._history = []

    def save(self) -> None:
        """Сохранить все BR турниры в файл."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = {
            "active": {
                f"{gid}:{cid}": t.to_dict()
                for (gid, cid), t in self._tournaments.items()
            },
            "history": [t.to_dict() for t in self._history],
        }
        atomic_write_json(BR_DATA_FILE, raw)

    def get(self, guild_id: int, channel_id: int) -> BRTournament | None:
        """Получить активный BR турнир по guild_id и channel_id."""
        return self._tournaments.get((guild_id, channel_id))

    def get_any(self, guild_id: int, channel_id: int) -> BRTournament | None:
        """Активный турнир канала, иначе последний архивный (для карточек)."""
        t = self._tournaments.get((guild_id, channel_id))
        if t is not None:
            return t
        for h in reversed(self._history):
            if h.guild_id == guild_id and h.channel_id == channel_id:
                return h
        return None

    def set(self, br_tournament: BRTournament) -> None:
        """Сохранить или обновить активный BR турнир."""
        if br_tournament.phase in ARCHIVE_PHASES:
            self.archive(br_tournament)
            return
        self._tournaments[(br_tournament.guild_id, br_tournament.channel_id)] = br_tournament
        self.save()

    def archive(self, br_tournament: BRTournament) -> None:
        """Перенести турнир в историю (завершён/отменён)."""
        key = (br_tournament.guild_id, br_tournament.channel_id)
        self._tournaments.pop(key, None)
        # не дублировать в истории
        self._history = [h for h in self._history if h.id != br_tournament.id]
        self._history.append(br_tournament)
        self.save()

    def delete(self, guild_id: int, channel_id: int) -> None:
        """Удалить активный BR турнир по guild_id и channel_id."""
        self._tournaments.pop((guild_id, channel_id), None)
        self.save()

    def all(self) -> list[BRTournament]:
        """Получить все активные BR турниры."""
        return list(self._tournaments.values())

    def history(self) -> list[BRTournament]:
        """Архив завершённых/отменённых турниров."""
        return list(self._history)

    def get_by_guild(self, guild_id: int) -> list[BRTournament]:
        """Получить все активные BR турниры сервера."""
        return [t for (gid, cid), t in self._tournaments.items() if gid == guild_id]

    def get_history_by_guild(self, guild_id: int) -> list[BRTournament]:
        return [t for t in self._history if t.guild_id == guild_id]


# Глобальный экземпляр хранилища
br_store = BRStore()
