"""Хранилище Ready Check («Найден матч») — PostgreSQL + JSON fallback.

Источник истины во время работы — память процесса; изменения
сохраняются в PostgreSQL (если доступен) либо в JSON-файл.
Все переходы состояний выполняются под asyncio.Lock на match_id
в MMCog, а критический переход READY_CHECK -> ALL_READY дополнительно
страхуется условным UPDATE в PostgreSQL.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from config import DATA_DIR, DATABASE_URL
from models.ready_check import ReadyCheck, ReadyCheckStatus, TERMINAL_STATUSES

logger = logging.getLogger(__name__)

RC_FILE = DATA_DIR / "ready_checks.json"


class ReadyCheckStore:
    """Хранилище проверок готовности."""

    def __init__(self) -> None:
        self._checks: dict[str, ReadyCheck] = {}
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
                logger.error("ReadyCheckStore: не удалось загрузить из PostgreSQL: %s", exc)
        self._load_json()

    async def _load_pg(self) -> None:
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT match_id, guild_id, search_channel_id, tournament_channel_id,"
                " status, deadline, channel_id, message_id, match_channel_id,"
                " cancel_reason, created_at, match_num, players"
                " FROM ready_checks"
            )
        for row in rows:
            data = dict(row)
            players = data.pop("players")
            if isinstance(players, str):
                players = json.loads(players)
            rc = ReadyCheck.from_dict({**data, "players": players})
            self._checks[rc.match_id] = rc
        logger.info("ReadyCheckStore: загружено %d проверок из PostgreSQL", len(rows))

    def _load_json(self) -> None:
        if not RC_FILE.exists():
            return
        try:
            raw = json.loads(RC_FILE.read_text(encoding="utf-8"))
            for match_id, data in raw.items():
                self._checks[match_id] = ReadyCheck.from_dict(data)
            logger.info("ReadyCheckStore: загружено %d проверок из JSON", len(self._checks))
        except Exception as exc:
            logger.error("ReadyCheckStore: ошибка загрузки JSON: %s", exc)
            self._checks = {}

    def _save_json(self) -> None:
        try:
            raw = {mid: rc.to_dict() for mid, rc in self._checks.items()}
            RC_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.error("ReadyCheckStore: ошибка сохранения JSON: %s", exc)

    async def _persist_pg(self, rc: ReadyCheck) -> None:
        """Upsert записи. Бросает исключение при ошибке БД."""
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ready_checks
                    (match_id, guild_id, search_channel_id, tournament_channel_id,
                     status, deadline, channel_id, message_id, match_channel_id,
                     cancel_reason, created_at, match_num, players)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb)
                ON CONFLICT (match_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    deadline = EXCLUDED.deadline,
                    channel_id = EXCLUDED.channel_id,
                    message_id = EXCLUDED.message_id,
                    match_channel_id = EXCLUDED.match_channel_id,
                    cancel_reason = EXCLUDED.cancel_reason,
                    players = EXCLUDED.players
                """,
                rc.match_id, rc.guild_id, rc.search_channel_id,
                rc.tournament_channel_id, rc.status.value, rc.deadline,
                rc.channel_id, rc.message_id, rc.match_channel_id,
                rc.cancel_reason, rc.created_at, rc.match_num,
                json.dumps([p.to_dict() for p in rc.players], ensure_ascii=False),
            )

    async def _persist(self, rc: ReadyCheck) -> None:
        """Сохранить с fallback на JSON при недоступности PostgreSQL."""
        if DATABASE_URL:
            try:
                await self._persist_pg(rc)
                return
            except Exception as exc:
                logger.error("ReadyCheckStore: PostgreSQL persist failed (%s), JSON fallback", exc)
        self._save_json()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(self, rc: ReadyCheck) -> bool:
        """Создать проверку. False если match_id занят или для поиска уже есть активная."""
        await self._ensure_loaded()
        if rc.match_id in self._checks:
            return False
        # PG-уровень: частичный UNIQUE INDEX uq_ready_checks_active_search
        # запрещает два активных RC на один поиск даже между процессами.
        # Проверяем заранее, чтобы не записать в память то, что БД
        # отклонит (иначе RC существовал бы только в JSON-fallback).
        if DATABASE_URL:
            try:
                from storage.db import get_pool

                pool = await get_pool()
                async with pool.acquire() as conn:
                    dup = await conn.fetchval(
                        "SELECT match_id FROM ready_checks"
                        " WHERE guild_id = $1 AND search_channel_id = $2"
                        " AND status NOT IN"
                        " ('LAUNCHED','DECLINED','TIMEOUT','CANCELLED')"
                        " LIMIT 1",
                        rc.guild_id, rc.search_channel_id,
                    )
                if dup is not None:
                    logger.warning(
                        "ReadyCheckStore.create: active RC %s already exists"
                        " for search %s/%s — rejecting %s",
                        dup, rc.guild_id, rc.search_channel_id, rc.match_id,
                    )
                    return False
            except Exception as exc:
                logger.warning("ReadyCheckStore.create PG pre-check failed: %s", exc)
        for existing in self._checks.values():
            if (
                existing.guild_id == rc.guild_id
                and existing.search_channel_id == rc.search_channel_id
                and existing.status not in TERMINAL_STATUSES
            ):
                return False
        self._checks[rc.match_id] = rc
        await self._persist(rc)
        return True

    async def get(self, match_id: str) -> ReadyCheck | None:
        await self._ensure_loaded()
        return self._checks.get(match_id)

    async def get_active_by_search(self, guild_id: int, search_channel_id: int) -> ReadyCheck | None:
        await self._ensure_loaded()
        for rc in self._checks.values():
            if (
                rc.guild_id == guild_id
                and rc.search_channel_id == search_channel_id
                and rc.status not in TERMINAL_STATUSES
            ):
                return rc
        return None

    async def list_active(self) -> list[ReadyCheck]:
        """Все незавершённые проверки (для recovery и sweeper'а)."""
        await self._ensure_loaded()
        return [rc for rc in self._checks.values() if rc.status not in TERMINAL_STATUSES]

    async def list_all(self) -> list[ReadyCheck]:
        await self._ensure_loaded()
        return list(self._checks.values())

    async def save(self, rc: ReadyCheck) -> None:
        """Сохранить изменённое состояние."""
        await self._ensure_loaded()
        self._checks[rc.match_id] = rc
        await self._persist(rc)

    async def try_transition(
        self, match_id: str, from_status: ReadyCheckStatus, to_status: ReadyCheckStatus
    ) -> bool:
        """Атомарно перевести статус from -> to. True если переход совершён нами."""
        rc = await self.get(match_id)
        if rc is None or rc.status != from_status:
            return False
        rc.status = to_status
        await self.save(rc)
        return True

    async def delete(self, match_id: str) -> None:
        await self._ensure_loaded()
        self._checks.pop(match_id, None)
        if DATABASE_URL:
            try:
                from storage.db import get_pool

                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute("DELETE FROM ready_checks WHERE match_id = $1", match_id)
                return
            except Exception as exc:
                logger.error("ReadyCheckStore: PG delete failed: %s", exc)
        self._save_json()



ready_check_store = ReadyCheckStore()
