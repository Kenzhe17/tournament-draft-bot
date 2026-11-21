"""Ежедневная ротация роли «Доступ» (ACCESS_ROLE_ID).

Правила:
- каждый день в 21:00 Asia/Almaty (или вручную через /admin
  access_rotate) один наименее активный владелец заменяется
  наиболее активным кандидатом из очереди — только если кандидат
  активнее;
- активность за 7 дней: 1 очко / 5 мин подтверждённого голоса
  (без дневного лимита) + 10 очков за подтверждённый матч
  (match_history);
- меньше 10 владельцев — свободные места заполняются из очереди
  без снятия чужих ролей;
- защита: ORG-1/2/3, MANAGER, активные организаторские назначения,
  временные exemptions и 72 часа после получения — не снимаются
  автоматической ротацией;
- идемпотентность: access_rotation_runs (guild_id, run_key UNIQUE);
- при ошибке выдачи роли прежний владелец восстанавливается.

PostgreSQL — источник истины для очереди/журнала; фактические
Discord-роли перепроверяются перед изменениями.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Any

import discord

from config import (
    ACCESS_MAX_HOLDERS,
    ACCESS_NEW_HOLDER_PROTECT_HOURS,
    ACCESS_ROLE_ID,
    ACCESS_ROTATION_HOUR,
    ADMIN_USER_IDS,
    BOT_OWNER_ID,
    MANAGER_ROLE_ID,
    ORG_1_ROLE_ID,
    ORG_2_ROLE_ID,
    ORG_3_ROLE_ID,
    ORG_ROLE_MAP,
)
from services.banner import activity as act
from storage import access_store
from utils.access_banner import edit_attachments

logger = logging.getLogger(__name__)

WINDOW_DAYS = 7
VOICE_POINTS_PER_5MIN = act.POINTS_PER_5MIN      # 1 очко / 5 мин
MATCH_POINTS = act.MATCH_POINTS                  # 10 очков / матч

_PROTECT_ROLE_IDS = {
    rid for rid in (ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID,
                    MANAGER_ROLE_ID) if rid
}


# ---------------------------------------------------------------------------
# Активность
# ---------------------------------------------------------------------------


async def activity_scores(
    guild_id: int,
    user_ids: list[int] | set[int],
    *,
    now: datetime | None = None,
) -> dict[int, int] | None:
    """user_id -> очки активности за последние WINDOW_DAYS дней.

    None — данные недоступны (БД/таблицы): ротацию надо пропустить,
    а не считать всех неактивными.
    """
    if not user_ids:
        return {}
    now = now or datetime.now(timezone.utc)
    today = act.today_almaty(now)
    start_day = today - timedelta(days=WINDOW_DAYS - 1)
    wanted = {int(u) for u in user_ids}

    try:
        from storage.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Голос: сырые секунды banner_voice_daily за 7 дней
            # (дневной лимит снят — учитывается всё подтверждённое
            # время; исторические часы не восстанавливаются).
            vrows = await conn.fetch(
                """
                SELECT user_id, day, seconds FROM banner_voice_daily
                WHERE guild_id=$1 AND day >= $2 AND day <= $3
                """,
                guild_id, start_day, today,
            )
            # Матчи: подтверждённые (match_history пишется только
            # после подтверждения результата).
            mstart, mend = act.almaty_day_bounds_utc(start_day)
            _s2, mend2 = act.almaty_day_bounds_utc(today + timedelta(days=1))
            mrows = await conn.fetch(
                """
                SELECT user_id, COUNT(DISTINCT match_id) AS cnt
                FROM match_history
                WHERE guild_id=$1 AND date >= $2 AND date < $3
                  AND match_id <> ''
                GROUP BY user_id
                """,
                guild_id, mstart, mend2,
            )
    except Exception as exc:
        logger.warning("access rotation: activity unavailable: %s", exc)
        return None

    # Очки голоса: seconds // 300 за каждый день, без cap.
    voice_pts: dict[int, int] = {}
    for r in vrows:
        uid = int(r["user_id"])
        if uid not in wanted:
            continue
        voice_pts[uid] = voice_pts.get(uid, 0) + (
            int(r["seconds"])
            // (VOICE_POINTS_PER_5MIN * 300)
        )
    match_cnt = {
        int(r["user_id"]): int(r["cnt"])
        for r in mrows if int(r["user_id"]) in wanted
    }

    scores: dict[int, int] = {}
    for uid in wanted:
        scores[uid] = (
            voice_pts.get(uid, 0)
            + MATCH_POINTS * match_cnt.get(uid, 0)
        )
    return scores


# ---------------------------------------------------------------------------
# Discord-чтение
# ---------------------------------------------------------------------------


def _role_ids(member) -> set[int]:
    return {r.id for r in getattr(member, "roles", []) or []}


async def access_role_members(
    guild: discord.Guild, *, strict: bool = False,
) -> list:
    """Фактические владельцы роли «Доступ».

    ``strict=True`` (для решений ротации): всегда ``fetch_members`` —
    неполный кэш ``guild.members`` не принимается за полный список
    владельцев; при ошибке fetch — исключение наверх.
    Обычный режим (панель/статус): кэш → fetch только если пуст.
    """
    if strict:
        members = [m async for m in guild.fetch_members(limit=None)]
        return [m for m in members if ACCESS_ROLE_ID in _role_ids(m)]
    members = list(getattr(guild, "members", []) or [])
    if not members:
        try:
            members = [m async for m in guild.fetch_members(limit=None)]
        except Exception as exc:
            logger.warning("access: fetch_members failed: %s", exc)
            members = []
    return [m for m in members if ACCESS_ROLE_ID in _role_ids(m)]


async def _member(guild: discord.Guild, user_id: int):
    m = guild.get_member(user_id)
    if m is not None:
        return m
    try:
        return await guild.fetch_member(user_id)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Защита от ротации
# ---------------------------------------------------------------------------


async def has_org_duties(guild: discord.Guild, user_id: int) -> bool:
    """Активные организаторские обязанности: ORG-роли, слоты,
    channel organizers, активные турниры, ORG_ROLE_MAP."""
    member = await _member(guild, user_id)
    if member is not None and _role_ids(member) & _PROTECT_ROLE_IDS:
        return True
    try:
        from views.org_role_view import load_state
        state = load_state()
        if user_id in state.get_holders(ORG_1_ROLE_ID) or any(
            user_id in state.get_holders(rid)
            for rid in (ORG_2_ROLE_ID, ORG_3_ROLE_ID)
        ):
            return True
    except Exception:
        pass
    try:
        from storage.organizer_store import organizer_store
        for _gid, _cid, orgs in organizer_store.all():
            if orgs.is_organizer(user_id):
                return True
    except Exception:
        pass
    try:
        from storage.json_store import store as tournament_store
        for t in tournament_store.all():
            phase = str(getattr(getattr(t, "phase", ""), "value",
                                getattr(t, "phase", "")) or "")
            if phase in ("complete", "cancelled"):
                continue
            if getattr(t, "organizer_id", 0) == user_id:
                return True
    except Exception:
        pass
    # ORG_ROLE_MAP: роль, назначенная для канала активного турнира.
    try:
        if member is not None and ORG_ROLE_MAP:
            rids = _role_ids(member)
            for t in tournament_store.all():
                rid = ORG_ROLE_MAP.get(int(getattr(t, "channel_id", 0) or 0))
                if rid and int(rid) in rids:
                    phase = str(getattr(getattr(t, "phase", ""), "value",
                                        getattr(t, "phase", "")) or "")
                    if phase not in ("complete", "cancelled"):
                        return True
    except Exception:
        pass
    return False


async def _protected(
    guild: discord.Guild,
    user_id: int,
    *,
    exemptions: dict[int, Any],
    now: datetime,
) -> str | None:
    """Причина защиты или None. ORG-проверка — всегда (и для
    manual), exemptions/72ч — тоже: «защита от ротации» единая."""
    if user_id in exemptions:
        return "exempt"
    try:
        granted = await access_store.last_grant_at(guild.id, user_id)
        if granted is not None:
            if granted.tzinfo is None:
                granted = granted.replace(tzinfo=timezone.utc)
            if now - granted < timedelta(
                    hours=ACCESS_NEW_HOLDER_PROTECT_HOURS):
                return "new_holder_72h"
    except Exception:
        pass
    try:
        if await has_org_duties(guild, user_id):
            return "org_duties"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Ротация
# ---------------------------------------------------------------------------


async def _grant(guild: discord.Guild, user_id: int) -> bool:
    member = await _member(guild, user_id)
    role = guild.get_role(ACCESS_ROLE_ID)
    if member is None or role is None:
        return False
    try:
        await member.add_roles(role)
    except discord.Forbidden:
        raise
    try:
        await access_store.queue_remove(guild.id, user_id)
    except Exception as exc:
        # Роль выдана — заявка в очереди безвредна (holders
        # отфильтровываются из кандидатов при следующем запуске).
        logger.warning("access: queue_remove after grant failed: %s", exc)
    return True


async def _revoke(guild: discord.Guild, user_id: int) -> bool:
    member = await _member(guild, user_id)
    role = guild.get_role(ACCESS_ROLE_ID)
    if member is None or role is None:
        return False
    if ACCESS_ROLE_ID not in _role_ids(member):
        return True  # уже нет роли — нечего снимать
    await member.remove_roles(role)
    return True


async def run_rotation(
    guild: discord.Guild,
    *,
    kind: str = "auto",
    actor_id: int | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Один запуск ротации. Идемпотентно для kind='auto' по дате
    (Asia/Almaty); manual — уникальный run_key каждый раз.

    ``dry_run=True`` — только расчёт: без резерва run_key, без
    изменений Discord-ролей и без записей в журнал (превью
    для /admin access_rotate).

    Возвращает dict результата; никогда не бросает в вызывающий
    код Discord-ошибки без статуса в ответе.
    """
    now = now or datetime.now(timezone.utc)
    today = act.today_almaty(now)
    run_key = (
        f"auto:{today.isoformat()}" if kind == "auto"
        else f"manual:{uuid.uuid4().hex[:12]}"
    )
    if dry_run:
        result: dict[str, Any] = {
            "status": "dry_run", "run_key": run_key,
            "day": today.isoformat(), "filled": [], "swapped": None,
            "skipped_reason": None, "holders": [],
        }
    else:
        try:
            reserved = await access_store.begin_run(
                guild.id, run_key, today, kind)
        except Exception as exc:
            logger.error("access rotation: run reserve failed: %s", exc)
            return {"status": "db_error"}
        if not reserved:
            return {"status": "already_today"}
        result = {
            "status": "done", "run_key": run_key,
            "day": today.isoformat(), "filled": [], "swapped": None,
            "skipped_reason": None,
        }

    async def _fail(status: str, detail: str) -> dict[str, Any]:
        result["status"] = status
        result["skipped_reason"] = detail
        if not dry_run:
            try:
                await access_store.finish_run(
                    guild.id, run_key, status, detail)
            except Exception:
                pass
        return result

    # Журнал намерений: перед каждой Discord-мутацией план пишется
    # в access_rotation_runs.detail — recover_stale_runs завершает
    # прерванный рестартом запуск по фактическому состоянию ролей.
    plan_state: dict[str, Any] = {
        "stage": "idle", "fills_done": [], "pending": None,
        "swap": None,
    }

    async def _save_plan() -> None:
        if dry_run:
            return
        try:
            await access_store.set_run_plan(
                guild.id, run_key, dict(plan_state))
        except Exception as exc:
            logger.warning("access: set_run_plan failed: %s", exc)

    if ACCESS_ROLE_ID <= 0:
        return await _fail("disabled", "ACCESS_ROLE_ID не настроен")

    # Восстановление запусков, зависших в 'running' после рестарта —
    # до резервирования нового run_key.
    if not dry_run:
        try:
            await recover_stale_runs(guild)
        except Exception:
            logger.exception("access: stale run recovery failed")

    # --- Фактические владельцы (строго: без частичного кэша) ---
    try:
        holders = await access_role_members(guild, strict=True)
    except Exception as exc:
        return await _fail(
            "discord_error",
            f"holders fetch failed: {exc} — ротация пропущена "
            "(неполный кэш не считается полным списком)")
    holder_ids = [m.id for m in holders]
    result["holders"] = [
        {"user_id": m.id, "score": 0} for m in holders
    ]

    # --- Очередь и кандидаты (без текущих владельцев) ---
    try:
        queue = await access_store.queue_list(guild.id)
    except Exception as exc:
        return await _fail("db_error", f"queue: {exc}")
    holder_set = set(holder_ids)
    candidates = [u for u in queue if u not in holder_set]

    # Убрать владельцев из очереди (заявка не нужна).
    if not dry_run:
        for uid in holder_set & set(queue):
            try:
                await access_store.queue_remove(guild.id, uid)
            except Exception:
                pass

    # --- Активность за 7 дней ---
    all_uids = list(holder_set | set(candidates))
    scores = await activity_scores(guild.id, all_uids, now=now)
    if scores is None:
        return await _fail("activity_unavailable",
                           "нет данных активности — ротация пропущена")

    exemptions = {}
    try:
        exemptions = await access_store.active_exemptions(guild.id)
    except Exception:
        pass

    def _score(uid: int) -> int:
        return scores.get(uid, 0)

    for h in result.get("holders", []):
        h["score"] = _score(h["user_id"])

    # --- 1. Заполнение свободных мест (<10), без снятия ролей ---
    if len(holder_ids) < ACCESS_MAX_HOLDERS:
        # Кандидаты по убыванию активности; при equal — старейшая заявка.
        cand_sorted = sorted(
            candidates,
            key=lambda u: (-_score(u), queue.index(u)),
        )
        for uid in cand_sorted:
            if len(holder_ids) >= ACCESS_MAX_HOLDERS:
                break
            if kind == "auto" and uid in exemptions:
                continue
            # Фиксируем намерение ДО мутации Discord.
            plan_state.update(stage="fill", pending=uid)
            await _save_plan()
            if dry_run:
                ok = True  # только расчёт — роль не выдаём
            else:
                try:
                    ok = await _grant(guild, uid)
                except discord.Forbidden:
                    return await _fail(
                        "discord_forbidden", f"grant {uid}: Forbidden")
                except discord.HTTPException as exc:
                    return await _fail(
                        "discord_error", f"grant {uid}: {exc}")
                except Exception as exc:
                    return await _fail(
                        "discord_error", f"grant {uid}: {exc}")
            if not ok:
                plan_state["pending"] = None
                await _save_plan()
                continue
            holder_ids.append(uid)
            holder_set.add(uid)
            result["filled"].append({"user_id": uid, "score": _score(uid)})
            if not dry_run:
                # Журнал ДО снятия pending: окно «роль выдана, журнал
                # не записан» сводится к recovery-проверке find_rotation.
                try:
                    await access_store.record_rotation(
                        guild.id, run_key, today,
                        old_user_id=None, new_user_id=uid,
                        new_score=_score(uid),
                        actor="manual" if kind == "manual" else "auto",
                        detail="fill_empty_slot",
                    )
                except Exception:
                    pass
            plan_state["pending"] = None
            plan_state["fills_done"].append(uid)
            await _save_plan()
            if dry_run:
                continue
        if len(holder_ids) < ACCESS_MAX_HOLDERS or not candidates:
            # Мест нет или очередь пуста — замены не будет.
            if not result["filled"]:
                result["skipped_reason"] = "no_candidates"
            if not dry_run:
                try:
                    await access_store.finish_run(
                        guild.id, run_key, "done",
                        result["skipped_reason"])
                except Exception:
                    pass
            return result

    # Пересчитать кандидатов после заполнения.
    candidates = [u for u in queue if u not in set(holder_ids)]
    if not candidates:
        result["skipped_reason"] = "no_candidates"
        if not dry_run:
            try:
                await access_store.finish_run(
                    guild.id, run_key, "done", "no_candidates")
            except Exception:
                pass
        return result

    # --- 2. Одна замена: самый неактивный ↔ самый активный ---
    # Держатели, защищённые от ротации, не снимаются.
    removable: list[tuple[int, int]] = []  # (uid, score)
    protected_skipped: list[dict[str, Any]] = []
    for m in holders:
        uid = m.id
        if uid not in holder_set:
            continue  # заполненные выше — уже новые, не трогаем
        reason = await _protected(guild, uid, exemptions=exemptions,
                                  now=now)
        if reason:
            protected_skipped.append({"user_id": uid, "reason": reason})
        else:
            removable.append((uid, _score(uid)))
    if not removable:
        result["skipped_reason"] = "all_holders_protected"
        result["protected"] = protected_skipped
        if not dry_run:
            try:
                await access_store.finish_run(
                    guild.id, run_key, "done", "all_holders_protected")
            except Exception:
                pass
        return result

    removable.sort(key=lambda p: (p[1], p[0]))
    old_uid, old_score = removable[0]

    cand_sorted = sorted(
        candidates, key=lambda u: (-_score(u), queue.index(u)))
    new_uid = None
    new_score = 0
    for uid in cand_sorted:
        if _score(uid) > old_score:
            new_uid, new_score = uid, _score(uid)
            break
    if new_uid is None:
        result["skipped_reason"] = "no_more_active_candidate"
        if not dry_run:
            try:
                await access_store.finish_run(
                    guild.id, run_key, "done", "no_more_active_candidate")
            except Exception:
                pass
        return result

    result["swapped"] = {
        "old_user_id": old_uid, "old_score": old_score,
        "new_user_id": new_uid, "new_score": new_score,
    }
    result["protected"] = protected_skipped
    if dry_run:
        return result

    # --- Замена с восстановлением при сбое ---
    # План фиксируется до каждой мутации: phase='revoke' → 'grant'.
    plan_state.update(
        stage="swap",
        swap={"old": old_uid, "new": new_uid, "phase": "revoke"},
    )
    await _save_plan()
    try:
        revoked = await _revoke(guild, old_uid)
    except discord.Forbidden:
        return await _fail("discord_forbidden",
                           f"revoke {old_uid}: Forbidden")
    except discord.HTTPException as exc:
        return await _fail("discord_error", f"revoke {old_uid}: {exc}")
    except Exception as exc:
        return await _fail("discord_error", f"revoke {old_uid}: {exc}")
    if not revoked:
        return await _fail(
            "discord_error", f"revoke {old_uid}: member/role missing")

    plan_state["swap"]["phase"] = "grant"
    await _save_plan()
    try:
        granted = await _grant(guild, new_uid)
    except Exception as exc:
        # Восстановление прежнего владельца.
        try:
            await _grant(guild, old_uid)
        except Exception:
            logger.error(
                "access rotation: restore grant failed guild=%s "
                "user=%s", guild.id, old_uid)
        return await _fail(
            "discord_error",
            f"grant {new_uid} failed ({exc}); old restored")
    if not granted:
        # _grant вернул False (member/role недоступны) — откат.
        try:
            await _grant(guild, old_uid)
        except Exception:
            logger.error(
                "access rotation: restore grant failed guild=%s "
                "user=%s", guild.id, old_uid)
        return await _fail(
            "discord_error",
            f"grant {new_uid}: member/role missing; old restored")
    try:
        await access_store.record_rotation(
            guild.id, run_key, today,
            old_user_id=old_uid, new_user_id=new_uid,
            old_score=old_score, new_score=new_score,
            actor="manual" if kind == "manual" else "auto",
            detail={"protected": protected_skipped},
        )
        await access_store.finish_run(guild.id, run_key, "done")
    except Exception as exc:
        logger.warning("access rotation: journal failed: %s", exc)
    return result


# ---------------------------------------------------------------------------
# Восстановление прерванных запусков
# ---------------------------------------------------------------------------

# Запуск, висящий в 'running' дольше этого срока, считается
# прерванным (Discord-операции ротации занимают секунды).
STALE_RUN_SECONDS = 600


def _parse_run_plan(detail) -> dict[str, Any] | None:
    try:
        plan = json.loads(detail) if detail else None
    except Exception:
        return None
    return plan if isinstance(plan, dict) else None


async def _backfill_fill_journal(
    guild: discord.Guild, run: dict, uid: int, scores: dict[int, int],
) -> None:
    """Дописать журнал выдачи, если роль выдана, а записи нет."""
    try:
        if await access_store.find_rotation(
                guild.id, run["run_key"], new_user_id=uid):
            return
        await access_store.record_rotation(
            guild.id, run["run_key"], run.get("day"),
            old_user_id=None, new_user_id=uid,
            new_score=scores.get(uid, 0),
            actor=run.get("kind") or "auto",
            detail="fill_empty_slot",
        )
    except Exception as exc:
        logger.warning("access: backfill fill journal failed: %s", exc)


async def _recover_run(guild: discord.Guild, run: dict) -> dict[str, Any]:
    """Завершить один зависший запуск по фактическому состоянию
    Discord-ролей и JSON-плану из detail."""
    run_key = run["run_key"]
    plan = _parse_run_plan(run.get("detail"))
    stage = (plan or {}).get("stage")

    async def _finish(status: str, detail: str) -> dict[str, Any]:
        try:
            await access_store.finish_run(
                guild.id, run_key, status, detail[:500])
        except Exception:
            pass
        return {"run_key": run_key, "status": status, "detail": detail}

    # Строгий список владельцев — решение принимаем по факту.
    try:
        holders = await access_role_members(guild, strict=True)
    except Exception as exc:
        return await _finish(
            "interrupted", f"holders fetch failed: {exc}")
    holder_ids = {m.id for m in holders}

    if stage == "fill":
        pending = (plan or {}).get("pending")
        if pending:
            uid = int(pending)
            if uid not in holder_ids:
                # Грант не дошёл — довыдаём (идемпотентно).
                try:
                    await _grant(guild, uid)
                except Exception as exc:
                    return await _finish(
                        "interrupted",
                        f"re-grant pending {uid} failed: {exc}")
            scores = await activity_scores(guild.id, [uid]) or {}
            await _backfill_fill_journal(guild, run, uid, scores)
        return await _finish(
            "done", "recovered: fill stage completed/verified")

    if stage == "swap":
        sw = (plan or {}).get("swap") or {}
        try:
            old_uid = int(sw.get("old"))
            new_uid = int(sw.get("new"))
        except (TypeError, ValueError):
            return await _finish("interrupted", "swap plan malformed")
        old_has = old_uid in holder_ids
        new_has = new_uid in holder_ids

        if old_has and not new_has:
            # Замена не началась/откачена — оставляем как есть.
            return await _finish(
                "interrupted", "swap not applied; holders unchanged")
        if not old_has and new_has:
            # Замена завершена — журнал мог не записаться.
            try:
                if not await access_store.find_rotation(
                        guild.id, run_key,
                        old_user_id=old_uid, new_user_id=new_uid):
                    scores = await activity_scores(
                        guild.id, [old_uid, new_uid]) or {}
                    await access_store.record_rotation(
                        guild.id, run_key, run.get("day"),
                        old_user_id=old_uid, new_user_id=new_uid,
                        old_score=scores.get(old_uid, 0),
                        new_score=scores.get(new_uid, 0),
                        actor=run.get("kind") or "auto",
                        detail={"recovered": True},
                    )
            except Exception as exc:
                logger.warning(
                    "access: swap journal backfill failed: %s", exc)
            return await _finish(
                "done", "recovered: swap already applied")
        if not old_has and not new_has:
            # revoke прошёл, grant упал — восстанавливаем старого.
            try:
                await _grant(guild, old_uid)
            except Exception as exc:
                return await _finish(
                    "error", f"restore old {old_uid} failed: {exc}")
            return await _finish(
                "interrupted",
                f"grant failed mid-swap; old {old_uid} restored")
        # Обе роли есть — неоднозначное состояние, не трогаем.
        return await _finish(
            "interrupted", "ambiguous: both old and new hold role")

    # idle/нет плана — мутаций не было, безопасно закрыть.
    return await _finish("interrupted", "no plan recorded")


async def recover_stale_runs(guild: discord.Guild) -> list[dict[str, Any]]:
    """Найти и завершить запуски, зависшие в 'running' после
    рестарта/падения процесса. Вызывать перед новым запуском."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=STALE_RUN_SECONDS)
        runs = await access_store.stale_running_runs(guild.id, cutoff)
    except Exception as exc:
        logger.warning("access: stale runs query failed: %s", exc)
        return []
    results = []
    for run in runs:
        try:
            results.append(await _recover_run(guild, run))
        except Exception:
            logger.exception(
                "access: recover run failed guild=%s key=%s",
                guild.id, run.get("run_key"))
    return results


# ---------------------------------------------------------------------------
# Панель
# ---------------------------------------------------------------------------


def next_rotation_dt(now: datetime | None = None) -> datetime:
    """Ближайшие 21:00 Asia/Almaty (сегодня или завтра)."""
    now = now or datetime.now(timezone.utc)
    tz = act._almaty_tz()
    local = now.astimezone(tz)
    target = local.replace(
        hour=ACCESS_ROTATION_HOUR, minute=0, second=0, microsecond=0)
    if local.time() >= time(ACCESS_ROTATION_HOUR, 0):
        target += timedelta(days=1)
    return target


async def panel_context(guild: discord.Guild) -> dict[str, Any]:
    """Данные для отрисовки панели: владельцы+очки, очередь,
    следующая ротация, последняя замена."""
    holders = await access_role_members(guild)
    queue = []
    try:
        queue = await access_store.queue_list(guild.id, limit=50)
    except Exception:
        pass
    uids = [m.id for m in holders] + [u for u in queue
                                    if u not in {m.id for m in holders}]
    scores = await activity_scores(guild.id, uids) or {}
    last = None
    try:
        last = await access_store.last_rotation(guild.id)
    except Exception:
        pass
    queue_count = 0
    try:
        queue_count = await access_store.queue_count(guild.id)
    except Exception:
        queue_count = len(queue)
    return {
        "holders": holders,
        "scores": scores,
        "queue_count": queue_count,
        "queue": queue,
        "next_rotation": next_rotation_dt(),
        "last": last,
    }


async def refresh_panel(guild: discord.Guild) -> bool:
    """Перерисовать постоянную панель в её канале (in-place edit)."""
    try:
        from views.access_panel import render_panel_view
        state = await access_store.get_state(guild.id)
        cid, mid = state.get("channel_id"), state.get("message_id")
        if not (cid and mid):
            return False
        channel = guild.get_channel(int(cid))
        if channel is None:
            try:
                channel = await guild.fetch_channel(int(cid))
            except Exception:
                return False
        message = await channel.fetch_message(int(mid))
        edit_kwargs = {}
        att = edit_attachments(message)
        if att is not None:
            edit_kwargs["attachments"] = att
        await message.edit(
            view=render_panel_view(guild, await panel_context(guild)),
            allowed_mentions=discord.AllowedMentions.none(),
            **edit_kwargs,
        )
        return True
    except Exception as exc:
        logger.warning("access panel refresh failed: %s", exc)
        return False
