"""r!z3 Voice Control — сервис ограничений голосовых каналов.

Ключевые принципы (safety hardening):
- Discord не различает, КТО наложил server_mute. Мы храним собственные
  ограничения в voice_control_restrictions и снимаем ТОЛЬКО их.
  Чужой/неопределённый мут никогда не трогаем.
- На одного участника может быть несколько ограничений (таймер +
  режим ведущего + массовый мут). Снятие одного не снимает Discord-флаг,
  пока остаётся другое активное ограничение того же kind.
- Каждая Discord-операция проверяет, что участник всё ещё в целевом
  канале, и повторно проверяет права перед API-вызовом.
- Операции сериализуются per-channel блокировками; массовые действия
  пишут voice_control_ops для идемпотентности и отмены.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import discord

from config import (
    ADMIN_USER_IDS,
    BOT_OWNER_ID,
    MOD_LOG_CHANNEL_ID,
    VOICE_CONTROL_ALLOWED_ROLE_IDS,
    VOICE_CONTROL_LOG_CHANNEL_ID,
    VOICE_CONTROL_PROTECTED_CHANNEL_IDS,
)
from storage.voice_control_store import voice_control_store as db

log = logging.getLogger(__name__)

KINDS = ("server_mute", "server_deafen")
FLAG_ATTR = {"server_mute": "mute", "server_deafen": "deaf"}
EDIT_KWARG = {"server_mute": "mute", "server_deafen": "deaf"}

# Происхождения ограничений.
ORIGIN_MANUAL = "manual"
ORIGIN_BULK_MUTE = "bulk_mute"
ORIGIN_SPEAKER = "speaker_mode"
ORIGIN_AUTO_MUTE = "auto_mute"
ORIGIN_TIMER = "timer"
ORIGIN_UNDO = "undo"
SYSTEM_ORIGINS = {
    ORIGIN_MANUAL, ORIGIN_BULK_MUTE, ORIGIN_SPEAKER,
    ORIGIN_AUTO_MUTE, ORIGIN_TIMER, ORIGIN_UNDO,
}

# Per-(guild,channel,user) блокировки операций с ограничениями.
_member_locks: dict[tuple[int, int, int], asyncio.Lock] = {}
# Per-channel блокировка массовых операций (двойные нажатия).
_channel_locks: dict[tuple[int, int], asyncio.Lock] = {}
# Ограничение параллелизма Discord API на гильдию.
_api_semaphores: dict[int, asyncio.Semaphore] = {}

MAX_API_RETRIES_429 = 2
API_SEMAPHORE_LIMIT = 5


def _member_lock(guild_id: int, channel_id: int, user_id: int) -> asyncio.Lock:
    key = (guild_id, channel_id, user_id)
    lock = _member_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _member_locks[key] = lock
    return lock


def _channel_lock(guild_id: int, channel_id: int) -> asyncio.Lock:
    key = (guild_id, channel_id)
    lock = _channel_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _channel_locks[key] = lock
    return lock


def _api_sem(guild_id: int) -> asyncio.Semaphore:
    sem = _api_semaphores.get(guild_id)
    if sem is None:
        sem = asyncio.Semaphore(API_SEMAPHORE_LIMIT)
        _api_semaphores[guild_id] = sem
    return sem


class StaleTargetError(Exception):
    """Участник покинул канал или перешёл в другой до API-вызова."""


class VoiceControlError(Exception):
    """Ожидаемая ошибка Voice Control (показывается пользователю)."""


def voice_control_enabled() -> bool:
    from config import VOICE_CONTROL_ENABLED
    return VOICE_CONTROL_ENABLED


def is_protected_channel(guild_id: int, channel_id: int, settings: dict | None = None) -> bool:
    return channel_id in VOICE_CONTROL_PROTECTED_CHANNEL_IDS


async def settings(guild_id: int) -> dict[str, Any]:
    return await db.settings(guild_id)


def can_use(member: discord.Member, conf: dict[str, Any] | None = None) -> bool:
    """Доступ к панели: владелец сервера, administrator, владелец бота,
    ADMIN_USER_IDS, настроенные роли."""
    if not isinstance(member, discord.Member):
        return False
    if member.id == member.guild.owner_id or member.guild_permissions.administrator:
        return True
    if member.id == BOT_OWNER_ID or member.id in ADMIN_USER_IDS:
        return True
    allowed = set((conf or {}).get("allowed_role_ids") or [])
    allowed |= VOICE_CONTROL_ALLOWED_ROLE_IDS
    if not allowed:
        return False
    return any(r.id in allowed for r in member.roles)


async def require_access(interaction: discord.Interaction) -> discord.Member:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        raise VoiceControlError("Команда доступна только на сервере.")
    conf = await db.settings(interaction.guild.id)
    if not can_use(interaction.user, conf):
        raise VoiceControlError("Недостаточно прав для Voice Control.")
    return interaction.user


def _is_human(member: discord.Member) -> bool:
    return not member.bot


def _member_is_protected(member: discord.Member, conf: dict[str, Any],
                         exceptions: list[dict[str, Any]] | None = None) -> bool:
    """Защита от массовых операций: владелец, боты, защищённые
    пользователи/роли, исключения канала."""
    if member.bot or member.id == member.guild.owner_id:
        return True
    if member.id in set(conf.get("protected_user_ids") or []):
        return True
    protected_roles = set(conf.get("protected_role_ids") or [])
    if any(r.id in protected_roles for r in member.roles):
        return True
    for exc in exceptions or []:
        if exc["target_type"] == "user" and exc["target_id"] == member.id:
            return True
        if exc["target_type"] == "role" and any(
            r.id == exc["target_id"] for r in member.roles
        ):
            return True
    return False


def _is_voice_channel(ch: Any) -> bool:
    """VoiceChannel, но не StageChannel. Duck-typing допускает
    тестовые объекты; у StageChannel type=stage_voice — отсекается."""
    if isinstance(ch, discord.VoiceChannel):
        return True
    if not (hasattr(ch, "members") and hasattr(ch, "name")):
        return False
    return getattr(ch, "type", discord.ChannelType.voice) \
        == discord.ChannelType.voice


def humans_in(channel: discord.VoiceChannel) -> list[discord.Member]:
    return [m for m in channel.members if _is_human(m)]


async def active_channels(guild: discord.Guild, conf: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Активные голосовые каналы: есть люди ИЛИ включён режим управления.
    Stage-каналы не поддерживаются. Сортировка по числу людей (убыв.)."""
    conf = conf or await db.settings(guild.id)
    modes = {m["channel_id"]: m for m in await db.modes(guild.id)}
    out: list[dict[str, Any]] = []
    for channel in guild.channels:
        if not _is_voice_channel(channel):
            continue  # StageChannel и прочие не поддерживаются
        if is_protected_channel(guild.id, channel.id, conf):
            continue
        mode = modes.get(channel.id)
        humans = humans_in(channel)
        mode_active = bool(mode and (
            mode.get("speaker_mode") or mode.get("auto_mute") or mode.get("closed")
            or mode.get("name_override") or mode.get("user_limit")
        ))
        if humans or mode_active:
            out.append({
                "channel": channel,
                "humans": humans,
                "count": len(humans),
                "mode": mode,
                "mode_active": mode_active,
            })
    out.sort(key=lambda x: (-x["count"], x["channel"].position))
    return out


# ----------------------------------------------------------------------
# Discord API с semaphore + ограниченным retry на 429
# ----------------------------------------------------------------------
async def _api_call(guild_id: int, coro_factory, *, what: str):
    """Выполнить Discord API вызов под семафором гильдии.

    429 — повторяем не более MAX_API_RETRIES_429 раз с retry_after
    ( capped ). Остальные ошибки пробрасываются вызывающему.
    """
    attempts = 0
    while True:
        async with _api_sem(guild_id):
            try:
                return await coro_factory()
            except discord.RateLimited as rl:
                wait = min(float(getattr(rl, "retry_after", 1.0) or 1.0), 15.0)
                attempts += 1
                if attempts > MAX_API_RETRIES_429:
                    raise
                await asyncio.sleep(wait)
            except discord.HTTPException:
                raise


# ----------------------------------------------------------------------
# Применение и снятие ограничений
# ----------------------------------------------------------------------
async def _current_flag(member: discord.Member, kind: str) -> bool:
    voice = getattr(member, "voice", None)
    if voice is None or voice.channel is None:
        return False
    return bool(getattr(voice, FLAG_ATTR[kind], False))


async def _set_flag(member: discord.Member, guild_id: int, kind: str, value: bool,
                    *, reason: str = "r!z3 Voice Control") -> None:
    kwarg = EDIT_KWARG[kind]
    if kind == "server_deafen":
        # server_deafen в Discord API — это и mute, и deaf.
        async def call():
            return await member.edit(deaf=value, reason=reason)
    else:
        async def call():
            return await member.edit(mute=value, reason=reason)
    return await _api_call(guild_id, call, what=f"edit {kwarg}={value}")


async def apply_restriction(
    member: discord.Member,
    channel: discord.VoiceChannel,
    kind: str,
    origin: str,
    *,
    actor_id: int = 0,
    reason: str = "",
    expires_at: datetime | None = None,
    request_key: str | None = None,
) -> dict[str, Any]:
    """Наложить server_mute/server_deafen с учётом происхождения.

    Идемпотентно по request_key. Бросает StaleTargetError, если
    участник больше не в канале, VoiceControlError при отказе Discord.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown restriction kind {kind}")
    if origin not in SYSTEM_ORIGINS:
        raise ValueError(f"unknown origin {origin}")
    guild = member.guild
    if member.bot:
        raise VoiceControlError("Боты не могут быть ограничены.")
    voice = getattr(member, "voice", None)
    if voice is None or voice.channel is None or voice.channel.id != channel.id:
        raise StaleTargetError("Участник покинул канал.")
    conf = await db.settings(guild.id)
    if _member_is_protected(member, conf):
        raise VoiceControlError("Участник защищён от Voice Control.")
    if member.id == actor_id or member.id == guild.owner_id:
        raise VoiceControlError("Нельзя ограничить себя или владельца сервера.")
    me = guild.me
    perm = me.guild_permissions.mute_members if kind == "server_mute" \
        else me.guild_permissions.deafen_members
    if not perm:
        raise VoiceControlError(
            "Боту не хватает права "
            + ("Mute Members" if kind == "server_mute" else "Deafen Members")
            + "."
        )
    if member.top_role >= me.top_role:
        raise VoiceControlError("Роль бота ниже роли участника.")
    # Идемпотентность по (kind, origin): то же происхождение — дубль.
    # Ограничения разных origin стекуются (таймер + режим ведущего +
    # массовый мут) — каждое снимается независимо.
    existing = await db.restrictions(
        guild.id, channel_id=channel.id, user_id=member.id, active_only=True,
    )
    same_kind = [r for r in existing if r["kind"] == kind]
    if any(r["origin"] == origin for r in same_kind):
        return {"restriction": next(
            r for r in same_kind if r["origin"] == origin),
            "skipped": "already_restricted"}
    # Флаг уже стоит, но наших активных ограничений этого kind нет —
    # источник неизвестен (модерация/сторонний мут). Не присваиваем.
    if await _current_flag(member, kind) and not same_kind:
        return {"restriction": None, "skipped": "foreign_mute"}

    rec = await db.add_restriction(
        guild.id, channel.id, member.id, kind, origin,
        reason=reason, expires_at=expires_at, request_key=request_key,
        created_by=actor_id,
    )
    if rec is None:
        # Дубль по request_key — вернуть существующую запись.
        rows = await db.restrictions(guild.id, active_only=False)
        rec = next((r for r in rows if r.get("request_key") == request_key), None)
        if rec is None:
            raise VoiceControlError("Не удалось создать ограничение (дубликат).")
        return {"restriction": rec, "skipped": "duplicate"}
    try:
        await _set_flag(member, guild.id, kind, True, reason=reason or "r!z3 Voice Control")
    except discord.Forbidden:
        await db.release_restriction(guild.id, rec["id"], actor_id=actor_id)
        raise VoiceControlError("Discord запретил изменение голосового состояния.")
    except discord.HTTPException:
        # Неопределённый результат: оставляем запись active —
        # reconciliation сверит реальное состояние позже.
        log.warning("voice control apply %s ambiguous guild=%s user=%s",
                    kind, guild.id, member.id)
        raise VoiceControlError(
            "Discord не подтвердил результат. Ограничение записано, "
            "сверка состояния будет выполнена автоматически.")
    return {"restriction": rec, "skipped": None}


async def _clear_flag(member: discord.Member, guild_id: int, kind: str,
                      *, reason: str = "r!z3 Voice Control") -> bool:
    """Снять server-флаг, если он реально установлен. True — вызов выполнен."""
    if not await _current_flag(member, kind):
        return True  # флаг уже снят — цель достигнута
    try:
        await _set_flag(member, guild_id, kind, False, reason=reason)
        return True
    except discord.Forbidden:
        return False
    except discord.HTTPException:
        log.warning("voice control clear %s failed guild=%s user=%s",
                    kind, guild_id, member.id)
        return False


async def release_restriction(restriction: dict[str, Any], *, actor_id: int = 0) -> dict[str, Any]:
    """Снять одно ограничение. Не трогает Discord-флаг, пока остаются
    другие активные ограничения того же kind у участника."""
    guild_id = restriction["guild_id"]
    kind = restriction["kind"]
    user_id = restriction["user_id"]
    channel_id = restriction["channel_id"]

    if await db.has_other_active(guild_id, channel_id, user_id, kind,
                                 exclude_id=restriction["id"]):
        # Осталось другое наше ограничение — Discord-флаг сохраняем.
        return await db.release_restriction(guild_id, restriction["id"], actor_id=actor_id) or restriction

    guild = _bot_guild(guild_id)
    member = None
    if guild is not None:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                member = None
    if member is not None:
        await _clear_flag(member, guild_id, kind)
    rec = await db.release_restriction(guild_id, restriction["id"], actor_id=actor_id)
    return rec or restriction


# ----------------------------------------------------------------------
# Массовые операции
# ----------------------------------------------------------------------
async def _eligible_for_mute(channel: discord.VoiceChannel, actor: discord.Member,
                             conf: dict[str, Any],
                             exceptions: list[dict[str, Any]]) -> tuple[list[discord.Member], int]:
    """Участники, доступные для серверного мута. Возвращает (list, skipped)."""
    targets: list[discord.Member] = []
    skipped = 0
    active = await db.restrictions(actor.guild.id, channel_id=channel.id,
                                   active_only=True)
    muted_ids = {
        r["user_id"] for r in active if r["kind"] == "server_mute"
    }
    for m in humans_in(channel):
        if _member_is_protected(m, conf, exceptions):
            skipped += 1
            continue
        if m.id == actor.id:
            skipped += 1
            continue
        if m.id in muted_ids:
            skipped += 1  # уже ограничен нами
            continue
        if await _current_flag(m, "server_mute"):
            skipped += 1  # флаг стоит, источник неизвестен — не трогаем
            continue
        targets.append(m)
    return targets, skipped


async def bulk_mute(channel: discord.VoiceChannel, actor: discord.Member, *,
                    duration: timedelta | None = None,
                    origin: str = ORIGIN_BULK_MUTE,
                    reason: str = "") -> dict[str, Any]:
    """«Выключить всем». Исключает актора, владельца, защищённых,
    ботов и уже ограниченных. Проверяет канал перед каждым вызовом."""
    guild = actor.guild
    conf = await db.settings(guild.id)
    exceptions = await db.exceptions(guild.id, channel.id)
    targets, skipped = await _eligible_for_mute(channel, actor, conf, exceptions)

    expires = datetime.now(UTC) + duration if duration else None
    op_id = uuid.uuid4().hex
    await db.op_create(op_id, guild.id, channel.id, "bulk_mute", actor.id,
                       {"targets": len(targets)})
    done = errors = stale = 0
    try:
        for m in targets:
            async with _member_lock(guild.id, channel.id, m.id):
                # Актуальность цели прямо перед API-вызовом.
                voice = getattr(m, "voice", None)
                if voice is None or voice.channel is None or voice.channel.id != channel.id:
                    stale += 1
                    continue
                try:
                    result = await apply_restriction(
                        m, channel, "server_mute", origin,
                        actor_id=actor.id, reason=reason or "Массовый мут (Voice Control)",
                        expires_at=expires,
                    )
                    if result.get("skipped"):
                        skipped += 1
                    else:
                        done += 1
                except StaleTargetError:
                    stale += 1
                except (VoiceControlError, discord.HTTPException) as exc:
                    errors += 1
                    log.info("bulk_mute member %s failed: %s", m.id, exc)
        status = "done" if errors == 0 and stale == 0 else (
            "failed" if done == 0 else "done")
        await db.op_finish(op_id, status,
                           {"done": done, "skipped": skipped,
                            "errors": errors, "stale": stale})
        await log_event(guild.id, actor.id, "voice.bulk_mute",
                        channel_id=channel.id,
                        detail={"done": done, "skipped": skipped,
                                "errors": errors, "stale": stale,
                                "origin": origin})
        return {"done": done, "skipped": skipped, "errors": errors,
                "stale": stale, "op_id": op_id}
    except Exception:
        await db.op_finish(op_id, "unknown", {"done": done})
        raise


async def bulk_unmute(channel: discord.VoiceChannel, actor: discord.Member, *,
                      reason: str = "") -> dict[str, Any]:
    """«Разрешить всем». Снимает ТОЛЬКО ограничения Voice Control.
    Модерационные и чужие муты не трогаются по построению."""
    guild = actor.guild
    mode = await db.mode(guild.id, channel.id)
    if mode and mode.get("speaker_mode"):
        return {"error": "speaker_mode_active",
                "done": 0, "skipped": 0, "errors": 0}

    restrictions = await db.restrictions(
        guild.id, channel_id=channel.id, user_id=None, active_only=True)
    mute_restrictions = [r for r in restrictions if r["kind"] == "server_mute"]
    op_id = uuid.uuid4().hex
    await db.op_create(op_id, guild.id, channel.id, "bulk_unmute", actor.id,
                       {"targets": len(mute_restrictions)})
    done = skipped = errors = 0
    try:
        for r in mute_restrictions:
            async with _member_lock(guild.id, channel.id, r["user_id"]):
                try:
                    await release_restriction(r, actor_id=actor.id)
                    done += 1
                except Exception as exc:
                    errors += 1
                    log.info("bulk_unmute restriction %s failed: %s", r["id"], exc)
        skipped = len(mute_restrictions) - done - errors
        await db.op_finish(op_id, "done",
                           {"done": done, "errors": errors})
        await log_event(guild.id, actor.id, "voice.bulk_unmute",
                        channel_id=channel.id,
                        detail={"done": done, "errors": errors})
        return {"done": done, "skipped": max(0, skipped), "errors": errors,
                "op_id": op_id}
    except Exception:
        await db.op_finish(op_id, "unknown", {"done": done})
        raise


# ----------------------------------------------------------------------
# Режим ведущего
# ----------------------------------------------------------------------
async def start_speaker_mode(channel: discord.VoiceChannel, actor: discord.Member,
                             speakers: list[discord.Member] | None = None) -> dict[str, Any]:
    """Активировать режим ведущего: все не-ведущие получают server_mute
    с origin='speaker_mode'. Ведущие по умолчанию — сам актор."""
    guild = actor.guild
    conf = await db.settings(guild.id)
    exceptions = await db.exceptions(guild.id, channel.id)
    speaker_ids = {s.id for s in (speakers or [])}
    speaker_ids.add(actor.id)
    for sid in speaker_ids:
        await db.add_speaker(guild.id, channel.id, sid, actor.id)
    await db.put_mode(guild.id, channel.id, speaker_mode=True)

    muted = skipped = errors = 0
    for m in humans_in(channel):
        if m.id in speaker_ids:
            continue
        if _member_is_protected(m, conf, exceptions):
            skipped += 1
            continue
        async with _member_lock(guild.id, channel.id, m.id):
            try:
                result = await apply_restriction(
                    m, channel, "server_mute", ORIGIN_SPEAKER,
                    actor_id=actor.id, reason="Режим ведущего",
                )
                if result.get("skipped"):
                    skipped += 1
                else:
                    muted += 1
            except (StaleTargetError, VoiceControlError, discord.HTTPException):
                errors += 1
    await log_event(guild.id, actor.id, "voice.speaker_start",
                    channel_id=channel.id,
                    detail={"speakers": sorted(speaker_ids), "muted": muted,
                            "skipped": skipped, "errors": errors})
    return {"speakers": sorted(speaker_ids), "muted": muted,
            "skipped": skipped, "errors": errors}


async def stop_speaker_mode(channel: discord.VoiceChannel, actor: discord.Member) -> dict[str, Any]:
    """Завершить режим. Снимаются только ограничения origin='speaker_mode',
    у которых нет других активных ограничений того же kind."""
    guild = actor.guild
    released = kept = errors = 0
    restrictions = await db.restrictions(guild.id, channel_id=channel.id,
                                         active_only=True)
    for r in [r for r in restrictions if r["origin"] == ORIGIN_SPEAKER]:
        async with _member_lock(guild.id, channel.id, r["user_id"]):
            try:
                before = await db.has_other_active(
                    guild.id, channel.id, r["user_id"], r["kind"],
                    exclude_id=r["id"])
                await release_restriction(r, actor_id=actor.id)
                if before:
                    kept += 1
                else:
                    released += 1
            except Exception:
                errors += 1
    await db.clear_speakers(guild.id, channel.id)
    await db.put_mode(guild.id, channel.id, speaker_mode=False)
    await db.delete_mode_if_idle(guild.id, channel.id)
    await log_event(guild.id, actor.id, "voice.speaker_stop",
                    channel_id=channel.id,
                    detail={"released": released, "kept_by_other": kept,
                            "errors": errors})
    return {"released": released, "kept": kept, "errors": errors}


async def speaker_add(channel: discord.VoiceChannel, member: discord.Member,
                      actor: discord.Member) -> dict[str, Any]:
    guild = actor.guild
    await db.add_speaker(guild.id, channel.id, member.id, actor.id)
    # Если режим активен и у ведущего наш speaker-мут — снять его.
    restrictions = await db.restrictions(guild.id, channel_id=channel.id,
                                         user_id=member.id, active_only=True)
    unmuted = False
    for r in [r for r in restrictions if r["origin"] == ORIGIN_SPEAKER]:
        async with _member_lock(guild.id, channel.id, member.id):
            await release_restriction(r, actor_id=actor.id)
            unmuted = True
    await log_event(guild.id, actor.id, "voice.speaker_add",
                    channel_id=channel.id, detail={"user_id": member.id})
    return {"unmuted": unmuted}


async def speaker_remove(channel: discord.VoiceChannel, member: discord.Member,
                         actor: discord.Member) -> dict[str, Any]:
    guild = actor.guild
    await db.remove_speaker(guild.id, channel.id, member.id)
    mode = await db.mode(guild.id, channel.id)
    muted = False
    if mode and mode.get("speaker_mode"):
        voice = getattr(member, "voice", None)
        if voice and voice.channel and voice.channel.id == channel.id:
            async with _member_lock(guild.id, channel.id, member.id):
                try:
                    result = await apply_restriction(
                        member, channel, "server_mute", ORIGIN_SPEAKER,
                        actor_id=actor.id, reason="Режим ведущего",
                    )
                    muted = not result.get("skipped")
                except (StaleTargetError, VoiceControlError, discord.HTTPException):
                    pass
    await log_event(guild.id, actor.id, "voice.speaker_remove",
                    channel_id=channel.id, detail={"user_id": member.id})
    return {"muted": muted}


async def apply_auto_mute(member: discord.Member, channel: discord.VoiceChannel,
                          actor_id: int = 0) -> dict[str, Any] | None:
    """Автомут нового участника при auto_mute-режиме канала."""
    mode = await db.mode(member.guild.id, channel.id)
    if not mode or not mode.get("auto_mute") or mode.get("speaker_mode"):
        return None
    conf = await db.settings(member.guild.id)
    exceptions = await db.exceptions(member.guild.id, channel.id)
    if _member_is_protected(member, conf, exceptions):
        return None
    if member.id == actor_id or member.id == member.guild.owner_id:
        return None
    speakers = await db.speakers(member.guild.id, channel.id)
    if member.id in speakers:
        return None
    async with _member_lock(member.guild.id, channel.id, member.id):
        try:
            return await apply_restriction(
                member, channel, "server_mute", ORIGIN_AUTO_MUTE,
                actor_id=actor_id, reason="Автомут новых участников",
            )
        except (StaleTargetError, VoiceControlError, discord.HTTPException):
            return None


# ----------------------------------------------------------------------
# Закрытие/открытие канала (permission overwrites)
# ----------------------------------------------------------------------
def _overwrite_pair(ow: discord.PermissionOverwrite | None) -> dict[str, Any] | None:
    if ow is None:
        return None
    return {"connect": ow.connect, "view_channel": ow.view_channel}


async def close_voice(channel: discord.VoiceChannel, actor: discord.Member) -> dict[str, Any]:
    """Закрыть вход: @everyone connect=False. Существующие member/role
    overwrites не трогаем — они и есть исключения. Исходные права
    сохраняются в voice_control_overwrites."""
    guild = actor.guild
    me = guild.me
    if not me.guild_permissions.manage_roles:
        raise VoiceControlError("Боту нужен Manage Roles для закрытия канала.")
    everyone = guild.default_role
    current = channel.overwrites_for(everyone)
    before = _overwrite_pair(current)
    applied = dict(before or {})
    applied["connect"] = False
    # Сохраняем исходное состояние до изменения.
    entries = [{
        "target_type": "role", "target_id": everyone.id,
        "before": before, "applied": applied, "state": "applied",
    }]
    saved_ids = await db.save_overwrites(guild.id, channel.id, entries, actor.id)
    try:
        await _api_call(guild.id,
                        lambda: channel.set_permissions(
                            everyone,
                            overwrite=discord.PermissionOverwrite(connect=False),
                            reason="r!z3 Voice Control: закрытие канала"),
                        what="close voice")
    except discord.HTTPException as exc:
        for oid in saved_ids:
            try:
                await db.resolve_overwrite(guild.id, oid, state="conflict",
                                           actor_id=actor.id)
            except Exception as exc:
                log.debug("overwrite conflict resolve failed: %s", exc)
        raise VoiceControlError(f"Discord отклонил изменение прав: {exc}")
    await db.put_mode(guild.id, channel.id, closed=True)
    await log_event(guild.id, actor.id, "voice.close",
                    channel_id=channel.id, detail={"before": before})
    return {"before": before}


async def open_voice(channel: discord.VoiceChannel, actor: discord.Member) -> dict[str, Any]:
    """Открыть канал: восстановить ТОЛЬКО overwrite'ы, изменённые
    панелью. При стороннем изменении — конфликт, не перезаписываем."""
    guild = actor.guild
    me = guild.me
    if not me.guild_permissions.manage_roles:
        raise VoiceControlError("Боту нужен Manage Roles для открытия канала.")
    restored = conflicts = 0
    rows = await db.active_overwrites(guild.id, channel.id)
    for row in rows:
        target = guild.get_role(row["target_id"]) if row["target_type"] == "role" \
            else guild.get_member(row["target_id"])
        if target is None:
            await db.resolve_overwrite(guild.id, row["id"], state="conflict",
                                       actor_id=actor.id)
            conflicts += 1
            continue
        current = _overwrite_pair(channel.overwrites_for(target))
        applied = row["applied"] or {}
        # Конфликт: текущее значение connect отличается от наложенного панелью.
        if current is None or current.get("connect") != applied.get("connect"):
            await db.resolve_overwrite(guild.id, row["id"], state="conflict",
                                       actor_id=actor.id)
            conflicts += 1
            continue
        before = row.get("before")
        ow = discord.PermissionOverwrite()
        if before is not None:
            ow.connect = before.get("connect")
            ow.view_channel = before.get("view_channel")
        # before=None → удаляем overwrite полностью.
        try:
            if before is None:
                await _api_call(guild.id,
                                lambda t=target: channel.set_permissions(
                                    t, overwrite=None,
                                    reason="r!z3 Voice Control: открытие канала"),
                                what="open voice restore")
            else:
                await _api_call(guild.id,
                                lambda t=target, o=ow: channel.set_permissions(
                                    t, overwrite=o,
                                    reason="r!z3 Voice Control: открытие канала"),
                                what="open voice restore")
            await db.resolve_overwrite(guild.id, row["id"], state="restored",
                                       actor_id=actor.id)
            restored += 1
        except discord.HTTPException as exc:
            log.warning("open_voice restore failed %s: %s", row["id"], exc)
            await db.resolve_overwrite(guild.id, row["id"], state="conflict",
                                       actor_id=actor.id)
            conflicts += 1
    await db.put_mode(guild.id, channel.id, closed=False)
    await db.delete_mode_if_idle(guild.id, channel.id)
    await log_event(guild.id, actor.id, "voice.open",
                    channel_id=channel.id,
                    detail={"restored": restored, "conflicts": conflicts})
    return {"restored": restored, "conflicts": conflicts}


def voice_exceptions_display(channel: discord.VoiceChannel) -> list[str]:
    """Кто может войти в закрытый канал: member/role overwrites с
    connect=True (кроме @everyone, у которого connect=False)."""
    out: list[str] = []
    guild = channel.guild
    for target, ow in channel.overwrites.items():
        if target.id == guild.id:  # @everyone
            continue
        if ow.connect is True or (ow.connect is None and ow.view_channel):
            name = getattr(target, "name", None) or getattr(target, "display_name", str(target.id))
            kind = "роль" if isinstance(target, discord.Role) else "участник"
            out.append(f"{kind}: {name}")
    return out[:10]


# ----------------------------------------------------------------------
# Перемещение и отключение
# ----------------------------------------------------------------------
async def move_member(member: discord.Member, target: discord.VoiceChannel,
                      actor: discord.Member) -> None:
    """Переместить участника. Проверки: цель в той же гильдии,
    это VoiceChannel (не Stage), бот имеет Move Members, иерархия."""
    guild = member.guild
    if target.guild.id != guild.id or not _is_voice_channel(target):
        raise VoiceControlError("Целевой канал недоступен.")
    if is_protected_channel(guild.id, target.id):
        raise VoiceControlError("Целевой канал защищён от Voice Control.")
    me = guild.me
    if not me.guild_permissions.move_members:
        raise VoiceControlError("Боту нужен Move Members.")
    if member.top_role >= me.top_role:
        raise VoiceControlError("Роль бота ниже роли участника.")
    voice = getattr(member, "voice", None)
    if voice is None or voice.channel is None:
        raise StaleTargetError("Участник не в войсе.")
    if voice.channel.id == target.id:
        return
    # Проверка прав входа в канал для самого участника.
    if not target.permissions_for(member).connect:
        raise VoiceControlError("Участник не может войти в этот канал.")
    limit = target.user_limit
    if limit:
        humans = len(humans_in(target))
        if humans >= limit:
            raise VoiceControlError("Целевой канал заполнен.")
    async def call():
        return await member.move_to(target, reason="r!z3 Voice Control")
    await _api_call(guild.id, call, what="move member")


async def disconnect_member(member: discord.Member, actor: discord.Member) -> None:
    guild = member.guild
    me = guild.me
    if not me.guild_permissions.move_members:
        raise VoiceControlError("Боту нужен Move Members.")
    if member.top_role >= me.top_role:
        raise VoiceControlError("Роль бота ниже роли участника.")
    voice = getattr(member, "voice", None)
    if voice is None or voice.channel is None:
        raise StaleTargetError("Участник не в войсе.")
    async def call():
        return await member.move_to(None, reason="r!z3 Voice Control")
    await _api_call(guild.id, call, what="disconnect member")


async def move_all(channel: discord.VoiceChannel, target: discord.VoiceChannel,
                   actor: discord.Member) -> dict[str, Any]:
    """Массовое перемещение с подтверждением (вызывается после confirm)."""
    guild = actor.guild
    conf = await db.settings(guild.id)
    exceptions = await db.exceptions(guild.id, channel.id)
    targets = [m for m in humans_in(channel)
               if not _member_is_protected(m, conf, exceptions) and m.id != actor.id]
    moved = skipped = errors = 0
    for m in targets:
        async with _member_lock(guild.id, channel.id, m.id):
            try:
                await move_member(m, target, actor)
                moved += 1
            except StaleTargetError:
                skipped += 1
            except (VoiceControlError, discord.HTTPException) as exc:
                errors += 1
                log.info("move_all member %s failed: %s", m.id, exc)
    await log_event(guild.id, actor.id, "voice.move_all",
                    channel_id=channel.id,
                    detail={"target_channel": target.id, "moved": moved,
                            "skipped": skipped, "errors": errors})
    return {"moved": moved, "skipped": skipped, "errors": errors}


async def disconnect_all(channel: discord.VoiceChannel, actor: discord.Member) -> dict[str, Any]:
    """Отключить всех (необратимое действие — не пишется в undo)."""
    guild = actor.guild
    conf = await db.settings(guild.id)
    exceptions = await db.exceptions(guild.id, channel.id)
    targets = [m for m in humans_in(channel)
               if not _member_is_protected(m, conf, exceptions) and m.id != actor.id]
    done = skipped = errors = 0
    for m in targets:
        async with _member_lock(guild.id, channel.id, m.id):
            try:
                await disconnect_member(m, actor)
                done += 1
            except StaleTargetError:
                skipped += 1
            except (VoiceControlError, discord.HTTPException) as exc:
                errors += 1
                log.info("disconnect_all member %s failed: %s", m.id, exc)
    await log_event(guild.id, actor.id, "voice.disconnect_all",
                    channel_id=channel.id,
                    detail={"done": done, "skipped": skipped, "errors": errors})
    return {"done": done, "skipped": skipped, "errors": errors}


# ----------------------------------------------------------------------
# Отмена последнего обратимого действия
# ----------------------------------------------------------------------
async def undo_last(channel: discord.VoiceChannel, actor: discord.Member) -> dict[str, Any]:
    """Отменить последнее обратимое действие, если состояние
    всё ещё актуально. Массовое отключение не является обратимым."""
    guild = actor.guild
    op = await db.last_reversible_op(guild.id, channel.id)
    if not op:
        return {"error": "nothing_to_undo"}
    kind = op["kind"]
    detail = op.get("detail") or {}
    try:
        if kind == "bulk_mute":
            result = await bulk_unmute(channel, actor, reason="Отмена Voice Control")
        elif kind == "bulk_unmute":
            # Повторный мут теми же origin='undo' — только если участники
            # всё ещё в канале (проверяется внутри apply_restriction).
            result = await bulk_mute(channel, actor, origin=ORIGIN_UNDO,
                                     reason="Отмена размьюта (Voice Control)")
        elif kind == "speaker_start":
            result = await stop_speaker_mode(channel, actor)
        elif kind == "speaker_stop":
            result = await start_speaker_mode(channel, actor)
        elif kind in ("mute_member", "unmute_member"):
            user_id = detail.get("user_id")
            member = guild.get_member(user_id) if user_id else None
            if member is None and user_id:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.HTTPException:
                    member = None
            if member is None:
                return {"error": "target_left"}
            voice = getattr(member, "voice", None)
            if voice is None or voice.channel is None or voice.channel.id != channel.id:
                return {"error": "target_left"}
            if kind == "mute_member":
                # Отмена мута = снять именно то ограничение, которое
                # было создано этим действием (а не все mute'ы юзера).
                rid = detail.get("restriction_id")
                released = 0
                restrictions = await db.restrictions(
                    guild.id, channel_id=channel.id, user_id=member.id,
                    active_only=True)
                for r in restrictions:
                    if r["kind"] == "server_mute" and (
                            rid is None or r["id"] == rid):
                        await release_restriction(r, actor_id=actor.id)
                        released += 1
                result = {"done": released}
            elif kind == "unmute_member":
                # Отмена размьюта: вернуть mute только если ни одно
                # другое активное mute-ограничение не покрывает флаг.
                active = await db.restrictions(
                    guild.id, channel_id=channel.id, user_id=member.id,
                    active_only=True)
                muted = False
                for r in active:
                    if r["kind"] == "server_mute":
                        muted = True
                        break
                if not muted and not await _current_flag(member, "server_mute"):
                    res = await apply_restriction(
                        member, channel, "server_mute", ORIGIN_UNDO,
                        actor_id=actor.id,
                        reason="Отмена размьюта (Voice Control)",
                    )
                    muted = not res.get("skipped")
                result = {"done": 1 if muted else 0}
        elif kind == "close_voice":
            result = await open_voice(channel, actor)
        elif kind == "open_voice":
            result = await close_voice(channel, actor)
        else:
            return {"error": "not_reversible"}
        await log_event(guild.id, actor.id, "voice.undo",
                        channel_id=channel.id,
                        detail={"undone": kind})
        return {"undone": kind, "result": result}
    except (VoiceControlError, discord.HTTPException) as exc:
        return {"error": str(exc)}


# ----------------------------------------------------------------------
# Журнал
# ----------------------------------------------------------------------
def _bot_guild(guild_id: int):
    # Bot instance хранится в сервисе через set_bot().
    bot = getattr(_bot_guild, "_bot", None)
    return bot.get_guild(guild_id) if bot else None


_bot = None


def set_bot(bot) -> None:
    global _bot
    _bot = bot
    _bot_guild._bot = bot


async def log_event(guild_id: int, actor_id: int, action: str, *,
                    channel_id: int | None = None,
                    detail: dict | None = None) -> None:
    """Append-only журнал: PostgreSQL — источник истины,
    Discord-канал — представление. Сбой Discord не откатывает действие."""
    try:
        await db.event(guild_id, actor_id, action, channel_id=channel_id,
                       detail=detail)
    except Exception:
        log.exception("voice control journal write failed guild=%s", guild_id)
    try:
        conf = await db.settings(guild_id)
        channel_id_log = (conf.get("log_channel_id")
                          or VOICE_CONTROL_LOG_CHANNEL_ID or MOD_LOG_CHANNEL_ID)
        if not channel_id_log or _bot is None:
            return
        channel = _bot.get_channel(int(channel_id_log))
        if channel is None:
            channel = await _bot.fetch_channel(int(channel_id_log))
        if channel is None:
            return
        titles = {
            "voice.bulk_mute": "Voice Control | Массовый мут",
            "voice.bulk_unmute": "Voice Control | Массовый размут",
            "voice.speaker_start": "Voice Control | Режим ведущего",
            "voice.speaker_stop": "Voice Control | Завершение режима ведущего",
            "voice.speaker_add": "Voice Control | Добавлен ведущий",
            "voice.speaker_remove": "Voice Control | Убран ведущий",
            "voice.close": "Voice Control | Канал закрыт",
            "voice.open": "Voice Control | Канал открыт",
            "voice.move_all": "Voice Control | Массовое перемещение",
            "voice.disconnect_all": "Voice Control | Отключение всех",
            "voice.undo": "Voice Control | Отмена действия",
        }
        title = titles.get(action, "Voice Control | Событие")
        lines = [f"**Действие:** `{action}`"]
        if actor_id:
            lines.append(f"**Администратор:** <@{actor_id}>")
        if channel_id:
            lines.append(f"**Канал:** <#{channel_id}>")
        for key, value in (detail or {}).items():
            if isinstance(value, (int, float, str, bool)):
                lines.append(f"**{key}:** {value}")
        lines.append(f"<t:{int(time.time())}:F>")
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(content="## " + title[:100]))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(content="\n".join(lines)[:3000]))
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        await channel.send(view=view,
                           allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        log.exception("voice control Discord log failed guild=%s", guild_id)


# ----------------------------------------------------------------------
# Синхронизация состояния (voice_state_update) и восстановление
# ----------------------------------------------------------------------
async def reconcile_member(member: discord.Member) -> None:
    """Сверка после входа/перемещения/переподключения.

    - Ушёл из канала → его channel-ограничения там освобождаются,
      Discord-флаг (если он ещё применим) снимается по правилам.
    - В канале с speaker/auto режимом → при необходимости домут.
    - Просроченные ограничения → отложенная очистка флага.
    """
    guild = member.guild
    if member.bot:
        return
    voice = getattr(member, "voice", None)
    current_channel = voice.channel if voice else None

    # 1. Активные ограничения в ДРУГИХ каналах — освобождаем
    #    (участник ушёл; Discord-флаг, если ещё стоит, снимется
    #    через release_restriction, т.к. member.voice актуален).
    active = await db.restrictions(guild.id, user_id=member.id,
                                   active_only=True)
    for r in active:
        if current_channel is None or r["channel_id"] != current_channel.id:
            try:
                async with _member_lock(guild.id, r["channel_id"], member.id):
                    await release_restriction(r)
            except Exception:
                log.exception("reconcile release failed r=%s", r["id"])

    if current_channel is None or not _is_voice_channel(current_channel):
        # Участник полностью вышел: отложенная очистка флагов,
        # которые мы не смогли подтвердить онлайн.
        await _cleanup_offline_flags(member)
        return

    channel = current_channel
    mode = await db.mode(guild.id, channel.id)
    conf = await db.settings(guild.id)
    exceptions = await db.exceptions(guild.id, channel.id)
    speakers = await db.speakers(guild.id, channel.id)

    channel_restrictions = await db.restrictions(
        guild.id, channel_id=channel.id, user_id=member.id, active_only=True)

    # 2. Режимы канала: домут при необходимости.
    needs_mute = False
    origin = None
    if mode and mode.get("speaker_mode") and member.id not in speakers \
            and not _member_is_protected(member, conf, exceptions):
        needs_mute = True
        origin = ORIGIN_SPEAKER
    elif mode and mode.get("auto_mute") and not mode.get("speaker_mode") \
            and member.id not in speakers \
            and not _member_is_protected(member, conf, exceptions):
        needs_mute = True
        origin = ORIGIN_AUTO_MUTE

    if needs_mute:
        async with _member_lock(guild.id, channel.id, member.id):
            # Не дублируем: если уже есть активный mute — ничего не делаем.
            if not any(r["kind"] == "server_mute" for r in channel_restrictions):
                try:
                    await apply_restriction(
                        member, channel, "server_mute", origin,
                        actor_id=0, reason="Автоограничение Voice Control",
                    )
                except (StaleTargetError, VoiceControlError, discord.HTTPException):
                    pass
            channel_restrictions = await db.restrictions(
                guild.id, channel_id=channel.id, user_id=member.id, active_only=True)

    # 3. Наши активные mute-ограничения: флаг должен стоять.
    for r in channel_restrictions:
        if r["kind"] != "server_mute":
            continue
        if not await _current_flag(member, "server_mute"):
            async with _member_lock(guild.id, channel.id, member.id):
                try:
                    await _set_flag(member, guild.id, "server_mute", True,
                                    reason="Voice Control: восстановление")
                except discord.HTTPException:
                    pass

    # 4. Отложенная очистка: флаг стоит, активных ограничений нет,
    #    но есть наша ранее созданная (released/expired) запись,
    #    для которой discord-флаг не был подтверждённо снят.
    if not any(r["kind"] == "server_mute" for r in channel_restrictions) \
            and await _current_flag(member, "server_mute"):
        history = await db.restrictions(guild.id, channel_id=channel.id,
                                        user_id=member.id, active_only=False)
        ours = [r for r in history if r["kind"] == "server_mute"]
        if ours and all(r["state"] != "active" for r in ours):
            async with _member_lock(guild.id, channel.id, member.id):
                try:
                    await _clear_flag(member, guild.id, "server_mute",
                                      reason="Voice Control: отложенная очистка")
                except discord.HTTPException:
                    pass


async def _cleanup_offline_flags(member: discord.Member) -> None:
    """Участник полностью вышел из войса: если серверный флаг всё ещё
    числится установленным и у нас нет активных ограничений —
    пробуем снять (Discord сохраняет флаг в voice state)."""
    guild = member.guild
    voice = getattr(member, "voice", None)
    if voice is None:
        return
    active = await db.restrictions(guild.id, user_id=member.id,
                                   active_only=True)
    # Если активных ограничений нет, а флаги стоят — и есть наша
    # история по этому каналу, снимаем (отложенная очистка).
    for kind in KINDS:
        if not await _current_flag(member, kind):
            continue
        if any(r["kind"] == kind for r in active):
            continue  # активное ограничение — флаг оставляем
        channel_id = getattr(getattr(voice, "channel", None), "id", None)
        history = await db.restrictions(
            guild.id, channel_id=channel_id or 0, user_id=member.id,
            active_only=False)
        if any(r["kind"] == kind for r in history):
            async with _member_lock(guild.id, channel_id or 0, member.id):
                try:
                    await _clear_flag(member, guild.id, kind,
                                      reason="Voice Control: выход из войса")
                except discord.HTTPException:
                    pass


async def sweep_timers(bot) -> dict[str, int]:
    """Периодическая задача: истёкшие таймеры + сверка активных
    ограничений с реальным состоянием Discord."""
    stats = {"expired": 0, "reconciled": 0, "errors": 0}
    try:
        expired = await db.expire_due_restrictions()
    except Exception:
        log.exception("voice control expire_due failed")
        return stats
    for r in expired:
        stats["expired"] += 1
        try:
            guild = bot.get_guild(r["guild_id"])
            if guild is None:
                continue
            member = guild.get_member(r["user_id"])
            if member is None:
                try:
                    member = await guild.fetch_member(r["user_id"])
                except discord.HTTPException:
                    continue
            if member.voice and member.voice.channel and \
                    member.voice.channel.id == r["channel_id"]:
                async with _member_lock(r["guild_id"], r["channel_id"], r["user_id"]):
                    # Если есть ДРУГИЕ активные ограничения того же kind —
                    # флаг не снимаем.
                    if not await db.has_other_active(
                            r["guild_id"], r["channel_id"], r["user_id"],
                            r["kind"], exclude_id=r["id"]):
                        await _clear_flag(member, r["guild_id"], r["kind"],
                                          reason="Voice Control: истёк таймер")
            await log_event(r["guild_id"], 0, "voice.timer_expired",
                            channel_id=r["channel_id"],
                            detail={"user_id": r["user_id"], "kind": r["kind"]})
        except Exception:
            stats["errors"] += 1
            log.exception("voice control timer expiry failed r=%s", r["id"])

    # Сверка: активные ограничения, у которых Discord-флаг не стоит,
    # а участник онлайн в том же канале — повторно применяем.
    try:
        active = await db.all_active_restrictions()
    except Exception:
        log.exception("voice control active scan failed")
        return stats
    for r in active[:500]:
        try:
            guild = bot.get_guild(r["guild_id"])
            if guild is None:
                continue
            member = guild.get_member(r["user_id"])
            if member is None:
                continue
            voice = getattr(member, "voice", None)
            if voice is None or voice.channel is None or \
                    voice.channel.id != r["channel_id"]:
                continue
            if await _current_flag(member, r["kind"]):
                continue
            await _set_flag(member, r["guild_id"], r["kind"], True,
                            reason="Voice Control: восстановление после рестарта")
            stats["reconciled"] += 1
        except Exception:
            stats["errors"] += 1
    return stats


async def startup_reconcile(bot) -> None:
    """on_ready: восстановление после рестарта. Режимы и таймеры
    живут в PostgreSQL — здесь только сверка Discord-флагов."""
    for guild in bot.guilds:
        try:
            for channel in guild.channels:
                if not isinstance(channel, discord.VoiceChannel):
                    continue
                for member in channel.members:
                    if member.bot:
                        continue
                    try:
                        await reconcile_member(member)
                    except Exception:
                        log.exception("voice control startup reconcile failed")
        except Exception:
            log.exception("voice control startup guild reconcile failed %s", guild.id)
