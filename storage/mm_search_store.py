"""Хранилище очередей Ranked Matchmaking — PostgreSQL + JSON fallback.

Источник истины во время работы — память процесса. Чтение синхронное
(get/set/all), запись в PostgreSQL выполняется фоновыми задачами
(asyncio.create_task), JSON-файл остаётся fallback на случай
недоступности БД и для тестов.

Ключ очереди — (guild_id, channel_id, queue_type): в одном канале
могут жить две очереди («Обычка» и «БЕЗ РУИН»). queue_type по
умолчанию "normal" — старые вызовы get/set/delete без него работают
с обычной очередью.

Таблица mm_queues создаётся в storage/db.py.
"""

import asyncio
import json
import logging
from pathlib import Path

from config import DATA_DIR, DATABASE_URL
from models.mm_search import MMSearch, QUEUE_TYPE_NORMAL

logger = logging.getLogger(__name__)

MM_SEARCH_FILE = DATA_DIR / "mm_search.json"


def _key(guild_id: int, channel_id: int, queue_type: str) -> tuple[int, int, str]:
    return (guild_id, channel_id, queue_type or QUEUE_TYPE_NORMAL)


class MMSearchStore:
    """Сохранение и загрузка MM поисков (JSON + PostgreSQL)."""

    def __init__(self) -> None:
        self._searches: dict[tuple[int, int, str], MMSearch] = {}
        self._loaded = False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # JSON загружаем сразу — синхронный API остаётся рабочим
        # даже до вызова async load() (например, в тестах без БД).
        self._load_json()

    # ------------------------------------------------------------------
    # Загрузка
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """Загрузить очереди из PostgreSQL (при наличии DATABASE_URL)."""
        if self._loaded:
            return
        self._loaded = True
        if not DATABASE_URL:
            return
        try:
            from storage.db import get_pool

            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT guild_id, channel_id, message_id, format, state,"
                    " queue_type, players, player_user_ids, created_at,"
                    " timer_started, tournament_channel_id, match_num"
                    " FROM mm_queues"
                )
            self._searches = {}
            for row in rows:
                data = dict(row)
                players = data.pop("players")
                player_user_ids = data.pop("player_user_ids")
                if isinstance(players, str):
                    players = json.loads(players)
                if isinstance(player_user_ids, str):
                    player_user_ids = json.loads(player_user_ids)
                search = MMSearch.from_dict({
                    **data,
                    "players": players or [],
                    "player_user_ids": player_user_ids or {},
                })
                self._searches[_key(
                    search.guild_id, search.channel_id, search.queue_type
                )] = search
            logger.info("MMSearchStore: загружено %d очередей из PostgreSQL",
                        len(self._searches))
        except Exception as exc:
            logger.error("MMSearchStore: не удалось загрузить из PostgreSQL: %s", exc)

    def _load_json(self) -> None:
        """Загрузить поиски из JSON-файла (fallback)."""
        if not MM_SEARCH_FILE.exists():
            return
        try:
            with open(MM_SEARCH_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for key, data in raw.items():
                parts = key.split(":")
                if len(parts) == 2:
                    gid, cid = int(parts[0]), int(parts[1])
                    qtype = QUEUE_TYPE_NORMAL
                elif len(parts) == 3:
                    gid, cid = int(parts[0]), int(parts[1])
                    qtype = parts[2] or QUEUE_TYPE_NORMAL
                else:
                    continue
                search = MMSearch.from_dict(data)
                qt = search.queue_type or qtype
                search.queue_type = qt
                self._searches[_key(gid, cid, qt)] = search
            logger.info("Загружено %d MM поисков из JSON", len(self._searches))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Ошибка загрузки MM JSON: %s", exc)
            self._searches = {}

    def _save_json(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            raw = {
                f"{gid}:{cid}:{qt}": s.to_dict()
                for (gid, cid, qt), s in self._searches.items()
            }
            with open(MM_SEARCH_FILE, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error("MMSearchStore: ошибка сохранения JSON: %s", exc)

    # ------------------------------------------------------------------
    # PostgreSQL persist (фоновый)
    # ------------------------------------------------------------------

    @staticmethod
    async def _persist_pg_one(search: MMSearch) -> None:
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO mm_queues
                    (guild_id, channel_id, message_id, format, state,
                     queue_type, players, player_user_ids, created_at,
                     timer_started, tournament_channel_id, match_num)
                VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9,$10,$11,$12)
                ON CONFLICT (guild_id, channel_id, queue_type) DO UPDATE SET
                    message_id = EXCLUDED.message_id,
                    format = EXCLUDED.format,
                    state = EXCLUDED.state,
                    players = EXCLUDED.players,
                    player_user_ids = EXCLUDED.player_user_ids,
                    timer_started = EXCLUDED.timer_started,
                    tournament_channel_id = EXCLUDED.tournament_channel_id,
                    match_num = EXCLUDED.match_num
                """,
                search.guild_id, search.channel_id, search.message_id,
                search.format.value, search.state,
                search.queue_type or QUEUE_TYPE_NORMAL,
                json.dumps(search.players, ensure_ascii=False),
                json.dumps(
                    {str(k): v for k, v in search.player_user_ids.items()},
                    ensure_ascii=False,
                ),
                search.created_at, search.timer_started,
                search.tournament_channel_id, search.match_num,
            )

    async def _persist_pg_delete(
        self, guild_id: int, channel_id: int, queue_type: str
    ) -> None:
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM mm_queues"
                " WHERE guild_id = $1 AND channel_id = $2 AND queue_type = $3",
                guild_id, channel_id, queue_type or QUEUE_TYPE_NORMAL,
            )

    def _schedule_pg(self, coro) -> None:
        """Запустить PG-persist в фоне; без event loop — пропускаем."""
        if not DATABASE_URL:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(coro)
        task.add_done_callback(self._on_pg_task_done)

    @staticmethod
    def _on_pg_task_done(task: asyncio.Task) -> None:
        try:
            task.result()
        except Exception as exc:
            logger.error("MMSearchStore: PostgreSQL persist failed: %s", exc)

    # ------------------------------------------------------------------
    # CRUD (синхронный API поверх памяти)
    # ------------------------------------------------------------------

    def get(
        self,
        guild_id: int,
        channel_id: int,
        queue_type: str = QUEUE_TYPE_NORMAL,
    ) -> MMSearch | None:
        """Получить поиск по guild_id, channel_id и типу очереди."""
        return self._searches.get(_key(guild_id, channel_id, queue_type))

    def set(self, search: MMSearch) -> None:
        """Сохранить или обновить поиск."""
        qt = search.queue_type or QUEUE_TYPE_NORMAL
        search.queue_type = qt
        self._searches[_key(search.guild_id, search.channel_id, qt)] = search
        self._save_json()
        if DATABASE_URL:
            self._schedule_pg(self._persist_pg_one(search))

    def delete(
        self,
        guild_id: int,
        channel_id: int,
        queue_type: str = QUEUE_TYPE_NORMAL,
    ) -> None:
        """Удалить поиск по guild_id, channel_id и типу очереди."""
        self._searches.pop(_key(guild_id, channel_id, queue_type), None)
        self._save_json()
        if DATABASE_URL:
            self._schedule_pg(
                self._persist_pg_delete(guild_id, channel_id, queue_type)
            )

    def all(self) -> list[MMSearch]:
        """Получить все активные поиски."""
        return list(self._searches.values())

    def get_by_guild(self, guild_id: int) -> list[MMSearch]:
        """Получить все поиски сервера."""
        return [s for (gid, _cid, _qt), s in self._searches.items() if gid == guild_id]

    def get_by_channel(self, guild_id: int, channel_id: int) -> dict[str, MMSearch]:
        """Все очереди канала: {queue_type: MMSearch}."""
        return {
            qt: s
            for (gid, cid, qt), s in self._searches.items()
            if gid == guild_id and cid == channel_id
        }


# Глобальный экземпляр хранилища
mm_search_store = MMSearchStore()
