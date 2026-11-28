"""Долговечное состояние спонтанных событий (Spontaneous Events 2.0).

Две таблицы (только PostgreSQL, JSON-fallback отсутствует —
события без durable-записи не запускаются):

- ``spontaneous_events`` — одна строка на событие. Машина
  состояний ``active → settling → finished`` (+``cancelled``):
  ``settle()`` атомарно фиксирует вычисленный outcome
  (победители/выплата) ДО начисления наград — повторная
  финализация никогда не перевыбирает случайного победителя.
  Частичный UNIQUE-индекс по (guild_id, channel_id) для
  ``state IN ('active','settling')``: атомарный claim «одно
  незавершённое событие на канал» работает и при гонке
  естественного триггера и /forcedrop, и между процессами.
  ``config``/``prize`` — JSONB с публичной и скрытой частью
  (скрытые поля никогда не попадают в карточку).
  ``actions`` — плоский JSONB-словарь зафиксированных действий
  (``p:<uid>``, ``choice:<uid>``, ``roll:<uid>``, ``click:<uid>``,
  ``winner`` …), обновляется атомарным ``||`` merge с PG-гардами:
  ``must_absent``-ключи (winner, первый выбор и т.п.) не
  перезаписываются, правки после эффективного дедлайна
  отклоняются на уровне БД — restart безопасно восстанавливает
  уже сделанные ходы.

- ``event_channel_state`` — кулдаун и последний тип события на
  канал. Кулдаун ставится в момент успешного старта и переживает
  рестарт; он отсчитывается от времени СТАРТА события.

Дедлайны — TIMESTAMPTZ (UTC на стороне БД); проверки
«событие ещё идёт» выполняются серверным временем, а не таймерами
клиента.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS spontaneous_events (
    event_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    game_type TEXT NOT NULL,
    prize JSONB NOT NULL,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    actions JSONB NOT NULL DEFAULT '{}'::jsonb,
    state TEXT NOT NULL DEFAULT 'active',
    deadline_at TIMESTAMPTZ NOT NULL,
    close_at TIMESTAMPTZ,
    message_id BIGINT,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
)
"""
INDEX_DDL = """
DROP INDEX IF EXISTS spontaneous_events_one_active;
CREATE UNIQUE INDEX IF NOT EXISTS spontaneous_events_one_active
ON spontaneous_events (guild_id, channel_id)
WHERE state IN ('active', 'settling')
"""
CHANNEL_DDL = """
CREATE TABLE IF NOT EXISTS event_channel_state (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    cooldown_until TIMESTAMPTZ,
    last_event_type TEXT,
    last_event_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, channel_id)
)
"""


class EventStore:
    def __init__(self) -> None:
        self._ddl_done = False

    async def _pool(self):
        from storage.db import get_pool
        return await get_pool()

    async def _ensure_ddl(self, conn) -> None:
        if not self._ddl_done:
            # Advisory lock сериализует DDL между процессами:
            # DROP+CREATE UNIQUE INDEX иначе может гоняться
            # и оставить канал без защиты от двойных active.
            await conn.execute("SELECT pg_advisory_lock(727001)")
            try:
                await conn.execute(DDL)
                await conn.execute(INDEX_DDL)
                await conn.execute(CHANNEL_DDL)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(727001)")
            self._ddl_done = True

    # ---------------------------------------------------------- события

    async def create(
        self,
        event_id: str,
        guild_id: int,
        channel_id: int,
        game_type: str,
        prize: dict[str, Any],
        config: dict[str, Any],
        deadline_at: datetime,
        close_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Атомарно создать активное событие.

        Возвращает строку события или None — активное событие на
        этом канале уже существует (частичный UNIQUE-индекс).
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            row = await conn.fetchrow(
                """
                INSERT INTO spontaneous_events
                    (event_id, guild_id, channel_id, game_type,
                     prize, config, state, deadline_at, close_at)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb,
                        'active', $7, $8)
                ON CONFLICT (guild_id, channel_id)
                    WHERE state IN ('active', 'settling')
                DO NOTHING
                RETURNING *
                """,
                event_id, guild_id, channel_id, game_type,
                json.dumps(prize, ensure_ascii=False),
                json.dumps(config, ensure_ascii=False),
                deadline_at, close_at,
            )
            return dict(row) if row else None

    async def get(self, event_id: str) -> dict[str, Any] | None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            row = await conn.fetchrow(
                "SELECT * FROM spontaneous_events WHERE event_id = $1",
                event_id,
            )
            return dict(row) if row else None

    async def get_active(self, guild_id: int,
                         channel_id: int) -> dict[str, Any] | None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            row = await conn.fetchrow(
                """
                SELECT * FROM spontaneous_events
                WHERE guild_id = $1 AND channel_id = $2
                  AND state IN ('active', 'settling')
                """,
                guild_id, channel_id,
            )
            return dict(row) if row else None

    async def list_active(self) -> list[dict[str, Any]]:
        """Все незавершённые события (active+settling) —
        восстановление после рестарта."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            rows = await conn.fetch(
                "SELECT * FROM spontaneous_events "
                "WHERE state IN ('active', 'settling')"
            )
            return [dict(r) for r in rows]

    async def list_settling(self) -> list[dict[str, Any]]:
        """События в состоянии settling — долги по выплатам,
        требующие доведения до finished."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            rows = await conn.fetch(
                "SELECT * FROM spontaneous_events "
                "WHERE state = 'settling'"
            )
            return [dict(r) for r in rows]

    async def overdue(self, now: datetime | None = None
                      ) -> list[dict[str, Any]]:
        """Активные события с истёкшим дедлайном — закрыть."""
        now = now or datetime.now(timezone.utc)
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            rows = await conn.fetch(
                """
                SELECT * FROM spontaneous_events
                WHERE state = 'active'
                  AND LEAST(deadline_at,
                            COALESCE(close_at, deadline_at)) <= $1
                """,
                now,
            )
            return [dict(r) for r in rows]

    async def set_message_id(self, event_id: str,
                             message_id: int) -> None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            await conn.execute(
                "UPDATE spontaneous_events SET message_id = $2 "
                "WHERE event_id = $1 "
                "AND (message_id IS NULL OR message_id = $2)",
                event_id, message_id,
            )

    async def patch_actions(self, event_id: str,
                            patch: dict[str, Any],
                            must_absent: list[str] | None = None
                            ) -> bool:
        """Атомарно влить действия в actions (jsonb ||).

        Ключи плоские (``p:<uid>``, ``choice:<uid>``, ``winner`` …).
        PG-гарды (одна UPDATE): ``state='active'``, эффективный
        дедлайн не истёк по часам БД, ключи ``must_absent`` ещё
        не зафиксированы. True — патч применён; False — событие
        закрыто/истекло или ключ занят (проверять результат!).
        """
        if not patch:
            return True
        absent = list(must_absent or [])
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            row = await conn.fetchrow(
                """
                UPDATE spontaneous_events
                SET actions = COALESCE(actions, '{}'::jsonb)
                              || $2::jsonb
                WHERE event_id = $1 AND state = 'active'
                  AND LEAST(deadline_at,
                            COALESCE(close_at, deadline_at)) > now()
                  AND NOT (COALESCE(actions, '{}'::jsonb)
                           ?| $3::text[])
                RETURNING event_id
                """,
                event_id, json.dumps(patch, ensure_ascii=False),
                absent,
            )
            return row is not None

    async def settle(self, event_id: str,
                     outcome: Any
                     ) -> dict[str, Any] | None:
        """Атомарно перевести active → settling, зафиксировав
        outcome в ``result`` ДО начисления наград.

        ``outcome`` — либо готовый dict, либо callable
        ``builder(row) -> dict``, вызываемый ВНУТРИ транзакции
        после ``SELECT ... FOR UPDATE``. Это гарантирует, что
        outcome вычислен из того же атомарно зафиксированного
        снимка ``actions``, который заморожен переходом в
        settling: конкурирующий ``patch_actions`` либо успеет
        до блокировки строки (и попадёт в снимок), либо будет
        отклонён PG-гардом ``state='active'`` — потерянных
        действий и «ответа на устаревший снимок» нет.

        None — событие уже settling/finished/отсутствует:
        единственный финализатор, случайный победитель не
        перевыбирается (outcome берётся из сохранённого result).
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM spontaneous_events "
                    "WHERE event_id = $1 FOR UPDATE",
                    event_id,
                )
                if row is None or row["state"] != "active":
                    return None
                data = dict(row)
                computed = (outcome(data) if callable(outcome)
                            else outcome)
                payload = {
                    "outcome": computed,
                    "settled_ts": datetime.now(timezone.utc)
                    .isoformat(),
                }
                await conn.execute(
                    "UPDATE spontaneous_events "
                    "SET state = 'settling', result = $2::jsonb "
                    "WHERE event_id = $1",
                    event_id,
                    json.dumps(payload, ensure_ascii=False),
                )
                data["state"] = "settling"
                data["result"] = payload
                return data

    async def finish(self, event_id: str, state: str,
                     result: dict[str, Any] | None = None
                     ) -> dict[str, Any] | None:
        """Атомарно закрыть событие. Переходы строгие:

        - ``finished`` — только из ``settling`` и только когда в
          сохранённом ``result`` уже есть валидный ``outcome``
          (обязательство по выплате зафиксировано до grant);
        - ``cancelled`` — только из ``active`` (отказ до
          начислений, например недоставленная карточка).

        None — переход недопустим или событие уже закрыто другим
        процессом (единственный финализатор карточки)."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            row = await conn.fetchrow(
                """
                UPDATE spontaneous_events
                SET state = $2,
                    result = $3::jsonb,
                    finished_at = now()
                WHERE event_id = $1
                  AND (
                    ($2 = 'finished' AND state = 'settling'
                     AND COALESCE(result, '{}'::jsonb) ? 'outcome')
                    OR
                    ($2 = 'cancelled' AND state = 'active')
                  )
                RETURNING *
                """,
                event_id, state,
                json.dumps(result or {}, ensure_ascii=False),
            )
            return dict(row) if row else None

    async def mark_card_edit(self, event_id: str,
                             status: str) -> None:
        """Отметить исход финального редактирования карточки
        (``card_edit`` в result) — для свипера повторных попыток."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            await conn.execute(
                """
                UPDATE spontaneous_events
                SET result = COALESCE(result, '{}'::jsonb)
                             || jsonb_build_object(
                                    'card_edit', $2::text)
                WHERE event_id = $1
                """,
                event_id, status,
            )

    async def list_failed_cards(self, within_seconds: int = 86400
                                ) -> list[dict[str, Any]]:
        """Недавно завершённые события, у которых финальная
        правка карточки не подтверждена — кандидаты на retry.
        Окно 24ч: восстановление финального состояния карточки
        не прекращается через 30 минут — нерешённые строки
        продолжают логироваться свипером как ошибки."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            rows = await conn.fetch(
                """
                SELECT * FROM spontaneous_events
                WHERE state = 'finished'
                  AND finished_at > now() - ($1 || ' seconds')::interval
                  AND COALESCE(result->>'card_edit', '') <> 'ok'
                """,
                str(within_seconds),
            )
            return [dict(r) for r in rows]

    # ---------------------------------------------------- channel state

    async def get_channel_state(self, guild_id: int,
                                channel_id: int
                                ) -> dict[str, Any] | None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            row = await conn.fetchrow(
                "SELECT * FROM event_channel_state "
                "WHERE guild_id = $1 AND channel_id = $2",
                guild_id, channel_id,
            )
            return dict(row) if row else None

    async def mark_started(self, guild_id: int, channel_id: int,
                           cooldown_until: datetime,
                           event_type: str, event_id: str) -> None:
        """Кулдаун отсчитывается от успешного СТАРТА события."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._ensure_ddl(conn)
            await conn.execute(
                """
                INSERT INTO event_channel_state
                    (guild_id, channel_id, cooldown_until,
                     last_event_type, last_event_id, updated_at)
                VALUES ($1, $2, $3, $4, $5, now())
                ON CONFLICT (guild_id, channel_id) DO UPDATE SET
                    cooldown_until = EXCLUDED.cooldown_until,
                    last_event_type = EXCLUDED.last_event_type,
                    last_event_id = EXCLUDED.last_event_id,
                    updated_at = now()
                """,
                guild_id, channel_id, cooldown_until,
                event_type, event_id,
            )

    async def cooldown_active(self, guild_id: int, channel_id: int,
                              now: datetime | None = None) -> bool:
        """True — кулдаун ещё идёт (переживает рестарт)."""
        now = now or datetime.now(timezone.utc)
        state = await self.get_channel_state(guild_id, channel_id)
        if not state or not state.get("cooldown_until"):
            return False
        until = state["cooldown_until"]
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return now < until

    async def last_event_type(self, guild_id: int,
                              channel_id: int) -> str | None:
        state = await self.get_channel_state(guild_id, channel_id)
        return (state or {}).get("last_event_type")


# Приведение JSONB-полей asyncpg (приходит str при text-касте,
# dict при jsonb — поддерживаем оба).
def _json_field(row: dict[str, Any], key: str) -> Any:
    v = row.get(key)
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return {}
    return v if v is not None else {}


event_store = EventStore()
