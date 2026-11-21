"""Transactional PostgreSQL state for isolated CASH CUP TEST sessions.

Полностью отдельное хранилище от cashcup_tournaments: тестовый прогон
никогда не читает и не изменяет настоящий Cash Cup. Сессия хранит
импортированный снимок команд (status='snapshot'), рабочее состояние
(status='running'/'paused'/'completed') и архивируется ('archived').
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any

from storage.db import get_pool


class CashCupTestError(ValueError):
    pass


ACTIVE_STATUSES = ("snapshot", "running", "paused")
ALL_STATUSES = ACTIVE_STATUSES + ("completed", "archived")


class CashCupTestStore:
    def __init__(self) -> None:
        self._ready = False
        self._schema_lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        if self._ready:
            return
        async with self._schema_lock:
            if self._ready:
                return
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS cashcup_test_sessions (
                        id TEXT PRIMARY KEY,
                        guild_id BIGINT NOT NULL,
                        channel_id BIGINT NOT NULL DEFAULT 0,
                        message_id BIGINT NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        creator_id BIGINT NOT NULL DEFAULT 0,
                        state JSONB NOT NULL,
                        version INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS cashcup_test_sessions_guild_idx
                    ON cashcup_test_sessions(guild_id, created_at DESC)
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS cashcup_test_audit (
                        id BIGSERIAL PRIMARY KEY,
                        session_id TEXT NOT NULL
                            REFERENCES cashcup_test_sessions(id),
                        actor_id BIGINT NOT NULL,
                        action TEXT NOT NULL,
                        details JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
            self._ready = True

    @staticmethod
    def _decode(row) -> dict[str, Any]:
        if row is None:
            raise CashCupTestError("Тестовая сессия CASH CUP не найдена")
        raw = row["state"]
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        data["session_id"] = row["id"]
        data["channel_id"] = row["channel_id"]
        data["message_id"] = row["message_id"]
        data["version"] = row["version"]
        data["created_at"] = row["created_at"]
        return data

    async def get(self, guild_id: int, session_id: str) -> dict[str, Any]:
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM cashcup_test_sessions
                WHERE guild_id=$1 AND id=$2
            """, guild_id, session_id)
        return self._decode(row) if row else None

    async def latest(self, guild_id: int) -> dict[str, Any] | None:
        """Последняя неархивная сессия сервера (новые первыми)."""
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM cashcup_test_sessions
                WHERE guild_id=$1 AND status <> 'archived'
                ORDER BY created_at DESC LIMIT 1
            """, guild_id)
        return self._decode(row) if row else None

    async def list_sessions(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM cashcup_test_sessions
                WHERE guild_id=$1 ORDER BY created_at DESC LIMIT $2
            """, guild_id, limit)
        return [self._decode(r) for r in rows]

    async def create_snapshot(
        self, guild_id: int, channel_id: int, creator_id: int,
        snapshot_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Новая тестовая сессия из импортированного снимка команд.

        snapshot_state — состояние Cash Cup (teams/prize/...), скопированное
        из production read-only экспорта. Базовая копия сохраняется в
        state['base'] для кнопки «Начать заново».
        """
        await self.ensure_schema()
        sid = uuid.uuid4().hex[:12]
        # Детерминированный seed на сессию: повторная симуляция
        # воспроизводима. Явный seed из снимка имеет приоритет.
        seed = snapshot_state.get("seed")
        if seed is None:
            seed = int(sid[:8], 16)
        state = {
            "status": "snapshot",
            "test": True,
            "guild_id": guild_id,
            "teams": snapshot_state.get("teams") or [],
            "prize": snapshot_state.get("prize", ""),
            "pool": snapshot_state.get("pool", 0),
            "prizes": snapshot_state.get("prizes"),
            "bracket": None,
            "payouts": [],
            "seed": seed,
            "base": {
                "status": "snapshot",
                "teams": snapshot_state.get("teams") or [],
                "prize": snapshot_state.get("prize", ""),
                "pool": snapshot_state.get("pool", 0),
                "prizes": snapshot_state.get("prizes"),
            },
        }
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO cashcup_test_sessions
                    (id,guild_id,channel_id,status,creator_id,state)
                VALUES ($1,$2,$3,'snapshot',$4,$5::jsonb)
            """, sid, guild_id, channel_id, creator_id, json.dumps(state))
            await conn.execute("""
                INSERT INTO cashcup_test_audit(session_id,actor_id,action,details)
                VALUES ($1,$2,'import_snapshot',$3::jsonb)
            """, sid, creator_id,
                json.dumps({"teams": len(state["teams"])}))
        session = await self.get(guild_id, sid)
        assert session is not None
        return session

    async def set_message(self, guild_id: int, session_id: str,
                          message_id: int, channel_id: int | None = None) -> None:
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            if channel_id is not None:
                await conn.execute("""
                    UPDATE cashcup_test_sessions
                    SET message_id=$3, channel_id=$4, updated_at=NOW()
                    WHERE guild_id=$1 AND id=$2
                """, guild_id, session_id, message_id, channel_id)
            else:
                await conn.execute("""
                    UPDATE cashcup_test_sessions
                    SET message_id=$3, updated_at=NOW()
                    WHERE guild_id=$1 AND id=$2
                """, guild_id, session_id, message_id)

    async def mutate(
        self, guild_id: int, actor_id: int, action: str,
        change: Callable[[dict[str, Any]], Any],
        details: dict | None = None,
        session_id: str | None = None,
    ) -> tuple[dict[str, Any], Any]:
        """Изменение состояния под row-lock с оптимистичной проверкой версии.

        session_id=None → последняя неархивная сессия guild.
        """
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn, conn.transaction():
            if session_id is not None:
                row = await conn.fetchrow("""
                        SELECT * FROM cashcup_test_sessions
                        WHERE guild_id=$1 AND id=$2 FOR UPDATE
                    """, guild_id, session_id)
            else:
                row = await conn.fetchrow("""
                        SELECT * FROM cashcup_test_sessions
                        WHERE guild_id=$1 AND status <> 'archived'
                        ORDER BY created_at DESC LIMIT 1 FOR UPDATE
                    """, guild_id)
            if row is None:
                raise CashCupTestError("Тестовая сессия CASH CUP не найдена")
            state = self._decode(row)
            if state.get("status") == "archived":
                raise CashCupTestError("Тестовая сессия удалена")
            old_version = state.pop("version")
            sid = state.pop("session_id")
            channel_id = state.pop("channel_id")
            message_id = state.pop("message_id")
            created_at = state.pop("created_at", None)
            result = change(state)
            new_status = state.get("status")
            if new_status not in ALL_STATUSES:
                raise CashCupTestError(f"Некорректный статус сессии: {new_status}")
            updated = await conn.execute("""
                    UPDATE cashcup_test_sessions
                    SET status=$2, state=$3::jsonb, version=version+1,
                        updated_at=NOW()
                    WHERE id=$1 AND version=$4
                """, sid, new_status, json.dumps(state), old_version)
            if updated.endswith(" 0"):
                raise CashCupTestError("Сессия изменилась параллельно — повторите")
            await conn.execute("""
                    INSERT INTO cashcup_test_audit(session_id,actor_id,action,details)
                    VALUES ($1,$2,$3,$4::jsonb)
                """, sid, actor_id, action, json.dumps(details or {}))
            return {**state, "session_id": sid, "channel_id": channel_id,
                    "message_id": message_id, "version": old_version + 1,
                    "created_at": created_at}, result


cashcup_test_store = CashCupTestStore()
