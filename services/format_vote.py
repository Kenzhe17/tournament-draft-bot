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
import logging
import random
import secrets
import time
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

# per-vote asyncio.Lock — защита от двойного голосования при
# одновременных нажатиях и от гонки finalize/клик. Ключ — vote_id
# конкретного голосования: локи/таймеры разных матчей в одном
# канале пула не пересекаются.
_LOCKS: dict[str, asyncio.Lock] = {}
_TIMERS: dict[str, asyncio.Task] = {}

# Сериализованный UI-worker на голосование: все правки сообщения
# голосования проходят через одну задачу, которая при flush
# перечитывает АКТУАЛЬНОЕ состояние — запоздавший PATCH открытого
# вида не может перезаписать финальный (done/cancelled).
_UI_DIRTY: dict[str, int] = {}
_UI_TASKS: dict[str, asyncio.Task] = {}
_UI_CTX: dict[str, tuple] = {}  # key -> (gid, cid, vote_id)
_UI_FAILS: dict[str, int] = {}  # key -> подряд неудачных flush'ей
_UI_GUILD_SINCE: dict[str, float] = {}  # key -> первый guild-unavailable


def _new_vote_id() -> str:
    """Уникальный ID поколения голосования (на матч — свой)."""
    return f"v{secrets.token_hex(4)}"


def _lock(key: str) -> asyncio.Lock:
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def _legacy_key(guild_id: int, channel_id: int, match_type: str,
                match_index: int) -> str:
    """Ключ для голосований, сохранённых до введения vote_id."""
    return f"{guild_id}:{channel_id}:{match_type}:{match_index}"


def _vote_key(fv: dict | None, guild_id: int, channel_id: int,
              match_type: str, match_index: int) -> str:
    """Ключ lock/timer: vote_id нового формата, legacy-ключ для старых."""
    vid = (fv or {}).get("vote_id")
    if vid:
        return f"vote:{vid}"
    return _legacy_key(guild_id, channel_id, match_type, match_index)


def _custom_id(guild_id: int, channel_id: int, vote_id: str,
               fmt: str) -> str:
    return f"fv:{guild_id}:{channel_id}:{vote_id}:{fmt}"


def _parse_custom_id(custom_id: str):
    """Разбор custom_id ``fv:{gid}:{cid}:{vote_id}:{fmt}``.

    Возвращает (gid, cid, vote_id, fmt) или None для чужих/старых
    (до-vote_id, 6 частей) идентификаторов — устаревшие кнопки
    отклоняются на уровне handle_vote_click.
    """
    parts = (custom_id or "").split(":")
    if len(parts) != 5 or parts[0] != "fv":
        return None
    try:
        return int(parts[1]), int(parts[2]), parts[3], parts[4]
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
        # Поколение голосования: канал пула переиспользуется
        # между матчами, поэтому (channel, match_type, index)
        # НЕ уникальны — уникален vote_id.
        "vote_id": _new_vote_id(),
        # Монотонная версия состояния — PostgreSQL CAS в
        # format_vote_store не даёт запоздавшей записи
        # (open) перезаписать done/cancelled.
        "version": 1,
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
                    fv.get("vote_id") or "",
                )
            )
        except Exception as exc:
            logger.warning("Format vote view register failed: %s", exc)
    _schedule_timer(
        tournament.guild_id, tournament.channel_id,
        match_type, match_index, fv=fv,
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
    status = fv.get("vote_status")
    done = status == "done"
    # done/cancelled — финальные состояния: кнопки отключены,
    # устаревший клик получит корректный ответ в handle_vote_click.
    closed = status in ("done", "cancelled")

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
        status_line = (
            "**Статус:** Голосование отменено"
            if status == "cancelled"
            else f"**Проголосовало:** {voted}/{VOTE_PLAYERS}\n"
                 f"**Завершение:** <t:{int(fv.get('vote_deadline') or 0)}:R>"
        )
        view.text(
            f"{b_line}\n{k_line}\n{status_line}"
        )
        view.sep()

    row = discord.ui.ActionRow()
    for fmt_key, label in (
        ("brazil", "🇧🇷 Brazil"),
        ("kazakhstan", "🇰🇿 Kazakhstan"),
    ):
        vote_id = fv.get("vote_id") or ""
        btn = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=_custom_id(gid, cid, vote_id, fmt_key),
            disabled=closed,
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

def _find_vote(guild_id: int, channel_id: int, vote_id: str):
    """Найти (tournament, meta, fv, match_type, match_index) по vote_id.

    custom_id содержит канал матча (= tournament.channel_id), поэтому
    поиск ограничен одним shard'ом store. None — кнопка от другого
    поколения/матча (устаревшая) или битое состояние.
    """
    tournament = store.get(guild_id, channel_id)
    if tournament is None:
        return None
    for meta in (tournament.match_channels or {}).values():
        if not isinstance(meta, dict):
            continue
        fv = meta.get("format_vote")
        if isinstance(fv, dict) and fv.get("vote_id") == vote_id:
            return (
                tournament, meta, fv,
                meta.get("match_type"),
                int(meta.get("match_index", 0) or 0),
            )
    return None


def _ensure_vote_id(tournament, meta: dict, fv: dict) -> None:
    """Миграция старых fv без vote_id — присвоить и сохранить."""
    if fv.get("vote_id"):
        return
    fv["vote_id"] = _new_vote_id()
    fv["version"] = int(fv.get("version") or 0) + 1
    try:
        store.set(tournament)
    except Exception as exc:
        logger.warning("vote_id migration store.set failed: %s", exc)


def _bump_version(fv: dict) -> None:
    fv["version"] = int(fv.get("version") or 0) + 1


# ---------------------------------------------------------------------------
# UI-worker: сериализованные правки сообщения голосования
# ---------------------------------------------------------------------------

def _schedule_vote_ui(guild_id: int, channel_id: int, vote_id: str) -> None:
    """Поставить обновление сообщения голосования в фоновую очередь.

    Воркер при flush заново читает состояние — даже если между
    постановкой и выполнением прошла финализация, отрендерится
    актуальный (финальный) вид, а не устаревший открытый.
    """
    key = f"vote:{vote_id}"
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Sync-контекст без event loop (напр. cancel из sync
        # теста/утилиты): UI обновится при следующем рендере
        # или restore — клики всё равно отклоняются по статусу.
        return
    _UI_CTX[key] = (guild_id, channel_id, vote_id)
    _UI_DIRTY[key] = _UI_DIRTY.get(key, 0) + 1
    task = _UI_TASKS.get(key)
    if task is None or task.done():
        task = asyncio.create_task(_vote_ui_worker(key))
        _UI_TASKS[key] = task


async def _vote_ui_worker(key: str) -> None:
    """Debounce + актуальное состояние на момент flush.

    При временных ошибках Discord (429/5xx/timeout) правка
    повторяется с backoff; processed не продвигается, пока
    поколение не отрендерено — следующий flush покажет
    актуальное состояние. После _UI_MAX_FAILS неудачных
    раундов воркер сдаётся (лог error), следующее
    _schedule_vote_ui запустит его заново.
    """
    processed = 0
    cancelled = False
    try:
        while True:
            await asyncio.sleep(0.2)
            dirty = _UI_DIRTY.get(key, 0)
            if dirty <= processed:
                return
            gid, cid, vote_id = _UI_CTX.get(key, (0, 0, ""))
            found = _find_vote(gid, cid, vote_id)
            if found is None:
                processed = dirty  # голосование удалено — править нечего
                continue
            tournament, _meta, fv, mt, mi = found
            try:
                await _edit_vote_message_retry(
                    tournament, mt, mi, fv, vote_id
                )
                processed = dirty
                _UI_FAILS.pop(key, None)
                _UI_GUILD_SINCE.pop(key, None)
            except _VoteMessageGone:
                processed = dirty  # сообщение удалено — дальше нечего
                _UI_FAILS.pop(key, None)
                _UI_GUILD_SINCE.pop(key, None)
            except _GuildUnavailable:
                # setup_hook до ready / guild ещё не в кэше —
                # это НЕ успешный flush: ждём и повторяем,
                # processed не двигаем. Потолок ожидания —
                # _UI_GUILD_TIMEOUT, чтобы не крутиться вечно.
                since = _UI_GUILD_SINCE.setdefault(
                    key, time.monotonic()
                )
                if time.monotonic() - since > _UI_GUILD_TIMEOUT:
                    logger.error(
                        "Vote UI edit giving up %s: guild "
                        "unavailable for %.0fs",
                        vote_id, _UI_GUILD_TIMEOUT,
                    )
                    processed = dirty
                    _UI_GUILD_SINCE.pop(key, None)
                else:
                    await asyncio.sleep(2.0)
                    continue
            except Exception as exc:
                fails = _UI_FAILS.get(key, 0) + 1
                _UI_FAILS[key] = fails
                logger.warning(
                    "Vote UI edit failed %s (attempt %d): %s",
                    vote_id, fails, exc,
                )
                if fails >= _UI_MAX_FAILS:
                    logger.error(
                        "Vote UI edit giving up %s after %d rounds",
                        vote_id, fails,
                    )
                    processed = dirty
                    _UI_FAILS.pop(key, None)
                # иначе processed < dirty → повтор на след. итерации
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        _UI_TASKS.pop(key, None)
        if not cancelled:
            try:
                if _UI_DIRTY.get(key, 0) > processed:
                    _schedule_vote_ui(*_UI_CTX[key][:2], _UI_CTX[key][2])
                else:
                    _UI_DIRTY.pop(key, None)
                    _UI_CTX.pop(key, None)
                    _UI_FAILS.pop(key, None)
            except (RuntimeError, KeyError):
                pass


_UI_MAX_FAILS = 5
_UI_GUILD_TIMEOUT = 300.0  # сек ожидания готовности guild


class _VoteMessageGone(Exception):
    """Внутренняя метка: сообщение голосования удалено (404)."""


class _GuildUnavailable(Exception):
    """guild ещё не доступен (bot не ready / нет в кэше)."""


async def _edit_vote_message_retry(tournament, match_type: str,
                                   match_index: int, fv: dict,
                                   vote_id: str) -> None:
    """_edit_vote_message с retry на временные ошибки Discord.

    404 (сообщение удалено) не ретраится — _VoteMessageGone.
    Остальные ошибки: до 3 попыток, пауза учитывает retry_after
    (Discord 429) и экспоненциально растёт.
    """
    delay = 0.4
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            await _edit_vote_message(tournament, match_type,
                                     match_index, fv)
            return
        except Exception as exc:
            last_exc = exc
            if getattr(exc, "status", None) == 404:
                raise _VoteMessageGone(vote_id) from exc
            if isinstance(exc, _GuildUnavailable):
                raise  # guild-unavailable — не транзиентный retry
            wait = delay
            ra = getattr(exc, "retry_after", None)
            if ra is not None:
                wait = max(wait, float(ra) + 0.2)
            wait = min(wait, 5.0)
            logger.info(
                "Vote UI edit retry %d/%d %s in %.1fs: %s",
                attempt + 1, 3, vote_id, wait, exc,
            )
            await asyncio.sleep(wait)
            delay *= 2
    raise last_exc  # type: ignore[misc]


async def _edit_vote_message(tournament, match_type: str,
                             match_index: int, fv: dict) -> None:
    """Отредактировать CV2-сообщение голосования по актуальному fv."""
    gid = int(getattr(tournament, "guild_id", 0) or 0)
    guild = store_bot_guild(gid)
    if guild is None:
        # Бот ещё не ready (setup_hook) или guild вне кэша —
        # это отложенная ситуация, а не «нечего править»:
        # worker повторит после готовности.
        raise _GuildUnavailable(gid)
    vch_id = int(fv.get("vote_channel_id") or 0)
    mid = int(fv.get("vote_message_id") or 0)
    if not (vch_id and mid):
        return
    vch = guild.get_channel(vch_id)
    if vch is None:
        vch = await guild.fetch_channel(vch_id)
    msg = await vch.fetch_message(mid)
    await msg.edit(
        view=build_vote_view(tournament, match_type, match_index, fv)
    )


async def handle_vote_click(
    interaction: discord.Interaction, fmt: str
) -> None:
    """Callback кнопки формата (и из живого DesignView, и из
    persistent view после рестарта).

    Lock удерживается только на время чтения/мутации состояния;
    Discord API (edit сообщения, ephemeral) и финализация
    выполняются ПОСЛЕ освобождения lock — вложенного захвата
    и долгих вызовов внутри критической секции нет.

    Interaction подтверждается (defer) СРАЗУ: финализация
    (PG + Discord API) и ожидание lock могут превысить 3-сек
    окно ответа Discord → иначе Unknown interaction.
    """
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        pass  # уже отвечено/неивается — идём дальше
    custom_id = (interaction.data or {}).get("custom_id", "")
    parsed = _parse_custom_id(custom_id)
    legacy = False
    if parsed is not None:
        gid, cid, vote_id, _fmt = parsed
        key = f"vote:{vote_id}"
    elif (custom_id or "").startswith("fv:"):
        # Кнопки, опубликованные до введения vote_id:
        # ``fv:{gid}:{cid}:{match_type}:{match_index}:{fmt}``.
        # Резолвим fv по (match_type, match_index) ТЕКУЩЕГО
        # турнира канала и проверяем vote_message_id — кнопка
        # старого сообщения в переиспользованном канале не
        # совпадёт с сообщением нового голосования и будет
        # отклонена. Миграция vote_id — под provisional lock,
        # затем голос учитывается под lock нового поколения.
        legacy = True
        lp = _parse_legacy_custom_id(custom_id)
        if lp is None:
            await _ephemeral(
                interaction,
                "ℹ️ Это голосование устарело — "
                "используйте кнопки актуального сообщения.",
            )
            return
        gid, cid, _lmt, _lmi = lp
        key = _legacy_key(gid, cid, _lmt, _lmi)
    else:
        return

    reply: str | None = None
    need_finalize = False
    tournament = None
    match_type = None
    match_index = 0
    fv = None
    resolved_vote_id = ""

    if legacy:
        # Фаза 1: resolve + миграция vote_id под provisional
        # lock (он же сериализует конкурентные legacy-клики).
        async with _lock(key):
            resolved = _resolve_legacy(gid, cid, _lmt, _lmi,
                                       interaction)
            if isinstance(resolved, str):
                reply = resolved
            else:
                (_t, _meta, _fv, _mt, _mi, resolved_vote_id) = resolved
        # Фаза 2: голос учитывается под lock СВОЕГО поколения —
        # без вложенных захватов.
        if reply is None and resolved_vote_id:
            async with _lock(f"vote:{resolved_vote_id}"):
                reply, need_finalize, ctx = await _cast_vote(
                    gid, cid, resolved_vote_id, interaction, fmt
                )
                if ctx is not None:
                    tournament, match_type, match_index, fv = ctx
    else:
        async with _lock(key):
            reply, need_finalize, ctx = await _cast_vote(
                gid, cid, vote_id, interaction, fmt
            )
            if ctx is not None:
                tournament, match_type, match_index, fv = ctx
                resolved_vote_id = vote_id
    # --- конец критических секций ---

    if reply is not None:
        await _ephemeral(interaction, reply)
        return

    if need_finalize:
        # finalize сам берёт lock — вызывать только вне _lock.
        await finalize_format_vote(
            interaction.guild, tournament, match_type, match_index,
            vote_id=resolved_vote_id or None,
        )
        await _ephemeral(interaction, "ℹ️ Голосование уже завершено.")
        return

    await _mirror_to_pg(tournament, match_type, match_index, fv)
    await _ephemeral(interaction, "✅ Голос учтён.")


async def _cast_vote(gid: int, cid: int, vote_id: str,
                     interaction: discord.Interaction,
                     fmt: str):
    """Применить голос под уже захваченным lock поколения.

    Возвращает (reply|None, need_finalize, ctx), где
    ctx = (tournament, match_type, match_index, fv) при успехе.
    """
    found = _find_vote(gid, cid, vote_id)
    if found is None:
        return (
            "❌ Голосование не найдено — оно могло быть "
            "отменено или заменено новым матчем.", False, None,
        )
    tournament, _meta, fv, match_type, match_index = found

    # Проверка поколения по сообщению: кнопка на старом
    # сообщении не должна менять состояние нового fv даже при
    # совпадении vote_id (защита от пересозданных view).
    mid_expect = int(fv.get("vote_message_id") or 0)
    mid_actual = _interaction_message_id(interaction)
    if mid_expect and mid_actual and mid_actual != mid_expect:
        logger.info(
            "mm_diag event=vote_stale_message vote_id=%s "
            "match_id=%s channel_id=%s expected_msg=%s got=%s",
            fv.get("vote_id"), fv.get("match_id"), cid,
            mid_expect, mid_actual,
        )
        return (
            "ℹ️ Это голосование устарело — "
            "используйте кнопки актуального сообщения.",
            False, None,
        )

    if fv.get("vote_status") != "open":
        return "ℹ️ Голосование уже завершено.", False, None
    if time.time() > float(fv.get("vote_deadline") or 0):
        return None, True, (tournament, match_type, match_index, fv)

    uid = int(getattr(interaction.user, "id", 0) or 0)
    if uid not in _player_uids(tournament):
        return "Вы не являетесь участником этого матча", False, None
    votes = fv.setdefault("votes", {})
    if str(uid) in votes or uid in votes:
        return "Вы уже проголосовали за этот формат", False, None
    if len(votes) >= VOTE_PLAYERS:
        # 8-й голос уже зафиксирован, финализация ещё не прошла —
        # лишний голос не принимаем.
        return None, True, (tournament, match_type, match_index, fv)
    votes[str(uid)] = fmt
    _bump_version(fv)
    store.set(tournament)
    _schedule_vote_ui(gid, cid, vote_id)
    return None, len(votes) >= VOTE_PLAYERS, \
        (tournament, match_type, match_index, fv)


def _interaction_message_id(interaction) -> int:
    msg = getattr(interaction, "message", None)
    return int(getattr(msg, "id", 0) or 0)


def _parse_legacy_custom_id(custom_id: str):
    """Разбор custom_id старого формата
    ``fv:{gid}:{cid}:{match_type}:{match_index}:{fmt}`` (6 частей).
    Возвращает (gid, cid, match_type, match_index) или None.
    """
    parts = (custom_id or "").split(":")
    if len(parts) != 6 or parts[0] != "fv":
        return None
    try:
        return int(parts[1]), int(parts[2]), parts[3], int(parts[4])
    except (TypeError, ValueError):
        return None


def _resolve_legacy(gid: int, cid: int, match_type: str,
                     match_index: int, interaction):
    """Resolve fv по (match_type, match_index) для legacy-кнопки.

    Возвращает строку-ошибку для ephemeral-ответа или кортеж
    (tournament, meta, fv, mt, mi, vote_id) после миграции
    vote_id. Вызывается под provisional lock.
    """
    t = store.get(gid, cid)
    if t is None:
        return "❌ Голосование не найдено — оно могло быть " \
               "отменено или заменено новым матчем."
    fv0 = get_vote_state(t, match_type, match_index)
    if not isinstance(fv0, dict):
        return "❌ Голосование не найдено — оно могло быть " \
               "отменено или заменено новым матчем."
    # Привязка к поколению через сообщение: legacy custom_id не
    # несёт vote_id — кнопка старого сообщения в канале,
    # переиспользованном новым матчем, отклоняется.
    mid_expect = int(fv0.get("vote_message_id") or 0)
    mid_actual = _interaction_message_id(interaction)
    if mid_expect and mid_actual and mid_actual != mid_expect:
        logger.info(
            "mm_diag event=vote_stale_message vote_id=%s "
            "match_id=%s channel_id=%s expected_msg=%s got=%s",
            fv0.get("vote_id"), fv0.get("match_id"), cid,
            mid_expect, mid_actual,
        )
        return ("ℹ️ Это голосование устарело — "
                "используйте кнопки актуального сообщения.")
    meta0 = _match_meta(t, match_type, match_index) or {}
    _ensure_vote_id(t, meta0, fv0)
    return t, meta0, fv0, match_type, match_index, \
        fv0.get("vote_id") or ""


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
                    match_type: str, match_index: int,
                    fv: dict | None = None) -> None:
    """Таймер дедлайна голосования. Ключ — vote_id (поколение),
    для legacy fv — guild:channel:match."""
    t = store.get(guild_id, channel_id)
    if fv is None and t is not None:
        fv = get_vote_state(t, match_type, match_index)
    # Legacy fv без vote_id — мигрируем ДО постановки таймера,
    # чтобы таймер был привязан к конкретному поколению, а не
    # к (channel, match_type, index), которые переиспользуются.
    if fv is not None and t is not None and not fv.get("vote_id"):
        meta = _match_meta(t, match_type, match_index)
        if meta is not None:
            _ensure_vote_id(t, meta, fv)
    key = _vote_key(
        fv, guild_id, channel_id, match_type, match_index
    )
    task = _TIMERS.get(key)
    if task is not None and not task.done():
        return
    task = asyncio.create_task(
        _vote_timer(
            guild_id, channel_id, match_type, match_index, key,
            vote_id=(fv or {}).get("vote_id") or "",
        )
    )
    _TIMERS[key] = task
    logger.info(
        "mm_diag event=vote_timer_scheduled vote_id=%s match_id=%s "
        "channel_id=%s match=%s:%s",
        (fv or {}).get("vote_id"), (fv or {}).get("match_id"),
        channel_id, match_type, match_index,
    )


async def _vote_timer(guild_id: int, channel_id: int,
                      match_type: str, match_index: int,
                      key: str, vote_id: str = "") -> None:
    """Таймер дедлайна. Если задан vote_id — резолвит fv ТОЛЬКО
    по поколению: при переиспользовании канала чужой матч с тем
    же (match_type, match_index) не будет финализирован этим
    таймером."""
    try:
        while True:
            if vote_id:
                found = _find_vote(guild_id, channel_id, vote_id)
                if found is None:
                    logger.info(
                        "mm_diag event=vote_timer_orphan vote_id=%s "
                        "channel_id=%s — голосование заменено/удалено",
                        vote_id, channel_id,
                    )
                    return
                tournament, _meta, fv, mt, mi = found
            else:
                # Legacy-таймер без vote_id: только если fv всё
                # ещё не имеет поколения (редкий путь до
                # миграции); иначе пропускаем.
                tournament = store.get(guild_id, channel_id)
                fv = (
                    get_vote_state(tournament, match_type, match_index)
                    if tournament is not None else None
                )
                if fv is None or fv.get("vote_id"):
                    # fv удалено ИЛИ поколение появилось — этот
                    # таймер устарел (новый таймер привязан к
                    # vote_id и работает независимо).
                    return
                mt, mi = match_type, match_index
            if fv.get("vote_status") != "open":
                return
            remain = float(fv.get("vote_deadline") or 0) - time.time()
            if remain <= 0:
                logger.info(
                    "mm_diag event=vote_deadline_reached vote_id=%s "
                    "match_id=%s channel_id=%s",
                    fv.get("vote_id"), fv.get("match_id"), channel_id,
                )
                await finalize_format_vote(
                    None, tournament, mt, mi, vote_id=vote_id or None
                )
                return
            await asyncio.sleep(min(remain + 0.5, 30.0))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Format vote timer failed: %s", key)
    finally:
        _TIMERS.pop(key, None)


async def finalize_format_vote(
    guild: discord.Guild | None,
    tournament,
    match_type: str,
    match_index: int,
    vote_id: str | None = None,
) -> bool:
    """Подвести итог: победитель по голосам, ничья/пусто — случайно.

    Критическая секция (подсчёт + перевод статуса + store.set)
    выполняется под per-vote lock; Discord API и PG-зеркало —
    после его освобождения. Идемпотентно: повторный вызов при
    vote_status != open возвращает False. Если передан vote_id —
    финализируется ТОЛЬКО это поколение: таймер/клик старого
    матча не тронет новое голосование в переиспользованном канале.
    """
    gid = int(getattr(tournament, "guild_id", 0) or 0)
    cid = int(getattr(tournament, "channel_id", 0) or 0)
    fresh0 = store.get(gid, cid) or tournament
    fv0 = get_vote_state(fresh0, match_type, match_index)
    key = (
        f"vote:{vote_id}" if vote_id
        else _vote_key(fv0, gid, cid, match_type, match_index)
    )

    payload = None
    async with _lock(key):
        fresh = store.get(gid, cid) or tournament
        fv = get_vote_state(fresh, match_type, match_index)
        if fv is None or fv.get("vote_status") != "open":
            return False
        if vote_id and fv.get("vote_id") != vote_id:
            logger.info(
                "mm_diag event=vote_finalize_mismatch vote_id=%s "
                "fv_vote_id=%s match_id=%s channel_id=%s",
                vote_id, fv.get("vote_id"), fv.get("match_id"), cid,
            )
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
        _bump_version(fv)
        store.set(fresh)
        meta = _match_meta(fresh, match_type, match_index) or {}
        payload = (fresh, fv, counts, selected, meta)
    # --- lock освобождён: дальше только Discord API / PG ---

    fresh, fv, counts, selected, meta = payload
    await _mirror_to_pg(fresh, match_type, match_index, fv)

    # CV2-сообщение → финальный вид через сериализованный
    # UI-worker: он перечитает состояние на момент flush, поэтому
    # запоздавший PATCH открытого вида не перезапишет финальный.
    _schedule_vote_ui(gid, cid, fv.get("vote_id") or "")

    # Строка «Формат» в карточке матча.
    try:
        from services.match_announcer import update_match_card

        await update_match_card(
            guild, fresh, match_type, match_index,
            state=meta.get("card_state") or "active",
        )
    except Exception as exc:
        logger.warning("Format vote card update failed: %s", exc)

    # Завершённое голосование: снять таймер. Lock-объект в
    # _LOCKS НЕ удаляем: даже «свободный» asyncio.Lock может
    # иметь ожидающих захвата — удаление создало бы второй
    # lock для того же ключа и потерю сериализации. Объекты
    # малы и ограничены числом голосований — утечки нет.
    task = _TIMERS.pop(key, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    logger.info(
        "mm_diag event=vote_finalized vote_id=%s match_id=%s "
        "channel_id=%s selected=%s votes=%s",
        fv.get("vote_id"), fv.get("match_id"), cid, selected, counts,
    )
    return True


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
    кнопки отключаются при следующем рендере/клике. Таймеры
    отменяются, lock/timer записи чистятся, итоги
    зеркалируются в PostgreSQL."""
    gid = int(getattr(tournament, "guild_id", 0) or 0)
    cid = int(getattr(tournament, "channel_id", 0) or 0)
    cancelled: list = []
    for meta in (tournament.match_channels or {}).values():
        fv = (meta or {}).get("format_vote")
        if not (isinstance(fv, dict) and fv.get("vote_status") == "open"):
            continue
        fv["vote_status"] = "cancelled"
        fv["closed_at"] = time.time()
        _ensure_vote_id(tournament, meta, fv)
        _bump_version(fv)
        mt = meta.get("match_type", "")
        mi = int(meta.get("match_index", 0) or 0)
        key = _vote_key(fv, gid, cid, mt, mi)
        task = _TIMERS.pop(key, None)
        if task is not None:
            task.cancel()
        # Lock не удаляем (см. finalize_format_vote) — возможны
        # ожидающие захвата.
        # Кнопки на сообщении должны отключиться — UI-worker
        # отрендерит актуальное (cancelled) состояние.
        _schedule_vote_ui(gid, cid, fv.get("vote_id") or "")
        cancelled.append((tournament, mt, mi, fv))
        logger.info(
            "mm_diag event=vote_cancelled vote_id=%s match_id=%s "
            "channel_id=%s",
            fv.get("vote_id"), fv.get("match_id"), cid,
        )
    store.set(tournament)
    # PG-зеркало статуса cancelled — best-effort, только если
    # есть running loop (в проде cancel вызывается из async
    # обработчиков; в sync-контексте зеркало пропускается).
    if cancelled:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        for t, mt, mi, fv in cancelled:
            asyncio.create_task(
                _mirror_to_pg(t, mt, mi, fv)
            )


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
    from views.format_vote_view import (
        FormatVoteButtonsView,
        LegacyFormatVoteView,
    )

    legacy_registered: set[tuple] = set()
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
            _ensure_vote_id(tournament, meta, fv)
            try:
                bot.add_view(
                    FormatVoteButtonsView(
                        gid, cid, fv.get("vote_id") or ""
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Format vote view restore failed %s: %s",
                    fv.get("match_id"), exc,
                )
            # Кнопки, опубликованные до vote_id, имеют старый
            # custom_id — регистрируем и legacy-view, иначе
            # после рестарта они не имеют обработчика.
            lkey = (gid, cid, mt, mi)
            if lkey not in legacy_registered:
                legacy_registered.add(lkey)
                try:
                    bot.add_view(
                        LegacyFormatVoteView(gid, cid, mt, mi)
                    )
                except Exception as exc:
                    logger.warning(
                        "Legacy format vote view restore failed "
                        "%s: %s", fv.get("match_id"), exc,
                    )
            if fv.get("vote_status") != "open":
                # done/cancelled: карточка могла остаться в
                # открытом виде (крах до финального edit) —
                # UI-worker перерендерит актуальное состояние.
                if fv.get("vote_message_id"):
                    _schedule_vote_ui(
                        gid, cid, fv.get("vote_id") or ""
                    )
                continue
            if time.time() > float(fv.get("vote_deadline") or 0):
                guild = bot.get_guild(gid)
                await finalize_format_vote(guild, tournament, mt, mi)
            else:
                _schedule_timer(gid, cid, mt, mi, fv=fv)

    # setup_hook выполняется до ready: store_bot_guild может
    # вернуть None, и UI-worker посчитает обновление выполненным,
    # хотя edit не дошёл. После wait_until_ready повторно
    # ставим обновления финальных карточек — не блокируя
    # setup_hook.
    pending = []
    for tournament in store.all():
        gid = int(getattr(tournament, "guild_id", 0) or 0)
        cid = int(getattr(tournament, "channel_id", 0) or 0)
        for meta in (tournament.match_channels or {}).values():
            fv = (meta or {}).get("format_vote")
            if (
                isinstance(fv, dict)
                and fv.get("vote_status") in ("done", "cancelled")
                and fv.get("vote_message_id")
                and fv.get("vote_id")
            ):
                pending.append((gid, cid, fv["vote_id"]))
    if pending:
        asyncio.create_task(
            _reschedule_ui_after_ready(bot, pending)
        )


async def _reschedule_ui_after_ready(bot, pending: list) -> None:
    """Повторная постановка UI-обновлений после ready.

    Покрывает случай, когда setup_hook отработал до готовности
    guild-кэша: первый flush мог «успешно» завершиться без
    реального edit.
    """
    try:
        await bot.wait_until_ready()
    except Exception:
        await asyncio.sleep(5)
    for gid, cid, vote_id in pending:
        _schedule_vote_ui(gid, cid, vote_id)


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
                # fv, созданные до появления version, шлём как 1 —
                # CAS-колонка NOT NULL DEFAULT 0, а 0 < 0 заблокировал
                # бы даже первую запись. Мутации всегда бампают
                # версию, поэтому коллизий 1-vs-1 нет.
                "version": max(1, int(fv.get("version") or 0)),
                "selected_format": fv.get("selected_format"),
                "vote_status": fv.get("vote_status") or "open",
                "tie": bool(fv.get("tie")),
            },
        )
    except Exception as exc:
        logger.debug("format vote PG mirror failed: %s", exc)
