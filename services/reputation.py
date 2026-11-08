"""Reputation System 2.0 — бизнес-логика.

- /rep: +1 REP, скользящий cooldown 24ч, пожизненный лимит на пару.
- INVITE: +5 REP за уникального приглашённого после 72ч проверки.
- BOOST: +10 REP за первый подтверждённый буст после 7 дней.

Все лимиты per-guild, персистентны в PostgreSQL. Начисление и журнал —
в одной транзакции (storage/reputation_store.py).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from config import (
    REP_BOOST_AMOUNT,
    REP_BOOST_VERIFICATION_DAYS,
    REP_INVITE_AMOUNT,
    REP_INVITE_VERIFICATION_HOURS,
    REP_MANUAL_AMOUNT,
    REP_MANUAL_COOLDOWN_HOURS,
)
from storage.reputation_store import (
    SRC_BOOST,
    SRC_INVITE,
    ManualRepResult,
    ReputationStore,
    reputation_store,
)

if TYPE_CHECKING:
    import discord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /rep
# ---------------------------------------------------------------------------

async def give_manual_rep(
    guild_id: int,
    giver_id: int,
    target_id: int,
    target_name: str = "",
    store: ReputationStore | None = None,
) -> ManualRepResult:
    """Выдать +1 REP через /rep (атомарно в БД)."""
    store = store or reputation_store
    return await store.try_give_manual_rep(
        guild_id=guild_id,
        giver_id=giver_id,
        target_id=target_id,
        amount=REP_MANUAL_AMOUNT,
        cooldown=timedelta(hours=REP_MANUAL_COOLDOWN_HOURS),
        target_name=target_name,
    )


def format_cooldown(seconds: int) -> str:
    """Человекочитаемый остаток cooldown: '13 часов 42 минуты'."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours} ч. {minutes} мин."
    if hours:
        return f"{hours} ч."
    if minutes:
        return f"{minutes} мин."
    return "менее минуты"


# ---------------------------------------------------------------------------
# Приглашения
# ---------------------------------------------------------------------------

def diff_invite_uses(
    before: dict[str, "discord.Invite"],
    after: dict[str, "discord.Invite"],
) -> list[tuple[str, "discord.Invite"]]:
    """Инвайты, у которых вырос счётчик использований.

    Возвращает [(code, invite_after), ...]. Пустой список — вход
    не через invite-ссылку (vanity/Widget/прямой линк).
    """
    out: list[tuple[str, Any]] = []
    for code, inv in after.items():
        prev = before.get(code)
        if prev is None:
            continue
        if inv.uses > prev.uses:
            out.append((code, inv))
    return out


def attribute_invite(
    member: "discord.Member",
    increased: list[tuple[str, "discord.Invite"]],
) -> tuple[int | None, str, str]:
    """Определить пригласившего по diff инвайтов.

    Возвращает (inviter_id | None, invite_code, attribution).
    attribution: ok | ambiguous | unknown | self | bot
    """
    if not increased:
        return None, "", "unknown"
    inviters = {
        inv.inviter.id
        for _code, inv in increased
        if inv.inviter is not None
    }
    if len(inviters) > 1:
        return None, increased[0][0], "ambiguous"
    inviter_id = next(iter(inviters), None)
    code = next(c for c, inv in increased if inv.inviter and inv.inviter.id == inviter_id)
    if inviter_id is None:
        return None, code, "unknown"
    if inviter_id == member.id:
        return inviter_id, code, "self"
    return inviter_id, code, "ok"


async def record_member_join(
    bot,
    member: "discord.Member",
    before_invites: dict[str, "discord.Invite"],
    store: ReputationStore | None = None,
) -> None:
    """Зафиксировать кандидата на REP-награду за приглашение.

    Вызывается из on_member_join. Неоднозначная атрибуция журналируется
    и не приводит к автоматическому начислению.
    """
    store = store or reputation_store
    guild_id = member.guild.id

    if member.bot:
        await store.create_invite_candidate(
            guild_id, None, member.id, "", _now() + _verify_delta(),
            attribution="bot", reject_reason="invitee is a bot",
        )
        return

    try:
        after_invites = {i.code: i for i in await member.guild.invites()}
    except Exception as exc:
        logger.warning("Failed to fetch invites for guild %s: %s", guild_id, exc)
        return

    increased = diff_invite_uses(before_invites, after_invites)
    inviter_id, code, attribution = attribute_invite(member, increased)
    reject_reason = ""
    if attribution == "ambiguous":
        reject_reason = "ambiguous invite attribution"
        logger.info(
            "Ambiguous invite attribution in guild %s for user %s",
            guild_id, member.id,
        )
    elif attribution == "self":
        reject_reason = "self invite"
    elif attribution == "unknown":
        reject_reason = "no invite diff detected"

    await store.create_invite_candidate(
        guild_id, inviter_id, member.id, code,
        _now() + _verify_delta(),
        attribution=attribution,
        reject_reason=reject_reason,
    )
    # Обновляем кэш инвайтов после обработки входа
    try:
        bot._rep_invite_cache[guild_id] = {
            i.code: i for i in await member.guild.invites()
        }
    except Exception:
        pass


async def process_due_invites(
    bot,
    store: ReputationStore | None = None,
) -> int:
    """Проверить и выдать REP за подтверждённые приглашения."""
    store = store or reputation_store
    rewarded = 0
    for inv in await store.get_due_invites(_now()):
        guild_id = int(inv["guild_id"])
        invitee_id = int(inv["invitee_id"])
        inviter_id = inv.get("inviter_id")
        inviter_id = int(inviter_id) if inviter_id else None

        async def _reject(reason: str) -> None:
            await store.update_invite(
                int(inv["id"]), reward_status="rejected", reject_reason=reason
            )

        if inv.get("attribution") != "ok" or inviter_id is None:
            await _reject(f"attribution={inv.get('attribution')}")
            continue

        guild = bot.get_guild(guild_id)
        if guild is None:
            try:
                guild = await bot.fetch_guild(guild_id)
            except Exception:
                await _reject("guild unavailable")
                continue

        # Приглашённый должен всё ещё быть на сервере
        try:
            member = await guild.fetch_member(invitee_id)
        except Exception:
            await store.update_invite(
                int(inv["id"]), reward_status="cancelled",
                reject_reason="invitee left before verification",
            )
            continue
        if member.bot:
            await store.update_invite(
                int(inv["id"]), reward_status="cancelled",
                reject_reason="invitee is a bot",
            )
            continue

        # Пригласивший тоже должен быть на сервере
        try:
            inviter = await guild.fetch_member(inviter_id)
        except Exception:
            await store.update_invite(
                int(inv["id"]), reward_status="cancelled",
                reject_reason="inviter left before verification",
            )
            continue

        # Уникальность: guild_id + invited_user_id (event_key)
        ok = await store.award_rep(
            guild_id=guild_id,
            user_id=inviter_id,
            source=SRC_INVITE,
            amount=REP_INVITE_AMOUNT,
            event_key=f"invite:{guild_id}:{invitee_id}",
            actor_user_id=inviter_id,
            subject_user_id=invitee_id,
            user_name=inviter.display_name,
        )
        if ok:
            await store.update_invite(int(inv["id"]), reward_status="rewarded")
            rewarded += 1
            logger.info(
                "Invite REP +%d to %s for inviting %s (guild %s)",
                REP_INVITE_AMOUNT, inviter_id, invitee_id, guild_id,
            )
        else:
            await store.update_invite(
                int(inv["id"]), reward_status="rejected",
                reject_reason="reward already granted for this invitee",
            )
    return rewarded


# ---------------------------------------------------------------------------
# Бусты
# ---------------------------------------------------------------------------

async def record_boost_start(
    member: "discord.Member",
    premium_since: datetime,
    store: ReputationStore | None = None,
) -> None:
    """Зафиксировать начало нового периода буста (ещё не подтверждён)."""
    store = store or reputation_store
    started = premium_since if premium_since.tzinfo else premium_since.replace(tzinfo=timezone.utc)
    await store.upsert_boost_pending(
        guild_id=member.guild.id,
        booster_id=member.id,
        boost_started_at=started,
        verify_until=_now() + timedelta(days=REP_BOOST_VERIFICATION_DAYS),
    )


async def record_boost_end(
    guild_id: int,
    booster_id: int,
    store: ReputationStore | None = None,
) -> None:
    """Буст отменён — сбросить ожидающий период (не трогает rewarded)."""
    store = store or reputation_store
    await store.cancel_boost_pending(guild_id, booster_id)


async def process_due_boosts(
    bot,
    store: ReputationStore | None = None,
) -> int:
    """Подтвердить и выдать REP за непрерывные бусты."""
    store = store or reputation_store
    rewarded = 0
    for row in await store.get_due_boosts(_now()):
        guild_id = int(row["guild_id"])
        booster_id = int(row["booster_id"])
        boost_id_status = await store.get_boost(guild_id, booster_id)
        if boost_id_status is None or boost_id_status.get("status") != "pending":
            continue

        guild = bot.get_guild(guild_id)
        if guild is None:
            try:
                guild = await bot.fetch_guild(guild_id)
            except Exception:
                continue

        try:
            member = await guild.fetch_member(booster_id)
        except Exception:
            await store.cancel_boost_pending(guild_id, booster_id)
            continue

        premium_since = getattr(member, "premium_since", None)
        started_raw = row.get("boost_started_at")
        if started_raw is not None and started_raw.tzinfo is None:
            started_raw = started_raw.replace(tzinfo=timezone.utc)

        if premium_since is None:
            # Буст отменён до подтверждения
            await store.cancel_boost_pending(guild_id, booster_id)
            continue

        ps = premium_since if premium_since.tzinfo else premium_since.replace(tzinfo=timezone.utc)
        if started_raw is not None and ps > started_raw:
            # premium_since сдвинулся — буст перезапускался, новый период
            await record_boost_start(member, premium_since, store=store)
            continue

        # Награда один раз на пользователя (UNIQUE event_key)
        ok = await store.award_rep(
            guild_id=guild_id,
            user_id=booster_id,
            source=SRC_BOOST,
            amount=REP_BOOST_AMOUNT,
            event_key=f"boost:{guild_id}:{booster_id}",
            actor_user_id=booster_id,
            subject_user_id=booster_id,
            user_name=member.display_name,
        )
        if ok:
            await store.mark_boost_rewarded(guild_id, booster_id)
            rewarded += 1
            logger.info(
                "Boost REP +%d to %s (guild %s)",
                REP_BOOST_AMOUNT, booster_id, guild_id,
            )
        else:
            await store.mark_boost_rewarded(guild_id, booster_id)
    return rewarded


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _verify_delta() -> timedelta:
    return timedelta(hours=REP_INVITE_VERIFICATION_HOURS)
