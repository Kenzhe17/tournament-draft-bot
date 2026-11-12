"""Публикация баннера: расчёт дня → рендер → guild.edit(banner=...).

Идемпотентно: banner_updates фиксирует статус по (guild_id, day).
При любой ошибке текущий баннер не трогаем.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import discord

from services.banner import activity as act
from services.banner.renderer import render_banner_png
from storage.banner_store import banner_store

logger = logging.getLogger(__name__)


async def _fetch_avatar(member: discord.Member | discord.User | None) -> bytes | None:
    if member is None:
        return None
    try:
        avatar = member.display_avatar
        return await avatar.read()
    except Exception as e:
        logger.warning("banner: avatar fetch failed for %s: %s", member.id, e)
        return None


def _day_label(day: date) -> str:
    months = ("янв", "фев", "мар", "апр", "мая", "июн",
              "июл", "авг", "сен", "окт", "ноя", "дек")
    return f"{day.day} {months[day.month - 1]}"


async def build_banner_for_day(
    guild: discord.Guild, day: date, *, save_snapshot: bool = True
) -> tuple[bytes, dict]:
    """Сгенерировать PNG баннера за завершённый день.

    Возвращает (png_bytes, info) где info содержит winner/entries.
    Не публикует — для /banner preview.
    """
    entries, winner = await act.compute_day(guild.id, day, banner_store)

    member = None
    nickname = None
    avatar = None
    voice_seconds = matches = 0
    has_activity = winner is not None

    if winner is not None:
        voice_seconds = winner.voice_seconds
        matches = winner.matches
        member = guild.get_member(winner.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(winner.user_id)
            except Exception:
                member = None
        nickname = member.display_name if member else f"ID {winner.user_id}"
        avatar = await _fetch_avatar(member)
    else:
        nickname = None

    png = await render_banner_png(
        nickname=nickname,
        avatar_png=avatar,
        voice_seconds=voice_seconds,
        matches=matches,
        has_activity=has_activity,
        report_day_label=f"Отчёт: {_day_label(day)}",
    )
    info = {
        "day": day.isoformat(),
        "winner_id": winner.user_id if winner else None,
        "winner_name": nickname,
        "entries": entries[:5],
        "has_activity": has_activity,
    }
    return png, info


async def publish_day_banner(
    guild: discord.Guild, day: date, *, force: bool = False
) -> dict:
    """Полный цикл обновления Server Banner за день.

    Возвращает dict со статусом. Никогда не бросает наружу —
    ошибки логируются и фиксируются в banner_updates.
    """
    # 1. Идемпотентность: уже опубликовано?
    #    Если баннер удалили вручную в настройках сервера — считаем,
    #    что публикации нет, и обновляем повторно.
    existing = await banner_store.get_update(guild.id, day)
    if (
        not force
        and existing
        and existing.get("status") == "published"
        and guild.banner is not None
    ):
        logger.info("banner: %s day %s already published, skip", guild.id, day)
        return {"status": "already_published", "day": day.isoformat()}

    # 2. Права
    me = guild.me
    if me is None or not me.guild_permissions.manage_guild:
        await banner_store.record_update(
            guild.id, day, "failed", detail="no manage_guild permission")
        return {"status": "failed", "reason": "no_permission"}

    # 3. Генерация
    try:
        png, info = await build_banner_for_day(guild, day)
    except Exception as e:
        logger.exception("banner: render failed for guild %s day %s", guild.id, day)
        await banner_store.record_update(
            guild.id, day, "failed", detail=f"render: {e}")
        return {"status": "failed", "reason": f"render: {e}"}

    # 4. Публикация
    try:
        await guild.edit(banner=png, reason="r!z3 daily activity banner")
    except discord.Forbidden:
        await banner_store.record_update(
            guild.id, day, "failed", detail="discord forbidden (banner feature/perm)")
        return {"status": "failed", "reason": "forbidden"}
    except discord.HTTPException as e:
        await banner_store.record_update(
            guild.id, day, "failed", detail=f"http {e.status}: {e.text[:200]}")
        return {"status": "failed", "reason": f"http_{e.status}"}
    except Exception as e:
        logger.exception("banner: guild.edit failed")
        await banner_store.record_update(
            guild.id, day, "failed", detail=f"edit: {e}")
        return {"status": "failed", "reason": f"edit: {e}"}

    await banner_store.record_update(
        guild.id, day, "published",
        winner_id=info["winner_id"], published=True)
    logger.info(
        "banner: published for guild=%s day=%s winner=%s(%s)",
        guild.id, day, info["winner_name"], info["winner_id"])
    return {"status": "published", "day": day.isoformat(), **info}


def yesterday_almaty() -> date:
    return act.today_almaty() - timedelta(days=1)
