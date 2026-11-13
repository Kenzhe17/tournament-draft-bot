"""Голосование за формат игры (🇧🇷 Brazil / 🇰🇿 Kazakhstan) для
Ranked MM-матчей CS 4×4 (8 участников).

Состояние живёт в ``tournament.match_channels[channel_id]
["format_vote"]`` — персистится вместе с турниром (json_store),
итог дополнительно зеркалируется в PostgreSQL
(storage/format_vote_store). Кнопки — persistent custom_id,
CV2-интерфейс целиком внутри одного Container (DesignView).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from typing import Any

import discord

from storage.json_store import store

logger = logging.getLogger(__name__)

VOTE_SECONDS = 20
VOTE_PLAYERS = 8

_FORMATS: dict[str, dict[str, str]] = {
    "brazil": {
        "flag": "🇧🇷",
        "name": "Brazil",
        "rules": (
            "Раунды: 13\n"
            "Монеты: 1-й — 1500, остальные — 9999\n"
            "Оружие: 1-й — Deagle/UZI, остальные — XM8/UMP\n"
            "Броня: Жилет 2, шлем 2\n"
            "Запрещено: Крыша и второй этаж"
        ),
    },
    "kazakhstan": {
        "flag": "🇰🇿",
        "name": "Kazakhstan",
        "rules": (
            "Раунды: 13\n"
            "Монеты: Все раунды — 9999\n"
            "Оружие: Все раунды — Deagle/UMP\n"
            "Броня: Жилет 2, шлем 2\n"
            "Крыша: Разрешена\n"
            "Второй этаж: Разрешён"
        ),
    },
}

# per-match asyncio.Lock — защита от двойного голосования при
# одновременных нажатиях и от гонки finalize/клик.
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


def _tid_token(tournament_id) -> str:
    """Короткий стабильный токен tournament.id для custom_id.

    Discord custom_id ≤ 100 символов: ``{gid}_{creation_time}``
    (~37 симв.) в полном custom_id может не влезть. Токен
    идемпотентен (повторное применение возвращает то же значение),
    поэтому безопасен и для сырых id, и для уже токенизированных.
    """
    s = str(tournament_id or "?")
    if len(s) <= 20 and ":" not in s:
        return s
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _key(guild_id: int, channel_id: int, tournament_id,
         match_type: str, match_index: int) -> str:
    """Уникальный ключ голосования.

    tournament_id обязателен: каналы MM_MATCH_CHANNEL_POOL
    переиспользуются — gid:cid:mt:mi совпадает у разных матчей
    (final:0 в одном канале), что ломало lock/timer/UI задачи.
    """
    return (
        f"{guild_id}:{channel_id}:{_tid_token(tournament_id)}:"
        f"{match_type}:{match_index}"
    )


def _custom_id(guild_id: int, channel_id: int, tournament_id,
               match_type: str, match_index: int, fmt: str) -> str:
    return (
        f"fv:{guild_id}:{channel_id}:{_tid_token(tournament_id)}:"
        f"{match_type}:{match_index}:{fmt}"
    )


def _parse_custom_id(custom_id: str):
    """fv:{gid}:{cid}:{tid}:{mt}:{mi}:{fmt} → (gid,cid,tid,mt,mi,fmt)."""
    parts = (custom_id or "").split(":")
    if len(parts) != 7 or parts[0] != "fv":
        return None
    try:
        return (
            int(parts[1]), int(parts[2]), parts[3], parts[4],
            int(parts[5]), parts[6],
        )
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Состояние
# ---------------------------------------------------------------------------

def _match_meta(tournament, match_type: str, match_index: int):
    """Запись match_channels для матча (по match_type/index)."""
    for meta in (tournament.match_channels or {}).values():
        if (
            isinstance(meta, dict)
            and meta.get("match_type") == match_type
            and int(meta.get("match_index", -1)) == int(match_index)
        ):
            return meta
    return None


def get_vote_state(tournament, match_type: str,
                   match_index: int) -> dict | None:
    meta = _match_meta(tournament, match_type, match_index)
    if not meta:
        return None
    fv = meta.get("format_vote")
    return fv if isinstance(fv, dict) else None


def match_format_label(tournament, match_type: str,
                       match_index: int) -> str:
    """Строка «Формат:» для карточки матча.

    done → флаг победившего формата; open → «Голосование»;
    иначе — пустая строка (строка не добавляется).
    """
    fv = get_vote_state(tournament, match_type, match_index)
    if not fv:
        return ""
    if fv.get("vote_status") == "done" and fv.get("selected_format"):
        f = _FORMATS.get(fv["selected_format"], {})
        return f"{f.get('flag', '')} {f.get('name', fv['selected_format'])}"
    if fv.get("vote_status") == "open":
        return "Голосование"
    return ""


def _player_uids(tournament) -> set[int]:
    """Discord ID всех 8 участников матча (обе команды)."""
    uid_map = getattr(tournament, "player_user_ids", None) or {}
    out: set[int] = set()
    for team in getattr(tournament, "teams", None) or []:
        for name in (team or {}).get("players") or []:
            uid = uid_map.get(name, 0)
            if uid:
                out.add(int(uid))
    return out


def _is_8v8(tournament) -> bool:
    teams = getattr(tournament, "teams", None) or []
    return (
        len(teams) == 2
        and all(len((t or {}).get("players") or []) == 4 for t in teams)
    )


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def prepare_format_vote(
    tournament,
    match_type: str,
    match_index: int,
    channel_id: int,
) -> dict | None:
    """Создать состояние голосования ДО отправки карточки матча —
    чтобы строка «Формат: Голосование» попала в основной embed.
    Идемпотентно. None — матч не 8v8 или голосование уже есть.
    """
    if not _is_8v8(tournament):
        return None
    fv = get_vote_state(tournament, match_type, match_index)
    if fv is not None:
        return fv
    meta = _match_meta(tournament, match_type, match_index)
    if meta is None:
        return None
    fv = {
        "match_id": (
            f"{getattr(tournament, 'id', '?')}_"
            f"{match_type}_{match_index}"
        ),
        "vote_message_id": 0,
        "vote_channel_id": int(channel_id),
        "vote_deadline": time.time() + VOTE_SECONDS,
        "votes": {},
        "selected_format": None,
        "vote_status": "open",
        "created_at": time.time(),
    }
    meta["format_vote"] = fv
    store.set(tournament)
    return fv


async def start_format_vote_message(
    guild: discord.Guild,
    tournament,
    match_type: str,
    match_index: int,
    channel,
    bot=None,
) -> bool:
    """Отправить CV2-сообщение голосования после карточки матча."""
    fv = get_vote_state(tournament, match_type, match_index)
    if fv is None or fv.get("vote_status") != "open":
        return False
    if fv.get("vote_message_id"):
        return True  # уже отправлено (retry/re-announce)
    try:
        view = build_vote_view(tournament, match_type, match_index, fv)
        msg = await channel.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        fv["vote_message_id"] = msg.id
        store.set(tournament)
    except Exception as exc:
        logger.warning("Format vote message send failed: %s", exc)
        return False
    if bot is not None:
        try:
            from views.format_vote_view import FormatVoteButtonsView

            bot.add_view(
                FormatVoteButtonsView(
                    tournament.guild_id, tournament.channel_id,
                    getattr(tournament, "id", "?"),
                    match_type, match_index,
                )
            )
        except Exception as exc:
            logger.warning("Format vote view register failed: %s", exc)
    _schedule_timer(
        tournament.guild_id, tournament.channel_id,
        getattr(tournament, "id", "?"),
        match_type, match_index,
    )
    await _mirror_to_pg(tournament, match_type, match_index, fv)
    return True


# ---------------------------------------------------------------------------
# CV2-представление
# ---------------------------------------------------------------------------

def _votes_text(fv: dict) -> tuple[str, str]:
    votes = fv.get("votes") or {}
    b = sum(1 for v in votes.values() if v == "brazil")
    k = sum(1 for v in votes.values() if v == "kazakhstan")

    def _plural(n: int) -> str:
        if n % 10 == 1 and n % 100 != 11:
            return "голос"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "голоса"
        return "голосов"

    return (
        f"🇧🇷 Brazil — {b} {_plural(b)}",
        f"🇰🇿 Kazakhstan — {k} {_plural(k)}",
    )


def build_vote_view(
    tournament,
    match_type: str,
    match_index: int,
    fv: dict,
) -> "discord.ui.LayoutView":
    """CV2-карточка голосования (открытая или финальная)."""
    from utils.design.layouts import DesignView

    gid = int(getattr(tournament, "guild_id", 0) or 0)
    cid = int(getattr(tournament, "channel_id", 0) or 0)
    done = fv.get("vote_status") == "done"

    if done:
        fmt = _FORMATS.get(fv.get("selected_format") or "", {})
        flag_name = (
            f"{fmt.get('flag', '')} {fmt.get('name', '?')}"
            if fmt else "не определён"
        )
        view = DesignView(title="ФОРМАТ МАТЧА ОПРЕДЕЛЁН", timeout=None)
        view.text(flag_name)
        b_line, k_line = _votes_text(fv)
        view.text(
            "**РЕЗУЛЬТАТЫ ГОЛОСОВАНИЯ**\n"
            f"{b_line}\n{k_line}\n"
            "**ПРАВИЛА ФОРМАТА**\n"
            f"{fmt.get('rules', '')}\n"
            "**Статус:** Голосование завершено"
            + (
                "\n**Причина выбора:** Равенство голосов."
                if fv.get("tie") else ""
            )
        )
    else:
        view = DesignView(title="ВЫБОР ФОРМАТА ИГРЫ", timeout=None)
        view.text("Выберите формат предстоящего матча.")
        view.sep()
        b_line, k_line = _votes_text(fv)
        voted = len(fv.get("votes") or {})
        view.text(
            f"{b_line}\n{k_line}\n"
            f"**Проголосовало:** {voted}/{VOTE_PLAYERS}\n"
            f"**Завершение:** <t:{int(fv.get('vote_deadline') or 0)}:R>"
        )
        view.sep()

    row = discord.ui.ActionRow()
    for fmt_key, label in (
        ("brazil", "🇧🇷 Brazil"),
        ("kazakhstan", "🇰🇿 Kazakhstan"),
    ):
        btn = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=_custom_id(
                gid, cid,
                getattr(tournament, "id", "?"),
                match_type, match_index, fmt_key
            ),
            disabled=done,
        )

        async def _cb(interaction, _fmt=fmt_key):
            from services.format_vote import handle_vote_click

            await handle_vote_click(interaction, _fmt)

        btn.callback = _cb
        row.add_item(btn)
    view.container.add_item(row)
    return view


# ---------------------------------------------------------------------------
# Голосование
# ---------------------------------------------------------------------------

async def handle_vote_click(
    interaction: discord.Interaction, fmt: str
) -> None:
    """Callback кнопки формата (и из живого DesignView, и из
    persistent view после рестарта)."""
    parsed = _parse_custom_id(
        (interaction.data or {}).get("custom_id", "")
    )
    if parsed is None:
        return
    gid, cid, tid, mt, mi, _ = parsed
    key = _key(gid, cid, tid, mt, mi)
    async with _lock(key):
        tournament = store.get(gid, cid)
        # Канал мог быть переиспользован пулом MM: кнопка обязана
        # принадлежать текущему турниру (match_id), иначе старое
        # сообщение влияет на новый матч.
        if (
            tournament is not None
            and _tid_token(getattr(tournament, "id", "")) != tid
        ):
            tournament = None
        fv = (
            get_vote_state(tournament, mt, mi)
            if tournament is not None else None
        )
        if tournament is None or fv is None:
            await _ephemeral(
                interaction, "❌ Матч не найден."
            )
            return
        if fv.get("vote_status") != "open":
            await _ephemeral(
                interaction, "ℹ️ Голосование уже завершено."
            )
            return
        uid = int(getattr(interaction.user, "id", 0) or 0)
        if uid not in _player_uids(tournament):
            await _ephemeral(
                interaction,
                "Вы не являетесь участником этого матча",
            )
            return
        votes = fv.setdefault("votes", {})
        if str(uid) in votes or uid in votes:
            await _ephemeral(
                interaction,
                "Вы уже проголосовали за этот формат",
            )
            return
        expired = time.time() > float(fv.get("vote_deadline") or 0)
        if expired:
            # После deadline новые голоса запрещены (ТЗ).
            need_finalize = True
        else:
            votes[str(uid)] = fmt
            store.set(tournament)
            await _mirror_to_pg(tournament, mt, mi, fv)
            need_finalize = len(votes) >= VOTE_PLAYERS
        logger.info(
            "format_vote_cast match=%s user=%s fmt=%s votes=%d "
            "deadline=%.0f expired=%s",
            fv.get("match_id"), uid, fmt, len(votes),
            float(fv.get("vote_deadline") or 0), expired,
        )

    # Всё ниже — ВНЕ lock: Discord-ответ не ждёт PATCH и наоборот.
    if expired:
        await _ephemeral(
            interaction, "ℹ️ Голосование уже завершено."
        )
        await finalize_format_vote(
            interaction.guild, tournament, mt, mi
        )
        return

    # Ответ пользователю — до любых долгих Discord-операций.
    await _ephemeral(interaction, "✅ Голос учтён.")

    # Карточка голосования — debounce/coalescing: серия кликов
    # даёт один PATCH со свежим состоянием.
    _schedule_vote_ui(key, interaction.guild, tournament, mt, mi)

    if need_finalize:
        # Отменяем отложенный UI — финальная карточка важнее.
        _cancel_vote_ui(key)
        await finalize_format_vote(
            interaction.guild, tournament, mt, mi
        )


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
# Завершение
# ---------------------------------------------------------------------------

def _schedule_timer(guild_id: int, channel_id: int,
                    tournament_id, match_type: str,
                    match_index: int) -> None:
    key = _key(guild_id, channel_id, tournament_id,
               match_type, match_index)
    task = _TIMERS.get(key)
    if task is not None and not task.done():
        return
    task = asyncio.create_task(
        _vote_timer(guild_id, channel_id, tournament_id,
                    match_type, match_index)
    )
    _TIMERS[key] = task


def _cancel_vote_ui(key: str) -> None:
    task = _UI_TASKS.pop(key, None)
    if task is not None and not task.done():
        task.cancel()


def _schedule_vote_ui(
    key: str,
    guild,
    tournament,
    match_type: str,
    match_index: int,
) -> None:
    """Debounced обновление карточки голосования.

    Серия голосов → один PATCH с последним состоянием. Задача
    перечитывает свежее состояние из store перед edit.
    """
    _cancel_vote_ui(key)

    async def _run() -> None:
        try:
            await asyncio.sleep(_UI_DEBOUNCE_S)
            fresh = store.get(
                int(getattr(tournament, "guild_id", 0) or 0),
                int(getattr(tournament, "channel_id", 0) or 0),
            ) or tournament
            # Канал мог быть переиспользован: чужой турнир —
            # это UI-задача старого матча, новое состояние не трогаем.
            if (
                getattr(fresh, "id", None) is not None
                and _tid_token(getattr(fresh, "id", ""))
                != _tid_token(getattr(tournament, "id", ""))
            ):
                return
            fv = get_vote_state(fresh, match_type, match_index)
            if fv is None or fv.get("vote_status") != "open":
                return
            try:
                g = guild
                if g is None:
                    gid = int(getattr(fresh, "guild_id", 0) or 0)
                    g = store_bot_guild(gid)
                vch_id = int(fv.get("vote_channel_id") or 0)
                mid = int(fv.get("vote_message_id") or 0)
                if g is not None and vch_id and mid:
                    vch = g.get_channel(vch_id)
                    if vch is None:
                        vch = await g.fetch_channel(vch_id)
                    msg = await vch.fetch_message(mid)
                    await msg.edit(
                        view=build_vote_view(
                            fresh, match_type, match_index, fv
                        )
                    )
            except Exception as exc:
                logger.warning(
                    "Format vote UI debounce edit failed: %s", exc
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Format vote UI task failed: %s", key)
        finally:
            _UI_TASKS.pop(key, None)

    _UI_TASKS[key] = asyncio.create_task(_run())


async def _vote_timer(guild_id: int, channel_id: int,
                      tournament_id, match_type: str,
                      match_index: int) -> None:
    key = _key(guild_id, channel_id, tournament_id,
               match_type, match_index)
    try:
        while True:
            try:
                tournament = store.get(guild_id, channel_id)
                # Пул MM переиспользует каналы: чужой турнир в
                # том же (gid,cid) не должен ни финализироваться
                # этим таймером, ни блокировать свой.
                if (
                    tournament is not None
                    and str(getattr(tournament, "id", ""))
                    != str(tournament_id)
                ):
                    return
                fv = (
                    get_vote_state(tournament, match_type, match_index)
                    if tournament is not None else None
                )
                if fv is None or fv.get("vote_status") != "open":
                    return
                remain = float(fv.get("vote_deadline") or 0) - time.time()
                if remain <= 0:
                    await finalize_format_vote(
                        None, tournament, match_type, match_index
                    )
                    return
                await asyncio.sleep(min(remain + 0.5, 30.0))
            except asyncio.CancelledError:
                raise
            except Exception:
                # Сбой не убивает таймер: watchdog подстрахует,
                # но и здесь повторяем через короткую паузу.
                logger.exception(
                    "format_vote_timer_failed key=%s", key
                )
                await asyncio.sleep(_TIMER_RETRY_S)
    finally:
        _TIMERS.pop(key, None)


async def finalize_format_vote(
    guild: discord.Guild | None,
    tournament,
    match_type: str,
    match_index: int,
) -> bool:
    """Подвести итог: победитель по голосам, ничья/пусто — случайно.
    Обновляет CV2-сообщение и строку «Формат» в карточке матча."""
    tid = getattr(tournament, "id", "?")
    key = _key(
        int(getattr(tournament, "guild_id", 0) or 0),
        int(getattr(tournament, "channel_id", 0) or 0),
        tid, match_type, match_index,
    )
    async with _lock(key):
        fresh = store.get(
            int(getattr(tournament, "guild_id", 0) or 0),
            int(getattr(tournament, "channel_id", 0) or 0),
        )
        # Переиспользованный канал: store может вернуть ЧУЖОЙ
        # турнир — финализируем только свой match_id.
        if fresh is not None and str(
            getattr(fresh, "id", "")
        ) != str(tid):
            fresh = None
        fresh = fresh or tournament
        fv = get_vote_state(fresh, match_type, match_index)
        if fv is None or fv.get("vote_status") != "open":
            return False

        votes = fv.get("votes") or {}
        counts = {f: 0 for f in _FORMATS}
        for v in votes.values():
            if v in counts:
                counts[v] += 1
        top = max(counts.values())
        winners = [f for f, c in counts.items() if c == top]
        if len(winners) == 1:
            selected = winners[0]
            tie = False
        else:
            selected = random.choice(winners)
            tie = top > 0  # ничья с голосами; 0 голосов — без причины
        fv["selected_format"] = selected
        fv["vote_status"] = "done"
        fv["tie"] = tie
        fv["closed_at"] = time.time()
        fv["ui_synced"] = False
        fv["ui_sync_attempts"] = 0
        store.set(fresh)
        await _mirror_to_pg(fresh, match_type, match_index, fv)
        logger.info(
            "format_vote_finalized match=%s format=%s votes=%s "
            "deadline=%.0f",
            fv.get("match_id"), selected, counts,
            float(fv.get("vote_deadline") or 0),
        )

    # UI-синхронизация ВНЕ lock — Discord PATCH не держит состояние.
    # Сбой API не откатывает зафиксированный итог; watchdog повторит.
    _cancel_vote_ui(key)
    await _sync_vote_ui(guild, fresh, match_type, match_index, fv)
    return True


async def _sync_vote_ui(
    guild,
    tournament,
    match_type: str,
    match_index: int,
    fv: dict,
) -> bool:
    """Discord-синхронизация финальной карточки + карточки матча.

    Идемпотентно и безопасно для повторов (watchdog). ui_synced=True
    ТОЛЬКО когда реально отредактированы И карточка голосования, И
    карточка матча: пропуск ветки (нет guild/channel/message_id) —
    несинхронизированное состояние, а не успех. Удалённое сообщение
    (404) — attempts в максимум: watchdog не долбит Discord вечно.
    """
    ok = True
    reason = ""
    try:
        if guild is None:
            gid = int(getattr(tournament, "guild_id", 0) or 0)
            guild = store_bot_guild(gid)
        vch_id = int(fv.get("vote_channel_id") or 0)
        mid = int(fv.get("vote_message_id") or 0)
        if guild is None:
            ok = False
            reason = "guild unavailable"
        elif not vch_id or not mid:
            ok = False
            reason = "vote message ref missing"
        else:
            vch = guild.get_channel(vch_id)
            if vch is None:
                vch = await guild.fetch_channel(vch_id)
            msg = await vch.fetch_message(mid)
            await msg.edit(
                view=build_vote_view(tournament, match_type,
                                     match_index, fv)
            )
    except discord.NotFound:
        ok = False
        reason = "vote message deleted"
        fv["ui_sync_attempts"] = _UI_SYNC_MAX_ATTEMPTS
        logger.warning(
            "format_vote_ui_sync_gone match=%s: message %s deleted "
            "— retries disabled",
            fv.get("match_id"), fv.get("vote_message_id"),
        )
    except Exception as exc:
        ok = False
        reason = str(exc)
        logger.warning(
            "format_vote_ui_sync_failed match=%s: %s",
            fv.get("match_id"), exc,
        )

    # Строка «Формат» в карточке матча.
    try:
        from services.match_announcer import update_match_card

        meta = _match_meta(tournament, match_type, match_index) or {}
        await update_match_card(
            guild, tournament, match_type, match_index,
            state=meta.get("card_state") or "active",
        )
    except Exception as exc:
        ok = False
        if not reason:
            reason = str(exc)
        logger.warning(
            "format_vote_card_update_failed match=%s: %s",
            fv.get("match_id"), exc,
        )

    fv["ui_sync_attempts"] = (
        _UI_SYNC_MAX_ATTEMPTS
        if reason == "vote message deleted"
        else int(fv.get("ui_sync_attempts") or 0) + 1
    )
    fv["ui_synced"] = ok
    if reason and not ok:
        fv["ui_sync_last_error"] = reason[:200]
    store.set(tournament)
    if ok:
        logger.info(
            "format_vote_ui_synced match=%s", fv.get("match_id")
        )
    return ok


def store_bot_guild(guild_id: int):
    """guild через глобальный bot instance (для таймера без guild)."""
    try:
        from models.tournament import get_bot_instance

        bot = get_bot_instance()
        return bot.get_guild(guild_id) if bot else None
    except Exception:
        return None


def cancel_open_format_votes(tournament) -> None:
    """Отмена голосований при отмене матча: статус cancelled,
    кнопки отключаются при следующем рендере/клике."""
    for ch_id, meta in (tournament.match_channels or {}).items():
        fv = (meta or {}).get("format_vote")
        if isinstance(fv, dict) and fv.get("vote_status") == "open":
            fv["vote_status"] = "cancelled"
            fv["closed_at"] = time.time()
            _TIMERS.pop(
                _key(
                    int(getattr(tournament, "guild_id", 0) or 0),
                    int(ch_id),
                    getattr(tournament, "id", "?"),
                    meta.get("match_type", ""),
                    int(meta.get("match_index", 0) or 0),
                ),
                None,
            )
            _UI_TASKS.pop(
                _key(
                    int(getattr(tournament, "guild_id", 0) or 0),
                    int(ch_id),
                    getattr(tournament, "id", "?"),
                    meta.get("match_type", ""),
                    int(meta.get("match_index", 0) or 0),
                ),
                None,
            )
    store.set(tournament)


# ---------------------------------------------------------------------------
# Restart recovery + PostgreSQL
# ---------------------------------------------------------------------------

async def restore_format_votes(bot) -> None:
    """setup_hook: persistent views + таймеры для активных голосований.

    Регистрирует FormatVoteButtonsView для каждого матча с
    format_vote (open/done — done тоже, чтобы устаревший клик
    получил корректный ответ), а для open с истёкшим deadline —
    немедленную финализацию, иначе — таймер.
    """
    from views.format_vote_view import FormatVoteButtonsView

    for tournament in store.all():
        if not getattr(tournament, "mm_search_channel_id", 0):
            continue
        gid = int(getattr(tournament, "guild_id", 0) or 0)
        cid = int(getattr(tournament, "channel_id", 0) or 0)
        for meta in (tournament.match_channels or {}).values():
            fv = (meta or {}).get("format_vote")
            if not isinstance(fv, dict):
                continue
            mt, mi = meta.get("match_type"), int(
                meta.get("match_index", 0) or 0
            )
            if not mt:
                continue
            tid = getattr(tournament, "id", "?")
            try:
                bot.add_view(
                    FormatVoteButtonsView(gid, cid, tid, mt, mi)
                )
            except Exception as exc:
                logger.warning(
                    "Format vote view restore failed %s: %s",
                    fv.get("match_id"), exc,
                )
            if fv.get("vote_status") != "open":
                continue
            if time.time() > float(fv.get("vote_deadline") or 0):
                guild = bot.get_guild(gid)
                await finalize_format_vote(guild, tournament, mt, mi)
            else:
                _schedule_timer(gid, cid, tid, mt, mi)
    ensure_format_vote_watchdog()


# ---------------------------------------------------------------------------
# Watchdog: гарантия завершения даже при потере таймера/ошибках API
# ---------------------------------------------------------------------------

def ensure_format_vote_watchdog() -> None:
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
    """Каждые ~12с: open+просрочен → финализация; done без
    ui_synced → повтор Discord-синхронизации (ограниченно)."""
    while True:
        try:
            await asyncio.sleep(_WATCHDOG_S)
            now = time.time()
            recovered = 0
            for tournament in store.all():
                if not getattr(tournament, "mm_search_channel_id", 0):
                    continue
                gid = int(getattr(tournament, "guild_id", 0) or 0)
                cid = int(getattr(tournament, "channel_id", 0) or 0)
                for meta in (tournament.match_channels or {}).values():
                    fv = (meta or {}).get("format_vote")
                    if not isinstance(fv, dict):
                        continue
                    status = fv.get("vote_status")
                    mt, mi = meta.get("match_type"), int(
                        meta.get("match_index", 0) or 0
                    )
                    if not mt:
                        continue
                    try:
                        if status == "open" and now > float(
                            fv.get("vote_deadline") or 0
                        ):
                            logger.warning(
                                "format_vote_watchdog_recovered "
                                "match=%s", fv.get("match_id"),
                            )
                            recovered += 1
                            await finalize_format_vote(
                                None, tournament, mt, mi
                            )
                        elif (
                            status == "done"
                            and not fv.get("ui_synced")
                            and int(fv.get("ui_sync_attempts") or 0)
                            < _UI_SYNC_MAX_ATTEMPTS
                        ):
                            await _sync_vote_ui(
                                None, tournament, mt, mi, fv
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "format_vote_watchdog_item_failed "
                            "match=%s", fv.get("match_id"),
                        )
            if recovered:
                from utils import perf_metrics

                perf_metrics.incr("format_votes_watchdog_recovered",
                                  recovered)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("format_vote_watchdog_failed")


async def _mirror_to_pg(tournament, match_type: str,
                        match_index: int, fv: dict) -> None:
    """Best-effort запись итога в PostgreSQL (не блокирует флоу)."""
    try:
        from storage import format_vote_store

        await format_vote_store.save_vote(
            int(getattr(tournament, "guild_id", 0) or 0),
            int(getattr(tournament, "channel_id", 0) or 0),
            {
                "match_id": fv.get("match_id") or "",
                "match_type": match_type,
                "match_index": int(match_index),
                "vote_message_id": int(fv.get("vote_message_id") or 0),
                "vote_channel_id": int(
                    fv.get("vote_channel_id") or 0
                ),
                "vote_deadline": float(
                    fv.get("vote_deadline") or 0
                ),
                "votes": dict(fv.get("votes") or {}),
                "selected_format": fv.get("selected_format"),
                "vote_status": fv.get("vote_status") or "open",
                "tie": bool(fv.get("tie")),
            },
        )
    except Exception as exc:
        logger.debug("format vote PG mirror failed: %s", exc)
