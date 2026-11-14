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

# per-vote asyncio.Lock — защита от двойного голосования при
# одновременных нажатиях и от гонки finalize/клик. Ключ — vote_id
# конкретного голосования: локи/таймеры разных матчей в одном
# канале пула не пересекаются.
_LOCKS: dict[str, asyncio.Lock] = {}
_TIMERS: dict[str, asyncio.Task] = {}


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
        vote_id = fv.get("vote_id") or ""
        btn = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=_custom_id(gid, cid, vote_id, fmt_key),
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
    try:
        store.set(tournament)
    except Exception as exc:
        logger.warning("vote_id migration store.set failed: %s", exc)


async def handle_vote_click(
    interaction: discord.Interaction, fmt: str
) -> None:
    """Callback кнопки формата (и из живого DesignView, и из
    persistent view после рестарта).

    Lock удерживается только на время чтения/мутации состояния;
    Discord API (edit сообщения, ephemeral) и финализация
    выполняются ПОСЛЕ освобождения lock — вложенного захвата
    и долгих вызовов внутри критической секции нет.
    """
    custom_id = (interaction.data or {}).get("custom_id", "")
    parsed = _parse_custom_id(custom_id)
    if parsed is None:
        # custom_id старого формата (без vote_id) или чужой —
        # голосование этого поколения не существует, отклоняем.
        if (custom_id or "").startswith("fv:"):
            await _ephemeral(
                interaction,
                "ℹ️ Это голосование устарело — "
                "используйте кнопки актуального сообщения.",
            )
        return
    gid, cid, vote_id, _ = parsed
    key = f"vote:{vote_id}"

    reply: str | None = None
    need_finalize = False
    tournament = None
    match_type = None
    match_index = 0
    fv = None

    async with _lock(key):
        found = _find_vote(gid, cid, vote_id)
        if found is None:
            reply = "❌ Голосование не найдено — оно могло быть отменено или заменено новым матчем."
        else:
            tournament, meta, fv, match_type, match_index = found
            _ensure_vote_id(tournament, meta, fv)
            if fv.get("vote_status") != "open":
                reply = "ℹ️ Голосование уже завершено."
            elif time.time() > float(fv.get("vote_deadline") or 0):
                need_finalize = True
            else:
                uid = int(getattr(interaction.user, "id", 0) or 0)
                if uid not in _player_uids(tournament):
                    reply = (
                        "Вы не являетесь участником этого матча"
                    )
                else:
                    votes = fv.setdefault("votes", {})
                    if str(uid) in votes or uid in votes:
                        reply = "Вы уже проголосовали за этот формат"
                    elif len(votes) >= VOTE_PLAYERS:
                        # 8-й голос уже зафиксирован, финализация ещё
                        # не прошла — лишний голос не принимаем.
                        need_finalize = True
                    else:
                        votes[str(uid)] = fmt
                        store.set(tournament)
                        need_finalize = len(votes) >= VOTE_PLAYERS
    # --- конец критической секции ---

    if reply is not None:
        await _ephemeral(interaction, reply)
        return

    if need_finalize:
        # finalize сам берёт lock — вызывать только вне _lock.
        await finalize_format_vote(
            interaction.guild, tournament, match_type, match_index
        )
        await _ephemeral(interaction, "ℹ️ Голосование уже завершено.")
        return

    # Обычный голос: обновить сообщение ВНЕ lock, затем ответить.
    try:
        vch_id = int(fv.get("vote_channel_id") or cid)
        vch = interaction.guild.get_channel(vch_id)
        if vch is None:
            vch = await interaction.guild.fetch_channel(vch_id)
        mid = int(fv.get("vote_message_id") or 0)
        if vch is not None and mid:
            msg = await vch.fetch_message(mid)
            await msg.edit(
                view=build_vote_view(
                    tournament, match_type, match_index, fv
                )
            )
    except Exception as exc:
        logger.warning("Format vote update failed: %s", exc)
    await _mirror_to_pg(tournament, match_type, match_index, fv)
    await _ephemeral(interaction, "✅ Голос учтён.")


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
    key = _vote_key(
        fv, guild_id, channel_id, match_type, match_index
    )
    task = _TIMERS.get(key)
    if task is not None and not task.done():
        return
    task = asyncio.create_task(
        _vote_timer(guild_id, channel_id, match_type, match_index, key)
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
                      key: str) -> None:
    try:
        while True:
            tournament = store.get(guild_id, channel_id)
            fv = (
                get_vote_state(tournament, match_type, match_index)
                if tournament is not None else None
            )
            if fv is None or fv.get("vote_status") != "open":
                return
            remain = float(fv.get("vote_deadline") or 0) - time.time()
            if remain <= 0:
                logger.info(
                    "mm_diag event=vote_deadline_reached vote_id=%s "
                    "match_id=%s channel_id=%s",
                    fv.get("vote_id"), fv.get("match_id"), channel_id,
                )
                await finalize_format_vote(
                    None, tournament, match_type, match_index
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
) -> bool:
    """Подвести итог: победитель по голосам, ничья/пусто — случайно.

    Критическая секция (подсчёт + перевод статуса + store.set)
    выполняется под per-vote lock; Discord API и PG-зеркало —
    после его освобождения. Идемпотентно: повторный вызов при
    vote_status != open возвращает False.
    """
    gid = int(getattr(tournament, "guild_id", 0) or 0)
    cid = int(getattr(tournament, "channel_id", 0) or 0)
    fresh0 = store.get(gid, cid) or tournament
    fv0 = get_vote_state(fresh0, match_type, match_index)
    key = _vote_key(fv0, gid, cid, match_type, match_index)

    payload = None
    async with _lock(key):
        fresh = store.get(gid, cid) or tournament
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
        store.set(fresh)
        meta = _match_meta(fresh, match_type, match_index) or {}
        payload = (fresh, fv, counts, selected, meta)
    # --- lock освобождён: дальше только Discord API / PG ---

    fresh, fv, counts, selected, meta = payload
    await _mirror_to_pg(fresh, match_type, match_index, fv)

    # CV2-сообщение → финальный вид с правилами.
    try:
        if guild is None:
            guild = store_bot_guild(gid)
        vch_id = int(fv.get("vote_channel_id") or 0)
        mid = int(fv.get("vote_message_id") or 0)
        if guild is not None and vch_id and mid:
            vch = guild.get_channel(vch_id)
            if vch is None:
                vch = await guild.fetch_channel(vch_id)
            msg = await vch.fetch_message(mid)
            await msg.edit(
                view=build_vote_view(fresh, match_type,
                                     match_index, fv)
            )
    except Exception as exc:
        logger.warning("Format vote final edit failed: %s", exc)

    # Строка «Формат» в карточке матча.
    try:
        from services.match_announcer import update_match_card

        await update_match_card(
            guild, fresh, match_type, match_index,
            state=meta.get("card_state") or "active",
        )
    except Exception as exc:
        logger.warning("Format vote card update failed: %s", exc)

    # Завершённое голосование: снять таймер и lock-запись.
    task = _TIMERS.pop(key, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    _LOCKS.pop(key, None)
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
    отменяются, lock/timer записи чистятся."""
    gid = int(getattr(tournament, "guild_id", 0) or 0)
    cid = int(getattr(tournament, "channel_id", 0) or 0)
    for meta in (tournament.match_channels or {}).values():
        fv = (meta or {}).get("format_vote")
        if not (isinstance(fv, dict) and fv.get("vote_status") == "open"):
            continue
        fv["vote_status"] = "cancelled"
        fv["closed_at"] = time.time()
        mt = meta.get("match_type", "")
        mi = int(meta.get("match_index", 0) or 0)
        key = _vote_key(fv, gid, cid, mt, mi)
        task = _TIMERS.pop(key, None)
        if task is not None:
            task.cancel()
        _LOCKS.pop(key, None)
        logger.info(
            "mm_diag event=vote_cancelled vote_id=%s match_id=%s "
            "channel_id=%s",
            fv.get("vote_id"), fv.get("match_id"), cid,
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
            if fv.get("vote_status") != "open":
                continue
            if time.time() > float(fv.get("vote_deadline") or 0):
                guild = bot.get_guild(gid)
                await finalize_format_vote(guild, tournament, mt, mi)
            else:
                _schedule_timer(gid, cid, mt, mi, fv=fv)


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
