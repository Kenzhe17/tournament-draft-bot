"""PostgreSQL-хранилище динамической конфигурации Control Center.

Таблицы (создаются идемпотентно через init_schema):

- admin_config_values    — текущее значение настройки per (guild_id, key);
                           guild_id = 0 означает глобальную область.
- admin_config_history   — append-only журнал изменений настроек
                           (old/new value, version, actor, correlation_id).
                           Источник для rollback и аудита конфигурации.
- admin_audit_log        — общий аудит административных операций
                           (config.set, admin actions, finance ops).
- admin_action_requests  — очередь типизированных административных
                           операций (идемпотентность, статусы, ретраи).
- admin_role_permissions — RBAC-разрешения панели per (guild_id, user_id).
- admin_templates        — шаблоны CV2-сообщений.

Все записи настроек идут через транзакцию: values + history +
audit + pg_notify атомарны относительно друг друга.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from storage.db import get_pool

logger = logging.getLogger(__name__)

GLOBAL_SCOPE_GUILD_ID = 0
NOTIFY_CHANNEL = "admin_config_changed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_config_values (
    guild_id BIGINT NOT NULL,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_by BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, key)
);
CREATE INDEX IF NOT EXISTS idx_admin_config_values_guild
    ON admin_config_values(guild_id);

CREATE TABLE IF NOT EXISTS admin_config_history (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    key TEXT NOT NULL,
    version INTEGER NOT NULL,
    old_value JSONB,
    new_value JSONB NOT NULL,
    changed_by BIGINT NOT NULL DEFAULT 0,
    correlation_id TEXT NOT NULL DEFAULT '',
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, key, version)
);
CREATE INDEX IF NOT EXISTS idx_admin_config_history_key
    ON admin_config_history(guild_id, key, version DESC);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL DEFAULT 0,
    actor_id BIGINT NOT NULL DEFAULT 0,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL DEFAULT '',
    object_id TEXT NOT NULL DEFAULT '',
    old_value JSONB,
    new_value JSONB,
    result TEXT NOT NULL DEFAULT 'success',
    config_version INTEGER,
    correlation_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_log_guild_time
    ON admin_audit_log(guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_log_actor
    ON admin_audit_log(actor_id, created_at DESC);

CREATE TABLE IF NOT EXISTS admin_action_requests (
    id UUID PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','success','failed','cancelled')),
    result JSONB,
    error TEXT NOT NULL DEFAULT '',
    actor_id BIGINT NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    UNIQUE (guild_id, action, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_admin_action_requests_status
    ON admin_action_requests(status, created_at);

CREATE TABLE IF NOT EXISTS admin_role_permissions (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    permission TEXT NOT NULL,
    granted_by BIGINT NOT NULL DEFAULT 0,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id, permission)
);

CREATE TABLE IF NOT EXISTS admin_templates (
    guild_id BIGINT NOT NULL,
    key TEXT NOT NULL,
    payload JSONB NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_by BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, key)
);

-- Heartbeat компонентов системы (бот, панель, фоновые задачи).
-- Dashboard читает last_seen/version_info для реального статуса.
CREATE TABLE IF NOT EXISTS admin_service_status (
    service TEXT PRIMARY KEY,
    version_info JSONB NOT NULL DEFAULT '{}'::jsonb,
    extra JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


async def init_schema() -> None:
    """Создать таблицы Control Center (идемпотентно)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
    logger.info("admin_config schema initialized")


async def get_all_values() -> list[dict[str, Any]]:
    """Все текущие значения (для полной синхронизации кэша)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT guild_id, key, value, version, updated_by, updated_at"
            " FROM admin_config_values"
        )
    return [dict(r) for r in rows]


async def get_value(guild_id: int, key: str) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT guild_id, key, value, version, updated_by, updated_at"
            " FROM admin_config_values WHERE guild_id = $1 AND key = $2",
            guild_id, key,
        )
    return dict(row) if row else None


class VersionConflictError(Exception):
    """Оптимистичная блокировка: expected_version не совпал."""

    def __init__(self, current_version: int | None):
        self.current_version = current_version
        super().__init__(
            f"config version conflict: current={current_version}"
        )


async def set_value(
    guild_id: int,
    key: str,
    value: Any,
    *,
    actor_id: int = 0,
    expected_version: int | None = None,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Атомарно обновить значение: values + history + audit + NOTIFY.

    guild_id = 0 — глобальная область. expected_version — optimistic
    concurrency control: при расхождении бросает VersionConflictError
    и ничего не записывает.
    """
    pool = await get_pool()
    correlation_id = correlation_id or uuid.uuid4().hex[:16]
    value_json = _j(value)

    async with pool.acquire() as conn:  # noqa: SIM117
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT version, value FROM admin_config_values"
                " WHERE guild_id = $1 AND key = $2 FOR UPDATE",
                guild_id, key,
            )
            current_version = row["version"] if row else None
            if expected_version is not None and current_version != expected_version:
                raise VersionConflictError(current_version)

            new_version = (current_version or 0) + 1
            old_value = row["value"] if row else None

            upserted = await conn.fetchrow(
                """
                INSERT INTO admin_config_values
                    (guild_id, key, value, version, updated_by, updated_at)
                VALUES ($1, $2, $3::jsonb, $4, $5, now())
                ON CONFLICT (guild_id, key) DO UPDATE
                SET value = EXCLUDED.value,
                    version = EXCLUDED.version,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                WHERE admin_config_values.version = $6
                RETURNING guild_id, key, value, version, updated_by, updated_at
                """,
                guild_id, key, value_json, new_version, actor_id,
                current_version if current_version is not None else -1,
            )
            if upserted is None:
                # Гонка: строку изменили между SELECT FOR UPDATE и UPSERT
                # быть не может внутри транзакции, но фиксируем явно.
                raise VersionConflictError(current_version)

            await conn.execute(
                """
                INSERT INTO admin_config_history
                    (guild_id, key, version, old_value, new_value,
                     changed_by, correlation_id)
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7)
                """,
                guild_id, key, new_version,
                _j(old_value) if old_value is not None else None,
                value_json, actor_id, correlation_id,
            )
            await conn.execute(
                """
                INSERT INTO admin_audit_log
                    (guild_id, actor_id, action, object_type, object_id,
                     old_value, new_value, result, config_version,
                     correlation_id)
                VALUES ($1, $2, 'config.set', 'config', $3,
                        $4::jsonb, $5::jsonb, 'success', $6, $7)
                """,
                guild_id, actor_id, key,
                _j(old_value) if old_value is not None else None,
                value_json, new_version, correlation_id,
            )
            await conn.execute(
                "SELECT pg_notify($1, $2)",
                NOTIFY_CHANNEL,
                _j({
                    "guild_id": guild_id,
                    "key": key,
                    "version": new_version,
                }),
            )
    return dict(upserted)


async def get_history(
    guild_id: int,
    key: str,
    *,
    limit: int = 50,
    before_version: int | None = None,
) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if before_version is None:
            rows = await conn.fetch(
                """
                SELECT id, guild_id, key, version, old_value, new_value,
                       changed_by, correlation_id, changed_at
                FROM admin_config_history
                WHERE guild_id = $1 AND key = $2
                ORDER BY version DESC
                LIMIT $3
                """,
                guild_id, key, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, guild_id, key, version, old_value, new_value,
                       changed_by, correlation_id, changed_at
                FROM admin_config_history
                WHERE guild_id = $1 AND key = $2 AND version < $3
                ORDER BY version DESC
                LIMIT $4
                """,
                guild_id, key, before_version, limit,
            )
    return [dict(r) for r in rows]


async def rollback_to_version(
    guild_id: int,
    key: str,
    target_version: int,
    *,
    actor_id: int = 0,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Откатить настройку к значению указанной версии.

    Значение берётся из admin_config_history (new_value той версии).
    Сам откат — обычный set с новой версией, поэтому история
    сохраняет и факт отката.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT new_value FROM admin_config_history
            WHERE guild_id = $1 AND key = $2 AND version = $3
            """,
            guild_id, key, target_version,
        )
    if row is None:
        raise ValueError(
            f"history version {target_version} not found for {guild_id}:{key}"
        )
    return await set_value(
        guild_id, key, row["new_value"],
        actor_id=actor_id, correlation_id=correlation_id,
    )


# ---------- Admin audit log (общий) ----------

async def insert_audit(
    *,
    guild_id: int = 0,
    actor_id: int = 0,
    action: str,
    object_type: str = "",
    object_id: str = "",
    old_value: Any = None,
    new_value: Any = None,
    result: str = "success",
    config_version: int | None = None,
    correlation_id: str = "",
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admin_audit_log
                (guild_id, actor_id, action, object_type, object_id,
                 old_value, new_value, result, config_version,
                 correlation_id)
            VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,$10)
            """,
            guild_id, actor_id, action, object_type, object_id,
            _j(old_value) if old_value is not None else None,
            _j(new_value) if new_value is not None else None,
            result, config_version, correlation_id or uuid.uuid4().hex[:16],
        )


async def list_audit(
    *,
    guild_id: int | None = None,
    actor_id: int | None = None,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    pool = await get_pool()
    clauses: list[str] = []
    args: list[Any] = []
    if guild_id is not None:
        args.append(guild_id)
        clauses.append(f"guild_id = ${len(args)}")
    if actor_id is not None:
        args.append(actor_id)
        clauses.append(f"actor_id = ${len(args)}")
    if action is not None:
        args.append(action)
        clauses.append(f"action = ${len(args)}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    args.extend([limit, offset])
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, guild_id, actor_id, action, object_type, object_id,
                   old_value, new_value, result, config_version,
                   correlation_id, created_at
            FROM admin_audit_log
            {where}
            ORDER BY id DESC
            LIMIT ${len(args) - 1} OFFSET ${len(args)}
            """,
            *args,
        )
    return [dict(r) for r in rows]


# ---------- Admin action requests ----------

async def create_action_request(
    *,
    guild_id: int,
    action: str,
    parameters: dict[str, Any] | None = None,
    idempotency_key: str,
    actor_id: int = 0,
    max_attempts: int = 3,
) -> dict[str, Any] | None:
    """Создать запрос операции. None — дубликат idempotency_key."""
    pool = await get_pool()
    action_id = uuid.uuid4()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO admin_action_requests
                (id, guild_id, action, parameters, idempotency_key,
                 actor_id, max_attempts)
            VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7)
            ON CONFLICT (guild_id, action, idempotency_key) DO NOTHING
            RETURNING id, guild_id, action, parameters, idempotency_key,
                      status, actor_id, attempts, created_at
            """,
            action_id, guild_id, action, _j(parameters or {}),
            idempotency_key, actor_id, max_attempts,
        )
    return dict(row) if row else None


async def get_action_request(action_id: str) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM admin_action_requests WHERE id = $1::uuid",
            action_id,
        )
    return dict(row) if row else None


async def update_action_request(
    action_id: str,
    *,
    status: str | None = None,
    result: Any = None,
    error: str | None = None,
    increment_attempts: bool = False,
) -> None:
    pool = await get_pool()
    sets: list[str] = ["updated_at = now()"]
    args: list[Any] = []
    if status is not None:
        args.append(status)
        sets.append(f"status = ${len(args)}")
        if status in ("success", "failed", "cancelled"):
            sets.append("finished_at = now()")
    if result is not None:
        args.append(_j(result))
        sets.append(f"result = ${len(args)}::jsonb")
    if error is not None:
        args.append(error)
        sets.append(f"error = ${len(args)}")
    if increment_attempts:
        sets.append("attempts = attempts + 1")
    args.append(action_id)
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE admin_action_requests SET {', '.join(sets)}"
            f" WHERE id = ${len(args)}::uuid",
            *args,
        )


async def list_pending_actions(limit: int = 50) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM admin_action_requests
            WHERE status IN ('pending','running')
            ORDER BY created_at
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


# ---------- RBAC ----------

async def grant_permission(
    guild_id: int, user_id: int, permission: str, *, granted_by: int = 0
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admin_role_permissions
                (guild_id, user_id, permission, granted_by)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (guild_id, user_id, permission) DO NOTHING
            """,
            guild_id, user_id, permission, granted_by,
        )


async def revoke_permission(
    guild_id: int, user_id: int, permission: str
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM admin_role_permissions"
            " WHERE guild_id = $1 AND user_id = $2 AND permission = $3",
            guild_id, user_id, permission,
        )


async def list_permissions(guild_id: int) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM admin_role_permissions WHERE guild_id = $1"
            " ORDER BY user_id, permission",
            guild_id,
        )
    return [dict(r) for r in rows]


async def report_service_status(
    service: str,
    *,
    version_info: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Upsert heartbeat сервиса (бот/панель) для Dashboard."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admin_service_status (service, version_info, extra, last_seen)
            VALUES ($1, $2::jsonb, $3::jsonb, now())
            ON CONFLICT (service) DO UPDATE
            SET version_info = EXCLUDED.version_info,
                extra = EXCLUDED.extra,
                last_seen = now()
            """,
            service, _j(version_info or {}), _j(extra or {}),
        )


async def list_service_status() -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT service, version_info, extra, last_seen"
            " FROM admin_service_status ORDER BY service"
        )
    return [dict(r) for r in rows]


async def user_permissions(guild_id: int, user_id: int) -> set[str]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT permission FROM admin_role_permissions"
            " WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )
    return {r["permission"] for r in rows}
