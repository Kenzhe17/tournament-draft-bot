"""Централизованный менеджер пула постоянных каналов матчей.

5 постоянных каналов из ``MM_MATCH_CHANNEL_POOL`` переиспользуются.
Состояние каждого слота хранится в PostgreSQL (таблица
``match_channel_slots``) — это гарантирует атомарность даже при
нескольких процессах бота: захват выполняется через
``SELECT ... FOR UPDATE SKIP LOCKED`` в транзакции.

Состояния слота:
    free      — доступен для захвата
    reserved  — захвачен, идёт настройка прав/очистка
    active    — матч идёт, канал назначен
    cleaning  — результат подтверждён, идёт очистка
    error     — что-то пошло не так, слот выведен из ротации

Все Discord-операции (overwrites, purge) проходят через этот модуль
с ограниченным retry при HTTP 429 (учитывается ``retry_after``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from config import MM_MATCH_CHANNEL_POOL
from storage.db import get_pool

logger = logging.getLogger(__name__)

FREE = "free"
RESERVED = "reserved"
ACTIVE = "active"
CLEANING = "cleaning"
ERROR = "error"
ALL_STATES = (FREE, RESERVED, ACTIVE, CLEANING, ERROR)

# Максимум попыток Discord-вызова при 429 и пауза между ними
_MAX_429_RETRIES = 2
_MAX_RETRY_AFTER = 60.0

_pool_ids: list[int] | None = None


def pool_channel_ids() -> list[int]:
    global _pool_ids
    if _pool_ids is None:
        _pool_ids = [int(x) for x in MM_MATCH_CHANNEL_POOL]
    return _pool_ids


# ---------------------------------------------------------------------------
# PostgreSQL: атомарные переходы состояний
# ---------------------------------------------------------------------------


async def ensure_slots() -> None:
    """Зарегистрировать все ID из конфига как free-слоты (идемпотентно)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        for cid in pool_channel_ids():
            await conn.execute(
                """
                INSERT INTO match_channel_slots (channel_id, state)
                VALUES ($1, 'free')
                ON CONFLICT (channel_id) DO NOTHING
                """,
                cid,
            )


async def acquire_slot(guild_id: int, owner: str) -> int | None:
    """Атомарно захватить свободный слот. Возвращает channel_id или None.

    ``FOR UPDATE SKIP LOCKED`` гарантирует, что два параллельных
    процесса/таска не получат один и тот же канал.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT channel_id FROM match_channel_slots
                WHERE state = 'free'
                  AND (guild_id = 0 OR guild_id = $1)
                ORDER BY channel_id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                guild_id,
            )
            if row is None:
                return None
            cid = int(row["channel_id"])
            await conn.execute(
                """
                UPDATE match_channel_slots
                SET state = 'reserved', owner = $2, guild_id = $1,
                    updated_at = now()
                WHERE channel_id = $3 AND state = 'free'
                """,
                guild_id, owner, cid,
            )
            return cid


async def transition(
    channel_id: int, expected: set[str], new_state: str, owner: str = ""
) -> bool:
    """Атомарный переход state → new_state, если текущий state ∈ expected."""
    if new_state not in ALL_STATES:
        raise ValueError(f"bad state {new_state}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            """
            UPDATE match_channel_slots
            SET state = $2, owner = $3, updated_at = now()
            WHERE channel_id = $1 AND state = ANY($4)
            """,
            channel_id, new_state, owner, list(expected),
        )
        return res.endswith(" 1")


async def slot_info(channel_id: int) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT channel_id, guild_id, state, owner, updated_at "
            "FROM match_channel_slots WHERE channel_id = $1",
            channel_id,
        )
        return dict(row) if row else None


async def list_slots() -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT channel_id, guild_id, state, owner, updated_at "
            "FROM match_channel_slots ORDER BY channel_id"
        )
        return [dict(r) for r in rows]


async def force_release(channel_id: int) -> dict[str, Any] | None:
    """Принудительно освободить слот (админ). Возвращает старую запись."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM match_channel_slots WHERE channel_id = $1 "
                "FOR UPDATE",
                channel_id,
            )
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE match_channel_slots
                SET state = 'free', owner = '', updated_at = now()
                WHERE channel_id = $1
                """,
                channel_id,
            )
            return dict(row)


# ---------------------------------------------------------------------------
# Discord-операции с ограниченным retry при 429
# ---------------------------------------------------------------------------


async def _discord_call(factory, what: str):
    """Вызвать Discord-корутину с retry на RateLimited.

    Учитывает ``retry_after``; не более ``_MAX_429_RETRIES`` повторов —
    бесконечных циклов нет.
    """
    delay = 0.0
    last_exc: Exception | None = None
    for attempt in range(_MAX_429_RETRIES + 1):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await factory()
        except discord.RateLimited as exc:
            last_exc = exc
            delay = min(float(getattr(exc, "retry_after", 5.0) or 5.0),
                        _MAX_RETRY_AFTER)
            logger.warning(
                "429 on %s (channel op), retry_after=%.1f attempt=%d",
                what, delay, attempt,
            )
    assert last_exc is not None
    raise last_exc


def build_overwrites(
    guild: discord.Guild,
    user_ids: list[int],
    role_ids: list[int] | None = None,
    organizer_id: int = 0,
) -> dict:
    """Overwrites приватного матч-канала: @everyone deny + доступ игрокам,
    ORG-ролям и организатору конкретного матча."""
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False,
            send_messages=False,
            read_message_history=False,
            connect=False,
        )
    }
    for uid in user_ids:
        if uid:
            overwrites[discord.Object(id=uid)] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                connect=True,
            )
    for rid in role_ids or []:
        if rid:
            overwrites[discord.Object(id=rid)] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                connect=True,
            )
    if organizer_id:
        overwrites[discord.Object(id=organizer_id)] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            connect=True,
        )
    return overwrites


async def purge_channel(channel: discord.TextChannel) -> None:
    """Полная очистка: unpin + удаление истории (с 429-retry)."""
    try:
        pins = await _discord_call(lambda: channel.pins(), "pins")
        for pin in pins:
            try:
                await _discord_call(lambda p=pin: p.unpin(), "unpin")
            except Exception:
                pass
    except Exception as exc:
        logger.warning("pins fetch failed for %s: %s", channel.id, exc)

    try:
        messages = await _discord_call(
            lambda: channel.history(limit=100).flatten(), "history"
        )
        if messages:
            await _discord_call(
                lambda: channel.delete_messages(messages), "bulk_delete"
            )
    except Exception as exc:
        logger.warning("bulk purge failed for %s: %s", channel.id, exc)
        # fallback — по одному сообщению
        try:
            async for msg in channel.history(limit=100):
                try:
                    await _discord_call(lambda m=msg: m.delete(), "msg_delete")
                except Exception:
                    pass
        except Exception as exc2:
            logger.warning("per-message purge failed for %s: %s",
                           channel.id, exc2)
            raise


async def acquire_channel(
    guild: discord.Guild,
    user_ids: list[int],
    role_ids: list[int] | None = None,
    organizer_id: int = 0,
    owner: str = "",
) -> tuple[discord.TextChannel | None, str | None]:
    """Захватить свободный слот пула и подготовить его под матч.

    Возвращает (channel, None) при успехе, (None, "pool_full") когда все
    слоты заняты, (None, "error:...") при сбое.
    """
    await ensure_slots()
    ids = pool_channel_ids()
    for _ in range(len(ids)):
        cid = await acquire_slot(guild.id, owner or "match")
        if cid is None:
            return None, "pool_full"
        channel = guild.get_channel(cid)
        if channel is None:
            try:
                channel = await _discord_call(
                    lambda c=cid: guild.fetch_channel(c), "fetch_channel"
                )
            except Exception:
                channel = None
        if not isinstance(channel, discord.TextChannel) or \
                getattr(channel, "guild", None) is None or \
                channel.guild.id != guild.id:
            await transition(cid, {RESERVED}, ERROR, owner or "match")
            continue
        try:
            overwrites = build_overwrites(
                guild, user_ids, role_ids, organizer_id
            )
            await _discord_call(
                lambda c=channel, o=overwrites: c.edit(overwrites=o),
                "edit_overwrites",
            )
            await purge_channel(channel)
        except Exception as exc:
            logger.error("channel prep failed for %s: %s", cid, exc)
            await transition(cid, {RESERVED}, ERROR, owner or "match")
            return None, f"error:{exc}"
        ok = await transition(cid, {RESERVED}, ACTIVE, owner or "match")
        if not ok:
            # Слот перехвачен/изменён извне — не используем канал
            logger.error("slot %s lost between reserve and activate", cid)
            return None, "error:slot_lost"
        return channel, None
    return None, "error:no_usable_slot"


async def release_channel(channel: discord.TextChannel) -> bool:
    """Безопасное освобождение: ACTIVE → CLEANING → FREE (или ERROR).

    Закрывает доступ старых игроков (lockdown overwrites), чистит
    историю и только после этого возвращает слот в ротацию.
    """
    cid = channel.id
    info = await slot_info(cid)
    if info is None:
        # Канал не из пула — освобождать нечего
        return True
    if info["state"] not in (ACTIVE, RESERVED, ERROR):
        return info["state"] == FREE

    if not await transition(cid, {ACTIVE, RESERVED, ERROR}, CLEANING):
        return False
    try:
        # Закрываем канал для всех до следующего матча — без наследования
        # прав категории (явный deny @everyone).
        overwrites = {
            channel.guild.default_role: discord.PermissionOverwrite(
                view_channel=False,
                send_messages=False,
                read_message_history=False,
                connect=False,
            )
        }
        await _discord_call(
            lambda: channel.edit(overwrites=overwrites), "lockdown_overwrites"
        )
        await purge_channel(channel)
    except Exception as exc:
        logger.error("cleanup failed for channel %s: %s", cid, exc)
        await transition(cid, {CLEANING}, ERROR, info.get("owner", ""))
        return False
    await transition(cid, {CLEANING}, FREE)
    return True


async def mark_error(channel_id: int, owner: str = "") -> None:
    await transition(channel_id, {FREE, RESERVED, ACTIVE, CLEANING},
                     ERROR, owner)


async def reconcile_stale() -> list[dict[str, Any]]:
    """Слоты в состоянии ERROR — кандидаты на проверку/force-release.

    Автоматически ничего не освобождает: активный матч нельзя ронять
    по таймеру. Админ решает через force_release.
    """
    slots = await list_slots()
    return [s for s in slots if s["state"] == ERROR]
