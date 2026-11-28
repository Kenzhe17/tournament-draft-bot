"""r!z3 Voice Control — сервис ограничений голосовых каналов.

Ключевые принципы (safety hardening):
- Discord не различает, КТО наложил server_mute. Мы храним собственные
  ограничения в voice_control_restrictions и снимаем ТОЛЬКО их.
  Чужой/неопределённый мут никогда не трогаем.
- Durable lifecycle: pending_apply → active → pending_release →
  released/expired. Запись НЕ считается применённой/снятой без
  подтверждения Discord API.
- Модерационный координатор: активные voice.* события
  moderation_events учитываются при снятии/восстановлении флага.
  Модерационный мут, наложенный после Voice Control, сохраняется.
- На одного участника может быть несколько ограничений (таймер +
  режим ведущего + массовый мут) НЕЗАВИСИМО от канала — флаг
  server mute/deafen принадлежит guild voice state.
- Блокировки per-(guild,user): единый ключ для всех операций,
  включая sweeper и voice_state_update.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import discord

from config import (
    BOT_OWNER_ID,
    MOD_LOG_CHANNEL_ID,
    VOICE_CONTROL_LOG_CHANNEL_ID,
    VOICE_CONTROL_PROTECTED_CHANNEL_IDS,
)
from storage import moderation_store as mod_db
from storage.voice_control_store import voice_control_store as db

log = logging.getLogger(__name__)

KINDS = ("server_mute", "server_deafen")
ACTIVE_STATES = ("active", "pending_apply")
FLAG_ATTR = {"server_mute": "mute", "server_deafen": "deaf"}
EDIT_KWARG = {"server_mute": "mute", "server_deafen": "deaf"}
# Поле в журнале модерации для каждого kind.
MOD_FIELD = {"server_mute": "mute", "server_deafen": "deafen"}

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

# Происхождения, для которых дедупликация ограничения — по
# (channel_id, kind, origin): режимы и разовые действия канала.
# Таймеры (ORIGIN_TIMER) — отдельные причины: дедуп только по
# request_key/expires_at, стек нескольких таймеров допустим.
_DEDUP_BY_CHANNEL = {
    ORIGIN_MANUAL, ORIGIN_BULK_MUTE, ORIGIN_SPEAKER,
    ORIGIN_AUTO_MUTE, ORIGIN_UNDO,
}

# Единый ключ блокировки: (guild_id, user_id) — серверный флаг
# не зависит от канала, операции над одним участником сериализуются.
# Тот же ключ обязан использовать модерационный ког (cogs/mod.py),
# чтобы проверка модерационного состояния и Discord API были
# атомарны относительно Voice Control.
_member_locks: dict[tuple[int, int], asyncio.Lock] = {}
# Per-channel блокировка массовых операций (двойные нажатия).
_channel_locks: dict[tuple[int, int], asyncio.Lock] = {}
# Ограничение параллелизма Discord API на гильдию.
_api_semaphores: dict[int, asyncio.Semaphore] = {}

MAX_API_RETRIES_429 = 2
API_SEMAPHORE_LIMIT = 5
SWEEP_BATCH = 100

# Курсор фоновой сверки активных ограничений (честная пагинация).
_scan_cursor = 0


def _member_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    key = (guild_id, user_id)
    lock = _member_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _member_locks[key] = lock
    return lock


def member_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    """Публичный доступ к единому координатору (guild_id, user_id).
    Используется cogs/mod.py — модерация и Voice Control обязаны
    сериализовать проверку состояния и Discord API одним замком."""
    return _member_lock(guild_id, user_id)


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
    """Участник покинул ожидаемый канал до API-вызова."""


class VoiceControlError(Exception):
    """Ожидаемая ошибка Voice Control (показывается пользователю)."""


def voice_control_enabled() -> bool:
    from config import VOICE_CONTROL_ENABLED
    return VOICE_CONTROL_ENABLED


def is_protected_channel(guild_id: int, channel_id: int,
                         settings: dict | None = None) -> bool:
    """Защищённые каналы: env + настройки из PostgreSQL."""
    if channel_id in VOICE_CONTROL_PROTECTED_CHANNEL_IDS:
        return True
    return channel_id in set((settings or {}).get("protected_channel_ids") or [])


async def settings(guild_id: int) -> dict[str, Any]:
    return await db.settings(guild_id)


def can_use(member: discord.Member, conf: dict[str, Any] | None = None) -> bool:
    """Доступ к ephemeral-панели Voice Control: BOT_OWNER_ID либо
    участник с Discord Administrator. Проверяется при КАЖДОМ
    взаимодействии — отозванный Administrator теряет доступ сразу.
    conf сохранён в сигнатуре для совместимости вызовов."""
    if member is None or getattr(member, "id", None) is None:
        return False
    return member.id == BOT_OWNER_ID or bool(
        getattr(member.guild_permissions, "administrator", False))


async def require_access(interaction: discord.Interaction) -> discord.Member:
    if not interaction.guild or getattr(interaction.user, "id", None) is None:
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


def _in_expected_channel(member: discord.Member, expected_channel_id: int | None) -> bool:
    """Фактическая проверка канала участника перед API-вызовом."""
    if expected_channel_id is None:
        return True
    voice = getattr(member, "voice", None)
    return bool(voice and voice.channel and voice.channel.id == expected_channel_id)


def _check_actor_hierarchy(actor: discord.Member | None,
                           member: discord.Member) -> None:
    """Инициатор без administrator не может действовать на участника
    с ролью не ниже своей (кроме владельца сервера и BOT_OWNER)."""
    if actor is None or not isinstance(actor, discord.Member):
        return
    if actor.id == member.guild.owner_id or actor.id == BOT_OWNER_ID:
        return
    if actor.guild_permissions.administrator:
        return
    if actor.top_role <= member.top_role:
        raise VoiceControlError(
            "Иерархия: роль инициатора не выше роли участника.")


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
# Модерационный координатор (P0.4)
# ----------------------------------------------------------------------
async def _mod_state(guild_id: int, user_id: int, kind: str) -> dict[str, Any]:
    """Состояние голосового флага по журналу модерации.

    'active'   — модерация подтверждённо держит флаг;
    'inactive' — модерация подтверждённо снимала флаг последней;
    'unknown'  — результат модерационного действия не подтверждён;
    'none'     — записей нет.
    """
    try:
        return await mod_db.voice_state(guild_id, user_id, MOD_FIELD[kind])
    except Exception:
        log.exception("voice control moderation state failed g=%s u=%s",
                      guild_id, user_id)
        # Недоступность журнала модерации — консервативно unknown.
        return {"status": "unknown", "at": None}


async def _mod_cleared_after(guild_id: int, user_id: int, kind: str,
                             since: datetime | None) -> bool:
    """Модерация снимала этот флаг ПОСЛЕ since? Тогда ограничение
    считается отменённым модерацией — автоматически не восстанавливаем."""
    if since is None:
        return False
    try:
        st = await mod_db.voice_state(guild_id, user_id, MOD_FIELD[kind])
    except Exception:
        return False
    if st.get("status") != "inactive":
        return False
    at = st.get("at")
    if at is None:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    since_utc = since if since.tzinfo else since.replace(tzinfo=UTC)
    # >= : при равных timestamp порядок недоказуем — консервативно
    # считаем, что модерация сняла флаг не раньше создания
    # ограничения (не восстанавливаем, показываем конфликт).
    return at >= since_utc


# ----------------------------------------------------------------------
# Применение и снятие ограничений (durable lifecycle)
# ----------------------------------------------------------------------
async def _current_flag(member: discord.Member, kind: str) -> bool:
    voice = getattr(member, "voice", None)
    if voice is None or voice.channel is None:
        # Участник не в войсе: voice state может сохранять флаг,
        # но member.voice обнуляется — флаг недоступен для проверки.
        return False
    return bool(getattr(voice, FLAG_ATTR[kind], False))


async def _set_flag(member: discord.Member, guild_id: int, kind: str, value: bool,
                    *, reason: str = "r!z3 Voice Control",
                    expected_channel_id: int | None = None) -> None:
    kwarg = EDIT_KWARG[kind]

    async def call():
        # Финальная проверка канала НЕПОСРЕДСТВЕННО перед API-вызовом:
        # участник мог перейти в другой войс, пока шли проверки/очередь.
        if expected_channel_id is not None and \
                not _in_expected_channel(member, expected_channel_id):
            raise StaleTargetError("Участник сменил канал до API-вызова.")
        if kind == "server_deafen":
            return await member.edit(deaf=value, reason=reason)
        return await member.edit(mute=value, reason=reason)

    return await _api_call(guild_id, call, what=f"edit {kwarg}={value}")


async def _flag_state_anywhere(member: discord.Member, kind: str) -> bool:
    """Флаг серверного мута/глухоты читается из voice state даже когда
    участник не в канале (server mute сохраняется в voice state)."""
    voice = getattr(member, "voice", None)
    if voice is None:
        return False
    return bool(getattr(voice, FLAG_ATTR[kind], False))


async def apply_restriction(
    member: discord.Member,
    channel: discord.VoiceChannel,
    kind: str,
    origin: str,
    *,
    actor: discord.Member | None = None,
    actor_id: int = 0,
    reason: str = "",
    expires_at: datetime | None = None,
    request_key: str | None = None,
    exceptions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Публичная точка наложения ограничения — берёт единый
    coordinator-замок (guild_id, user_id). Внутренние вызовы под
    уже удерживаемым замком используют _apply_restriction."""
    async with _member_lock(member.guild.id, member.id):
        return await _apply_restriction(
            member, channel, kind, origin,
            actor=actor, actor_id=actor_id, reason=reason,
            expires_at=expires_at, request_key=request_key,
            exceptions=exceptions)


async def _apply_restriction(
    member: discord.Member,
    channel: discord.VoiceChannel,
    kind: str,
    origin: str,
    *,
    actor: discord.Member | None = None,
    actor_id: int = 0,
    reason: str = "",
    expires_at: datetime | None = None,
    request_key: str | None = None,
    exceptions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Наложить server_mute/server_deafen с учётом происхождения.

    Вызывается под _member_lock(guild_id, user_id). Идемпотентно по
    request_key и по (channel_id, kind, origin) для режимных origin.
    Ограничения разных каналов/origin стекуются. Запись создаётся в
    pending_apply и переходит в active только после подтверждённого
    Discord API; delivery_state фиксирует стадию доставки.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown restriction kind {kind}")
    if origin not in SYSTEM_ORIGINS:
        raise ValueError(f"unknown origin {origin}")
    if not voice_control_enabled():
        raise VoiceControlError("Voice Control отключён.")
    guild = member.guild
    if member.bot:
        raise VoiceControlError("Боты не могут быть ограничены.")
    if not _in_expected_channel(member, channel.id):
        raise StaleTargetError("Участник не в ожидаемом канале.")
    conf = await db.settings(guild.id)
    if exceptions is None:
        exceptions = await db.exceptions(guild.id, channel.id)
    if _member_is_protected(member, conf, exceptions):
        raise VoiceControlError("Участник защищён от Voice Control.")
    if member.id == (actor.id if actor else actor_id) \
            or member.id == guild.owner_id:
        raise VoiceControlError("Нельзя ограничить себя или владельца сервера.")
    _check_actor_hierarchy(actor, member)
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
    # Идемпотентность: область дедупликации — (channel, kind, origin)
    # для режимных origin; таймеры — независимые причины. Ограничения
    # разных каналов НЕ считаются дублями: server-флаг глобален, но
    # логическое ограничение принадлежит каналу привязки.
    existing = await db.restrictions(
        guild.id, user_id=member.id, active_only=True,
    )
    same_kind = [r for r in existing if r["kind"] == kind]
    dup = None
    for r in same_kind:
        if r["channel_id"] != channel.id or r["origin"] != origin:
            continue
        if origin in _DEDUP_BY_CHANNEL:
            dup = r
            break
        # Таймеры: дубль только при совпадении срока (одна причина).
        if r.get("expires_at") == expires_at:
            dup = r
            break
    if dup is not None:
        return {"restriction": dup, "skipped": "already_restricted"}
    # Флаг уже стоит, но наших покрывающих ограничений этого kind нет —
    # источник неизвестен (модерация/сторонний мут). Не присваиваем.
    if await _current_flag(member, kind) and not same_kind:
        return {"restriction": None, "skipped": "foreign_mute"}

    rec = await db.add_restriction(
        guild.id, channel.id, member.id, kind, origin,
        reason=reason, expires_at=expires_at, request_key=request_key,
        created_by=actor.id if actor else actor_id,
        state="pending_apply", delivery_state="not_dispatched",
    )
    if rec is None:
        # Дубль по request_key — вернуть существующую запись.
        rows = await db.restrictions(guild.id, active_only=False)
        rec = next((r for r in rows if r.get("request_key") == request_key), None)
        if rec is None:
            raise VoiceControlError("Не удалось создать ограничение (дубликат).")
        return {"restriction": rec, "skipped": "duplicate"}
    actor_ref = actor.id if actor else actor_id
    # dispatch_started ДО вызова API: после этой точки исход
    # считается неизвестным, пока Discord не подтвердит результат.
    # delivery_state и lifecycle state далее меняются АТОМАРНО
    # (set_state с delivery_state) — они не могут разойтись.
    await db.mark_delivery(guild.id, rec["id"], "dispatch_started")
    try:
        await _set_flag(member, guild.id, kind, True,
                        reason=reason or "r!z3 Voice Control",
                        expected_channel_id=channel.id)
    except StaleTargetError:
        # Проверка канала упала ДО отправки запроса — Discord API
        # гарантированно не вызывался: завершаем запись без unmute,
        # чужой флаг не трогаем.
        await db.set_state(guild.id, rec["id"], "failed",
                           actor_id=actor_ref,
                           from_states=("pending_apply",),
                           delivery_state="not_dispatched")
        raise
    except discord.Forbidden:
        # API однозначно отклонил — флаг не применялся.
        await db.set_state(guild.id, rec["id"], "failed",
                           actor_id=actor_ref,
                           from_states=("pending_apply",),
                           delivery_state="not_dispatched")
        raise VoiceControlError("Discord запретил изменение голосового состояния.")
    except discord.HTTPException:
        # Неопределённый результат: delivery=outcome_unknown —
        # sweeper/reconcile разрешит её по фактическому состоянию,
        # не присваивая владение флагом автоматически.
        await db.mark_delivery(guild.id, rec["id"], "outcome_unknown")
        log.warning("voice control apply %s ambiguous guild=%s user=%s",
                    kind, guild.id, member.id)
        raise VoiceControlError(
            "Discord не подтвердил результат. Ограничение записано, "
            "сверка состояния будет выполнена автоматически.")
    # Discord подтвердил применение. CAS pending_apply→active:
    # истёкший во время медленного apply таймер уже перевёл запись
    # в pending_release — просроченную запись в active не возвращаем,
    # а подтверждённый флаг безопасно очищаем.
    applied = await db.set_state(
        guild.id, rec["id"], "active", actor_id=actor_ref,
        from_states=("pending_apply",),
        delivery_state="confirmed_applied")
    if applied is None:
        fresh = await db.restriction_by_id(guild.id, rec["id"]) or rec
        if fresh.get("state") == "pending_release":
            # Таймер истёк между dispatch и commit — Discord применение
            # ПОДТВЕРДИЛ, фиксируем delivery и уходим в безопасную
            # очистку (unmute подтверждённо нашего флага).
            await db.mark_delivery(guild.id, rec["id"],
                                   "confirmed_applied")
            finished = await finish_pending_release(fresh, actor_id=actor_ref)
            return {"restriction": finished, "skipped": None,
                    "expired_during_apply": True}
        return {"restriction": fresh, "skipped": "state_changed"}
    return {"restriction": applied, "skipped": None}


async def _clear_flag(member: discord.Member, guild_id: int, kind: str,
                      *, reason: str = "r!z3 Voice Control",
                      expected_channel_id: int | None = None) -> bool:
    """Снять server-флаг. True — Discord API подтвердил результат.

    Локальное наблюдение флага НЕ является доказательством: edit
    идемпотентен, поэтому вызываем API безусловно — так мы не
    «считаем снятым» при ложном чтении voice state."""
    try:
        await _set_flag(member, guild_id, kind, False, reason=reason,
                        expected_channel_id=expected_channel_id)
        return True
    except StaleTargetError:
        # Покинул канал — флаг всё равно нужно снять: server mute
        # переживает выход из канала. Повторяем без привязки к каналу.
        try:
            await _set_flag(member, guild_id, kind, False, reason=reason)
            return True
        except discord.HTTPException:
            return False
    except discord.Forbidden:
        return False
    except discord.HTTPException:
        log.warning("voice control clear %s failed guild=%s user=%s",
                    kind, guild_id, member.id)
        return False


async def _final_state(restriction: dict[str, Any]) -> str:
    """'expired' для истёкших таймеров, иначе 'released'."""
    exp = restriction.get("expires_at")
    if exp is not None:
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp <= datetime.now(UTC):
            return "expired"
    return "released"


async def _fetch_member(guild: discord.Guild | None, user_id: int):
    if guild is None:
        return None
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None
    return member


def _delivery(restriction: dict[str, Any]) -> str:
    """Стадия доставки Discord API. NULL (строки до миграции)
    трактуется по контексту состояния записи."""
    d = restriction.get("delivery_state")
    if d:
        return d
    if restriction.get("state") == "active":
        return "confirmed_applied"
    if restriction.get("state") in ("pending_apply", "pending_release",
                                    "conflict"):
        return "outcome_unknown"
    return "not_dispatched"


async def _attempt_flag_release(member: discord.Member, restriction: dict[str, Any],
                                *, actor_id: int = 0,
                                claim_token: str | None = None,
                                force_unmute: bool = False) -> str:
    """Попытаться снять Discord-флаг для pending_release-ограничения.

    Вызывается под _member_lock(guild_id, user_id).
    Возвращает 'cleared' | 'kept_moderation' | 'kept_unknown' | 'retry'.
    Модерация имеет приоритет: активный/неопределённый модерационный
    флаг мы не снимаем и не присваиваем. При outcome_unknown владение
    текущим флагом не присваивается — фиксируется conflict.
    force_unmute=True — явный запрос администратора (release через
    панель/команду, emergency): владелец принимает решение о владении
    и флаг снимается. Автоматические пути (sweep/reconcile/таймер)
    вызывают с force_unmute=False и никогда не снимают флаг с
    недоказанным владением."""
    guild_id = restriction["guild_id"]
    kind = restriction["kind"]
    mod = await _mod_state(guild_id, restriction["user_id"], kind)
    status = mod.get("status")
    if status in ("active", "unknown"):
        # Модерация держит флаг (или результат не подтверждён) —
        # наш флаг не трогаем. 'active' → логическое снятие нашей
        # записи; 'unknown' → конфликт для администратора.
        if status == "unknown":
            await db.set_state(guild_id, restriction["id"], "conflict",
                               actor_id=actor_id,
                               expected_claim_token=claim_token)
            await log_event(guild_id, actor_id, "voice.conflict",
                            channel_id=restriction["channel_id"],
                            detail={"user_id": restriction["user_id"],
                                    "kind": kind,
                                    "reason": "moderation_state_unknown"})
            return "kept_unknown"
        await db.release_restriction(guild_id, restriction["id"],
                                     actor_id=actor_id,
                                     final_state=await _final_state(restriction),
                                     expected_claim_token=claim_token)
        await log_event(guild_id, actor_id, "voice.moderation_kept",
                        channel_id=restriction["channel_id"],
                        detail={"user_id": restriction["user_id"],
                                "kind": kind})
        return "kept_moderation"
    delivery = _delivery(restriction)
    if delivery == "not_dispatched":
        # Discord API гарантированно не применял наш флаг (проверка
        # канала упала до отправки или API однозначно отклонил) —
        # снимаем запись БЕЗ unmute: возможный чужой флаг не трогаем.
        await db.release_restriction(guild_id, restriction["id"],
                                     actor_id=actor_id,
                                     final_state=await _final_state(restriction),
                                     expected_claim_token=claim_token)
        return "cleared"
    if delivery in ("dispatch_started", "outcome_unknown"):
        # Исход первичного применения неизвестен: нельзя ни доказать,
        # что флаг наш, ни безопасно его снять. Флаг не установлен —
        # чисто; установлен или состояние не проверить — conflict
        # для администратора (без слепого unmute).
        voice = getattr(member, "voice", None)
        if voice is None:
            return "retry"  # флаг не проверить — повторим позже
        flag = bool(getattr(voice, FLAG_ATTR[kind], False))
        if not flag:
            await db.release_restriction(guild_id, restriction["id"],
                                         actor_id=actor_id,
                                         final_state=await _final_state(restriction),
                                         expected_claim_token=claim_token)
            return "cleared"
        if force_unmute:
            # Явное решение администратора: снимаем флаг как свой.
            # Модерация уже проверена выше (active/unknown → conflict).
            ok = await _clear_flag(member, guild_id, kind,
                                   reason="r!z3 Voice Control: "
                                          "снятие ограничения (решение администратора)")
            if ok:
                await db.release_restriction(guild_id, restriction["id"],
                                             actor_id=actor_id,
                                             final_state=await _final_state(restriction),
                                             expected_claim_token=claim_token)
                return "cleared"
            return "retry"
        await db.set_state(guild_id, restriction["id"], "conflict",
                           actor_id=actor_id,
                           expected_claim_token=claim_token)
        await log_event(guild_id, actor_id, "voice.conflict",
                        channel_id=restriction["channel_id"],
                        detail={"user_id": restriction["user_id"],
                                "kind": kind,
                                "reason": "delivery_unknown_flag_set",
                                "delivery": delivery})
        return "kept_unknown"
    # confirmed_applied — мы подтверждённо ставили флаг: снимаем
    # безусловным идемпотентным edit (edit не верит локальному
    # чтению voice state).
    ok = await _clear_flag(member, guild_id, kind,
                           reason="r!z3 Voice Control: снятие ограничения")
    if ok:
        await db.release_restriction(guild_id, restriction["id"],
                                     actor_id=actor_id,
                                     final_state=await _final_state(restriction),
                                     expected_claim_token=claim_token)
        return "cleared"
    return "retry"


async def finish_pending_release(restriction: dict[str, Any], *,
                                 actor_id: int = 0,
                                 claim_token: str | None = None,
                                 force_unmute: bool = False) -> dict[str, Any]:
    """Завершить pending_release: модерация → не трогаем флаг;
    иначе снимаем флаг и фиксируем терминальное состояние.

    Вызывается под _member_lock(guild_id, user_id). Под замком
    запись перечитывается: состояние и claim могли измениться,
    пока шли предыдущие шаги."""
    guild_id = restriction["guild_id"]
    fresh = await db.restriction_by_id(guild_id, restriction["id"])
    if fresh is None:
        return restriction
    if fresh["state"] != "pending_release":
        return fresh  # состояние изменилось — не вмешиваемся
    cur_claim = fresh.get("claim_token")
    if cur_claim is not None and cur_claim != claim_token:
        return fresh  # claim перехвачен другим worker'ом
    restriction = fresh
    if await db.has_other_active(guild_id, restriction["user_id"],
                                 restriction["kind"],
                                 exclude_id=restriction["id"]):
        # Другие наши ограничения того же kind ещё активны —
        # флаг сохраняем, запись логически завершена.
        return await db.release_restriction(
            guild_id, restriction["id"], actor_id=actor_id,
            final_state=await _final_state(restriction),
            expected_claim_token=claim_token) or restriction
    guild = _bot_guild(guild_id)
    member = await _fetch_member(guild, restriction["user_id"])
    if member is None:
        if guild is not None:
            # Участник есть в гильдии, но не в кэше и не fetched —
            # fetch_member вернул 404 → пользователь ушёл из сервера:
            # чистить нечего.
            try:
                await guild.fetch_member(restriction["user_id"])
            except discord.NotFound:
                return await db.release_restriction(
                    guild_id, restriction["id"], actor_id=actor_id,
                    final_state=await _final_state(restriction),
                    expected_claim_token=claim_token) or restriction
            except discord.HTTPException:
                pass  # сеть — повторим позже
        # Гильдия недоступна или сетевая ошибка — оставляем pending.
        return restriction
    # Продлеваем lease перед Discord HTTP: длинная операция не
    # должна потерять claim посреди вызова. Не наш claim — выходим.
    if claim_token is not None and not await db.renew_claim(
            restriction["id"], claim_token):
        return restriction
    result = await _attempt_flag_release(member, restriction,
                                         actor_id=actor_id,
                                         claim_token=claim_token,
                                         force_unmute=force_unmute)
    if result == "retry":
        return restriction
    # _attempt_flag_release уже зафиксировал терминальное состояние
    # (released/expired/conflict) — возвращаем актуальную запись.
    return await db.restriction_by_id(guild_id, restriction["id"]) \
        or restriction


async def _release_restriction(restriction: dict[str, Any], *,
                               actor: discord.Member | None = None,
                               actor_id: int = 0,
                               claim_token: str | None = None,
                               ) -> dict[str, Any]:
    """Внутренняя версия release_restriction — вызывается под
    _member_lock(guild_id, user_id)."""
    guild_id = restriction["guild_id"]
    kind = restriction["kind"]
    user_id = restriction["user_id"]
    actor_id = actor.id if actor else actor_id

    if restriction.get("state") not in ("active", "pending_apply",
                                        "pending_release", "conflict"):
        return restriction  # уже завершено — идемпотентно
    # 'conflict' тоже переводим в pending_release: явный запрос
    # администратора — это и есть безопасное разрешение конфликта
    # (force_unmute передаётся дальше и позволяет снять флаг с
    # недоказанным владением — решение принимает владелец).
    # Переход в pending_release — durable точка «очистка запрошена».
    claimed = await db.set_state(
        guild_id, restriction["id"], "pending_release", actor_id=actor_id,
        from_states=("active", "pending_apply", "pending_release",
                     "conflict"),
        expected_claim_token=claim_token)
    if claimed is None:
        return restriction  # гонка: другой воркер уже обрабатывает
    if await db.has_other_active(guild_id, user_id, kind,
                                 exclude_id=restriction["id"]):
        # Флаг покрыт другими нашими ограничениями — Discord-флаг
        # сохраняем, запись завершаем сразу.
        return await db.release_restriction(
            guild_id, restriction["id"], actor_id=actor_id,
            final_state=await _final_state(restriction),
            expected_claim_token=claim_token) or claimed
    # Явный запрос человека (actor/actor_id) — force_unmute;
    # автоматические пути (sweep/reconcile, actor_id=0) — нет.
    force = actor is not None or actor_id != 0
    return await finish_pending_release(claimed, actor_id=actor_id,
                                        claim_token=claim_token,
                                        force_unmute=force)


async def release_restriction(restriction: dict[str, Any], *,
                              actor: discord.Member | None = None,
                              actor_id: int = 0) -> dict[str, Any]:
    """Публичная точка снятия ограничения — берёт единый
    coordinator-замок (guild_id, user_id)."""
    async with _member_lock(restriction["guild_id"],
                            restriction["user_id"]):
        return await _release_restriction(
            restriction, actor=actor, actor_id=actor_id)


async def _resolve_pending_apply(restriction: dict[str, Any], *,
                                 actor_id: int = 0,
                                 claim_token: str | None = None,
                                 ) -> dict[str, Any]:
    """Довести pending_apply до определённого состояния.

    Вызывается под _member_lock(guild_id, user_id). Учитывает
    delivery_state:
    - not_dispatched — API гарантированно не вызывался → failed
      без Discord-вызовов;
    - confirmed_applied — apply подтверждён до коммита → CAS в
      active (или безопасная очистка, если запись уже pending_release);
    - dispatch_started/outcome_unknown — исход ПЕРВОГО запроса
      неизвестен. Если Discord-флаг уже установлен, владение
      недоказано: повторный mute=True НЕ отправляется (иначе мы
      «присвоили» бы чужой флаг), фиксируется conflict для
      разрешения владельцем. Если флаг не установлен — повторное
      применение безопасно и доказывает владение. Ошибка повтора
      НЕ уничтожает outcome_unknown предыдущего запроса.
    """
    guild_id = restriction["guild_id"]
    kind = restriction["kind"]
    user_id = restriction["user_id"]
    channel_id = restriction["channel_id"]
    actor_ref = actor_id
    delivery = _delivery(restriction)

    if delivery == "not_dispatched":
        # API гарантированно не вызывался — завершить без Discord.
        await db.set_state(guild_id, restriction["id"], "failed",
                           actor_id=actor_ref,
                           from_states=("pending_apply",),
                           expected_claim_token=claim_token,
                           delivery_state="not_dispatched")
        return restriction

    guild = _bot_guild(guild_id)
    member = await _fetch_member(guild, user_id)
    if member is None:
        # Не можем проверить — оставляем до появления пользователя.
        return restriction
    if not voice_control_enabled():
        # Feature flag OFF: не восстанавливаем и не применяем новые
        # ограничения — только безопасная очистка ранее созданных.
        await db.set_state(guild_id, restriction["id"], "pending_release",
                           actor_id=actor_ref,
                           expected_claim_token=claim_token)
        return restriction

    # Модерационные действия ПОСЛЕ создания ограничения: mute после —
    # конфликт (модерация могла забрать флаг); unmute после — наш
    # pending_apply не должен восстанавливать снятый флаг.
    mod = await _mod_state(guild_id, user_id, kind)
    if mod.get("status") in ("active", "unknown"):
        await db.set_state(guild_id, restriction["id"], "conflict",
                           actor_id=actor_ref,
                           expected_claim_token=claim_token)
        await log_event(guild_id, actor_ref, "voice.conflict",
                        channel_id=channel_id,
                        detail={"user_id": user_id, "kind": kind,
                                "reason": "moderation_owns_flag"})
        return restriction
    if await _mod_cleared_after(guild_id, user_id, kind,
                                restriction.get("created_at")):
        await db.set_state(guild_id, restriction["id"], "pending_release",
                           actor_id=actor_ref,
                           expected_claim_token=claim_token)
        return restriction

    if not _in_expected_channel(member, channel_id):
        # Применение не подтверждено и участник уже не там:
        # безопасный путь — pending_release (снимет флаг, если он есть).
        await db.set_state(guild_id, restriction["id"], "pending_release",
                           actor_id=actor_ref,
                           expected_claim_token=claim_token)
        return restriction
    # Participant мог стать защищённым после создания записи —
    # автоматическое применение не выполняем, уходим в очистку.
    conf = await db.settings(guild_id)
    exceptions = await db.exceptions(guild_id, channel_id)
    if _member_is_protected(member, conf, exceptions):
        await db.set_state(guild_id, restriction["id"], "pending_release",
                           actor_id=actor_ref,
                           expected_claim_token=claim_token)
        return restriction

    if delivery == "confirmed_applied":
        # Apply был подтверждён Discord API до коммита состояния —
        # фиксируем active атомарно. CAS не вернёт запись в active,
        # если истёкший таймер уже перевёл её в pending_release.
        active = await db.set_state(
            guild_id, restriction["id"], "active", actor_id=actor_ref,
            from_states=("pending_apply",),
            expected_claim_token=claim_token,
            delivery_state="confirmed_applied")
        if active is None:
            fresh = await db.restriction_by_id(guild_id, restriction["id"]) \
                or restriction
            if fresh.get("state") == "pending_release":
                return await finish_pending_release(
                    fresh, actor_id=actor_ref, claim_token=claim_token)
            return fresh
        return active

    # delivery in (dispatch_started, outcome_unknown): исход первого
    # запроса неизвестен. Проверяем ФАКТИЧЕСКИЙ флаг — но только для
    # решения, не для присвоения владения.
    voice = getattr(member, "voice", None)
    if voice is None or voice.channel is None:
        # Состояние Discord проверить нельзя — сохраняем pending
        # (повторим позже), информацию о неизвестном исходе не теряем.
        return restriction
    flag_set = bool(getattr(voice, FLAG_ATTR[kind], False))
    if flag_set:
        # Флаг установлен, но принадлежность недоказана: повторный
        # mute=True «присвоил» бы чужой флаг нашему ограничению.
        # Конфликт — владелец разрешает вручную (release → unmute).
        await db.set_state(guild_id, restriction["id"], "conflict",
                           actor_id=actor_ref,
                           expected_claim_token=claim_token)
        await log_event(guild_id, actor_ref, "voice.conflict",
                        channel_id=channel_id,
                        detail={"user_id": user_id, "kind": kind,
                                "reason": "delivery_unknown_flag_set",
                                "delivery": delivery})
        return restriction
    # Флаг не установлен: повторное применение безопасно — оно и есть
    # цель pending_apply и доказывает владение после подтверждения.
    # fencing: mark_delivery только если claim ещё наш.
    fenced = await db.mark_delivery(
        guild_id, restriction["id"], "dispatch_started",
        expected_claim_token=claim_token)
    if claim_token is not None and not fenced:
        return restriction  # claim перехвачен — не interfering
    # Продлеваем lease перед Discord HTTP повтора: длинная операция
    # не должна потерять claim посреди вызова API.
    if claim_token is not None and not await db.renew_claim(
            restriction["id"], claim_token):
        return restriction
    try:
        await _set_flag(member, guild_id, kind, True,
                        reason="r!z3 Voice Control: восстановление",
                        expected_channel_id=channel_id)
    except StaleTargetError:
        # Этот ПОВТОРНЫЙ запрос не отправлялся, но исход ПЕРВОГО
        # неизвестен — delivery остаётся outcome_unknown, запись
        # уходит в безопасную очистку (проверит флаг по факту).
        await db.set_state(guild_id, restriction["id"], "pending_release",
                           actor_id=actor_ref,
                           from_states=("pending_apply",),
                           expected_claim_token=claim_token,
                           delivery_state="outcome_unknown")
        return restriction
    except discord.Forbidden:
        # Повтор отклонён, но исход первого запроса неизвестен —
        # не «not_dispatched»: прежняя информация сохраняется.
        await db.set_state(guild_id, restriction["id"], "pending_release",
                           actor_id=actor_ref,
                           from_states=("pending_apply",),
                           expected_claim_token=claim_token,
                           delivery_state="outcome_unknown")
        return restriction
    except discord.HTTPException:
        # Сетевая ошибка повтора — delivery остаётся outcome_unknown,
        # pending_apply сохраняется для следующего sweep.
        await db.mark_delivery(guild_id, restriction["id"],
                               "outcome_unknown",
                               expected_claim_token=claim_token)
        return restriction
    # Повтор подтверждён — владение доказано нами.
    active = await db.set_state(
        guild_id, restriction["id"], "active", actor_id=actor_ref,
        from_states=("pending_apply",),
        expected_claim_token=claim_token,
        delivery_state="confirmed_applied")
    if active is None:
        # CAS проигран (expiry/manual release успел раньше) — но
        # повтор подтверждён: фиксируем delivery и чистим безопасно.
        await db.mark_delivery(guild_id, restriction["id"],
                               "confirmed_applied",
                               expected_claim_token=claim_token)
        fresh = await db.restriction_by_id(guild_id, restriction["id"]) \
            or restriction
        if fresh.get("state") == "pending_release":
            return await finish_pending_release(
                fresh, actor_id=actor_ref, claim_token=claim_token)
        return fresh
    return active


# ----------------------------------------------------------------------
# Массовые операции
# ----------------------------------------------------------------------
async def _guild_mute_restrictions(guild_id: int) -> set[int]:
    """User id с активными/pending mute-ограничениями Voice Control
    по ВСЕЙ гильдии (флаг не зависит от канала)."""
    rows = await db.restrictions(guild_id, active_only=True, limit=1000)
    return {r["user_id"] for r in rows if r["kind"] == "server_mute"}


async def _eligible_for_mute(channel: discord.VoiceChannel, actor: discord.Member,
                             conf: dict[str, Any],
                             exceptions: list[dict[str, Any]]) -> tuple[list[discord.Member], int]:
    """Участники, доступные для серверного мута. Возвращает (list, skipped)."""
    targets: list[discord.Member] = []
    skipped = 0
    already = await _guild_mute_restrictions(actor.guild.id)
    for m in humans_in(channel):
        if _member_is_protected(m, conf, exceptions):
            skipped += 1
            continue
        if m.id == actor.id:
            skipped += 1
            continue
        if m.id in already:
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
    if not voice_control_enabled():
        return {"error": "disabled", "done": 0, "skipped": 0,
                "errors": 0, "stale": 0, "op_id": ""}
    guild = actor.guild
    conf = await db.settings(guild.id)
    exceptions = await db.exceptions(guild.id, channel.id)
    targets, skipped = await _eligible_for_mute(channel, actor, conf, exceptions)

    expires = datetime.now(UTC) + duration if duration else None
    op_id = uuid.uuid4().hex
    await db.op_create(op_id, guild.id, channel.id, "bulk_mute", actor.id,
                       {"targets": len(targets)})
    done = errors = stale = 0
    async with _channel_lock(guild.id, channel.id):
        try:
            for m in targets:
                async with _member_lock(guild.id, m.id):
                    # Актуальность цели прямо перед API-вызовом.
                    if not _in_expected_channel(m, channel.id):
                        stale += 1
                        continue
                    try:
                        result = await _apply_restriction(
                            m, channel, "server_mute", origin,
                            actor=actor,
                            reason=reason or "Массовый мут (Voice Control)",
                            expires_at=expires,
                            exceptions=exceptions,
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
            await db.op_finish(op_id, "done",
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
    Модерационные и чужие муты не трогаются по построению.
    Считает обработанных УЧАСТНИКОВ, а не строки ограничений.
    Это безопасная очистка ранее созданных операций — допустима
    и при выключенном feature flag."""
    guild = actor.guild
    mode = await db.mode(guild.id, channel.id)
    if mode and mode.get("speaker_mode"):
        return {"error": "speaker_mode_active",
                "done": 0, "skipped": 0, "errors": 0}

    restrictions = await db.restrictions(
        guild.id, channel_id=channel.id, active_only=True)
    mute_restrictions = [r for r in restrictions if r["kind"] == "server_mute"]
    user_ids = sorted({r["user_id"] for r in mute_restrictions})
    op_id = uuid.uuid4().hex
    await db.op_create(op_id, guild.id, channel.id, "bulk_unmute", actor.id,
                       {"targets": len(user_ids)})
    done_users: set[int] = set()
    errors = 0
    try:
        for r in mute_restrictions:
            async with _member_lock(guild.id, r["user_id"]):
                try:
                    await _release_restriction(r, actor=actor)
                except Exception as exc:
                    errors += 1
                    log.info("bulk_unmute restriction %s failed: %s",
                             r["id"], exc)
        # Считаем участников, у которых не осталось покрывающих
        # mute-ограничений Voice Control (флаг может держать модерация).
        for uid in user_ids:
            remaining = await db.restrictions(
                guild.id, user_id=uid, active_only=True)
            if not any(x["kind"] == "server_mute" for x in remaining):
                done_users.add(uid)
        skipped = len(user_ids) - len(done_users)
        await db.op_finish(op_id, "done",
                           {"done": len(done_users), "errors": errors})
        await log_event(guild.id, actor.id, "voice.bulk_unmute",
                        channel_id=channel.id,
                        detail={"done": len(done_users), "errors": errors})
        return {"done": len(done_users), "skipped": max(0, skipped),
                "errors": errors, "op_id": op_id}
    except Exception:
        await db.op_finish(op_id, "unknown", {"done": len(done_users)})
        raise


# ----------------------------------------------------------------------
# Режим ведущего
# ----------------------------------------------------------------------
async def start_speaker_mode(channel: discord.VoiceChannel, actor: discord.Member,
                             speakers: list[discord.Member] | None = None) -> dict[str, Any]:
    """Активировать режим ведущего: все не-ведущие получают server_mute
    с origin='speaker_mode'. Ведущие по умолчанию — сам актор."""
    if not voice_control_enabled():
        return {"error": "disabled", "speakers": [], "muted": 0,
                "skipped": 0, "errors": 0}
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
        async with _member_lock(guild.id, m.id):
            try:
                result = await _apply_restriction(
                    m, channel, "server_mute", ORIGIN_SPEAKER,
                    actor=actor, reason="Режим ведущего",
                    exceptions=exceptions,
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
    """Завершить режим. Снимаются только ограничения origin='speaker_mode';
    флаг остаётся, пока покрыт таймером/массовым/модерационным мутом."""
    guild = actor.guild
    released = kept = errors = 0
    restrictions = await db.restrictions(guild.id, channel_id=channel.id,
                                         active_only=True)
    for r in [r for r in restrictions if r["origin"] == ORIGIN_SPEAKER]:
        async with _member_lock(guild.id, r["user_id"]):
            try:
                before = await db.has_other_active(
                    guild.id, r["user_id"], r["kind"],
                    exclude_id=r["id"])
                await _release_restriction(r, actor=actor)
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
        async with _member_lock(guild.id, member.id):
            await _release_restriction(r, actor=actor)
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
    if mode and mode.get("speaker_mode") and \
            _in_expected_channel(member, channel.id):
        exceptions = await db.exceptions(guild.id, channel.id)
        async with _member_lock(guild.id, member.id):
            try:
                result = await _apply_restriction(
                    member, channel, "server_mute", ORIGIN_SPEAKER,
                    actor=actor, reason="Режим ведущего",
                    exceptions=exceptions,
                )
                muted = not result.get("skipped")
            except (StaleTargetError, VoiceControlError, discord.HTTPException):
                pass
    await log_event(guild.id, actor.id, "voice.speaker_remove",
                    channel_id=channel.id, detail={"user_id": member.id})
    return {"muted": muted}


async def apply_auto_mute(member: discord.Member, channel: discord.VoiceChannel,
                          actor_id: int = 0) -> dict[str, Any] | None:
    """Автомут нового участника при auto_mute-режиме канала.
    При выключенном feature flag новые ограничения не создаются."""
    if not voice_control_enabled():
        return None
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
    async with _member_lock(member.guild.id, member.id):
        try:
            return await _apply_restriction(
                member, channel, "server_mute", ORIGIN_AUTO_MUTE,
                actor_id=actor_id, reason="Автомут новых участников",
                exceptions=exceptions,
            )
        except (StaleTargetError, VoiceControlError, discord.HTTPException):
            return None


async def set_auto_mute(channel: discord.VoiceChannel, actor: discord.Member,
                        enabled: bool) -> dict[str, Any]:
    """Включить/выключить автомут новых участников канала.

    При выключении завершаются СОБСТВЕННЫЕ ограничения origin=
    'auto_mute' этого канала — таймеры, массовые и модерационные
    мute не затрагиваются (у каждого ограничения свой lifecycle)."""
    guild = actor.guild
    await db.put_mode(guild.id, channel.id, auto_mute=enabled)
    released = 0
    if not enabled:
        restrictions = await db.restrictions(
            guild.id, channel_id=channel.id, active_only=True)
        for r in [r for r in restrictions if r["origin"] == ORIGIN_AUTO_MUTE]:
            async with _member_lock(guild.id, r["user_id"]):
                try:
                    await _release_restriction(r, actor=actor)
                    released += 1
                except Exception:
                    log.exception("auto_mute release failed r=%s", r["id"])
    await log_event(guild.id, actor.id, "voice.auto_mute",
                    channel_id=channel.id,
                    detail={"enabled": enabled, "released": released})
    return {"enabled": enabled, "released": released}


# ----------------------------------------------------------------------
# Закрытие/открытие канала (permission overwrites, P0.1)
# ----------------------------------------------------------------------
# Бит connect в Permissions bitfield.
_CONNECT_VALUE = discord.Permissions(connect=True).value


def _jload(raw: Any) -> dict[str, Any] | None:
    """JSONB-колонки asyncpg возвращает строкой — парсим в dict.
    None/'null' → None."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
        except ValueError:
            return None
        return val if isinstance(val, dict) else None
    return None


def _ow_snapshot(ow: discord.PermissionOverwrite | None) -> dict[str, Any] | None:
    """Полный снимок overwrite: allow/deny bitset + состояние connect.
    None — overwrite отсутствует."""
    if ow is None:
        return None
    allow, deny = ow.pair()
    ai, di = allow.value, deny.value
    return {"allow": ai, "deny": di,
            "connect": _connect_state(ai, di)}


def _connect_state(allow: int, deny: int) -> str:
    if allow & _CONNECT_VALUE:
        return "allow"
    if deny & _CONNECT_VALUE:
        return "deny"
    return "none"


def _ow_with_connect(allow: int, deny: int, connect: str
                     ) -> discord.PermissionOverwrite | None:
    """Overwrite из bitset, где connect переведён в нужное состояние,
    остальные биты сохранены. None — если результат пуст."""
    a = (allow | _CONNECT_VALUE) if connect == "allow" \
        else (allow & ~_CONNECT_VALUE)
    d = (deny | _CONNECT_VALUE) if connect == "deny" \
        else (deny & ~_CONNECT_VALUE)
    if a == 0 and d == 0:
        return None
    return discord.PermissionOverwrite.from_pair(
        discord.Permissions(a), discord.Permissions(d))


def _current_ow(channel: discord.VoiceChannel, target) -> dict[str, Any] | None:
    """Снимок ТЕКУЩЕГО overwrite цели на канале (не effective rights)."""
    overwrites = getattr(channel, "overwrites", None)
    if overwrites is None:
        return None
    ow = overwrites.get(target)
    if ow is None:
        return None
    return _ow_snapshot(ow)


async def _set_overwrite(channel: discord.VoiceChannel, target,
                        ow: discord.PermissionOverwrite | None,
                        *, guild_id: int, reason: str) -> None:
    async def call():
        return await channel.set_permissions(target, overwrite=ow,
                                             reason=reason)
    await _api_call(guild_id, call, what="set channel overwrite")


async def close_voice(channel: discord.VoiceChannel, actor: discord.Member) -> dict[str, Any]:
    """Закрыть вход: @everyone connect=False.

    - Сохраняется ПОЛНЫЙ allow/deny bitset исходного overwrite.
    - Меняется ТОЛЬКО connect; остальные биты сохраняются.
    - Member/role overwrites не трогаем — они и есть исключения.
    - Идемпотентно: повторный вызов при закрытом канале — no-op.
    - Запись 'saved' создаётся ДО Discord API; 'applied' — после
      подтверждения. Крэш между ними восстанавливается open_voice
      и startup-сверкой."""
    guild = actor.guild
    me = guild.me
    if not me.guild_permissions.manage_roles:
        raise VoiceControlError("Боту нужен Manage Roles для закрытия канала.")
    everyone = guild.default_role

    # Идемпотентность: канал уже закрыт панелью.
    mode = await db.mode(guild.id, channel.id)
    existing = await db.active_overwrites(guild.id, channel.id)
    if mode and mode.get("closed") and existing:
        return {"already": True, "before": existing[0].get("before")}

    before = _current_ow(channel, everyone)
    # applied: исходные биты + connect принудительно в deny.
    base_allow = before["allow"] if before else 0
    base_deny = before["deny"] if before else 0
    applied_ow = _ow_with_connect(base_allow, base_deny, "deny")
    applied_snap = _ow_snapshot(applied_ow)
    entries = [{
        "target_type": "role", "target_id": everyone.id,
        "before": before, "applied": applied_snap, "state": "saved",
    }]
    saved_ids = await db.save_overwrites(guild.id, channel.id, entries, actor.id)
    try:
        await _set_overwrite(
            channel, everyone, applied_ow, guild_id=guild.id,
            reason="r!z3 Voice Control: закрытие канала")
    except discord.HTTPException as exc:
        for oid in saved_ids:
            try:
                await db.resolve_overwrite(guild.id, oid, state="conflict",
                                           actor_id=actor.id)
            except Exception as exc2:
                log.debug("overwrite conflict resolve failed: %s", exc2)
        raise VoiceControlError(f"Discord отклонил изменение прав: {exc}")
    for oid in saved_ids:
        try:
            await db.resolve_overwrite(guild.id, oid, state="applied",
                                       actor_id=actor.id)
        except Exception as exc:
            log.debug("overwrite mark applied failed: %s", exc)
    await db.put_mode(guild.id, channel.id, closed=True)
    await log_event(guild.id, actor.id, "voice.close",
                    channel_id=channel.id, detail={"before": before})
    return {"before": before, "already": False}


async def open_voice(channel: discord.VoiceChannel, actor: discord.Member) -> dict[str, Any]:
    """Открыть канал: восстановить ТОЛЬКО connect-бит, изменённый
    панелью. Сторонние изменения остальных permission bits сохраняются.

    - Конфликт detect'ится только по connect-битам: если третья
      сторона изменила connect после нашего закрытия — не трогаем,
      помечаем conflict.
    - 'saved'-строки (крэш до подтверждения apply): если connect
      сейчас не deny — применения не было, закрывать нечего →
      resolved 'restored' без API-вызова.
    - Канал помечается открытым ТОЛЬКО при restored==всего и
      отсутствии конфликтов."""
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
        cur = _current_ow(channel, target)
        applied = _jload(row.get("applied")) or {}
        applied_connect = applied.get("connect", "deny")
        cur_allow = cur["allow"] if cur else 0
        cur_deny = cur["deny"] if cur else 0
        cur_connect = _connect_state(cur_allow, cur_deny)
        # 'saved' без подтверждённого применения: connect не deny →
        # Discord-изменения не было, восстанавливать нечего.
        was_applied = row.get("state") == "applied" or cur_connect == "deny"
        if not was_applied:
            await db.resolve_overwrite(guild.id, row["id"], state="restored",
                                       actor_id=actor.id)
            restored += 1
            continue
        # Конфликт: connect отличается от наложенного панелью.
        if cur_connect != applied_connect:
            await db.resolve_overwrite(guild.id, row["id"], state="conflict",
                                       actor_id=actor.id)
            conflicts += 1
            continue
        before = _jload(row.get("before"))
        before_connect = (before or {}).get("connect", "none")
        # Восстанавливаем ТОЛЬКО connect; остальные биты — текущие
        # (сторонние изменения сохраняются).
        new_allow = (cur_allow & ~_CONNECT_VALUE) | \
            (_CONNECT_VALUE if before_connect == "allow" else 0)
        new_deny = (cur_deny & ~_CONNECT_VALUE) | \
            (_CONNECT_VALUE if before_connect == "deny" else 0)
        new_ow = discord.PermissionOverwrite.from_pair(
            discord.Permissions(new_allow),
            discord.Permissions(new_deny)) if (new_allow or new_deny) else None
        try:
            await _set_overwrite(
                channel, target, new_ow, guild_id=guild.id,
                reason="r!z3 Voice Control: открытие канала")
            await db.resolve_overwrite(guild.id, row["id"], state="restored",
                                       actor_id=actor.id)
            restored += 1
        except discord.HTTPException as exc:
            log.warning("open_voice restore failed %s: %s", row["id"], exc)
            await db.resolve_overwrite(guild.id, row["id"], state="conflict",
                                       actor_id=actor.id)
            conflicts += 1
    # Помечаем открытым только при полном подтверждённом восстановлении.
    if conflicts == 0:
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
# Перемещение и отключение (P0.2: expected_channel_id)
# ----------------------------------------------------------------------
async def _check_move_target(member: discord.Member,
                             target: discord.VoiceChannel,
                             actor: discord.Member) -> None:
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
    _check_actor_hierarchy(actor, member)
    conf = await db.settings(guild.id)
    src = getattr(getattr(member, "voice", None), "channel", None)
    exceptions = await db.exceptions(guild.id, src.id) if src else []
    if _member_is_protected(member, conf, exceptions):
        raise VoiceControlError("Участник защищён от Voice Control.")
    # Проверка effective-прав входа в канал для самого участника.
    if not target.permissions_for(member).connect:
        raise VoiceControlError("Участник не может войти в этот канал.")
    limit = target.user_limit
    if limit:
        humans = len(humans_in(target))
        if humans >= limit:
            raise VoiceControlError("Целевой канал заполнен.")


async def move_member(member: discord.Member, target: discord.VoiceChannel,
                      actor: discord.Member, *,
                      expected_channel_id: int | None = None) -> None:
    """Переместить участника. expected_channel_id — снимок исходного
    канала: участник, перешедший в другой войс, не затрагивается."""
    guild = member.guild
    await _check_move_target(member, target, actor)
    voice = getattr(member, "voice", None)
    if voice is None or voice.channel is None:
        raise StaleTargetError("Участник не в войсе.")
    if expected_channel_id is not None and voice.channel.id != expected_channel_id:
        raise StaleTargetError("Участник сменил канал до перемещения.")
    if voice.channel.id == target.id:
        return

    async def call():
        # Повторная проверка НЕПОСРЕДСТВЕННО перед API-вызовом.
        if expected_channel_id is not None and \
                not _in_expected_channel(member, expected_channel_id):
            raise StaleTargetError("Участник сменил канал до API-вызова.")
        return await member.move_to(target, reason="r!z3 Voice Control")
    await _api_call(guild.id, call, what="move member")


async def disconnect_member(member: discord.Member, actor: discord.Member, *,
                            expected_channel_id: int | None = None) -> None:
    guild = member.guild
    me = guild.me
    if not me.guild_permissions.move_members:
        raise VoiceControlError("Боту нужен Move Members.")
    _check_actor_hierarchy(actor, member)
    conf = await db.settings(guild.id)
    src = getattr(getattr(member, "voice", None), "channel", None)
    exceptions = await db.exceptions(guild.id, src.id) if src else []
    if _member_is_protected(member, conf, exceptions):
        raise VoiceControlError("Участник защищён от Voice Control.")
    voice = getattr(member, "voice", None)
    if voice is None or voice.channel is None:
        raise StaleTargetError("Участник не в войсе.")
    if expected_channel_id is not None and voice.channel.id != expected_channel_id:
        raise StaleTargetError("Участник сменил канал до отключения.")
    if member.top_role >= me.top_role:
        raise VoiceControlError("Роль бота ниже роли участника.")

    async def call():
        if expected_channel_id is not None and \
                not _in_expected_channel(member, expected_channel_id):
            raise StaleTargetError("Участник сменил канал до API-вызова.")
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
        async with _member_lock(guild.id, m.id):
            try:
                await move_member(m, target, actor,
                                  expected_channel_id=channel.id)
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
        async with _member_lock(guild.id, m.id):
            try:
                await disconnect_member(m, actor,
                                        expected_channel_id=channel.id)
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
def _jdetail(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else {}
        except ValueError:
            return {}
    return {}


async def undo_last(channel: discord.VoiceChannel, actor: discord.Member) -> dict[str, Any]:
    """Отменить последнее обратимое действие, если состояние
    всё ещё актуально. Конфликты и устаревшие состояния отсекаются
    внутри вызываемых операций."""
    guild = actor.guild
    op = await db.last_reversible_op(guild.id, channel.id)
    if not op:
        return {"error": "nothing_to_undo"}
    kind = op["kind"]
    detail = _jdetail(op.get("detail"))
    try:
        if kind == "bulk_mute":
            result = await bulk_unmute(channel, actor, reason="Отмена Voice Control")
        elif kind == "bulk_unmute":
            result = await bulk_mute(channel, actor, origin=ORIGIN_UNDO,
                                     reason="Отмена размьюта (Voice Control)")
        elif kind == "speaker_start":
            result = await stop_speaker_mode(channel, actor)
        elif kind == "speaker_stop":
            result = await start_speaker_mode(channel, actor)
        elif kind in ("mute_member", "unmute_member"):
            user_id = detail.get("user_id")
            member = await _fetch_member(guild, user_id) if user_id else None
            if member is None:
                return {"error": "target_left"}
            if not _in_expected_channel(member, channel.id):
                return {"error": "target_left"}
            if kind == "mute_member":
                # Отмена мута = снять именно то ограничение, которое
                # было создано этим действием (а не все mute'ы юзера).
                rid = detail.get("restriction_id")
                released = 0
                restrictions = await db.restrictions(
                    guild.id, user_id=member.id, active_only=True)
                for r in restrictions:
                    if r["kind"] == "server_mute" and (
                            rid is None or r["id"] == rid):
                        await release_restriction(r, actor=actor)
                        released += 1
                result = {"done": released}
            elif kind == "unmute_member":
                # Отмена размьюта: вернуть mute только если ни одно
                # другое активное mute-ограничение не покрывает флаг
                # и модерация не держит свой мут.
                active = await db.restrictions(
                    guild.id, user_id=member.id, active_only=True)
                muted = False
                for r in active:
                    if r["kind"] == "server_mute":
                        muted = True
                        break
                mod = await _mod_state(guild.id, member.id, "server_mute")
                flag = await _flag_state_anywhere(member, "server_mute")
                if not muted and not flag and mod.get("status") != "active":
                    res = await apply_restriction(
                        member, channel, "server_mute", ORIGIN_UNDO,
                        actor=actor,
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
            "voice.conflict": "Voice Control | Конфликт состояний",
            "voice.moderation_kept": "Voice Control | Модерация удерживает флаг",
            "voice.timer_expired": "Voice Control | Таймер мута истёк",
            "voice.emergency": "Voice Control | Аварийная очистка",
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

    Ограничения канало-скоуплены: активные записи, чей channel_id
    не совпадает с текущим каналом участника, завершаются
    (pending_release → безопасная очистка). Переход в обычный войс
    не переносит mute; переход между управляемыми — создаёт
    ограничение назначения ДО снятия старого, поэтому флаг
    остаётся покрытым (has_other_active видит pending_apply).

    - pending_release/pending_apply разрешаются в первую очередь.
    - Домут для режимов ТЕКУЩЕГО канала — только при включённом
      feature flag и только если ограничение этого канала ещё
      не покрыто (ограничение предыдущего войса не считается).
    - Восстановление флага — только для активных ограничений
      ТЕКУЩЕГО канала, только при включённом flag, только если
      модерация не отменяла/не удерживает флаг и участник не стал
      защищённым."""
    guild = member.guild
    if member.bot:
        return

    async with _member_lock(guild.id, member.id):
        # Актуальное voice-состояние читаем ПОД замком: участник мог
        # сменить войс, пока ждал освобождения coordinator-замка.
        voice = getattr(member, "voice", None)
        raw_channel = voice.channel if voice else None
        current_channel = raw_channel if (
            raw_channel is not None and _is_voice_channel(raw_channel)) \
            else None
        current_id = current_channel.id if current_channel else None
        rows = await db.restrictions(guild.id, user_id=member.id,
                                     active_only=False, limit=300)

        # 1. pending_apply → разрешение по фактическому состоянию
        #    (не создаёт новых ограничений при выключенном flag —
        #    внутри _resolve_pending_apply).
        for r in rows:
            if r["state"] == "pending_apply":
                try:
                    await _resolve_pending_apply(r)
                except Exception:
                    log.exception("reconcile pending_apply failed r=%s",
                                  r["id"])

        rows = await db.restrictions(guild.id, user_id=member.id,
                                     active_only=False, limit=300)
        conf = exceptions = mode = speakers = None

        # 2. Домут для режимов ТЕКУЩЕГО канала — до снятия
        #    ограничений других каналов, чтобы флаг оставался
        #    покрытым. Только при включённом feature flag.
        if current_channel is not None and voice_control_enabled():
            conf = await db.settings(guild.id)
            exceptions = await db.exceptions(guild.id, current_id)
            mode = await db.mode(guild.id, current_id)
            speakers = await db.speakers(guild.id, current_id)
            origin = None
            if mode and not _member_is_protected(member, conf, exceptions) \
                    and member.id not in speakers \
                    and member.id != guild.owner_id:
                if mode.get("speaker_mode"):
                    origin = ORIGIN_SPEAKER
                elif mode.get("auto_mute"):
                    origin = ORIGIN_AUTO_MUTE
            if origin is not None:
                # Покрытие — только ограничением ЭТОГО канала с тем
                # же origin: чужой/межканальный мут режим не покрывает.
                covered = any(
                    r["kind"] == "server_mute"
                    and r["channel_id"] == current_id
                    and r["origin"] == origin
                    and r["state"] in ACTIVE_STATES
                    for r in rows)
                if not covered:
                    try:
                        await _apply_restriction(
                            member, current_channel, "server_mute", origin,
                            actor_id=0,
                            reason="Автоограничение Voice Control",
                            exceptions=exceptions,
                        )
                    except (StaleTargetError, VoiceControlError,
                            discord.HTTPException):
                        pass
                    rows = await db.restrictions(
                        guild.id, user_id=member.id,
                        active_only=False, limit=300)

        # 3. pending_release → попытка очистки (после шага 2 —
        #    покрытие назначения уже зафиксировано в PG).
        for r in rows:
            if r["state"] == "pending_release":
                try:
                    await finish_pending_release(r)
                except Exception:
                    log.exception("reconcile pending_release failed r=%s",
                                  r["id"])

        # 4. Активные ограничения ДРУГИХ каналов (или все — при
        #    выходе из войса) завершаются: channel-scope.
        #    Очистка старого канала не снимает новое ограничение
        #    назначения — has_other_active внутри _release_restriction.
        active = await db.restrictions(guild.id, user_id=member.id,
                                       active_only=True, limit=300)
        for r in active:
            if r["channel_id"] != current_id:
                try:
                    await _release_restriction(r, actor_id=0)
                except Exception:
                    log.exception("reconcile foreign-channel release "
                                  "failed r=%s", r["id"])

        # 5. Восстановление флага — только для активных ограничений
        #    ТЕКУЩЕГО канала и только при включённом feature flag.
        if not voice_control_enabled() or current_channel is None:
            return
        if conf is None:
            conf = await db.settings(guild.id)
            exceptions = await db.exceptions(guild.id, current_id)
        active = await db.restrictions(guild.id, user_id=member.id,
                                       active_only=True, limit=300)
        for r in active:
            if r["channel_id"] != current_id or r["state"] != "active":
                continue
            if _member_is_protected(member, conf, exceptions):
                continue  # защиту не снимаем и флаг не восстанавливаем
            kind = r["kind"]
            if await _flag_state_anywhere(member, kind):
                continue
            if await _mod_cleared_after(guild.id, member.id, kind,
                                        r.get("created_at")):
                # Модерация сняла флаг после создания ограничения —
                # не восстанавливаем, помечаем конфликт.
                await db.set_state(guild.id, r["id"], "conflict")
                await log_event(guild.id, 0, "voice.conflict",
                                channel_id=current_id,
                                detail={"user_id": member.id,
                                        "kind": kind,
                                        "reason": "moderation_unmute"})
                continue
            mod = await _mod_state(guild.id, member.id, kind)
            if mod.get("status") in ("active", "unknown"):
                # Модерация держит свой флаг или её исход неизвестен —
                # НЕ восстанавливаем при неизвестном состоянии.
                continue
            try:
                await _set_flag(member, guild.id, kind, True,
                                reason="Voice Control: восстановление",
                                expected_channel_id=current_id)
            except discord.HTTPException:
                pass


async def sweep_timers(bot) -> dict[str, int]:
    """Периодическая задача: истёкшие таймеры, durable claims
    pending_apply/pending_release, сверка активных ограничений.
    Крэш-устойчиво: всё незавершённое живёт в PostgreSQL."""
    stats = {"expired": 0, "pending_apply": 0, "pending_release": 0,
             "reconciled": 0, "errors": 0}
    worker = f"sweep-{id(bot):x}"

    # 1. Истёкшие таймеры → pending_release → попытка очистки.
    #    Expiry обрабатывается ПОД coordinator-замком через CAS:
    #    expire не гонится с in-flight apply — если apply уже
    #    подтвердил флаг, запись уходит в безопасную очистку;
    #    просроченная запись никогда не возвращается в active
    #    (финальный переход apply — CAS from pending_apply).
    try:
        due = await db.due_restrictions(limit=SWEEP_BATCH)
    except Exception:
        log.exception("voice control due scan failed")
        due = []
    for r in due:
        stats["expired"] += 1
        await log_event(r["guild_id"], 0, "voice.timer_expired",
                        channel_id=r["channel_id"],
                        detail={"user_id": r["user_id"], "kind": r["kind"]})
        # Истёкший таймер — завершение ранее созданной операции:
        # безопасно и при выключенном feature flag.
        async with _member_lock(r["guild_id"], r["user_id"]):
            fresh = await db.restriction_by_id(r["guild_id"], r["id"])
            if fresh is None or fresh["state"] not in ACTIVE_STATES:
                continue
            exp = fresh.get("expires_at")
            if exp is not None:
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=UTC)
                if exp > datetime.now(UTC):
                    continue  # срок продлили — не истёк
            # CAS: только из active/pending_apply — не трогаем
            # записи, уже ушедшие в pending_release/terminal.
            claimed = await db.set_state(
                r["guild_id"], r["id"], "pending_release",
                from_states=("active", "pending_apply"))
            if claimed is None:
                continue
            try:
                await finish_pending_release(claimed)
            except Exception:
                stats["errors"] += 1
                log.exception("voice control timer release failed r=%s",
                              r["id"])

    # 2. Durable claims: pending_apply и pending_release.
    #    Все переходы — под единым coordinator-замком (guild,user),
    #    claim проверяется по token: устаревший worker не завершит
    #    и не освободит claim нового.
    for state, key in (("pending_apply", "pending_apply"),
                       ("pending_release", "pending_release")):
        try:
            batch = await db.claim_pending(state, worker, limit=SWEEP_BATCH)
        except Exception:
            log.exception("voice control claim %s failed", state)
            continue
        stats[key] = len(batch)
        for r in batch:
            token = r.get("claim_token") or worker
            try:
                async with _member_lock(r["guild_id"], r["user_id"]):
                    fresh = await db.restriction_by_id(r["guild_id"], r["id"])
                    if fresh is None or fresh["state"] != state \
                            or fresh.get("claim_token") != token:
                        continue  # claim потерян/состояние изменилось
                    try:
                        if state == "pending_apply":
                            await _resolve_pending_apply(fresh,
                                                         claim_token=token)
                        else:
                            await finish_pending_release(fresh,
                                                         claim_token=token)
                    except Exception:
                        stats["errors"] += 1
                        log.exception("voice control sweep %s failed r=%s",
                                      state, r["id"])
            finally:
                try:
                    await db.release_claim(r["id"], token)
                except Exception:
                    log.debug("voice control release_claim failed r=%s",
                              r["id"], exc_info=True)

    # 3. Сверка активных: флаг должен стоять, если ограничение активно
    #    и модерация не отменяла его. Курсорная пагинация — без
    #    starvation после первых записей. При feature flag OFF —
    #    только безопасная очистка (шаги 1–2), без восстановления.
    global _scan_cursor
    try:
        batch = await db.scan_active(after_id=_scan_cursor,
                                     limit=SWEEP_BATCH)
        if not batch:
            _scan_cursor = 0
            batch = await db.scan_active(after_id=0, limit=SWEEP_BATCH)
        if batch:
            _scan_cursor = batch[-1]["id"]
    except Exception:
        log.exception("voice control active scan failed")
        return stats
    settings_cache: dict[int, dict] = {}
    for r in batch:
        try:
            if r["state"] != "active":
                continue
            if not voice_control_enabled():
                # Feature flag OFF: не восстанавливаем принудительно
                # отключённые флаги — только безопасная очистка выше.
                continue
            guild = bot.get_guild(r["guild_id"])
            if guild is None:
                continue
            member = guild.get_member(r["user_id"])
            if member is None:
                continue
            async with _member_lock(r["guild_id"], r["user_id"]):
                fresh = await db.restriction_by_id(r["guild_id"], r["id"])
                if fresh is None or fresh["state"] != "active":
                    continue
                voice = getattr(member, "voice", None)
                cur_ch = voice.channel if voice else None
                if cur_ch is None or cur_ch.id != fresh["channel_id"]:
                    continue
                # Защита проверяется и при автоматическом применении.
                if r["guild_id"] not in settings_cache:
                    settings_cache[r["guild_id"]] = \
                        await db.settings(r["guild_id"])
                conf = settings_cache[r["guild_id"]]
                exceptions = await db.exceptions(r["guild_id"],
                                                 fresh["channel_id"])
                if _member_is_protected(member, conf, exceptions):
                    continue
                if await _flag_state_anywhere(member, fresh["kind"]):
                    continue
                if await _mod_cleared_after(guild.id, r["user_id"],
                                            fresh["kind"],
                                            fresh.get("created_at")):
                    await db.set_state(guild.id, fresh["id"], "conflict")
                    continue
                mod = await _mod_state(guild.id, r["user_id"],
                                       fresh["kind"])
                if mod.get("status") in ("active", "unknown"):
                    continue  # модерация владеет флагом или исход неизвестен
                await _set_flag(member, r["guild_id"], fresh["kind"], True,
                                reason="Voice Control: восстановление после рестарта",
                                expected_channel_id=fresh["channel_id"])
                stats["reconciled"] += 1
        except Exception:
            stats["errors"] += 1
    return stats


async def _reconcile_overwrites(bot) -> None:
    """Startup-сверка permission overwrites: запись 'applied' должна
    соответствовать фактическому connect=deny на канале; расхождение —
    conflict (стороннее изменение), 'saved' без применения — restored."""
    try:
        rows = await db.all_active_overwrites()
    except Exception:
        log.exception("voice control overwrites scan failed")
        return
    for row in rows:
        try:
            guild = bot.get_guild(row["guild_id"])
            if guild is None:
                continue
            channel = guild.get_channel(row["channel_id"])
            if channel is None or not _is_voice_channel(channel):
                await db.resolve_overwrite(row["guild_id"], row["id"],
                                           state="conflict")
                continue
            target = guild.get_role(row["target_id"]) \
                if row["target_type"] == "role" \
                else guild.get_member(row["target_id"])
            if target is None:
                await db.resolve_overwrite(row["guild_id"], row["id"],
                                           state="conflict")
                continue
            cur = _current_ow(channel, target)
            cur_connect = _connect_state(cur["allow"], cur["deny"]) \
                if cur else "none"
            applied_snap = _jload(row.get("applied")) or {}
            applied_connect = applied_snap.get("connect", "deny")
            if row.get("state") == "saved" and cur_connect != "deny":
                # Применение не подтверждено и connect не deny —
                # Discord-изменения не было.
                await db.resolve_overwrite(row["guild_id"], row["id"],
                                           state="restored")
            elif cur_connect != applied_connect:
                await db.resolve_overwrite(row["guild_id"], row["id"],
                                           state="conflict")
            elif row.get("state") == "saved":
                await db.resolve_overwrite(row["guild_id"], row["id"],
                                           state="applied")
        except Exception:
            log.exception("voice control overwrite reconcile failed %s",
                          row.get("id"))


async def startup_reconcile(bot) -> None:
    """on_ready: восстановление после рестарта. Режимы, таймеры и
    незавершённые операции живут в PostgreSQL — здесь сверка
    Discord-флагов, overwrites и pending-состояний."""
    await _reconcile_overwrites(bot)
    for guild in bot.guilds:
        try:
            for channel in guild.channels:
                if not _is_voice_channel(channel):
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
    # Глобальная сверка pending_* не только для онлайн-участников.
    try:
        worker = "startup"
        for state in ("pending_apply", "pending_release"):
            batch = await db.claim_pending(state, worker, limit=SWEEP_BATCH)
            for r in batch:
                token = r.get("claim_token") or worker
                try:
                    async with _member_lock(r["guild_id"], r["user_id"]):
                        fresh = await db.restriction_by_id(r["guild_id"],
                                                           r["id"])
                        if fresh is None or fresh["state"] != state \
                                or fresh.get("claim_token") != token:
                            continue
                        try:
                            if state == "pending_apply":
                                await _resolve_pending_apply(fresh,
                                                             claim_token=token)
                            else:
                                await finish_pending_release(fresh,
                                                             claim_token=token)
                        except Exception:
                            log.exception("voice control startup %s "
                                          "failed r=%s", state, r["id"])
                finally:
                    try:
                        await db.release_claim(r["id"], token)
                    except Exception:
                        log.debug("voice control startup release_claim "
                                  "failed r=%s", r["id"], exc_info=True)
    except Exception:
        log.exception("voice control startup pending sweep failed")


# ----------------------------------------------------------------------
# Аварийная очистка (owner-only, P0.7)
# ----------------------------------------------------------------------
async def emergency_cleanup(guild: discord.Guild, actor: discord.Member, *,
                            dry_run: bool = True) -> dict[str, Any]:
    """Завершить все активные режимы и снять ограничения Voice Control.

    dry_run=True — только отчёт. Выполнение: speaker/auto режимы
    выключаются, закрытые каналы открываются, name/limit overrides
    откатываются к сохранённым значениям, все активные и
    pending-ограничения переводятся в pending_release и очищаются.
    Записи не удаляются — только при подтверждённой очистке Discord.
    """
    modes = [m for m in await db.modes(guild.id)
             if m.get("speaker_mode") or m.get("auto_mute")
             or m.get("closed") or m.get("name_override")
             or m.get("user_limit")]
    restrictions = await db.restrictions(guild.id, active_only=True,
                                         limit=1000)
    pending = await db.pending_states()
    overwrites = await db.all_active_overwrites()
    guild_overwrites = [o for o in overwrites if o["guild_id"] == guild.id]
    report = {
        "dry_run": dry_run,
        "channels_with_modes": len(modes),
        "active_restrictions": len(restrictions),
        "pending_states": pending,
        "active_overwrites": len(guild_overwrites),
        "actions": [],
    }
    if dry_run:
        for m in modes[:20]:
            report["actions"].append({
                "channel_id": m["channel_id"],
                "speaker_mode": bool(m.get("speaker_mode")),
                "auto_mute": bool(m.get("auto_mute")),
                "closed": bool(m.get("closed")),
                "name_override": m.get("name_override"),
                "user_limit": m.get("user_limit"),
            })
        return report

    # 1. Режимы каналов.
    for m in modes:
        channel_id = m["channel_id"]
        channel = guild.get_channel(channel_id)
        try:
            if m.get("speaker_mode"):
                if _is_voice_channel(channel):
                    await stop_speaker_mode(channel, actor)
                else:
                    # Канал удалён — снимаем режим и ограничения.
                    restrictions_ch = await db.restrictions(
                        guild.id, channel_id=channel_id, active_only=True)
                    for r in [r for r in restrictions_ch
                              if r["origin"] == ORIGIN_SPEAKER]:
                        async with _member_lock(guild.id, r["user_id"]):
                            await _release_restriction(r, actor=actor)
                    await db.clear_speakers(guild.id, channel_id)
                    await db.put_mode(guild.id, channel_id, speaker_mode=False)
            if m.get("auto_mute"):
                await db.put_mode(guild.id, channel_id, auto_mute=False)
            if m.get("closed") and _is_voice_channel(channel):
                await open_voice(channel, actor)
            if m.get("name_override") is not None or m.get("user_limit"):
                if _is_voice_channel(channel):
                    kwargs = {}
                    if m.get("name_before"):
                        kwargs["name"] = m["name_before"]
                    if m.get("user_limit_before") is not None:
                        kwargs["user_limit"] = m["user_limit_before"]
                    elif m.get("user_limit") is not None:
                        kwargs["user_limit"] = 0
                    if kwargs:
                        try:
                            await channel.edit(reason="r!z3 Voice Control: аварийная очистка",
                                               **kwargs)
                        except discord.HTTPException as exc:
                            report["actions"].append(
                                {"channel_id": channel_id,
                                 "error": f"channel.edit: {exc}"})
                await db.put_mode(guild.id, channel_id, name_override=None,
                                  user_limit=None)
            await db.delete_mode_if_idle(guild.id, channel_id)
        except Exception as exc:
            report["actions"].append({"channel_id": channel_id,
                                      "error": str(exc)})

    # 2. Все активные ограничения → pending_release → очистка.
    released = errors = 0
    for r in restrictions:
        async with _member_lock(guild.id, r["user_id"]):
            try:
                await _release_restriction(r, actor=actor)
                released += 1
            except Exception:
                errors += 1
    # pending_release, оставшиеся после попыток — считаем честно.
    states = await db.pending_states()
    pending_left = states.get("pending_release", 0) + \
        states.get("pending_apply", 0)
    report.update({
        "released_attempted": released,
        "pending_remaining": pending_left,
        "errors": errors,
    })
    await log_event(guild.id, actor.id, "voice.emergency",
                    detail={"dry_run": False, "released": released,
                            "pending_remaining": pending_left,
                            "errors": errors})
    return report
