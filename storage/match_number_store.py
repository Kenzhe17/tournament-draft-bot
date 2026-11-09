"""Глобальная нумерация матчей — PostgreSQL + JSON fallback.

Каждому матчу (match_key = "{tournament_id}_{match_type}_{match_index}")
присваивается сквозной номер, начинающийся с 1. Номер выдаётся один раз
при первом обращении (get_or_assign) и больше не меняется.

Таблицы:
- match_counter: одна строка (id=1) со значением счётчика.
- match_numbers: match_key -> number, announced.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from config import DATA_DIR, DATABASE_URL

logger = logging.getLogger(__name__)

NUM_FILE = DATA_DIR / "match_numbers.json"


class MatchNumberStore:
    def __init__(self) -> None:
        self._numbers: dict[str, dict[str, Any]] = {}  # key -> {"number": int, "announced": bool}
        self._counter = 0
        self._loaded = False
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Загрузка / сохранение
    # ------------------------------------------------------------------

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if DATABASE_URL:
            try:
                await self._load_pg()
                return
            except Exception as exc:
                logger.error("MatchNumberStore: не удалось загрузить из PostgreSQL: %s", exc)
        self._load_json()

    async def _load_pg(self) -> None:
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            counter = await conn.fetchval(
                "SELECT value FROM match_counter WHERE id = 1"
            )
            self._counter = int(counter or 0)
            rows = await conn.fetch(
                "SELECT match_key, number, announced FROM match_numbers"
            )
            for row in rows:
                self._numbers[row["match_key"]] = {
                    "number": int(row["number"]),
                    "announced": bool(row["announced"]),
                }
        logger.info(
            "MatchNumberStore: загружено %d матчей, счётчик=%d",
            len(self._numbers), self._counter,
        )

    def _load_json(self) -> None:
        if not NUM_FILE.exists():
            return
        try:
            raw = json.loads(NUM_FILE.read_text(encoding="utf-8"))
            self._counter = int(raw.get("counter", 0))
            for key, val in (raw.get("matches") or {}).items():
                self._numbers[key] = {
                    "number": int(val.get("number", 0)),
                    "announced": bool(val.get("announced", False)),
                }
            logger.info(
                "MatchNumberStore: загружено %d матчей из JSON, счётчик=%d",
                len(self._numbers), self._counter,
            )
        except Exception as exc:
            logger.error("MatchNumberStore: ошибка загрузки JSON: %s", exc)
            self._numbers = {}
            self._counter = 0

    def _save_json(self) -> None:
        try:
            raw = {
                "counter": self._counter,
                "matches": self._numbers,
            }
            NUM_FILE.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.error("MatchNumberStore: ошибка сохранения JSON: %s", exc)

    async def _persist_pg(self) -> None:
        """Полная перезапись в PostgreSQL (объём данных мал)."""
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO match_counter (id, value) VALUES (1, $1)"
                " ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value",
                self._counter,
            )
            for key, val in self._numbers.items():
                await conn.execute(
                    """
                    INSERT INTO match_numbers (match_key, number, announced)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (match_key) DO UPDATE SET
                        number = EXCLUDED.number,
                        announced = EXCLUDED.announced
                    """,
                    key, val["number"], val["announced"],
                )

    async def _persist(self) -> None:
        if DATABASE_URL:
            try:
                await self._persist_pg()
                return
            except Exception as exc:
                logger.error("MatchNumberStore: PostgreSQL persist failed (%s), JSON fallback", exc)
        self._save_json()

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    async def get_or_assign(self, match_key: str) -> int:
        """Вернуть номер матча; при первом обращении выдать следующий."""
        await self._ensure_loaded()
        entry = self._numbers.get(match_key)
        if entry is not None:
            return int(entry["number"])
        self._counter += 1
        self._numbers[match_key] = {"number": self._counter, "announced": False}
        await self._persist()
        return self._counter

    async def assign(self, match_key: str, number: int) -> bool:
        """Пред-привязать существующий номер к ключу матча (без нового номера).

        Используется, когда номер уже выдан на этапе ready-check
        (cs-4x4-match-N), а турнирный match_key появляется позже.
        True если запись создана; False если ключ уже занят.
        """
        await self._ensure_loaded()
        if match_key in self._numbers:
            return False
        self._numbers[match_key] = {"number": int(number), "announced": False}
        self._counter = max(self._counter, int(number))
        await self._persist()
        return True

    async def release(self, match_key: str) -> bool:
        """Освободить номер отменённого матча (не анонсированного).

        Запись удаляется; неанонсированные номера выше сдвигаются
        вниз (без пересечения с анонсированными), счётчик
        пересчитывается — следующий матч получит освобождённый
        номер. Анонсированные номера не трогаем: их уже видели.
        """
        await self._ensure_loaded()
        entry = self._numbers.get(match_key)
        if entry is None or entry.get("announced"):
            return False
        num = int(entry["number"])
        del self._numbers[match_key]
        # Номер переиспользуется, только если он был последним выданным —
        # тогда счётчик откатывается и следующий матч получит его же.
        # «Дыры» от отмены более старых матчей не заполняем: их номера
        # могли уже отображаться на карточках Ready Check.
        if num >= self._counter:
            self._counter = max(
                (int(e["number"]) for e in self._numbers.values()),
                default=0,
            )
        await self._persist()
        return True

    async def get(self, match_key: str) -> int | None:
        await self._ensure_loaded()
        entry = self._numbers.get(match_key)
        return int(entry["number"]) if entry else None

    async def is_announced(self, match_key: str) -> bool:
        await self._ensure_loaded()
        entry = self._numbers.get(match_key)
        return bool(entry and entry["announced"])

    async def mark_announced(self, match_key: str) -> None:
        await self._ensure_loaded()
        entry = self._numbers.get(match_key)
        if entry is None:
            return
        if entry["announced"]:
            return
        entry["announced"] = True
        await self._persist()


match_number_store = MatchNumberStore()
