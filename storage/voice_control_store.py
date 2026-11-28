"""PostgreSQL source of truth for r!z3 Voice Control.

Все записи изолированы по guild_id. Как и moderation_store, стор
намеренно не имеет JSON-fallback: при недоступности PostgreSQL
административные операции fail-closed, а безопасная очистка
ограничений логируется и продолжается после восстановления БД.

Схема — только добавочные миграции (CREATE TABLE IF NOT EXISTS /
CREATE INDEX IF NOT EXISTS), вызывается из ensure().
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from storage.db import get_pool

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_control_settings (
    guild_id BIGINT PRIMARY KEY,
    allowed_role_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    protected_user_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    protected_role_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    protected_channel_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    log_channel_id BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS voice_control_modes (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    speaker_mode BOOLEAN NOT NULL DEFAULT FALSE,
    auto_mute BOOLEAN NOT NULL DEFAULT FALSE,
    closed BOOLEAN NOT NULL DEFAULT FALSE,
    name_override TEXT,
    user_limit INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, channel_id)
);
CREATE TABLE IF NOT EXISTS voice_control_speakers (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    added_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, channel_id, user_id)
);
CREATE TABLE IF NOT EXISTS voice_control_exceptions (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('user','role')),
    target_id BIGINT NOT NULL,
    added_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, channel_id, target_type, target_id)
);
CREATE TABLE IF NOT EXISTS voice_control_restrictions (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('server_mute','server_deafen')),
    origin TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    expires_at TIMESTAMPTZ,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','released','expired')),
    request_key TEXT,
    created_by BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at TIMESTAMPTZ,
    released_by BIGINT
);
CREATE INDEX IF NOT EXISTS vc_restrictions_active_idx
    ON voice_control_restrictions(guild_id, channel_id, user_id, state);
CREATE INDEX IF NOT EXISTS vc_restrictions_due_idx
    ON voice_control_restrictions(expires_at)
    WHERE state = 'active' AND expires_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS vc_restrictions_reqkey_idx
    ON voice_control_restrictions(request_key)
    WHERE request_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS voice_control_overwrites (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('role','member')),
    target_id BIGINT NOT NULL,
    before JSONB,
    applied JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'saved'
        CHECK (state IN ('saved','applied','restored','conflict')),
    created_by BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    restored_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS vc_overwrites_channel_idx
    ON voice_control_overwrites(guild_id, channel_id, state);
CREATE TABLE IF NOT EXISTS voice_control_ops (
    op_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','done','failed','unknown')),
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    actor_id BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS voice_control_events (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    actor_id BIGINT NOT NULL DEFAULT 0,
    channel_id BIGINT,
    action TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS vc_events_guild_idx
    ON voice_control_events(guild_id, id DESC);
"""

_MODE_FIELDS = ("speaker_mode", "auto_mute", "closed", "name_override", "user_limit")
_SETTINGS_FIELDS = (
    "allowed_role_ids", "protected_user_ids", "protected_role_ids",
    "protected_channel_ids", "log_channel_id",
)


def _dict(row) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class VoiceControlStore:
    def __init__(self) -> None:
        self._ready = False
        self._init_lock = asyncio.Lock()

    async def ensure(self) -> None:
        if self._ready:
            return
        async with self._init_lock:
            if not self._ready:
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(_SCHEMA)
                self._ready = True

    # ------------------------------------------------------------------
    # Настройки гильдии
    # ------------------------------------------------------------------
    async def settings(self, guild_id: int) -> dict[str, Any]:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM voice_control_settings WHERE guild_id=$1",
                guild_id,
            )
        data = _dict(row) or {}
        for key in (
            "allowed_role_ids", "protected_user_ids",
            "protected_role_ids", "protected_channel_ids",
        ):
            val = data.get(key)
            if isinstance(val, str):
                try:
                    data[key] = json.loads(val)
                except ValueError:
                    data[key] = []
            elif val is None:
                data[key] = []
        return data

    async def put_settings(self, guild_id: int, **values) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, value in values.items():
            if key not in _SETTINGS_FIELDS:
                continue
            if key == "log_channel_id":
                if value:
                    clean[key] = int(value)
            elif isinstance(value, (list, tuple, set)):
                clean[key] = json.dumps(sorted({int(v) for v in value}))
            elif value is None:
                clean[key] = "[]"
        if not clean:
            return await self.settings(guild_id)
        await self.ensure()
        columns = ", ".join(clean)
        inserts = ", ".join(f"${i + 2}" for i in range(len(clean)))
        updates = ", ".join(f"{k}=EXCLUDED.{k}" for k in clean)
        sql = (
            f"INSERT INTO voice_control_settings(guild_id,{columns}) "
            f"VALUES($1,{inserts}) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {updates}, updated_at=now() "
            f"RETURNING *"
        )
        # jsonb-колонки принимают JSON-строки — asyncpg кодирует str
        # в jsonb по типу колонки.
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, guild_id, *clean.values())
        return await self.settings(guild_id) if row is None else dict(row)

    # ------------------------------------------------------------------
    # Режимы каналов
    # ------------------------------------------------------------------
    async def mode(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            return _dict(await conn.fetchrow(
                "SELECT * FROM voice_control_modes WHERE guild_id=$1 AND channel_id=$2",
                guild_id, channel_id,
            ))

    async def modes(self, guild_id: int) -> list[dict[str, Any]]:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM voice_control_modes WHERE guild_id=$1",
                guild_id,
            )
        return [dict(r) for r in rows]

    async def put_mode(self, guild_id: int, channel_id: int, **fields) -> dict[str, Any]:
        clean = {k: v for k, v in fields.items() if k in _MODE_FIELDS}
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            if clean:
                columns = ", ".join(clean)
                inserts = ", ".join(f"${i + 3}" for i in range(len(clean)))
                updates = ", ".join(f"{k}=EXCLUDED.{k}" for k in clean)
                await conn.execute(
                    f"INSERT INTO voice_control_modes(guild_id,channel_id,{columns}) "
                    f"VALUES($1,$2,{inserts}) "
                    f"ON CONFLICT(guild_id,channel_id) DO UPDATE SET {updates}, updated_at=now()",
                    guild_id, channel_id, *clean.values(),
                )
            row = await conn.fetchrow(
                "SELECT * FROM voice_control_modes WHERE guild_id=$1 AND channel_id=$2",
                guild_id, channel_id,
            )
        return _dict(row) or {}

    async def delete_mode_if_idle(self, guild_id: int, channel_id: int) -> None:
        """Удалить строку режима, если в ней нет активных флагов/данных."""
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """DELETE FROM voice_control_modes
                   WHERE guild_id=$1 AND channel_id=$2
                     AND NOT speaker_mode AND NOT auto_mute AND NOT closed
                     AND name_override IS NULL AND user_limit IS NULL""",
                guild_id, channel_id,
            )
            speaker_count = await conn.fetchval(
                "SELECT count(*) FROM voice_control_speakers "
                "WHERE guild_id=$1 AND channel_id=$2",
                guild_id, channel_id,
            )
            if speaker_count == 0:
                await conn.execute(
                    "DELETE FROM voice_control_speakers "
                    "WHERE guild_id=$1 AND channel_id=$2",
                    guild_id, channel_id,
                )

    # ------------------------------------------------------------------
    # Ведущие и исключения
    # ------------------------------------------------------------------
    async def speakers(self, guild_id: int, channel_id: int) -> list[int]:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id FROM voice_control_speakers "
                "WHERE guild_id=$1 AND channel_id=$2 ORDER BY user_id",
                guild_id, channel_id,
            )
        return [int(r["user_id"]) for r in rows]

    async def add_speaker(self, guild_id: int, channel_id: int, user_id: int, actor_id: int) -> None:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO voice_control_speakers(guild_id,channel_id,user_id,added_by) "
                "VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                guild_id, channel_id, user_id, actor_id,
            )

    async def remove_speaker(self, guild_id: int, channel_id: int, user_id: int) -> None:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM voice_control_speakers WHERE guild_id=$1 AND channel_id=$2 AND user_id=$3",
                guild_id, channel_id, user_id,
            )

    async def clear_speakers(self, guild_id: int, channel_id: int) -> None:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM voice_control_speakers WHERE guild_id=$1 AND channel_id=$2",
                guild_id, channel_id,
            )

    async def exceptions(self, guild_id: int, channel_id: int) -> list[dict[str, Any]]:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM voice_control_exceptions WHERE guild_id=$1 AND channel_id=$2",
                guild_id, channel_id,
            )
        return [dict(r) for r in rows]

    async def add_exception(self, guild_id: int, channel_id: int, target_type: str,
                            target_id: int, actor_id: int) -> None:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO voice_control_exceptions(guild_id,channel_id,target_type,target_id,added_by) "
                "VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                guild_id, channel_id, target_type, target_id, actor_id,
            )

    async def remove_exception(self, guild_id: int, channel_id: int,
                               target_type: str, target_id: int) -> None:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM voice_control_exceptions "
                "WHERE guild_id=$1 AND channel_id=$2 AND target_type=$3 AND target_id=$4",
                guild_id, channel_id, target_type, target_id,
            )

    # ------------------------------------------------------------------
    # Ограничения (server mute / deafen), учёт происхождения
    # ------------------------------------------------------------------
    async def add_restriction(self, guild_id: int, channel_id: int, user_id: int,
                              kind: str, origin: str, *, reason: str = "",
                              expires_at=None, request_key: str | None = None,
                              created_by: int = 0) -> dict[str, Any] | None:
        """Идемпотентно по request_key. Возвращает None при дубле."""
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO voice_control_restrictions
                   (guild_id,channel_id,user_id,kind,origin,reason,expires_at,request_key,created_by)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT DO NOTHING RETURNING *""",
                guild_id, channel_id, user_id, kind, origin, reason,
                expires_at, request_key, created_by,
            )
            # ON CONFLICT без target не ловит частичный уникальный индекс —
            # дубли request_key отсекаем явно.
            if row is None and request_key:
                row = await conn.fetchrow(
                    "SELECT * FROM voice_control_restrictions WHERE request_key=$1",
                    request_key,
                )
                if row and row["state"] != "active":
                    return None
            return _dict(row)

    async def restrictions(self, guild_id: int, *, channel_id: int | None = None,
                           user_id: int | None = None, active_only: bool = True,
                           limit: int = 500) -> list[dict[str, Any]]:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            sql = "SELECT * FROM voice_control_restrictions WHERE guild_id=$1"
            args: list[Any] = [guild_id]
            if channel_id is not None:
                args.append(channel_id)
                sql += f" AND channel_id=${len(args)}"
            if user_id is not None:
                args.append(user_id)
                sql += f" AND user_id=${len(args)}"
            if active_only:
                sql += " AND state='active'"
            sql += " ORDER BY id DESC LIMIT $" + str(len(args) + 1)
            args.append(min(1000, limit))
            rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]

    async def release_restriction(self, guild_id: int, restriction_id: int, *,
                                  actor_id: int = 0) -> dict[str, Any] | None:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """UPDATE voice_control_restrictions
                   SET state='released', released_at=now(), released_by=$3
                   WHERE guild_id=$1 AND id=$2 AND state='active'
                   RETURNING *""",
                guild_id, restriction_id, actor_id,
            )
            return _dict(row)

    async def expire_due_restrictions(self, limit: int = 200) -> list[dict[str, Any]]:
        """Таймеры с истёкшим сроком. Вызывается sweeper'ом."""
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """UPDATE voice_control_restrictions SET state='expired', released_at=now()
                   WHERE id IN (
                       SELECT id FROM voice_control_restrictions
                       WHERE state='active' AND expires_at IS NOT NULL AND expires_at<=now()
                       ORDER BY expires_at LIMIT $1
                       FOR UPDATE SKIP LOCKED)
                   RETURNING *""",
                limit,
            )
        return [dict(r) for r in rows]

    async def has_other_active(self, guild_id: int, channel_id: int, user_id: int,
                               kind: str, *, exclude_id: int = 0) -> bool:
        """Есть ли ДРУГОЕ активное ограничение того же kind — значит
        Discord-флаг снимать нельзя."""
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            return bool(await conn.fetchval(
                """SELECT 1 FROM voice_control_restrictions
                   WHERE guild_id=$1 AND channel_id=$2 AND user_id=$3 AND kind=$4
                     AND state='active' AND id<>$5 LIMIT 1""",
                guild_id, channel_id, user_id, kind, exclude_id,
            ))

    # ------------------------------------------------------------------
    # Permission overwrites (закрытие канала)
    # ------------------------------------------------------------------
    async def save_overwrites(self, guild_id: int, channel_id: int, entries: list[dict[str, Any]],
                              actor_id: int) -> list[int]:
        """Сохранить исходные overwrite'ы. Возвращает id созданных строк."""
        await self.ensure()
        ids: list[int] = []
        pool = await get_pool()
        async with pool.acquire() as conn:
            for e in entries:
                row = await conn.fetchrow(
                    """INSERT INTO voice_control_overwrites
                       (guild_id,channel_id,target_type,target_id,before,applied,state,created_by)
                       VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8) RETURNING id""",
                    guild_id, channel_id, e["target_type"], e["target_id"],
                    json.dumps(e.get("before")), json.dumps(e["applied"]),
                    e.get("state", "applied"), actor_id,
                )
                ids.append(int(row["id"]))
        return ids

    async def active_overwrites(self, guild_id: int, channel_id: int) -> list[dict[str, Any]]:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM voice_control_overwrites "
                "WHERE guild_id=$1 AND channel_id=$2 AND state IN ('saved','applied','conflict') "
                "ORDER BY id",
                guild_id, channel_id,
            )
        return [dict(r) for r in rows]

    async def resolve_overwrite(self, guild_id: int, overwrite_id: int, *,
                                state: str, actor_id: int = 0) -> dict[str, Any] | None:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE voice_control_overwrites
                   SET state=$3, restored_at=CASE WHEN $3='restored' THEN now() ELSE restored_at END
                   WHERE guild_id=$1 AND id=$2 AND state<>'restored'
                   RETURNING *""",
                guild_id, overwrite_id, state,
            )
            return _dict(row)

    # ------------------------------------------------------------------
    # Операции и журнал
    # ------------------------------------------------------------------
    async def op_create(self, op_id: str, guild_id: int, channel_id: int,
                        kind: str, actor_id: int, detail: dict | None = None) -> None:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO voice_control_ops(op_id,guild_id,channel_id,kind,detail,actor_id)
                   VALUES($1,$2,$3,$4,$5::jsonb,$6)
                   ON CONFLICT(op_id) DO NOTHING""",
                op_id, guild_id, channel_id, kind,
                json.dumps(detail or {}, ensure_ascii=False), actor_id,
            )

    async def op_finish(self, op_id: str, status: str, detail: dict | None = None) -> None:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE voice_control_ops
                   SET status=$2, finished_at=now(),
                       detail=detail || $3::jsonb
                   WHERE op_id=$1""",
                op_id, status, json.dumps(detail or {}, ensure_ascii=False),
            )

    async def last_reversible_op(self, guild_id: int, channel_id: int) -> dict[str, Any] | None:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            return _dict(await conn.fetchrow(
                """SELECT * FROM voice_control_ops
                   WHERE guild_id=$1 AND channel_id=$2 AND status='done'
                     AND kind IN ('bulk_mute','bulk_unmute','speaker_start','speaker_stop',
                                  'mute_member','unmute_member','close_voice','open_voice')
                   ORDER BY created_at DESC LIMIT 1""",
                guild_id, channel_id,
            ))

    async def event(self, guild_id: int, actor_id: int, action: str, *,
                    channel_id: int | None = None, detail: dict | None = None) -> None:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO voice_control_events(guild_id,actor_id,channel_id,action,detail)
                   VALUES($1,$2,$3,$4,$5::jsonb)""",
                guild_id, actor_id, channel_id, action,
                json.dumps(detail or {}, ensure_ascii=False),
            )

    async def events(self, guild_id: int, *, limit: int = 15) -> list[dict[str, Any]]:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM voice_control_events WHERE guild_id=$1 "
                "ORDER BY id DESC LIMIT $2",
                guild_id, min(50, limit),
            )
        return [dict(r) for r in rows]

    async def all_active_restrictions(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Все активные ограничения всех гильдий — для sweeper'а."""
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM voice_control_restrictions WHERE state='active' "
                "ORDER BY guild_id, channel_id, user_id LIMIT $1",
                limit,
            )
        return [dict(r) for r in rows]


voice_control_store = VoiceControlStore()
