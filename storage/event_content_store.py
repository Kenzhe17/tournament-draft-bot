"""PostgreSQL-хранилище контента событийных игр
(Events Content Expansion).

Рабочие копии каталогов (редактируемые через Control Center)
живут здесь; bundled JSON — базовая версия и fallback.

Схема (только аддитивные CREATE IF NOT EXISTS):

* ``event_content_entries`` — запись каталога:
  PK (game, id); payload jsonb + колонки для фильтров.
* ``event_content_catalog`` — метаданные версии на игру.
* ``event_content_cycle_state`` — per (game, guild):
  ``used`` jsonb-массив id, уже выданных в текущем цикле;
  ``cycle_count``; ``last_entry_id``. Одна строка на пару —
  все выборы сериализуются ``FOR UPDATE``.
* ``event_content_draws`` — append-only журнал выдач
  (аудит + «показанное задание» для восстановления).
* ``event_content_reservations`` — lease на выданную запись:
  резервируется при выборе, подтверждается после успешной
  публикации карточки; снимается в finish/fail/TTL.

Политика выбора (TZ §5):
  1. Уровни = distinct difficulty среди enabled, не
     зарезервированных, не входящих в disabled_categories.
  2. Уровень выбирается взвешенно (weights из настроек;
     easy/medium/hard). Все веса <=0 → равные веса
     (задокументированный fallback).
  3. Внутри уровня — случайная запись, не входящая в
     ``used`` текущего цикла и без активной резервации.
  4. Пул уровня исчерпан → следующий цикл (``used``
     очищается, ``cycle_count``++), повтор последнего
     задания исключается, если есть альтернатива.
  5. Запись помечается used + draw + reservation одной
     транзакцией → параллельные выборы не пересекаются.

Политика failed send: карточка могла быть опубликована до
подтверждения — задание НЕ возвращается в пул до TTL
резервации (по умолчанию 10 мин); свипер снимает lease по
завершении/отмене события. Запись из ``draws`` всегда
восстановима по event_id — повторная публикация после
рестарта использует то же задание из config события.
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

DDL_LOCK_KEY = 0xE1E2C0  # 'events-content'

RESERVATION_TTL_SECONDS = 600  # 10 минут — дольше event-окна

DIFFICULTIES = ("easy", "medium", "hard")
DEFAULT_DIFFICULTY_WEIGHTS = {"easy": 40, "medium": 45,
                              "hard": 15}

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS event_content_catalog (
        game            TEXT PRIMARY KEY,
        content_version TEXT NOT NULL DEFAULT '',
        source          TEXT NOT NULL DEFAULT 'bundled',
        entry_count     INTEGER NOT NULL DEFAULT 0,
        imported_at     TIMESTAMPTZ,
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_by      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_content_entries (
        game        TEXT NOT NULL,
        id          TEXT NOT NULL,
        enabled     BOOLEAN NOT NULL DEFAULT TRUE,
        difficulty  TEXT NOT NULL,
        category    TEXT NOT NULL DEFAULT '',
        payload     JSONB NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (game, id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_event_content_entries_game
        ON event_content_entries (game, enabled, difficulty,
                                  category)
    """,
    """
    CREATE TABLE IF NOT EXISTS event_content_cycle_state (
        game          TEXT NOT NULL,
        guild_id      TEXT NOT NULL,
        used          JSONB NOT NULL DEFAULT '[]'::jsonb,
        cycle_count   BIGINT NOT NULL DEFAULT 0,
        last_entry_id TEXT,
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (game, guild_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_content_draws (
        id          BIGSERIAL PRIMARY KEY,
        game        TEXT NOT NULL,
        guild_id    TEXT NOT NULL,
        entry_id    TEXT NOT NULL,
        event_id    TEXT,
        drawn_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_event_content_draws_game
        ON event_content_draws (game, guild_id, id DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS event_content_reservations (
        id          BIGSERIAL PRIMARY KEY,
        game        TEXT NOT NULL,
        guild_id    TEXT NOT NULL,
        entry_id    TEXT NOT NULL,
        event_id    TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at  TIMESTAMPTZ NOT NULL,
        released    BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    # Одна активная резервация на (game, entry_id) — глобально,
    # т.к. запись не должна выдаваться двум серверам сразу
    # (единый каталог; per-guild циклы независимы, но одна
    # и та же загадка в двух ивентах одновременно — плохо).
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_event_content_resv_active
        ON event_content_reservations (game, entry_id)
        WHERE released = FALSE
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_event_content_resv_expiry
        ON event_content_reservations (expires_at)
        WHERE released = FALSE
    """,
]


class EventContentStore:
    """Каталог + циклическая выдача для event games."""

    def __init__(self, pool=None):
        self._pool_obj = pool

    async def _acquire(self):
        """Ленивый пул — как у EventStore (get_pool())."""
        if self._pool_obj is None:
            from storage.db import get_pool
            self._pool_obj = await get_pool()
        return self._pool_obj.acquire()

    # ---------------------------------------------------------- schema

    async def ensure_schema(self) -> None:
        async with await self._acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)", DDL_LOCK_KEY)
            for stmt in DDL_STATEMENTS:
                await conn.execute(stmt)

    # ----------------------------------------------------- import/sync

    async def import_catalog(self, game: str, entries: list[dict],
                             *, source: str = "control_center",
                             version: str = "",
                             actor: str | None = None,
                             replace: bool = False) -> dict[str, int]:
        """Запись каталога (upsert по id; replace=True чистит
        записи без активных резерваций)."""
        if game not in ("word", "krokodil"):
            raise ValueError(f"unknown game {game!r}")
        now = datetime.now(UTC)
        inserted = updated = 0
        async with await self._acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)", DDL_LOCK_KEY)
            if replace:
                await conn.execute(
                    """
                        DELETE FROM event_content_entries e
                        WHERE e.game = $1
                          AND NOT EXISTS (
                              SELECT 1 FROM event_content_reservations r
                              WHERE r.game = e.game
                                AND r.entry_id = e.id
                                AND r.released = FALSE
                                AND r.expires_at > now()
                          )
                        """, game)
            for e in entries:
                payload = json.dumps(e, ensure_ascii=False)
                row = await conn.fetchrow(
                    """
                        INSERT INTO event_content_entries
                            (game, id, enabled, difficulty,
                             category, payload,
                             created_at, updated_at)
                        VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$7)
                        ON CONFLICT (game, id) DO UPDATE SET
                            enabled    = EXCLUDED.enabled,
                            difficulty = EXCLUDED.difficulty,
                            category   = EXCLUDED.category,
                            payload    = EXCLUDED.payload,
                            updated_at = EXCLUDED.updated_at
                        RETURNING (xmax = 0) AS inserted
                        """,
                    game, e["id"], bool(e.get("enabled", True)),
                    e.get("difficulty", "easy"),
                    e.get("category", ""),
                    payload, now)
                if row and row["inserted"]:
                    inserted += 1
                else:
                    updated += 1
            await conn.execute(
                """
                    INSERT INTO event_content_catalog
                        (game, content_version, source,
                         entry_count, imported_at, updated_at,
                         updated_by)
                    VALUES ($1,$2,$3,
                            (SELECT count(*) FROM event_content_entries
                              WHERE game=$1),
                            now(), now(), $4)
                    ON CONFLICT (game) DO UPDATE SET
                        content_version = EXCLUDED.content_version,
                        source          = EXCLUDED.source,
                        entry_count     = EXCLUDED.entry_count,
                        imported_at     = now(),
                        updated_at      = now(),
                        updated_by      = EXCLUDED.updated_by
                    """, game, version, source, actor)
        return {"inserted": inserted, "updated": updated}

    async def seed_bundled(self, game: str,
                           entries: list[dict],
                           version: str) -> dict[str, int]:
        """Первичное наполнение из bundled, если пусто."""
        async with await self._acquire() as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM event_content_entries "
                "WHERE game=$1", game)
        if n:
            return {"skipped": int(n), "inserted": 0, "updated": 0}
        res = await self.import_catalog(game, entries,
                                        source="bundled",
                                        version=version,
                                        replace=True)
        return {**res, "skipped": 0}

    async def catalog_stats(self, game: str) -> dict[str, Any]:
        async with await self._acquire() as conn:
            meta = await conn.fetchrow(
                "SELECT * FROM event_content_catalog WHERE game=$1",
                game)
            counts = await conn.fetch(
                """
                SELECT difficulty, count(*) AS n,
                       count(*) FILTER (WHERE enabled) AS enabled_n
                FROM event_content_entries
                WHERE game=$1 GROUP BY difficulty
                """, game)
            total = await conn.fetchval(
                "SELECT count(*) FROM event_content_entries "
                "WHERE game=$1", game)
            enabled = await conn.fetchval(
                "SELECT count(*) FROM event_content_entries "
                "WHERE game=$1 AND enabled", game)
        by_diff = {r["difficulty"]: {"total": r["n"],
                                     "enabled": r["enabled_n"]}
                   for r in counts}
        return {
            "game": game,
            "content_version": (meta or {}).get("content_version", ""),
            "source": (meta or {}).get("source", ""),
            "imported_at": str((meta or {}).get("imported_at") or ""),
            "total": total or 0,
            "enabled": enabled or 0,
            "by_difficulty": by_diff,
        }

    async def list_categories(self, game: str) -> list[str]:
        async with await self._acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT category FROM event_content_entries "
                "WHERE game=$1 ORDER BY category", game)
        return [r["category"] for r in rows if r["category"]]

    # ---------------------------------------------------------- entries

    async def list_entries(self, game: str, *, q: str = "",
                           difficulty: str = "",
                           category: str = "",
                           enabled: bool | None = None,
                           limit: int = 50,
                           offset: int = 0) -> tuple[list[dict], int]:
        where = ["game=$1"]
        args: list[Any] = [game]
        idx = 2
        if q:
            where.append(
                f"(payload->>'word' ILIKE ${idx} OR "
                f" payload->>'answer' ILIKE ${idx} OR "
                f" payload->>'clue' ILIKE ${idx})")
            args.append(f"%{q}%")
            idx += 1
        if difficulty:
            where.append(f"difficulty=${idx}")
            args.append(difficulty)
            idx += 1
        if category:
            where.append(f"category=${idx}")
            args.append(category)
            idx += 1
        if enabled is not None:
            where.append(f"enabled=${idx}")
            args.append(enabled)
            idx += 1
        wsql = " AND ".join(where)
        async with await self._acquire() as conn:
            total = await conn.fetchval(
                f"SELECT count(*) FROM event_content_entries "
                f"WHERE {wsql}", *args)
            rows = await conn.fetch(
                f"""
                SELECT id, enabled, difficulty, category, payload
                FROM event_content_entries
                WHERE {wsql}
                ORDER BY id LIMIT ${idx} OFFSET ${idx+1}
                """, *args, min(limit, 200), offset)
        out = []
        for r in rows:
            p = r["payload"]
            if isinstance(p, str):
                p = json.loads(p)
            item = dict(p)
            item.update({"id": r["id"], "enabled": r["enabled"],
                         "difficulty": r["difficulty"],
                         "category": r["category"]})
            out.append(item)
        return out, total or 0

    async def get_entry(self, game: str, entry_id: str
                        ) -> dict[str, Any] | None:
        async with await self._acquire() as conn:
            r = await conn.fetchrow(
                "SELECT id, enabled, difficulty, category, payload "
                "FROM event_content_entries WHERE game=$1 AND id=$2",
                game, entry_id)
        if not r:
            return None
        p = r["payload"]
        if isinstance(p, str):
            p = json.loads(p)
        item = dict(p)
        item.update({"id": r["id"], "enabled": r["enabled"],
                     "difficulty": r["difficulty"],
                     "category": r["category"]})
        return item

    async def upsert_entry(self, game: str, entry: dict[str, Any],
                           actor: str | None = None
                           ) -> tuple[str, str]:
        """Создать/обновить одну запись. Возвращает (id, op)."""
        if game not in ("word", "krokodil"):
            raise ValueError(f"unknown game {game!r}")
        eid = entry.get("id")
        if not eid:
            eid = uuid.uuid4().hex[:16]
            entry = {**entry, "id": eid}
        payload = json.dumps(entry, ensure_ascii=False)
        now = datetime.now(UTC)
        async with await self._acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO event_content_entries
                    (game, id, enabled, difficulty, category,
                     payload, created_at, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$7)
                ON CONFLICT (game, id) DO UPDATE SET
                    enabled    = EXCLUDED.enabled,
                    difficulty = EXCLUDED.difficulty,
                    category   = EXCLUDED.category,
                    payload    = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at
                RETURNING (xmax = 0) AS inserted
                """,
                game, eid, bool(entry.get("enabled", True)),
                entry.get("difficulty", "easy"),
                entry.get("category", ""), payload, now)
        return eid, ("inserted" if row and row["inserted"]
                     else "updated")

    async def delete_entry(self, game: str, entry_id: str) -> bool:
        async with await self._acquire() as conn:
            res = await conn.execute(
                "DELETE FROM event_content_entries "
                "WHERE game=$1 AND id=$2", game, entry_id)
        return res.endswith("1")

    async def export_entries(self, game: str) -> list[dict]:
        async with await self._acquire() as conn:
            rows = await conn.fetch(
                "SELECT payload FROM event_content_entries "
                "WHERE game=$1 ORDER BY id", game)
        out = []
        for r in rows:
            p = r["payload"]
            out.append(json.loads(p) if isinstance(p, str) else p)
        return out

    async def import_entries(self, game: str, entries: list[dict],
                             *, version: str = "",
                             actor: str | None = None,
                             replace: bool = True
                             ) -> dict[str, int]:
        """Валидация + запись; при ошибках — ValueError,
        рабочий каталог не меняется."""
        from services.event_content import validate_catalog
        pseudo = {"schema_version": 1, "content_version": version,
                  "locale": "ru", "game": game,
                  "entries": entries}
        errors = validate_catalog(pseudo, game)
        if errors:
            raise ValueError("; ".join(errors[:20]))
        return await self.import_catalog(game, entries,
                                         source="control_center",
                                         version=version,
                                         actor=actor,
                                         replace=replace)

    # ---------------------------------------------------- selection

    async def _eligible_conn(self, conn, game: str,
                             difficulties: tuple[str, ...] | None,
                             disabled_categories: set[str],
                             used: set[str] | None = None,
                             ignore_used: bool = False
                             ) -> dict[str, list[dict]]:
        """По уровням: записи (enabled, категории разрешены),
        минус ``used`` текущего цикла и активные резервации.
        ``ignore_used=True`` — пул без учёта used (для
        проверки «цикл исчерпан vs каталог пуст»)."""
        where = ["e.game=$1", "e.enabled"]
        args: list[Any] = [game]
        idx = 2
        if difficulties:
            where.append(f"e.difficulty = ANY(${idx})")
            args.append(list(difficulties))
            idx += 1
        if disabled_categories:
            where.append(f"e.category <> ALL(${idx})")
            args.append(list(disabled_categories))
            idx += 1
        rows = await conn.fetch(
            f"""
            SELECT e.id, e.difficulty, e.payload
            FROM event_content_entries e
            WHERE {' AND '.join(where)}
            ORDER BY e.id
            """, *args)
        # ``used`` передаётся снаружи — вызывающий код сам
        # решает, учитывать ли used текущего цикла.
        used = used or set()
        # Активные резервации — глобально по игре
        res_rows = await conn.fetch(
            """
            SELECT entry_id FROM event_content_reservations
            WHERE game=$1 AND released=FALSE AND expires_at > now()
            """, game)
        reserved = {r["entry_id"] for r in res_rows}
        by_diff: dict[str, list[dict]] = {}
        for r in rows:
            if (not ignore_used and r["id"] in used) or \
                    r["id"] in reserved:
                continue
            p = r["payload"]
            if isinstance(p, str):
                p = json.loads(p)
            by_diff.setdefault(r["difficulty"], []).append(
                dict(p, id=r["id"]))
        return by_diff

    @staticmethod
    def _pick_level(by_diff: dict[str, list],
                    weights: dict[str, int],
                    rng: random.Random) -> str | None:
        """Взвешенный выбор уровня среди непустых.
        Все веса <=0 → равные (fallback)."""
        avail = [d for d, lst in by_diff.items() if lst]
        if not avail:
            return None
        w = {d: max(0, int(weights.get(d, 0))) for d in avail}
        total = sum(w.values())
        if total <= 0:
            # fallback: равные веса — конфигурация с
            # нулевыми весами не блокирует игру
            w = {d: 1 for d in avail}
            total = len(avail)
        roll = rng.uniform(0, total)
        acc = 0.0
        for d in avail:
            acc += w[d]
            if roll < acc:
                return d
        return avail[-1]

    async def pick(self, game: str, *, guild_id: str,
                   event_id: str | None = None,
                   difficulties: tuple[str, ...] | None = None,
                   difficulty_weights: dict[str, int] | None = None,
                   disabled_categories: set[str] | None = None,
                   rng: random.Random | None = None
                   ) -> dict[str, Any] | None:
        """Выбрать запись для нового события.

        Возвращает payload записи (с полем ``id``) или None,
        если доступных заданий нет — игра тогда недоступна
        для выбора без падения.
        """
        rng = rng or random.Random()
        weights = difficulty_weights or DEFAULT_DIFFICULTY_WEIGHTS
        disabled = disabled_categories or set()
        async with await self._acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)", DDL_LOCK_KEY)
            await conn.execute(
                """
                    INSERT INTO event_content_cycle_state
                        (game, guild_id) VALUES ($1,$2)
                    ON CONFLICT DO NOTHING
                    """, game, guild_id)
            state = await conn.fetchrow(
                """
                    SELECT used, cycle_count, last_entry_id
                    FROM event_content_cycle_state
                    WHERE game=$1 AND guild_id=$2 FOR UPDATE
                    """, game, guild_id)
            u = state["used"]
            if isinstance(u, str):
                u = json.loads(u)
            used = set(u or [])
            cycle = int(state["cycle_count"] or 0)
            last_id = state["last_entry_id"]

            by_diff = await self._eligible_conn(
                conn, game, difficulties, disabled,
                used=used)
            if not any(by_diff.values()):
                # Либо цикл исчерпан, либо каталог пуст.
                raw = await self._eligible_conn(
                    conn, game, difficulties, disabled,
                    ignore_used=True)
                if not any(raw.values()):
                    return None  # нет допустимых заданий
                # Новый цикл
                cycle += 1
                used = set()
                by_diff = raw

            level = self._pick_level(by_diff, weights, rng)
            if level is None:
                return None
            candidates = list(by_diff[level])
            # Если в выбранном уровне всё израсходовано —
            # берём из любого другого непустого уровня.
            if not candidates:
                for d in DIFFICULTIES:
                    if d != level and by_diff.get(d):
                        candidates = list(by_diff[d])
                        level = d
                        break
            if not candidates:
                return None
            # Исключаем немедленный повтор последнего
            # задания, если есть альтернатива.
            if len(candidates) > 1 and last_id:
                alt = [e for e in candidates
                       if e["id"] != last_id]
                if alt:
                    candidates = alt
            entry = rng.choice(candidates)
            eid = entry["id"]

            # used + cycle state
            used.add(eid)
            used_list = sorted(used)
            await conn.execute(
                """
                    UPDATE event_content_cycle_state SET
                        used=$3::jsonb, cycle_count=$4,
                        last_entry_id=$5, updated_at=now()
                    WHERE game=$1 AND guild_id=$2
                    """, game, guild_id,
                json.dumps(used_list), cycle, eid)
            await conn.execute(
                """
                    INSERT INTO event_content_draws
                        (game, guild_id, entry_id, event_id,
                         drawn_at)
                    VALUES ($1,$2,$3,$4,now())
                    """, game, guild_id, eid, event_id)
            # Истёкшие резервации записи снимаем, затем lease
            await conn.execute(
                """
                    UPDATE event_content_reservations SET released=TRUE
                    WHERE game=$1 AND entry_id=$2
                      AND released=FALSE AND expires_at <= now()
                    """, game, eid)
            await conn.execute(
                """
                    INSERT INTO event_content_reservations
                        (game, guild_id, entry_id, event_id,
                         created_at, expires_at, released)
                    VALUES ($1,$2,$3,$4,now(),
                            now() + make_interval(secs => $5),
                            FALSE)
                    ON CONFLICT DO NOTHING
                    """, game, guild_id, eid, event_id,
                RESERVATION_TTL_SECONDS)
            got = await conn.fetchval(
                """
                    SELECT 1 FROM event_content_reservations
                    WHERE game=$1 AND entry_id=$2
                      AND released=FALSE AND expires_at > now()
                    """, game, eid)
            if not got:
                return None
            return dict(entry)

    async def release(self, game: str, entry_id: str,
                      event_id: str | None = None) -> None:
        async with await self._acquire() as conn:
            await conn.execute(
                """
                UPDATE event_content_reservations SET released=TRUE
                WHERE game=$1 AND entry_id=$2 AND released=FALSE
                  AND ($3::text IS NULL OR event_id=$3)
                """, game, entry_id, event_id)

    async def release_event(self, event_id: str) -> None:
        async with await self._acquire() as conn:
            await conn.execute(
                "UPDATE event_content_reservations SET released=TRUE "
                "WHERE event_id=$1 AND released=FALSE", event_id)

    async def state(self, game: str, guild_id: str
                    ) -> dict[str, Any]:
        async with await self._acquire() as conn:
            r = await conn.fetchrow(
                "SELECT used, cycle_count, last_entry_id "
                "FROM event_content_cycle_state "
                "WHERE game=$1 AND guild_id=$2", game, guild_id)
        if not r:
            return {"used": [], "cycle_count": 0,
                    "last_entry_id": None}
        u = r["used"]
        if isinstance(u, str):
            u = json.loads(u)
        return {"used": list(u or []),
                "cycle_count": int(r["cycle_count"] or 0),
                "last_entry_id": r["last_entry_id"]}

    async def recent_draws(self, game: str, guild_id: str,
                           limit: int = 50) -> list[dict]:
        async with await self._acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT entry_id, event_id, drawn_at
                FROM event_content_draws
                WHERE game=$1 AND guild_id=$2
                ORDER BY id DESC LIMIT $3
                """, game, guild_id, limit)
        return [dict(r) for r in rows]




event_content_store = EventContentStore()
