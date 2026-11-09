"""VAC Service — единая точка проверки/выдачи/снятия tournament-ban.

Источник истины — PostgreSQL (vac_store). VAC истекает по expires_at
без фоновых таймеров; проверка активна пока expires_at > now().
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from storage.vac_store import vac_store

logger = logging.getLogger(__name__)

# Максимальный срок VAC (дней), переопределяется VAC_MAX_DAYS.
import os
MAX_VAC_DAYS = int(os.getenv("VAC_MAX_DAYS", "365"))

_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")


def parse_duration(text: str) -> timedelta | None:
    """'30m', '2h', '1d', '7d', '14d', '30d', '1d12h' → timedelta.

    None — невалидный/нулевой/отрицательный/слишком большой срок.
    """
    text = (text or "").strip().lower()
    m = _DURATION_RE.match(text)
    if not m or not any(m.groups()):
        return None
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    delta = timedelta(days=days, hours=hours, minutes=minutes)
    if delta <= timedelta(0) or delta > timedelta(days=MAX_VAC_DAYS):
        return None
    return delta


async def is_vac_active(guild_id: int, user_id: int) -> bool:
    """True — у пользователя активный VAC."""
    try:
        return await vac_store.get_active(guild_id, user_id) is not None
    except Exception:
        logger.exception("vac is_vac_active failed")
        # При недоступности БД не блокируем регистрацию — fail-open,
        # чтобы сбой PG не ломал турниры.
        return False


async def get_active_vac(guild_id: int, user_id: int) -> dict | None:
    try:
        return await vac_store.get_active(guild_id, user_id)
    except Exception:
        logger.exception("vac get_active_vac failed")
        return None


async def issue_vac(
    guild_id: int, user_id: int, issued_by: int, duration: timedelta,
) -> tuple[dict | None, str | None]:
    """Выдать VAC. Возвращает (запись|None, ошибка|None)."""
    existing = await vac_store.get_active(guild_id, user_id)
    if existing:
        return None, "already_active"
    expires_at = datetime.now(timezone.utc) + duration
    row = await vac_store.issue(guild_id, user_id, issued_by, expires_at)
    if row is None:
        # Гонка двух админов — уникальный индекс отклонил второй INSERT.
        return None, "already_active"
    return row, None


async def revoke_vac(guild_id: int, user_id: int, revoked_by: int) -> dict | None:
    try:
        return await vac_store.revoke(guild_id, user_id, revoked_by)
    except Exception:
        logger.exception("vac revoke_vac failed")
        return None


async def vac_active_ids(guild_id: int, user_ids) -> set[int]:
    """Множество user_id с активным VAC из переданного списка."""
    out: set[int] = set()
    for uid in user_ids:
        try:
            if await vac_store.get_active(guild_id, int(uid)) is not None:
                out.add(int(uid))
        except Exception:
            logger.exception("vac_active_ids failed for %s", uid)
    return out


def vac_blocked_text(until_ts: int) -> str:
    return (
        "Участие в турнирах ограничено\n\n"
        "У вас активен VAC.\n"
        f"Окончание ограничения: <t:{until_ts}:R> (<t:{until_ts}:f>)\n\n"
        "До истечения срока вы не можете участвовать в турнирах r!z3."
    )


async def list_active_vacs(guild_id: int) -> list[dict]:
    try:
        return await vac_store.list_active(guild_id)
    except Exception:
        logger.exception("vac list_active_vacs failed")
        return []
