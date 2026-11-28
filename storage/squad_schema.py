"""DDL для Squad Hub 2.0 — команды, приглашения, заявки, LFG, аудит.

Все операторы идемпотентны (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS),
поэтому безопасны при каждом старте init_db и при повторных миграциях.
Существующие таблицы и данные не удаляются.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

# Расширение teams: режим вступления, набор, цель, описание,
# требования, логотип, хеш пароля, рекрутинговая карточка.
_TEAMS_ALTER = [
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS join_mode TEXT NOT NULL DEFAULT 'applications'",
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS recruiting BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'casual'",
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS requirements JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS logo_url TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS password_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS status_message_id BIGINT",
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS recruitment_channel_id BIGINT",
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS recruitment_message_id BIGINT",
]

_STATEMENTS = [
    # Уникальность названия команды внутри сервера (без учёта регистра)
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_teams_guild_lower_name "
    "ON teams (guild_id, lower(team_name))",
    # Приглашения в команды
    """
    CREATE TABLE IF NOT EXISTS team_invites (
        invite_id BIGSERIAL PRIMARY KEY,
        guild_id BIGINT NOT NULL,
        team_id TEXT NOT NULL REFERENCES teams(team_id),
        inviter_id BIGINT NOT NULL,
        invited_user_id BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        message TEXT NOT NULL DEFAULT '',
        dm_channel_id BIGINT,
        dm_message_id BIGINT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL,
        resolved_at TIMESTAMPTZ
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_team_invites_pending "
    "ON team_invites(team_id, invited_user_id) WHERE status = 'pending'",
    "CREATE INDEX IF NOT EXISTS idx_team_invites_user "
    "ON team_invites(invited_user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_team_invites_team "
    "ON team_invites(team_id, status)",
    # Заявки на вступление
    """
    CREATE TABLE IF NOT EXISTS team_applications (
        application_id BIGSERIAL PRIMARY KEY,
        guild_id BIGINT NOT NULL,
        team_id TEXT NOT NULL REFERENCES teams(team_id),
        user_id BIGINT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL,
        resolved_at TIMESTAMPTZ
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_team_app_pending "
    "ON team_applications(team_id, user_id) WHERE status = 'pending'",
    "CREATE INDEX IF NOT EXISTS idx_team_app_user "
    "ON team_applications(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_team_app_team "
    "ON team_applications(team_id, status)",
    # «Ищу команду» (LFG)
    """
    CREATE TABLE IF NOT EXISTS team_lfg (
        guild_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        device TEXT NOT NULL DEFAULT '',
        play_role TEXT NOT NULL DEFAULT '',
        purpose TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        active BOOLEAN NOT NULL DEFAULT TRUE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_team_lfg_active "
    "ON team_lfg(guild_id) WHERE active",
    # Аудит действий с командами
    """
    CREATE TABLE IF NOT EXISTS team_audit (
        audit_id BIGSERIAL PRIMARY KEY,
        guild_id BIGINT NOT NULL,
        team_id TEXT,
        actor_id BIGINT NOT NULL,
        action TEXT NOT NULL,
        target_id BIGINT,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_team_audit_team "
    "ON team_audit(team_id, created_at DESC)",
    # Публичная карточка Squad Hub (одна на guild)
    """
    CREATE TABLE IF NOT EXISTS squad_hub_state (
        guild_id BIGINT PRIMARY KEY,
        channel_id BIGINT NOT NULL,
        message_id BIGINT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
]


async def ensure_squad_schema(conn: "asyncpg.Connection") -> None:
    """Применить DDL Squad Hub. Вызывать внутри init_db."""
    for stmt in _TEAMS_ALTER:
        await conn.execute(stmt)
    for stmt in _STATEMENTS:
        await conn.execute(stmt)
    logger.info("Squad Hub schema ensured")
