"""PostgreSQL-хранилище Squad Hub: приглашения, заявки, LFG, аудит,
состояние публичной карточки.

Работает только при настроенном DATABASE_URL — PostgreSQL является
источником истины. Без БД операции логируются и возвращают пустой
результат (graceful degradation).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DATA_DIR  # noqa: F401  (для будущих файловых fallback)

logger = logging.getLogger(__name__)

INVITE_TTL = timedelta(hours=24)
APPLICATION_TTL = timedelta(hours=48)
LFG_TTL = timedelta(hours=72)

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"


@dataclass
class SquadInvite:
    invite_id: int
    guild_id: int
    team_id: str
    inviter_id: int
    invited_user_id: int
    status: str = STATUS_PENDING
    message: str = ""
    dm_channel_id: int | None = None
    dm_message_id: int | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    resolved_at: datetime | None = None

    @classmethod
    def from_row(cls, row) -> "SquadInvite":
        return cls(
            invite_id=int(row["invite_id"]),
            guild_id=int(row["guild_id"]),
            team_id=row["team_id"],
            inviter_id=int(row["inviter_id"]),
            invited_user_id=int(row["invited_user_id"]),
            status=row["status"],
            message=row["message"] or "",
            dm_channel_id=(int(row["dm_channel_id"])
                           if row["dm_channel_id"] is not None else None),
            dm_message_id=(int(row["dm_message_id"])
                           if row["dm_message_id"] is not None else None),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            resolved_at=row["resolved_at"],
        )

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING

    @property
    def is_expired(self) -> bool:
        if self.status != STATUS_PENDING or self.expires_at is None:
            return False
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > exp


@dataclass
class SquadApplication:
    application_id: int
    guild_id: int
    team_id: str
    user_id: int
    message: str = ""
    status: str = STATUS_PENDING
    created_at: datetime | None = None
    expires_at: datetime | None = None
    resolved_at: datetime | None = None

    @classmethod
    def from_row(cls, row) -> "SquadApplication":
        return cls(
            application_id=int(row["application_id"]),
            guild_id=int(row["guild_id"]),
            team_id=row["team_id"],
            user_id=int(row["user_id"]),
            message=row["message"] or "",
            status=row["status"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            resolved_at=row["resolved_at"],
        )

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING

    @property
    def is_expired(self) -> bool:
        if self.status != STATUS_PENDING or self.expires_at is None:
            return False
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > exp


@dataclass
class LfgProfile:
    guild_id: int
    user_id: int
    device: str = ""
    play_role: str = ""
    purpose: str = ""
    description: str = ""
    active: bool = True
    updated_at: datetime | None = None
    expires_at: datetime | None = None

    @classmethod
    def from_row(cls, row) -> "LfgProfile":
        return cls(
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            device=row["device"] or "",
            play_role=row["play_role"] or "",
            purpose=row["purpose"] or "",
            description=row["description"] or "",
            active=bool(row["active"]),
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )

    @property
    def is_active(self) -> bool:
        if not self.active:
            return False
        if self.expires_at is None:
            return True
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) <= exp


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db_url() -> str:
    """Текущий DATABASE_URL (динамически — тесты подменяют
    dbmod.DATABASE_URL после импорта)."""
    try:
        import storage.db as dbmod
        return dbmod.DATABASE_URL or ""
    except Exception:
        return ""


async def _pool():
    if not _db_url():
        return None
    from storage.db import get_pool
    return await get_pool()


# ----------------------------------------------------------------------
# Аудит
# ----------------------------------------------------------------------

async def audit(guild_id: int, team_id: str | None, actor_id: int,
                action: str, target_id: int | None = None,
                metadata: dict | None = None) -> None:
    """Записать действие в team_audit. Не бросает исключений."""
    try:
        pool = await _pool()
        if pool is None:
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO team_audit
                    (guild_id, team_id, actor_id, action, target_id,
                     metadata)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb)
                """,
                guild_id, team_id, actor_id, action, target_id,
                json.dumps(metadata or {}),
            )
    except Exception as exc:
        logger.warning("team_audit write failed: %s", exc)


# ----------------------------------------------------------------------
# Приглашения
# ----------------------------------------------------------------------

async def create_invite(guild_id: int, team_id: str, inviter_id: int,
                        invited_user_id: int,
                        message: str = "") -> SquadInvite | None:
    """Создать приглашение (pending, TTL 24ч). None — дубликат/ошибка."""
    pool = await _pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Истёкшие pending не должны блокировать новое
                # приглашение (частичный уникальный индекс считает
                # их занятыми до явного протухания).
                await conn.execute(
                    "UPDATE team_invites SET status = 'expired', "
                    "resolved_at = now() WHERE team_id = $1 "
                    "AND invited_user_id = $2 AND status = 'pending' "
                    "AND expires_at < now()",
                    team_id, invited_user_id)
                row = await conn.fetchrow(
                """
                INSERT INTO team_invites
                    (guild_id, team_id, inviter_id, invited_user_id,
                     message, expires_at)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (team_id, invited_user_id)
                    WHERE status = 'pending'
                DO NOTHING
                RETURNING invite_id, guild_id, team_id, inviter_id,
                          invited_user_id, status, message,
                          dm_channel_id, dm_message_id, created_at,
                          expires_at, resolved_at
                """,
                guild_id, team_id, inviter_id, invited_user_id,
                message, _now() + INVITE_TTL,
            )
            if row is None:
                return None
            return SquadInvite.from_row(row)
    except Exception as exc:
        logger.warning("create_invite failed: %s", exc)
        return None


_INVITE_COLS = ("invite_id, guild_id, team_id, inviter_id, "
                "invited_user_id, status, message, dm_channel_id, "
                "dm_message_id, created_at, expires_at, resolved_at")


async def get_invite(invite_id: int) -> SquadInvite | None:
    pool = await _pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_INVITE_COLS} FROM team_invites "
                "WHERE invite_id = $1", invite_id)
            return SquadInvite.from_row(row) if row else None
    except Exception as exc:
        logger.warning("get_invite failed: %s", exc)
        return None


async def list_pending_invites_for_user(guild_id: int,
                                        user_id: int) -> list[SquadInvite]:
    """Активные приглашения пользователя (pending, не истёкшие)."""
    pool = await _pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE team_invites SET status = 'expired', "
                "resolved_at = now() WHERE invited_user_id = $1 "
                "AND status = 'pending' AND expires_at < now()",
                user_id)
            rows = await conn.fetch(
                f"SELECT {_INVITE_COLS} FROM team_invites "
                "WHERE guild_id = $1 AND invited_user_id = $2 "
                "AND status = 'pending' ORDER BY created_at DESC",
                guild_id, user_id)
            return [SquadInvite.from_row(r) for r in rows]
    except Exception as exc:
        logger.warning("list_pending_invites failed: %s", exc)
        return []


async def list_pending_invites_for_team(team_id: str) -> list[SquadInvite]:
    pool = await _pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE team_invites SET status = 'expired', "
                "resolved_at = now() WHERE team_id = $1 "
                "AND status = 'pending' AND expires_at < now()",
                team_id)
            rows = await conn.fetch(
                f"SELECT {_INVITE_COLS} FROM team_invites "
                "WHERE team_id = $1 AND status = 'pending' "
                "ORDER BY created_at DESC", team_id)
            return [SquadInvite.from_row(r) for r in rows]
    except Exception as exc:
        logger.warning("list_team_invites failed: %s", exc)
        return []


async def set_invite_dm(invite_id: int, channel_id: int,
                        message_id: int) -> None:
    pool = await _pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE team_invites SET dm_channel_id = $2, "
                "dm_message_id = $3 WHERE invite_id = $1",
                invite_id, channel_id, message_id)
    except Exception as exc:
        logger.warning("set_invite_dm failed: %s", exc)


async def resolve_invite(invite_id: int, status: str) -> bool:
    """Перевести приглашение в финальный статус (accepted/declined/
    cancelled). True — обновлено (была pending)."""
    pool = await _pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE team_invites SET status = $2, resolved_at = now() "
                "WHERE invite_id = $1 AND status = 'pending'",
                invite_id, status)
            return res.endswith("1")
    except Exception as exc:
        logger.warning("resolve_invite failed: %s", exc)
        return False


async def cancel_invites_for_user(guild_id: int, user_id: int,
                                  reason: str = "joined_team") -> int:
    """Закрыть все pending-приглашения пользователя (вступил в команду)."""
    pool = await _pool()
    if pool is None:
        return 0
    try:
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE team_invites SET status = 'cancelled', "
                "resolved_at = now(), message = message || ' [' || $3 || ']' "
                "WHERE guild_id = $1 AND invited_user_id = $2 "
                "AND status = 'pending'",
                guild_id, user_id, reason)
            try:
                return int(res.split()[-1])
            except (ValueError, IndexError):
                return 0
    except Exception as exc:
        logger.warning("cancel_invites_for_user failed: %s", exc)
        return 0


async def cancel_invites_for_team(team_id: str) -> int:
    pool = await _pool()
    if pool is None:
        return 0
    try:
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE team_invites SET status = 'cancelled', "
                "resolved_at = now() WHERE team_id = $1 "
                "AND status = 'pending'", team_id)
            try:
                return int(res.split()[-1])
            except (ValueError, IndexError):
                return 0
    except Exception as exc:
        logger.warning("cancel_invites_for_team failed: %s", exc)
        return 0


# ----------------------------------------------------------------------
# Заявки
# ----------------------------------------------------------------------

_APP_COLS = ("application_id, guild_id, team_id, user_id, message, "
             "status, created_at, expires_at, resolved_at")


async def create_application(guild_id: int, team_id: str, user_id: int,
                             message: str = "") -> SquadApplication | None:
    """Подать заявку (pending, TTL 48ч). None — дубликат/ошибка."""
    pool = await _pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE team_applications SET status = 'expired', "
                    "resolved_at = now() WHERE team_id = $1 "
                    "AND user_id = $2 AND status = 'pending' "
                    "AND expires_at < now()",
                    team_id, user_id)
                row = await conn.fetchrow(
                """
                INSERT INTO team_applications
                    (guild_id, team_id, user_id, message, expires_at)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT (team_id, user_id)
                    WHERE status = 'pending'
                DO NOTHING
                RETURNING application_id, guild_id, team_id, user_id,
                          message, status, created_at, expires_at,
                          resolved_at
                """,
                guild_id, team_id, user_id, message,
                _now() + APPLICATION_TTL,
            )
            if row is None:
                return None
            return SquadApplication.from_row(row)
    except Exception as exc:
        logger.warning("create_application failed: %s", exc)
        return None


async def get_application(application_id: int) -> SquadApplication | None:
    pool = await _pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_APP_COLS} FROM team_applications "
                "WHERE application_id = $1", application_id)
            return SquadApplication.from_row(row) if row else None
    except Exception as exc:
        logger.warning("get_application failed: %s", exc)
        return None


async def list_pending_applications_for_team(
        team_id: str) -> list[SquadApplication]:
    pool = await _pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE team_applications SET status = 'expired', "
                "resolved_at = now() WHERE team_id = $1 "
                "AND status = 'pending' AND expires_at < now()",
                team_id)
            rows = await conn.fetch(
                f"SELECT {_APP_COLS} FROM team_applications "
                "WHERE team_id = $1 AND status = 'pending' "
                "ORDER BY created_at", team_id)
            return [SquadApplication.from_row(r) for r in rows]
    except Exception as exc:
        logger.warning("list_team_applications failed: %s", exc)
        return []


async def list_pending_applications_for_user(
        guild_id: int, user_id: int) -> list[SquadApplication]:
    pool = await _pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE team_applications SET status = 'expired', "
                "resolved_at = now() WHERE user_id = $1 "
                "AND status = 'pending' AND expires_at < now()",
                user_id)
            rows = await conn.fetch(
                f"SELECT {_APP_COLS} FROM team_applications "
                "WHERE guild_id = $1 AND user_id = $2 "
                "AND status = 'pending' ORDER BY created_at DESC",
                guild_id, user_id)
            return [SquadApplication.from_row(r) for r in rows]
    except Exception as exc:
        logger.warning("list_user_applications failed: %s", exc)
        return []


async def resolve_application(application_id: int, status: str) -> bool:
    pool = await _pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE team_applications SET status = $2, "
                "resolved_at = now() WHERE application_id = $1 "
                "AND status = 'pending'",
                application_id, status)
            return res.endswith("1")
    except Exception as exc:
        logger.warning("resolve_application failed: %s", exc)
        return False


async def _captain_gate_on(conn, team_id: str,
                           actor_id: int) -> bool:
    """Проверка актуального капитанства под FOR UPDATE на строке
    teams в текущей транзакции. False — actor не капитан
    (бывший капитан не завершает ранее открытую операцию)."""
    cap = await conn.fetchval(
        "SELECT captain_id FROM teams WHERE team_id = $1 "
        "FOR UPDATE", team_id)
    return cap is not None and int(cap) == int(actor_id)


async def resolve_application_as_captain(
        application_id: int, actor_id: int,
        status: str = "declined") -> bool:
    """Капитан отклоняет/обрабатывает заявку: актуальное
    капитанство проверяется под FOR UPDATE в той же транзакции,
    что и обновление заявки."""
    pool = await _pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT team_id FROM team_applications "
                    "WHERE application_id = $1 "
                    "AND status = 'pending'", application_id)
                if row is None:
                    return False
                if not await _captain_gate_on(
                        conn, row["team_id"], actor_id):
                    return False
                res = await conn.execute(
                    "UPDATE team_applications SET status = $2, "
                    "resolved_at = now() WHERE application_id = $1 "
                    "AND status = 'pending'",
                    application_id, status)
                return res.endswith("1")
    except Exception as exc:
        logger.warning("resolve_application_as_captain failed: %s",
                       exc)
        return False


async def cancel_invite_as_captain(invite_id: int,
                                   actor_id: int) -> bool:
    """Капитан отзывает отправленное приглашение: проверка
    капитанства под FOR UPDATE в той же транзакции."""
    pool = await _pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT team_id FROM team_invites "
                    "WHERE invite_id = $1 AND status = 'pending'",
                    invite_id)
                if row is None:
                    return False
                if not await _captain_gate_on(
                        conn, row["team_id"], actor_id):
                    return False
                res = await conn.execute(
                    "UPDATE team_invites SET status = 'cancelled', "
                    "resolved_at = now() WHERE invite_id = $1 "
                    "AND status = 'pending'", invite_id)
                return res.endswith("1")
    except Exception as exc:
        logger.warning("cancel_invite_as_captain failed: %s", exc)
        return False


async def withdraw_application(application_id: int,
                               user_id: int) -> bool:
    """Игрок отзывает свою заявку."""
    pool = await _pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE team_applications SET status = 'cancelled', "
                "resolved_at = now() WHERE application_id = $1 "
                "AND user_id = $2 AND status = 'pending'",
                application_id, user_id)
            return res.endswith("1")
    except Exception as exc:
        logger.warning("withdraw_application failed: %s", exc)
        return False


async def cancel_applications_for_user(guild_id: int, user_id: int,
                                       reason: str = "joined_team") -> int:
    """Закрыть все pending-заявки пользователя (вступил в команду)."""
    pool = await _pool()
    if pool is None:
        return 0
    try:
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE team_applications SET status = 'cancelled', "
                "resolved_at = now(), message = message || ' [' || $3 || ']' "
                "WHERE guild_id = $1 AND user_id = $2 "
                "AND status = 'pending'",
                guild_id, user_id, reason)
            try:
                return int(res.split()[-1])
            except (ValueError, IndexError):
                return 0
    except Exception as exc:
        logger.warning("cancel_applications_for_user failed: %s", exc)
        return 0


async def cancel_applications_for_team(team_id: str,
                                       status: str = "cancelled") -> int:
    pool = await _pool()
    if pool is None:
        return 0
    try:
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE team_applications SET status = $2, "
                "resolved_at = now() WHERE team_id = $1 "
                "AND status = 'pending'", team_id, status)
            try:
                return int(res.split()[-1])
            except (ValueError, IndexError):
                return 0
    except Exception as exc:
        logger.warning("cancel_applications_for_team failed: %s", exc)
        return 0


# ----------------------------------------------------------------------
# LFG — «Ищу команду»
# ----------------------------------------------------------------------


async def upsert_lfg(guild_id: int, user_id: int, *, device: str = "",
                     play_role: str = "", purpose: str = "",
                     description: str = "",
                     ttl: timedelta = LFG_TTL) -> LfgProfile | None:
    """Включить/обновить поиск команды. Срок — 72ч по умолчанию."""
    pool = await _pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO team_lfg
                    (guild_id, user_id, device, play_role, purpose,
                     description, active, updated_at, expires_at)
                VALUES ($1,$2,$3,$4,$5,$6,TRUE,now(),$7)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    device = EXCLUDED.device,
                    play_role = EXCLUDED.play_role,
                    purpose = EXCLUDED.purpose,
                    description = EXCLUDED.description,
                    active = TRUE,
                    updated_at = now(),
                    expires_at = EXCLUDED.expires_at
                RETURNING guild_id, user_id, device, play_role, purpose,
                          description, active, updated_at, expires_at
                """,
                guild_id, user_id, device, play_role, purpose,
                description, _now() + ttl,
            )
            return LfgProfile.from_row(row) if row else None
    except Exception as exc:
        logger.warning("upsert_lfg failed: %s", exc)
        return None


async def get_lfg(guild_id: int, user_id: int) -> LfgProfile | None:
    pool = await _pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT guild_id, user_id, device, play_role, purpose, "
                "description, active, updated_at, expires_at "
                "FROM team_lfg WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id)
            return LfgProfile.from_row(row) if row else None
    except Exception as exc:
        logger.warning("get_lfg failed: %s", exc)
        return None


async def deactivate_lfg(guild_id: int, user_id: int) -> bool:
    pool = await _pool()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE team_lfg SET active = FALSE, updated_at = now() "
                "WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id)
            return res.endswith("1")
    except Exception as exc:
        logger.warning("deactivate_lfg failed: %s", exc)
        return False


async def list_lfg(guild_id: int, *, exclude_user_ids: set[int] | None = None,
                   device: str = "", play_role: str = "",
                   purpose: str = "", limit: int = 25) -> list[LfgProfile]:
    """Активные профили поиска (не истёкшие, без пользователей
    уже состоящих в командах — фильтрация снаружи)."""
    pool = await _pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE team_lfg SET active = FALSE "
                "WHERE guild_id = $1 AND active AND expires_at < now()",
                guild_id)
            rows = await conn.fetch(
                "SELECT guild_id, user_id, device, play_role, purpose, "
                "description, active, updated_at, expires_at "
                "FROM team_lfg WHERE guild_id = $1 AND active "
                "AND expires_at > now() "
                "ORDER BY updated_at DESC LIMIT $2",
                guild_id, max(1, min(limit, 100)))
            out = [LfgProfile.from_row(r) for r in rows]
            if exclude_user_ids:
                out = [p for p in out if p.user_id not in exclude_user_ids]
            if device:
                out = [p for p in out if p.device == device]
            if play_role:
                out = [p for p in out if p.play_role == play_role]
            if purpose:
                out = [p for p in out if p.purpose == purpose]
            return out
    except Exception as exc:
        logger.warning("list_lfg failed: %s", exc)
        return []


async def deactivate_lfg_for_user(guild_id: int, user_id: int) -> None:
    """Авто-отключение поиска после вступления в команду."""
    try:
        await deactivate_lfg(guild_id, user_id)
    except Exception:
        pass


# ----------------------------------------------------------------------
# Conn-scoped варианты — для использования ВНУТРИ транзакции
# team_store.join_team_tx (без вложенного acquire из пула).
# ----------------------------------------------------------------------

async def audit_on(conn, guild_id: int, team_id: str | None,
                   actor_id: int, action: str,
                   target_id: int | None = None,
                   metadata: dict | None = None) -> None:
    await conn.execute(
        """
        INSERT INTO team_audit
            (guild_id, team_id, actor_id, action, target_id, metadata)
        VALUES ($1,$2,$3,$4,$5,$6::jsonb)
        """,
        guild_id, team_id, actor_id, action, target_id,
        json.dumps(metadata or {}),
    )


async def resolve_invite_on(conn, invite_id: int, status: str,
                            *, require_unexpired: bool = False) -> bool:
    """resolve_invite на уже открытом соединении.

    ``require_unexpired=True`` — дополнительно ``expires_at > now()``
    (для принятия: истёкшее приглашение нельзя активировать)."""
    sql = ("UPDATE team_invites SET status = $2, resolved_at = now() "
           "WHERE invite_id = $1 AND status = 'pending'")
    if require_unexpired:
        sql += " AND expires_at > now()"
    res = await conn.execute(sql, invite_id, status)
    return res.endswith("1")


async def get_invite_on(conn, invite_id: int) -> SquadInvite | None:
    row = await conn.fetchrow(
        f"SELECT {_INVITE_COLS} FROM team_invites WHERE invite_id = $1",
        invite_id)
    return SquadInvite.from_row(row) if row else None


async def resolve_application_on(conn, application_id: int,
                                 status: str,
                                 *, require_unexpired: bool = False
                                 ) -> bool:
    sql = ("UPDATE team_applications SET status = $2, "
           "resolved_at = now() WHERE application_id = $1 "
           "AND status = 'pending'")
    if require_unexpired:
        sql += " AND expires_at > now()"
    res = await conn.execute(sql, application_id, status)
    return res.endswith("1")


async def get_application_on(conn, application_id: int
                             ) -> SquadApplication | None:
    row = await conn.fetchrow(
        f"SELECT {_APP_COLS} FROM team_applications "
        "WHERE application_id = $1", application_id)
    return SquadApplication.from_row(row) if row else None


async def close_user_membership_on(conn, guild_id: int, user_id: int,
                                   reason: str = "joined_team") -> None:
    """В одной транзакции: закрыть заявки/приглашения игрока и
    выключить LFG. Вызывать после успешного добавления в команду."""
    await conn.execute(
        "UPDATE team_applications SET status = 'cancelled', "
        "resolved_at = now(), message = message || ' [' || $3 || ']' "
        "WHERE guild_id = $1 AND user_id = $2 AND status = 'pending'",
        guild_id, user_id, reason)
    await conn.execute(
        "UPDATE team_invites SET status = 'cancelled', "
        "resolved_at = now(), message = message || ' [' || $3 || ']' "
        "WHERE guild_id = $1 AND invited_user_id = $2 "
        "AND status = 'pending'",
        guild_id, user_id, reason)
    await conn.execute(
        "UPDATE team_lfg SET active = FALSE, updated_at = now() "
        "WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id)


async def mark_team_full_applications_on(conn, team_id: str) -> int:
    """Закрыть pending-заявки заполненной команды."""
    res = await conn.execute(
        "UPDATE team_applications SET status = 'team_full', "
        "resolved_at = now() WHERE team_id = $1 AND status = 'pending'",
        team_id)
    try:
        return int(res.split()[-1])
    except (ValueError, IndexError):
        return 0


async def get_lfg_on(conn, guild_id: int,
                     user_id: int) -> LfgProfile | None:
    row = await conn.fetchrow(
        "SELECT guild_id, user_id, device, play_role, purpose, "
        "description, active, updated_at, expires_at "
        "FROM team_lfg WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id)
    return LfgProfile.from_row(row) if row else None


# ----------------------------------------------------------------------
# Squad Hub state + статистика каталога
# ----------------------------------------------------------------------

async def get_hub_state(guild_id: int) -> dict[str, int] | None:
    pool = await _pool()
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT channel_id, message_id FROM squad_hub_state "
                "WHERE guild_id = $1", guild_id)
            if row is None:
                return None
            return {"channel_id": int(row["channel_id"]),
                    "message_id": int(row["message_id"])}
    except Exception as exc:
        logger.warning("get_hub_state failed: %s", exc)
        return None


async def set_hub_state(guild_id: int, channel_id: int,
                        message_id: int) -> None:
    pool = await _pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO squad_hub_state (guild_id, channel_id,
                                             message_id, updated_at)
                VALUES ($1,$2,$3,now())
                ON CONFLICT (guild_id) DO UPDATE SET
                    channel_id = EXCLUDED.channel_id,
                    message_id = EXCLUDED.message_id,
                    updated_at = now()
                """, guild_id, channel_id, message_id)
    except Exception as exc:
        logger.warning("set_hub_state failed: %s", exc)


async def clear_hub_state(guild_id: int) -> None:
    pool = await _pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM squad_hub_state WHERE guild_id = $1",
                guild_id)
    except Exception as exc:
        logger.warning("clear_hub_state failed: %s", exc)


async def catalog_teams(guild_id: int) -> list[dict[str, Any]]:
    """Команды, доступные для каталога: набор открыт, есть места,
    не закрытый режим. Возвращает список dict с полями team.
    """
    from models.team import JOIN_CLOSED, TEAM_MAX_MEMBERS
    pool = await _pool()
    if pool is None:
        # JSON-fallback: отдать из кэша team_store
        from storage.team_store import team_store
        out = []
        for t in team_store.get_by_guild(guild_id):
            if (t.recruiting and not t.is_full()
                    and t.join_mode != JOIN_CLOSED):
                out.append(_team_brief(t))
        return out
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_TEAM_BRIEF_COLS} FROM teams
                WHERE guild_id = $1 AND recruiting = TRUE
                  AND join_mode <> 'closed'
                  AND cardinality(members) < {TEAM_MAX_MEMBERS}
                ORDER BY updated_at DESC
                """, guild_id)
            return [_row_brief(r) for r in rows]
    except Exception as exc:
        logger.warning("catalog_teams failed: %s", exc)
        return []


_TEAM_BRIEF_COLS = (
    "team_id, team_name, captain_id, members, join_mode, recruiting, "
    "purpose, description, requirements, logo_url, created_at, updated_at"
)


def _team_brief(t) -> dict[str, Any]:
    return {
        "team_id": t.team_id, "team_name": t.team_name,
        "captain_id": t.captain_id, "members": list(t.members),
        "join_mode": t.join_mode, "recruiting": t.recruiting,
        "purpose": t.purpose, "description": t.description,
        "requirements": dict(t.requirements or {}),
        "logo_url": t.logo_url, "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


def _row_brief(r) -> dict[str, Any]:
    req = r["requirements"]
    if isinstance(req, str):
        try:
            req = json.loads(req)
        except (ValueError, TypeError):
            req = {}
    members = r["members"] or []
    return {
        "team_id": r["team_id"], "team_name": r["team_name"],
        "captain_id": int(r["captain_id"]),
        "members": [int(m) for m in members],
        "join_mode": r["join_mode"], "recruiting": bool(r["recruiting"]),
        "purpose": r["purpose"], "description": r["description"] or "",
        "requirements": req or {},
        "logo_url": r["logo_url"] or "",
        "created_at": (r["created_at"].isoformat()
                       if r["created_at"] else ""),
        "updated_at": (r["updated_at"].isoformat()
                       if r["updated_at"] else ""),
    }


async def squad_hub_stats(guild_id: int) -> dict[str, int]:
    """Счётчики для публичной карточки Squad Hub."""
    pool = await _pool()
    if pool is None:
        from storage.team_store import team_store
        teams = team_store.get_by_guild(guild_id)
        return {
            "teams": len(teams),
            "recruiting": sum(1 for t in teams if t.recruiting),
            "lfg": 0,
        }
    try:
        async with pool.acquire() as conn:
            teams_total = await conn.fetchval(
                "SELECT count(*) FROM teams WHERE guild_id = $1", guild_id)
            teams_recruiting = await conn.fetchval(
                "SELECT count(*) FROM teams WHERE guild_id = $1 "
                "AND recruiting = TRUE", guild_id)
            lfg_count = await conn.fetchval(
                "SELECT count(*) FROM team_lfg WHERE guild_id = $1 "
                "AND active = TRUE AND expires_at > now()", guild_id)
            return {
                "teams": int(teams_total or 0),
                "recruiting": int(teams_recruiting or 0),
                "lfg": int(lfg_count or 0),
            }
    except Exception as exc:
        logger.warning("squad_hub_stats failed: %s", exc)
        return {"teams": 0, "recruiting": 0, "lfg": 0}
