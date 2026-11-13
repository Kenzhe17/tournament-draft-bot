"""Голосование за правила матча (BRAZIL / KAZAKHSTAN) для обычных
турниров ``/tournament create``.

Новая фаза ``TournamentPhase.RULES_VOTING`` между «Старт» и
формированием команд (Draft / Balanced SR / Random). Состояние
хранится в ``tournament.rules_vote`` (json_store) и зеркалируется
в PostgreSQL (storage/rules_vote_store). Итог — ``tournament.ruleset``.

Правила голосования (ТЗ):
- голосуют только зарегистрированные участники, один голос;
- ровно 20 секунд, досрочного завершения нет даже при 100% голосов;
- ничья и «ноль голосов» → KAZAKHSTAN (заранее объявленный дефолт).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord

from models.tournament import TournamentPhase
from storage.json_store import store

logger = logging.getLogger(__name__)

VOTE_SECONDS = 20
DEFAULT_RULESET = "KAZAKHSTAN"

# Определения правил — единый источник из голосования за формат MM.
from services.format_vote import _FORMATS  # noqa: E402

_LOCKS: dict[str, asyncio.Lock] = {}
_TIMERS: dict[str, asyncio.Task] = {}
_UI_TASKS: dict[str, asyncio.Task] = {}
_WATCHDOG: asyncio.Task | None = None
_UI_DEBOUNCE_S = 0.6
_WATCHDOG_S = 12.0
_TIMER_RETRY_S = 5.0
_UI_SYNC_MAX_ATTEMPTS = 20


def _lock(key: str) -> asyncio.Lock:
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def _key(guild_id: int, channel_id: int) -> str:
    return f"{guild_id}:{channel_id}"


def _custom_id(guild_id: int, channel_id: int, fmt: str) -> str:
    return f"rv:{guild_id}:{channel_id}:{fmt}"


def _parse_custom_id(custom_id: str):
    parts = (custom_id or "").split(":")
    if len(parts) != 4 or parts[0] != "rv":
        return None
    try:
        return int(parts[1]), int(parts[2]), parts[3]
    except (TypeError, ValueError):
        return None


def _ruleset_value(fmt_key: str) -> str:
    """Ключ кнопки (brazil/kazakhstan) → значение ruleset."""
    return (fmt_key or "").upper()


def ruleset_label(ruleset: str) -> str:
    """Человекочитаемая метка правил для embed/карточек."""
    key = (ruleset or "").lower()
    fmt = _FORMATS.get(key, {})
    if fmt:
        return f"{fmt.get('flag', '')} {fmt.get('name', ruleset)}".strip()
    return ruleset or "—"


def participant_uids(tournament) -> set[int]:
    """Discord ID всех зарегистрированных участников турнира."""
    out: set[int] = set()
    for uid in (getattr(tournament, "player_user_ids", None) or {}).values():
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        if uid > 0:
            out.add(uid)
    return out


def get_vote(tournament) -> dict | None:
    rv = getattr(tournament, "rules_vote", None)
    return rv if isinstance(rv, dict) and rv else None


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def _vote_state(tournament, channel_id: int) -> dict:
    return {
        "vote_message_id": 0,
        "vote_channel_id": int(channel_id),
        "vote_deadline": time.time() + VOTE_SECONDS,
        "votes": {},
        "selected_ruleset": None,
        "vote_status": "open",
        "created_at": time.time(),
    }


async def start_rules_vote(
    guild: discord.Guild,
    channel,
    tournament,
    bot=None,
) -> bool:
    """Перевести турнир в RULES_VOTING и отправить CV2-карточку.

    Идемпотентно: если голосование уже open — только досылает
    сообщение при необходимости.
    """
    rv = get_vote(tournament)
    if rv is None:
        tournament.rules_vote = _vote_state(
            tournament, getattr(channel, "id", tournament.channel_id)
        )
        rv = tournament.rules_vote
    if rv.get("vote_status") != "open":
        return False
    tournament.phase = TournamentPhase.RULES_VOTING
    store.set(tournament)
    # Таймер ставим до отправки сообщения: даже при сбое API
    # голосование завершится и турнир продолжится.
    _schedule_timer(int(tournament.guild_id), int(tournament.channel_id))

    if not rv.get("vote_message_id"):
        try:
            view = build_rules_vote_view(tournament, rv)
            msg = await channel.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            rv["vote_message_id"] = msg.id
            store.set(tournament)
        except Exception as exc:
            logger.warning("Rules vote message send failed: %s", exc)
            return False
    if bot is not None:
        try:
            from views.rules_vote_view import RulesVoteButtonsView

            bot.add_view(
                RulesVoteButtonsView(
                    int(tournament.guild_id), int(tournament.channel_id)
                )
            )
        except Exception as exc:
            logger.warning("Rules vote view register failed: %s", exc)
    await _mirror_to_pg(tournament, rv)
    return True


# ---------------------------------------------------------------------------
# CV2-представление
# ---------------------------------------------------------------------------

def _counts(rv: dict) -> tuple[int, int]:
    votes = rv.get("votes") or {}
    b = sum(1 for v in votes.values() if v == "brazil")
    k = sum(1 for v in votes.values() if v == "kazakhstan")
    return b, k


def build_rules_vote_view(
    tournament, rv: dict
) -> "discord.ui.LayoutView":
    """CV2-карточка голосования (открытая или финальная)."""
    from utils.design.layouts import DesignView

    gid = int(getattr(tournament, "guild_id", 0) or 0)
    cid = int(getattr(tournament, "channel_id", 0) or 0)
    done = rv.get("vote_status") == "done"
    b, k = _counts(rv)
    total = len(getattr(tournament, "players_pool", None)
                or getattr(tournament, "player_user_ids", {}) or {})

    if done:
        sel = rv.get("selected_ruleset") or DEFAULT_RULESET
        view = DesignView(
            title="r!z3 | ГОЛОСОВАНИЕ ЗАВЕРШЕНО", timeout=None
        )
        view.text(
            f"Brazil — {b} {_plural(b, 'голос', 'голоса', 'голосов')}.\n"
            f"Kazakhstan — {k} {_plural(k, 'голос', 'голоса', 'голосов')}.\n\n"
            f"**Выбранные правила: {sel}**"
        )
    else:
        view = DesignView(
            title="r!z3 | ГОЛОСОВАНИЕ ЗА ПРАВИЛА", timeout=None
        )
        view.text(
            "Выберите правила, по которым пройдёт турнир."
        )
        view.sep()
        voted = len(rv.get("votes") or {})
        view.text(
            f"**BRAZIL** — {b} {_plural(b, 'голос', 'голоса', 'голосов')}\n"
            f"**KAZAKHSTAN** — {k} {_plural(k, 'голос', 'голоса', 'голосов')}\n\n"
            f"**Участников:** {voted}/{total} проголосовали\n"
            f"**Осталось:** <t:{int(rv.get('vote_deadline') or 0)}:R>"
        )
        view.sep()

    row = discord.ui.ActionRow()
    for fmt_key, label in (
        ("brazil", "BRAZIL"),
        ("kazakhstan", "KAZAKHSTAN"),
    ):
        btn = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=_custom_id(gid, cid, fmt_key),
            disabled=done,
        )

        async def _cb(interaction, _fmt=fmt_key):
            from services.rules_vote import handle_rules_vote_click

            await handle_rules_vote_click(interaction, _fmt)

        btn.callback = _cb
        row.add_item(btn)
    view.container.add_item(row)
    return view


# ---------------------------------------------------------------------------
# Голосование
# ---------------------------------------------------------------------------

async def handle_rules_vote_click(
    interaction: discord.Interaction, fmt: str
) -> None:
    """Callback кнопки BRAZIL/KAZAKHSTAN."""
    parsed = _parse_custom_id(
        (interaction.data or {}).get("custom_id", "")
    )
    if parsed is None:
        return
    gid, cid, _ = parsed
    key = _key(gid, cid)
    async with _lock(key):
        tournament = store.get(gid, cid)
        rv = get_vote(tournament) if tournament is not None else None
        if tournament is None or rv is None:
            await _ephemeral(interaction, "❌ Турнир не найден.")
            return
        if rv.get("vote_status") != "open":
            await _ephemeral(
                interaction, "ℹ️ Голосование уже завершено."
            )
            return
        if time.time() > float(rv.get("vote_deadline") or 0):
            expired = True
        else:
            expired = False
            uid = int(getattr(interaction.user, "id", 0) or 0)
            if uid not in participant_uids(tournament):
                await _ephemeral(
                    interaction,
                    "Вы не являетесь участником этого турнира",
                )
                return
            votes = rv.setdefault("votes", {})
            if str(uid) in votes or uid in votes:
                await _ephemeral(
                    interaction,
                    "Вы уже проголосовали за правила",
                )
                return
            votes[str(uid)] = fmt
            store.set(tournament)
            await _mirror_to_pg(tournament, rv)
            logger.info(
                "rules_vote_cast tournament=%s user=%s fmt=%s "
                "votes=%d deadline=%.0f",
                getattr(tournament, "id", "?"), uid, fmt,
                len(votes), float(rv.get("vote_deadline") or 0),
            )

    # Вне lock: ответ и Discord-обновления не держат состояние.
    if expired:
        await _ephemeral(
            interaction, "ℹ️ Голосование уже завершено."
        )
        await finalize_rules_vote(interaction.guild, tournament)
        return

    # Ответ — до долгих PATCH; досрочного завершения нет (ТЗ).
    await _ephemeral(interaction, "✅ Голос учтён.")
    _schedule_vote_ui(key, interaction.guild, tournament)


async def _ephemeral(interaction: discord.Interaction,
                     text: str) -> None:
    try:
        await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        try:
            await interaction.followup.send(text, ephemeral=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Завершение и продолжение турнира
# ---------------------------------------------------------------------------

def _schedule_timer(guild_id: int, channel_id: int) -> None:
    key = _key(guild_id, channel_id)
    task = _TIMERS.get(key)
    if task is not None and not task.done():
        return
    task = asyncio.create_task(_vote_timer(guild_id, channel_id))
    _TIMERS[key] = task


def _cancel_vote_ui(key: str) -> None:
    task = _UI_TASKS.pop(key, None)
    if task is not None and not task.done():
        task.cancel()


def _schedule_vote_ui(
    key: str, guild, tournament
) -> None:
    """Debounced обновление карточки: серия голосов → один PATCH."""
    _cancel_vote_ui(key)

    async def _run() -> None:
        try:
            await asyncio.sleep(_UI_DEBOUNCE_S)
            fresh = store.get(
                int(getattr(tournament, "guild_id", 0) or 0),
                int(getattr(tournament, "channel_id", 0) or 0),
            ) or tournament
            rv = get_vote(fresh)
            if rv is None or rv.get("vote_status") != "open":
                return
            try:
                g = guild
                if g is None:
                    g = _bot_guild(
                        int(getattr(fresh, "guild_id", 0) or 0)
                    )
                vch_id = int(rv.get("vote_channel_id") or 0)
                mid = int(rv.get("vote_message_id") or 0)
                if g is not None and vch_id and mid:
                    vch = g.get_channel(vch_id)
                    if vch is None:
                        vch = await g.fetch_channel(vch_id)
                    msg = await vch.fetch_message(mid)
                    await msg.edit(
                        view=build_rules_vote_view(fresh, rv)
                    )
            except Exception as exc:
                logger.warning(
                    "Rules vote UI debounce edit failed: %s", exc
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Rules vote UI task failed: %s", key)
        finally:
            _UI_TASKS.pop(key, None)

    _UI_TASKS[key] = asyncio.create_task(_run())


async def _vote_timer(guild_id: int, channel_id: int) -> None:
    key = _key(guild_id, channel_id)
    try:
        while True:
            try:
                tournament = store.get(guild_id, channel_id)
                rv = get_vote(tournament) if tournament is not None else None
                if rv is None or rv.get("vote_status") != "open":
                    return
                remain = float(rv.get("vote_deadline") or 0) - time.time()
                if remain <= 0:
                    await finalize_rules_vote(None, tournament)
                    return
                await asyncio.sleep(min(remain + 0.5, 30.0))
            except asyncio.CancelledError:
                raise
            except Exception:
                # Сбой не убивает таймер — повтор через паузу.
                logger.exception(
                    "rules_vote_timer_failed key=%s", key
                )
                await asyncio.sleep(_TIMER_RETRY_S)
    finally:
        _TIMERS.pop(key, None)


async def finalize_rules_vote(
    guild: discord.Guild | None,
    tournament,
) -> bool:
    """Подвести итог голосования и запустить формирование команд.

    Победитель: больше голосов; ничья и «ноль голосов» → KAZAKHSTAN.
    Идемпотентно: повторный вызов при status != open → False.
    """
    gid = int(getattr(tournament, "guild_id", 0) or 0)
    cid = int(getattr(tournament, "channel_id", 0) or 0)
    key = _key(gid, cid)
    async with _lock(key):
        fresh = store.get(gid, cid) or tournament
        rv = get_vote(fresh)
        if rv is None or rv.get("vote_status") != "open":
            return False

        b, k = _counts(rv)
        # ТЗ: равенство (включая 0:0) → KAZAKHSTAN.
        selected = "BRAZIL" if b > k else DEFAULT_RULESET
        rv["selected_ruleset"] = selected
        rv["vote_status"] = "done"
        rv["tie"] = b == k and b > 0  # 0:0 — не «равенство», а дефолт
        rv["closed_at"] = time.time()
        rv["ui_synced"] = False
        rv["ui_sync_attempts"] = 0
        fresh.ruleset = selected
        store.set(fresh)
        await _mirror_to_pg(fresh, rv)
        logger.info(
            "rules_vote_finalized tournament=%s ruleset=%s "
            "brazil=%s kazakhstan=%s deadline=%.0f",
            getattr(fresh, "id", "?"), selected, b, k,
            float(rv.get("vote_deadline") or 0),
        )

    # UI-синхронизация ВНЕ lock — сбой Discord API не откатывает итог.
    _cancel_vote_ui(key)
    await _sync_vote_ui(guild, fresh, rv)

    # Формирование команд — после выхода из лока (может быть долго).
    await _continue_tournament_start(guild, fresh)
    return True


async def _sync_vote_ui(
    guild, tournament, rv: dict
) -> bool:
    """Финальная CV2-карточка + флаг ui_synced для watchdog-повторов.

    ui_synced=True ТОЛЬКО после подтверждённого msg.edit: пропуск
    ветки (нет guild/channel/message_id) — это несинхронизированное
    состояние, а не успех. Удалённое сообщение (404) — даём up:
    attempts выставляются в максимум, watchdog не долбит Discord
    по несуществующему сообщению бесконечно.
    """
    ok = False
    reason = ""
    try:
        if guild is None:
            guild = _bot_guild(
                int(getattr(tournament, "guild_id", 0) or 0)
            )
        vch_id = int(rv.get("vote_channel_id") or 0)
        mid = int(rv.get("vote_message_id") or 0)
        if guild is None:
            reason = "guild unavailable"
        elif not vch_id or not mid:
            reason = "vote message ref missing"
        else:
            vch = guild.get_channel(vch_id)
            if vch is None:
                vch = await guild.fetch_channel(vch_id)
            msg = await vch.fetch_message(mid)
            await msg.edit(
                view=build_rules_vote_view(tournament, rv)
            )
            ok = True
    except discord.NotFound:
        reason = "vote message deleted"
        # Бессмысленно повторять: сообщение удалено навсегда.
        rv["ui_sync_attempts"] = _UI_SYNC_MAX_ATTEMPTS
        logger.warning(
            "rules_vote_ui_sync_gone tournament=%s: message %s "
            "deleted — retries disabled",
            getattr(tournament, "id", "?"), rv.get("vote_message_id"),
        )
    except Exception as exc:
        reason = str(exc)
        logger.warning(
            "rules_vote_ui_sync_failed tournament=%s: %s",
            getattr(tournament, "id", "?"), exc,
        )
    rv["ui_sync_attempts"] = (
        _UI_SYNC_MAX_ATTEMPTS
        if reason == "vote message deleted"
        else int(rv.get("ui_sync_attempts") or 0) + 1
    )
    rv["ui_synced"] = ok
    if reason and not ok:
        rv["ui_sync_last_error"] = reason[:200]
    store.set(tournament)
    if ok:
        logger.info(
            "rules_vote_ui_synced tournament=%s",
            getattr(tournament, "id", "?"),
        )
    return ok


async def _continue_tournament_start(
    guild: discord.Guild | None,
    tournament,
) -> None:
    """Запустить существующее формирование команд после голосования.

    Запуск СТРОГО однократный: атомарный флаг ``formation_started``
    выставляется под _lock до вызова launch — параллельные
    финализация/watchdog/restore не могут запустить формирование
    дважды (двойное распределение игроков, повторные матчи).
    При падении launch до смены фазы флаг сбрасывается — watchdog
    повторит; если фаза уже сменилась — проверка фазы отсекает
    повторы сама.
    """
    gid = int(getattr(tournament, "guild_id", 0) or 0)
    cid = int(getattr(tournament, "channel_id", 0) or 0)
    key = _key(gid, cid)
    async with _lock(key):
        fresh = store.get(gid, cid) or tournament
        if getattr(fresh, "phase", None) != TournamentPhase.RULES_VOTING:
            return
        rv = get_vote(fresh)
        if rv is not None and rv.get("formation_started"):
            return
        if rv is not None:
            rv["formation_started"] = True
        store.set(fresh)
        tournament = fresh
    try:
        if guild is None:
            guild = _bot_guild(int(getattr(tournament, "guild_id", 0) or 0))
        if guild is None:
            # guild временно недоступен — считаем сбоем запуска:
            # флаг сбросится в except, watchdog повторит позже.
            raise RuntimeError(
                f"guild {getattr(tournament, 'guild_id', '?')} "
                "not found"
            )
        channel_id = int(getattr(tournament, "channel_id", 0) or 0)
        channel = guild.get_channel(channel_id)
        if channel is None:
            channel = await guild.fetch_channel(channel_id)
        bot = _bot_instance()
        from views.setup_view import launch_tournament_formation

        logger.info(
            "rules_vote_continue_started tournament=%s",
            getattr(tournament, "id", "?"),
        )
        await launch_tournament_formation(
            guild, channel, tournament, bot
        )
        logger.info(
            "rules_vote_continue_completed tournament=%s",
            getattr(tournament, "id", "?"),
        )
    except Exception:
        # Сбой до смены фазы — сбрасываем formation_started, чтобы
        # watchdog/restore могли повторить запуск. Если launch успел
        # перевести фазу — проверка фазы в начале функции отсекает
        # повторы, флаг безвреден.
        try:
            async with _lock(key):
                f2 = store.get(gid, cid)
                rv2 = get_vote(f2) if f2 is not None else None
                if (
                    rv2 is not None
                    and getattr(f2, "phase", None)
                    == TournamentPhase.RULES_VOTING
                ):
                    rv2["formation_started"] = False
                    store.set(f2)
        except Exception:
            logger.exception(
                "rules_vote_continue_reset_failed tournament=%s",
                getattr(tournament, "id", "?"),
            )
        logger.exception(
            "rules_vote_continue_failed tournament=%s",
            getattr(tournament, "id", "?"),
        )


def _bot_instance():
    try:
        from models.tournament import get_bot_instance

        return get_bot_instance()
    except Exception:
        return None


def _bot_guild(guild_id: int):
    bot = _bot_instance()
    return bot.get_guild(guild_id) if bot else None


# ---------------------------------------------------------------------------
# Отмена / восстановление / PostgreSQL
# ---------------------------------------------------------------------------

def cancel_rules_vote(tournament) -> None:
    """Отмена голосования (удаление турнира и т.п.)."""
    rv = get_vote(tournament)
    if rv is not None and rv.get("vote_status") == "open":
        rv["vote_status"] = "cancelled"
        rv["closed_at"] = time.time()
        store.set(tournament)
    _TIMERS.pop(
        _key(
            int(getattr(tournament, "guild_id", 0) or 0),
            int(getattr(tournament, "channel_id", 0) or 0),
        ),
        None,
    )


async def restore_rules_votes(bot) -> None:
    """setup_hook: persistent views + таймеры/финализация.

    Для каждого турнира с rules_vote регистрирует кнопки; open с
    истёкшим deadline → финализация (и продолжение турнира), open
    без истечения → таймер. done, но phase всё ещё RULES_VOTING
    (падение между итогом и формированием) → продолжение запуска.
    """
    from views.rules_vote_view import RulesVoteButtonsView

    for tournament in store.all():
        rv = get_vote(tournament)
        if rv is None:
            # Фаза RULES_VOTING без состояния — прерываем зависший
            # запуск: возвращаем в SETUP для повторного «Старт».
            if getattr(tournament, "phase", None) == (
                TournamentPhase.RULES_VOTING
            ):
                tournament.phase = TournamentPhase.SETUP
                store.set(tournament)
            continue
        gid = int(getattr(tournament, "guild_id", 0) or 0)
        cid = int(getattr(tournament, "channel_id", 0) or 0)
        try:
            bot.add_view(RulesVoteButtonsView(gid, cid))
        except Exception as exc:
            logger.warning(
                "Rules vote view restore failed %s: %s",
                getattr(tournament, "id", "?"), exc,
            )
        status = rv.get("vote_status")
        if status == "open":
            if time.time() > float(rv.get("vote_deadline") or 0):
                await finalize_rules_vote(bot.get_guild(gid), tournament)
            else:
                _schedule_timer(gid, cid)
        elif status == "done" and getattr(
            tournament, "phase", None
        ) == TournamentPhase.RULES_VOTING:
            # Итог зафиксирован, но формирование не стартовало.
            await _continue_tournament_start(bot.get_guild(gid), tournament)
        elif (
            status == "done"
            and not rv.get("ui_synced")
            and int(rv.get("ui_sync_attempts") or 0)
            < _UI_SYNC_MAX_ATTEMPTS
        ):
            await _sync_vote_ui(bot.get_guild(gid), tournament, rv)
    ensure_rules_vote_watchdog()


# ---------------------------------------------------------------------------
# Watchdog: гарантия завершения при потере таймера/сбоях API
# ---------------------------------------------------------------------------

def ensure_rules_vote_watchdog() -> None:
    """Идемпотентный запуск периодической проверки голосований."""
    global _WATCHDOG
    if _WATCHDOG is not None and not _WATCHDOG.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _WATCHDOG = loop.create_task(_watchdog_loop())


async def _watchdog_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_WATCHDOG_S)
            now = time.time()
            recovered = 0
            for tournament in store.all():
                rv = get_vote(tournament)
                if rv is None:
                    continue
                status = rv.get("vote_status")
                gid = int(getattr(tournament, "guild_id", 0) or 0)
                try:
                    if status == "open" and now > float(
                        rv.get("vote_deadline") or 0
                    ):
                        logger.warning(
                            "rules_vote_watchdog_recovered "
                            "tournament=%s",
                            getattr(tournament, "id", "?"),
                        )
                        recovered += 1
                        await finalize_rules_vote(
                            _bot_guild(gid), tournament
                        )
                    elif status == "done" and getattr(
                        tournament, "phase", None
                    ) == TournamentPhase.RULES_VOTING:
                        # Итог есть, формирование не выполнено.
                        await _continue_tournament_start(
                            _bot_guild(gid), tournament
                        )
                    elif (
                        status == "done"
                        and not rv.get("ui_synced")
                        and int(rv.get("ui_sync_attempts") or 0)
                        < _UI_SYNC_MAX_ATTEMPTS
                    ):
                        await _sync_vote_ui(
                            _bot_guild(gid), tournament, rv
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "rules_vote_watchdog_item_failed "
                        "tournament=%s", getattr(tournament, "id", "?"),
                    )
            if recovered:
                from utils import perf_metrics

                perf_metrics.incr("rules_votes_watchdog_recovered",
                                  recovered)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("rules_vote_watchdog_failed")


async def _mirror_to_pg(tournament, rv: dict) -> None:
    """Best-effort запись в PostgreSQL (не блокирует флоу)."""
    try:
        from storage import rules_vote_store

        await rules_vote_store.save_vote(
            int(getattr(tournament, "guild_id", 0) or 0),
            int(getattr(tournament, "channel_id", 0) or 0),
            {
                "tournament_id": getattr(tournament, "id", "") or "",
                "vote_status": rv.get("vote_status") or "open",
                "vote_deadline": float(rv.get("vote_deadline") or 0),
                "vote_message_id": int(rv.get("vote_message_id") or 0),
                "vote_channel_id": int(rv.get("vote_channel_id") or 0),
                "votes": dict(rv.get("votes") or {}),
                "selected_ruleset": rv.get("selected_ruleset"),
                "ruleset": getattr(tournament, "ruleset", "") or "",
                "created_at": float(rv.get("created_at") or 0),
                "closed_at": float(rv.get("closed_at") or 0),
            },
        )
    except Exception as exc:
        logger.debug("rules vote PG mirror failed: %s", exc)
