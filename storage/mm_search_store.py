"""Хранилище очередей Ranked Matchmaking — PostgreSQL + JSON fallback.

Источник истины во время работы — память процесса. Чтение синхронное
(get/set/all), запись в PostgreSQL выполняется фоновыми задачами
(asyncio.create_task), JSON-файл остаётся fallback на случай
недоступности БД и для тестов.

Таблица mm_queues создаётся в storage/db.py.
"""

import asyncio
import json
import logging
from pathlib import Path

from config import DATA_DIR, DATABASE_URL
from models.mm_search import MMSearch

logger = logging.getLogger(__name__)

MM_SEARCH_FILE = DATA_DIR / "mm_search.json"


class MMSearchStore:
    """Сохранение и загрузка MM поисков (JSON + PostgreSQL)."""

    def __init__(self) -> None:
        self._searches: dict[tuple[int, int], MMSearch] = {}
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
                    " players, player_user_ids, created_at, timer_started,"
                    " tournament_channel_id, match_num"
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
                self._searches[(search.guild_id, search.channel_id)] = search
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
                    self._searches[(gid, cid)] = MMSearch.from_dict(data)
            logger.info("Загружено %d MM поисков из JSON", len(self._searches))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Ошибка загрузки MM JSON: %s", exc)
            self._searches = {}

    def _save_json(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            raw = {f"{gid}:{cid}": s.to_dict()
                   for (gid, cid), s in self._searches.items()}
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
                     players, player_user_ids, created_at, timer_started,
                     tournament_channel_id, match_num)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,$10,$11)
                ON CONFLICT (guild_id, channel_id) DO UPDATE SET
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
                json.dumps(search.players, ensure_ascii=False),
                json.dumps(
                    {str(k): v for k, v in search.player_user_ids.items()},
                    ensure_ascii=False,
                ),
                search.created_at, search.timer_started,
                search.tournament_channel_id, search.match_num,
            )

    async def _persist_pg_delete(self, guild_id: int, channel_id: int) -> None:
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM mm_queues WHERE guild_id = $1 AND channel_id = $2",
                guild_id, channel_id,
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

    def get(self, guild_id: int, channel_id: int) -> MMSearch | None:
        """Получить поиск по guild_id и channel_id."""
        return self._searches.get((guild_id, channel_id))

    def set(self, search: MMSearch) -> None:
        """Сохранить или обновить поиск."""
        self._searches[(search.guild_id, search.channel_id)] = search
        self._save_json()
        if DATABASE_URL:
            self._schedule_pg(self._persist_pg_one(search))

    def delete(self, guild_id: int, channel_id: int) -> None:
        """Удалить поиск по guild_id и channel_id."""
        self._searches.pop((guild_id, channel_id), None)
        self._save_json()
        if DATABASE_URL:
            self._schedule_pg(self._persist_pg_delete(guild_id, channel_id))

    def all(self) -> list[MMSearch]:
        """Получить все активные поиски."""
        return list(self._searches.values())

    def get_by_guild(self, guild_id: int) -> list[MMSearch]:
        """Получить все поиски сервера."""
        return [s for (gid, cid), s in self._searches.items() if gid == guild_id]


# Глобальный экземпляр хранилища
mm_search_store = MMSearchStore()
