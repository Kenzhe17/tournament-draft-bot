"""Transactional PostgreSQL state for CASH CUP, isolated from legacy tournaments."""
from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from typing import Any, Callable
from datetime import datetime, timezone

from storage.db import get_pool


class CashCupError(ValueError):
    pass


class CashCupStore:
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
                    CREATE TABLE IF NOT EXISTS cashcup_tournaments (
                        id TEXT PRIMARY KEY,
                        guild_id BIGINT NOT NULL,
                        channel_id BIGINT NOT NULL,
                        message_id BIGINT NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        creator_id BIGINT NOT NULL,
                        state JSONB NOT NULL,
                        version INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                await conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS cashcup_active_guild_idx
                    ON cashcup_tournaments(guild_id)
                    WHERE status IN ('registration','running')
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS cashcup_audit (
                        id BIGSERIAL PRIMARY KEY,
                        tournament_id TEXT NOT NULL REFERENCES cashcup_tournaments(id),
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
            raise CashCupError("Активный CASH CUP не найден")
        raw = row["state"]
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        data["tournament_id"] = row["id"]
        data["channel_id"] = row["channel_id"]
        data["message_id"] = row["message_id"]
        data["version"] = row["version"]
        return data

    async def active(self, guild_id: int) -> dict[str, Any] | None:
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM cashcup_tournaments WHERE guild_id=$1
                AND status IN ('registration','running') LIMIT 1
            """, guild_id)
        return self._decode(row) if row else None

    async def latest(self, guild_id: int) -> dict[str, Any] | None:
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM cashcup_tournaments WHERE guild_id=$1
                ORDER BY created_at DESC LIMIT 1
            """, guild_id)
        return self._decode(row) if row else None

    async def last_completed(self, guild_id: int) -> dict[str, Any] | None:
        """Последний ЗАВЕРШЁННЫЙ реальный турнир (status='completed').

        latest() может вернуть активный или отменённый турнир —
        для «сброса команд прошлого Cash Cup» нужен именно
        завершённый."""
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM cashcup_tournaments WHERE guild_id=$1
                AND status='completed'
                ORDER BY created_at DESC LIMIT 1
            """, guild_id)
        return self._decode(row) if row else None

    async def create(self, guild_id: int, channel_id: int, creator_id: int,
                     prize: str, pool: int | None = None,
                     prize1: int | None = None, currency: str = "KZT") -> dict[str, Any]:
        await self.ensure_schema()
        tid = uuid.uuid4().hex[:12]
        state = {"status": "registration", "prize": prize, "pool": pool or 0,
                 "guild_id": guild_id, "created_at": datetime.now(timezone.utc).isoformat(),
                 "teams": [], "bracket": None, "payouts": []}
        if prize1:
            state["prizes"] = {"places": {"1": prize1, "2": 0, "3": 0},
                               "currency": (currency or "KZT")[:10],
                               "total": prize1}
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("""
                        INSERT INTO cashcup_tournaments
                            (id,guild_id,channel_id,status,creator_id,state)
                        VALUES ($1,$2,$3,'registration',$4,$5::jsonb)
                    """, tid, guild_id, channel_id, creator_id, json.dumps(state))
                    await conn.execute("""
                        INSERT INTO cashcup_audit(tournament_id,actor_id,action)
                        VALUES ($1,$2,'create')
                    """, tid, creator_id)
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise CashCupError("На сервере уже проводится CASH CUP") from exc
            raise
        return await self.active(guild_id)

    async def list_past(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        """Последние турниры сервера (новые первыми) — история/чемпионы."""
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM cashcup_tournaments WHERE guild_id=$1
                ORDER BY created_at DESC LIMIT $2
            """, guild_id, limit)
        return [self._decode(r) for r in rows]

    async def set_message(self, guild_id: int, message_id: int) -> None:
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE cashcup_tournaments SET message_id=$2
                WHERE guild_id=$1 AND status IN ('registration','running')
            """, guild_id, message_id)

    async def mutate(
        self, guild_id: int, actor_id: int, action: str,
        change: Callable[[dict[str, Any]], Any],
        details: dict | None = None,
        include_finished: bool = False,
        tournament_id: str | None = None,
    ) -> tuple[dict[str, Any], Any]:
        """All state changes happen under a row lock and write a durable audit entry."""
        await self.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if tournament_id is not None:
                    query = "SELECT * FROM cashcup_tournaments WHERE guild_id=$1 AND id=$2 FOR UPDATE"
                    row = await conn.fetchrow(query, guild_id, tournament_id)
                else:
                    query = ("SELECT * FROM cashcup_tournaments WHERE guild_id=$1 "
                             + ("ORDER BY created_at DESC LIMIT 1 FOR UPDATE" if include_finished
                                else "AND status IN ('registration','running') FOR UPDATE"))
                    row = await conn.fetchrow(query, guild_id)
                state = self._decode(row)
                old_version = state.pop("version")
                tid = state.pop("tournament_id")
                channel_id = state.pop("channel_id")
                message_id = state.pop("message_id")
                # change может принимать (state, conn) — тогда она
                # работает на том же соединении/транзакции и может
                # брать согласованные блокировки (например, FOR
                # UPDATE на teams при регистрации squads). Async
                # change тоже поддерживается.
                try:
                    n_params = sum(
                        1 for p in inspect.signature(change)
                        .parameters.values()
                        if p.kind in (p.POSITIONAL_ONLY,
                                      p.POSITIONAL_OR_KEYWORD))
                except (TypeError, ValueError):
                    n_params = 1
                result = (change(state, conn) if n_params >= 2
                          else change(state))
                if inspect.isawaitable(result):
                    result = await result
                await conn.execute("""
                    UPDATE cashcup_tournaments
                    SET status=$2, state=$3::jsonb, version=version+1
                    WHERE id=$1 AND version=$4
                """, tid, state["status"], json.dumps(state), old_version)
                await conn.execute("""
                    INSERT INTO cashcup_audit(tournament_id,actor_id,action,details)
                    VALUES ($1,$2,$3,$4::jsonb)
                """, tid, actor_id, action, json.dumps(details or {}))
                return {**state, "tournament_id": tid, "channel_id": channel_id,
                        "message_id": message_id, "version": old_version+1}, result


cashcup_store = CashCupStore()
