"""DDL для Squad Hub 2.0 — команды, приглашения, заявки, LFG, аудит.

Все операторы идемпотентны (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS),
поэтому безопасны при каждом старте init_db и при повторных миграциях.
Существующие таблицы и данные не удаляются.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

# Расширение teams: режим вступления, набор, цель, описание,
# требования, логотип, хеш пароля, рекрутинговая карточка.
_TEAMS_ALTER = [
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS join_mode TEXT NOT NULL DEFAULT 'applications'",
    # recruiting default FALSE на уровне колонки: существующие строки
    # получают FALSE (см. _LEGACY_MIGRATION), новые команды создаются
    # с recruiting=TRUE явно через Team.
    "ALTER TABLE teams ADD COLUMN IF NOT EXISTS recruiting BOOLEAN NOT NULL DEFAULT FALSE",
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

# Миграция legacy-команд (созданных до Squad Hub):
# - вход только по существующему паролю → join_mode='password';
# - публичный набор НЕ включаем автоматически → recruiting=FALSE;
# - пароль будет захеширован при load_from_postgres (password →
#   password_hash), здесь только помечаем режим.
# Идемпотентно: срабатывает только на строках, где password ещё
# хранится plaintext (password <> '' AND password_hash = '').
_LEGACY_MIGRATION = [
    """
    UPDATE teams
    SET join_mode = 'password', recruiting = FALSE,
        updated_at = COALESCE(updated_at, now())
    WHERE password <> '' AND COALESCE(password_hash, '') = ''
      AND COALESCE(join_mode, '') <> 'password'
    """,
]

_STATEMENTS = [
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


async def _audit_legacy_teams(conn: "asyncpg.Connection") -> dict:
    """Проверить существующие команды на конфликты ДО создания
    уникального индекса. Возвращает отчёт; при конфликтах уникальный
    индекс НЕ создаётся (безопасный план: ручное разрешение
    администратором, автопереименование запрещено)."""
    report: dict[str, Any] = {
        "duplicate_names": [],
        "oversized_teams": [],
        "captain_not_member": [],
        "multi_team_members": [],
    }
    try:
        rows = await conn.fetch("""
            SELECT guild_id, lower(team_name) AS lname,
                   array_agg(team_id) AS ids, count(*) AS n
            FROM teams GROUP BY guild_id, lower(team_name)
            HAVING count(*) > 1
        """)
        for r in rows:
            report["duplicate_names"].append({
                "guild_id": int(r["guild_id"]),
                "name": r["lname"], "team_ids": list(r["ids"])})
    except Exception as exc:
        logger.warning("legacy audit dup-names failed: %s", exc)
    try:
        rows = await conn.fetch(
            "SELECT team_id, guild_id, team_name, captain_id, members "
            "FROM teams")
        seen: dict[tuple[int, int], str] = {}
        for r in rows:
            tid = r["team_id"]
            gid = int(r["guild_id"])
            members = [int(m) for m in (r["members"] or [])]
            cap = int(r["captain_id"])
            if len(members) > 4:
                report["oversized_teams"].append({
                    "team_id": tid, "guild_id": gid,
                    "team_name": r["team_name"],
                    "members": len(members)})
            if cap not in members:
                report["captain_not_member"].append({
                    "team_id": tid, "guild_id": gid,
                    "team_name": r["team_name"], "captain_id": cap})
            for m in members:
                key = (gid, m)
                if key in seen and seen[key] != tid:
                    report["multi_team_members"].append({
                        "guild_id": gid, "user_id": m,
                        "team_ids": [seen[key], tid]})
                else:
                    seen[key] = tid
    except Exception as exc:
        logger.warning("legacy audit roster failed: %s", exc)
    return report


async def _ensure_unique_name_index(conn: "asyncpg.Connection") -> None:
    """Уникальный индекс по (guild_id, lower(team_name)) — только
    если нет конфликтов. При конфликтах пишем отчёт и создаём
    обычный (не уникальный) индекс для производительности."""
    report = await _audit_legacy_teams(conn)
    conflicts = any(report.values())
    if conflicts:
        try:
            import json as _json
            from config import DATA_DIR
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = DATA_DIR / "squad_migration_report.json"
            path.write_text(
                _json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as exc:
            logger.warning("migration report write failed: %s", exc)
        logger.error(
            "Squad Hub: найдены конфликты legacy-команд %s — "
            "уникальный индекс названий НЕ создан. Отчёт: %s. "
            "Требуется ручное разрешение (команды не изменялись).",
            {k: len(v) for k, v in report.items()},
            "squad_migration_report.json")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_teams_guild_lower_name "
            "ON teams (guild_id, lower(team_name))")
        return
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_teams_guild_lower_name "
        "ON teams (guild_id, lower(team_name))")


async def ensure_squad_schema(conn: "asyncpg.Connection") -> None:
    """Применить DDL Squad Hub. Вызывать внутри init_db."""
    for stmt in _TEAMS_ALTER:
        await conn.execute(stmt)
    for stmt in _LEGACY_MIGRATION:
        await conn.execute(stmt)
    for stmt in _STATEMENTS:
        await conn.execute(stmt)
    await _ensure_unique_name_index(conn)
    logger.info("Squad Hub schema ensured")
