"""VAC Service — единая точка проверки/выдачи/снятия tournament-ban.

Источник истины — PostgreSQL (vac_store). VAC истекает по expires_at
без фоновых таймеров; проверка активна пока expires_at > now().

Автоматические нарушения идут через record_violation: прогрессивная
лестница 3м→10м→30м→1ч→3ч→6ч (уровни 1–6), снижение уровня на 1 за
каждые полные 24ч без нарушений. Ручной /vac задаёт срок явно и
уровень не повышает; /unvac историю не сбрасывает.
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


class VacStoreUnavailable(Exception):
    """VAC-хранилище недоступно — регистрацию нужно отклонить
    временной ошибкой, а не считать игрока незаблокированным."""


async def check_vac(guild_id: int, user_id: int) -> dict | None:
    """Единый API проверки VAC. Возвращает активную запись или None.

    Запись содержит expires_at, reason, level (если выдан
    автоматически), issued_at. При сбое БД бросает
    VacStoreUnavailable — fail-closed для точек входа.
    """
    try:
        return await vac_store.get_active(guild_id, user_id)
    except Exception as e:
        logger.exception("vac check_vac failed")
        raise VacStoreUnavailable(str(e)) from e


async def is_vac_active(guild_id: int, user_id: int) -> bool:
    """True — у пользователя активный VAC."""
    return await check_vac(guild_id, user_id) is not None


async def get_active_vac(guild_id: int, user_id: int) -> dict | None:
    """Alias of check_vac — обратная совместимость."""
    return await check_vac(guild_id, user_id)


async def issue_vac(
    guild_id: int, user_id: int, issued_by: int, duration: timedelta,
    reason: str | None = None,
) -> tuple[dict | None, str | None]:
    """Выдать VAC вручную (/vac). Не повышает автоматический уровень.

    Возвращает (запись|None, ошибка|None).
    """
    try:
        existing = await check_vac(guild_id, user_id)
    except VacStoreUnavailable:
        return None, "store_error"
    if existing:
        return None, "already_active"
    expires_at = datetime.now(timezone.utc) + duration
    row = await vac_store.issue(
        guild_id, user_id, issued_by, expires_at, reason=reason
    )
    if row is None:
        # Гонка двух админов — уникальный индекс отклонил второй INSERT.
        return None, "already_active"
    return row, None


async def record_violation(
    guild_id: int,
    user_id: int,
    issued_by: int,
    reason: str,
    event_key: str | None = None,
) -> tuple[dict | None, str | None]:
    """Автоматическое нарушение → прогрессивный VAC.

    Возвращает (запись|None, ошибка|None). None при дубликате
    event_key — то же событие уже наказано, повторно не наказывать.
    """
    try:
        row = await vac_store.record_violation(
            guild_id, user_id, issued_by, reason, event_key=event_key
        )
        return row, None
    except Exception:
        logger.exception("vac record_violation failed")
        return None, "store_error"


async def revoke_vac(guild_id: int, user_id: int, revoked_by: int) -> dict | None:
    try:
        return await vac_store.revoke(guild_id, user_id, revoked_by)
    except Exception:
        logger.exception("vac revoke_vac failed")
        return None


async def vac_active_ids(guild_id: int, user_ids) -> set[int]:
    """Множество user_id с активным VAC из переданного списка.

    При сбое VAC-хранилища бросает VacStoreUnavailable — вызывающий
    код обязан прервать операцию, а не пропускать проверку.
    """
    out: set[int] = set()
    for uid in user_ids:
        if await check_vac(guild_id, int(uid)) is not None:
            out.add(int(uid))
    return out


def vac_blocked_text(until_ts: int, reason: str | None = None,
                     level: int | None = None) -> str:
    """Текст отказа: причина, окончание и оставшееся время."""
    lines = [
        "Участие в турнирах ограничено",
        "",
        "У вас активен VAC.",
    ]
    if level:
        lines.append(f"Уровень наказания: {level}")
    if reason:
        lines.append(f"Причина: {reason}")
    lines += [
        f"Окончание ограничения: <t:{until_ts}:R> (<t:{until_ts}:f>)",
        f"Осталось: <t:{until_ts}:R>",
        "",
        ("До истечения срока вы не можете участвовать в турнирах r!z3 "
         "— все режимы, без исключений."),
    ]
    return "\n".join(lines)


async def list_active_vacs(guild_id: int) -> list[dict]:
    try:
        return await vac_store.list_active(guild_id)
    except Exception:
        logger.exception("vac list_active_vacs failed")
        return []
