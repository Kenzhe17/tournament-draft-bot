"""Cog: приём скриншотов результатов матчей для OCR-сопоставления.

Капитан нажимает «📸 Скриншот» на матче (views/ocr_match_view.py),
затем отправляет изображение в канал результатов OCR_STATS_CHANNEL_ID —
этот cog ловит аттачмент, прогоняет OCR и публикует в том же канале
сообщение с embed'ом (скриншот внутри), пингом обоих капитанов и
кнопками сопоставления. Кнопка «Неправильный скриншот» позволяет
прислать новый скриншот — embed обновляется.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
from datetime import datetime
from typing import Any

import discord
from discord.ui import Button, FileUpload, Label, Modal, Select, View
from utils.design import compose, text_card
from discord.ext import commands

from config import (
    get_emoji, replace_emojis, MM_MATCH_CHANNEL_POOL,
    OCR_MAPPING_CHANNEL_ID,
    OCR_RESULT_CHANNEL_ID as _OCR_RESULT_CHANNEL_ID,
)
from storage.json_store import store
from views.ocr_match_view import (
    OCR_STATS_CHANNEL_ID,
    OCRPublicView,
    SquadAssignedView,
    SquadClaimView,
    _match_tuple,
    _can_finalize_room,
    _session_key,
    _team_display_name,
    pop_pending_upload,
    pop_pending_upload_any,
    register_pending_upload,
    team_roster,
)

logger = logging.getLogger(__name__)

MAX_ATTACH = 10 * 1024 * 1024
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
SHOT_FILENAME = "match_result.png"

# Канал итоговых результатов матчей (команды + игроки + скриншот)
RESULT_CHANNEL_ID = _OCR_RESULT_CHANNEL_ID


_KNOWN_TEAM_NAMES = ("хищники", "разрушители")


def _normalize_team_name(raw: str) -> str:
    """Привести OCR-искажённое имя команды к каноническому виду.

    Кириллическая OCR-модель периодически читает «ХИЩНИКИ» как
    «ИНЖИТИХ» и т.п. Сравниваем нормализованную строку с известными
    названиями команд режима по similarity — при достаточном
    совпадении подставляем каноническое имя, иначе оставляем как есть.
    """
    import difflib
    import re as _re

    norm = _re.sub(r"[^а-яa-zё]", "", str(raw).lower())
    if not norm:
        return raw
    best, best_score = None, 0.0
    for canon in _KNOWN_TEAM_NAMES:
        score = difflib.SequenceMatcher(None, norm, canon).ratio()
        if score > best_score:
            best, best_score = canon, score
    if best is not None and best_score >= 0.55:
        return best.capitalize()
    return raw


def _match_label(match_type: str, match_index: int) -> str:
    if match_type == "final":
        return "Финал"
    if match_type == "semifinal":
        return f"Полуфинал #{match_index + 1}"
    return f"Отборочный #{match_index + 1}"


def _ocr_boxes(data: bytes) -> list:
    """Запустить OCR и вернуть текстовые боксы (executor)."""
    from services.ocr_service import _run_ocr

    return _run_ocr(data) or []


def _parse_squad(boxes: list) -> dict[str, Any] | None:
    """Детальный экран одного отряда (K/D/A + урон/нокдауны/...)."""
    from services.cs_squad_parser import parse_squad_results

    return parse_squad_results(boxes)


def _parse_classic(boxes: list) -> dict[str, Any] | None:
    """Классический двух-панельный экран CS 4x4."""
    from services import cs_parser

    parsed = cs_parser.parse_cs_results(boxes)
    if parsed is None or (not parsed["team_a"] and not parsed["team_b"]):
        logger.warning(
            "OCR parse failed: %d boxes | %s",
            len(boxes),
            " | ".join(b["text"] for b in boxes[:60]),
        )
        return None
    return parsed


_BANNER_LABELS = {
    "win": "🏆 ПОБЕДА",
    "lose": "💀 ПОРАЖЕНИЕ",
}


def _fmt_num(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return f"{v:,}".replace(",", " ")


async def _stats_channel(message: discord.Message, tournament=None):
    """Канал результатов: заданный ID, иначе канал сообщения.

    Для MM-матчей (канал cs-4x4-match-N в match_channels) скриншот и
    карточка сопоставления живут в канале матча, а не в канале
    статистики.
    """
    if (
        tournament is not None
        and getattr(tournament, "mm_search_channel_id", 0)
        and str(message.channel.id) in (tournament.match_channels or {})
    ):
        return message.channel
    channel = message.guild.get_channel(OCR_STATS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await message.guild.fetch_channel(OCR_STATS_CHANNEL_ID)
        except Exception:
            channel = None
    return channel or message.channel


_OCR_MAP_CAPTION = (
    "Игроки жмут **«Это мой ник»** и выбирают свой ник. "
    "Капитаны назначают оставшиеся. Всё закреплено — "
    "результат зафиксируется автоматически."
)

_SQUAD_MAP_CAPTION = (
    "Капитан нажимает свою кнопку ниже и сопоставляет игровые ники "
    "с участниками Discord. После сопоставления обеих команд результат "
    "зафиксируется автоматически."
)


def _mapping_caption(tournament, session: dict) -> str:
    """Инструкция по маппингу с упоминанием капитанов.

    Скриншот присылает капитан ПОБЕДИВШЕЙ команды — его команда всегда
    на Левой Панели, соперник — на Правой. Если загрузил админ
    (uploaded_by не совпадает с капитаном), показываем общий текст.
    """
    teams = session.get("teams") or []
    uploaded_by = session.get("uploaded_by")
    uids = getattr(tournament, "player_user_ids", {}) or {}
    tlist = getattr(tournament, "teams", []) or []

    def _cap_uid(idx: int):
        if not (0 <= idx < len(tlist)):
            return None
        captain = (tlist[idx] or {}).get("captain", "")
        uid = uids.get(captain)
        try:
            return int(uid) if uid else None
        except (TypeError, ValueError):
            return None

    left_idx = right_idx = None
    for idx in teams:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            continue
        if _cap_uid(idx) is not None and _cap_uid(idx) == uploaded_by:
            left_idx = idx
        elif right_idx is None:
            right_idx = idx

    if left_idx is not None and right_idx is not None:
        return (
            f"<@{_cap_uid(left_idx)}> и <@{_cap_uid(right_idx)}> — "
            "назначьте оставшиеся ники кнопками команд. "
            "Игроки жмут **«Это мой ник»**."
        )

    # Загрузил не капитан (админ) — общий текст: стороны могут
    # определиться автоматически, иначе капитаны выберут вручную.
    mentions = [
        f"<@{_cap_uid(i)}>" for i in teams
        if _cap_uid(i) is not None
    ]
    if mentions:
        who = " ".join(mentions)
        return (
            f"{who} нажмите на свои кнопки ниже и сопоставьте "
            "игроков своих команд."
        )
    return _OCR_MAP_CAPTION


def _mapping_layout(view, caption: str):
    """V2-карточка сопоставления: скриншот игры (MediaGallery) внутри
    карточки + подпись + кнопки view."""
    from utils.design.layouts import DesignView

    layout = DesignView(timeout=None)
    layout.component(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(media=f"attachment://{SHOT_FILENAME}")
        )
    )
    layout.sep(large=True)
    layout.text(caption)
    composed = compose(view=view, text=None)
    for child in composed.walk_children():
        if isinstance(child, discord.ui.ActionRow):
            layout.container.add_item(child)
    return layout


async def _send_results_message(
    channel: discord.TextChannel,
    data: bytes,
    view: OCRPublicView,
    caption: str = _OCR_MAP_CAPTION,
) -> discord.Message:
    """Новое сообщение: инструкция по сопоставлению + скриншот + кнопки.

    Без embed и итоговых данных — результат публикуется только после
    полного сопоставления (publish_match_result_card).
    """
    file = discord.File(io.BytesIO(data), filename=SHOT_FILENAME)
    return await channel.send(
        view=_mapping_layout(view, caption),
        file=file,
    )


async def _update_results_message(
    channel: discord.TextChannel,
    message_id: int,
    data: bytes,
    view: OCRPublicView,
    caption: str = _OCR_MAP_CAPTION,
) -> discord.Message:
    """Обновить существующее сообщение (повторный скриншот).

    При любой ошибке редактирования кидает исключение — вызывающий
    код отправит новое сообщение.
    """
    msg = await channel.fetch_message(message_id)
    await msg.edit(
        view=_mapping_layout(view, caption),
        attachments=[
            discord.AttachmentInput(io.BytesIO(data), filename=SHOT_FILENAME)
        ],
    )
    return msg


async def _neutralize_stale_ocr_message(
    channel, message_id: int | None
) -> None:
    """Погасить сообщение OCR-комплекта, чья сессия уже отменена
    или заменена, пока шёл Discord API-вызов.

    Сообщение не удаляется (сохраняем аудит), но view обнуляется —
    кнопки маппинга/отмены больше неактивны. Best effort: любая
    ошибка Discord игнорируется. НЕ вызывать под _room_lock.
    """
    if not channel or not message_id:
        return
    try:
        msg = await channel.fetch_message(int(message_id))
        await msg.edit(
            content=replace_emojis(
                "❌ Этот комплект скриншотов был отменён или "
                "заменён — кнопки больше не активны."
            ),
            view=None,
            attachments=[],
        )
    except Exception as e:
        logger.warning(
            "Neutralize stale OCR message %s failed: %s",
            message_id, e,
        )


# ---------------------------------------------------------------------------
# Итоговый пост в канал результатов (RESULT_CHANNEL_ID)
# ---------------------------------------------------------------------------

def _captain_name(tournament, team_index: int) -> str:
    """Название команды = имя капитана."""
    team = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
    return team.get("captain") or _team_display_name(tournament, team_index)


def _roster_mentions(tournament, team_index: int) -> str:
    """Состав команды строкой ментионов <@id>."""
    parts = []
    for name in team_roster(tournament, team_index):
        uid = tournament.player_user_ids.get(name)
        parts.append(f"<@{uid}>" if uid and uid > 0 else name)
    return " ".join(parts)


def _format_duration(seconds: float) -> str:
    """«10 мин. 25 сек.» / «1 ч. 12 мин.» — длительность матча."""
    secs = max(0, int(seconds))
    hours, rem = divmod(secs, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours} ч. {minutes} мин."
    return f"{minutes} мин. {seconds} сек."


def _result_card(
    tournament, match_type: str, match_index: int, t1: int, t2: int,
    match_num: int = 0,
    duration: str = "",
    rooms_count: int = 0,
) -> "discord.ui.LayoutView":
    """V2-карточка итога матча: заголовок + скриншот, без составов.

    Для MM-турниров заголовок без эмодзи.
    """
    from utils.design.layouts import DesignView

    name_a = _captain_name(tournament, t1)
    name_b = _captain_name(tournament, t2)
    is_mm = bool(getattr(tournament, "mm_search_channel_id", 0))
    prefix = "" if is_mm else f"{get_emoji('winner')} "
    num_part = f"#{match_num}: " if match_num else ""
    view = DesignView(
        title=f"{prefix}ИТОГ МАТЧА {num_part}{name_a} vs {name_b}",
        timeout=None,
    )
    view.component(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(media=f"attachment://{SHOT_FILENAME}")
        )
    )
    return view


async def _result_channel(guild: discord.Guild, fallback):
    """Канал итогов: RESULT_CHANNEL_ID, иначе fallback (канал статистики).

    Валидирует, что настроенный канал существует и является текстовым.
    """
    channel = guild.get_channel(RESULT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(RESULT_CHANNEL_ID)
        except Exception:
            channel = None
    if channel is not None and not isinstance(channel, discord.TextChannel):
        logger.warning(
            "RESULT_CHANNEL_ID %s is not a text channel — using fallback",
            RESULT_CHANNEL_ID,
        )
        channel = None
    if channel is None and fallback is not None:
        logger.warning(
            "Results channel %s unavailable — using fallback channel %s",
            RESULT_CHANNEL_ID, getattr(fallback, "id", fallback),
        )
    return channel or fallback


# ---------------------------------------------------------------------------
# Финальная карточка результата (генерируемый PNG по новому дизайну)
# ---------------------------------------------------------------------------

_RESULT_CARD_CACHE: dict[str, bytes] = {}
_RESULT_CARD_CACHE_MAX = 50
_RESULT_PUBLISH_LOCKS: dict[str, asyncio.Lock] = {}
_RESULT_PUBLISH_LOCKS_MAX = 200


async def _member_avatar_url(guild: discord.Guild, user_id: int) -> str:
    """URL аватара участника сервера (пустая строка если недоступен)."""
    if not user_id or user_id <= 0:
        return ""
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            member = None
    if member is None:
        return ""
    url = member.display_avatar.url
    return url + ("&" if "?" in url else "?") + "size=128"


def _resolve_card_score(
    session: dict,
    kills_a: int,
    kills_b: int,
    ordered_score: tuple[int, int] | None = None,
) -> tuple[int, int, str]:
    """(score_a, score_b, label) для PNG-карточки.

    «СЧЁТ МАТЧА» — только если OCR реально распознал счёт раундов И
    он уже привязан к командам через ordered_round_score (порядок
    «команда t1 : команда t2», а не «левый:правый» с экрана).
    Иначе показываем суммы убийств команд с честной подписью
    «УБИЙСТВА КОМАНД» — счёт раундов никогда не подменяется киллами.
    """
    if ordered_score:
        try:
            return (
                int(ordered_score[0]), int(ordered_score[1]), "СЧЁТ МАТЧА"
            )
        except (TypeError, ValueError):
            pass
    return int(kills_a or 0), int(kills_b or 0), "УБИЙСТВА КОМАНД"


async def publish_match_result_card(
    guild: discord.Guild,
    tournament,
    match_type: str,
    match_index: int,
    t1: int,
    t2: int,
    winning_team_index: int,
    temp_stats: dict,
    session: dict,
) -> "discord.Message | None":
    """Один матч = одна карточка результата.

    Блокирует параллельные публикации (авто- и ручное подтверждение,
    повторный finalize) и перечитывает result_message_id из store —
    иначе изредка отправлялось два одинаковых embed с результатом.
    """
    key = _session_key(match_type, match_index)
    match_id = f"{tournament.id}_{key}"
    lock = _RESULT_PUBLISH_LOCKS.get(match_id)
    if lock is None:
        lock = asyncio.Lock()
        _RESULT_PUBLISH_LOCKS[match_id] = lock
        if len(_RESULT_PUBLISH_LOCKS) > _RESULT_PUBLISH_LOCKS_MAX:
            _RESULT_PUBLISH_LOCKS.pop(next(iter(_RESULT_PUBLISH_LOCKS)))
    async with lock:
        try:
            fresh = store.get(guild.id, tournament.channel_id)
            fs = fresh.ocr_sessions.get(key) if fresh else None
            if fs and fs.get("result_message_id"):
                session["result_message_id"] = fs["result_message_id"]
        except Exception:
            pass
        return await _publish_match_result_card_impl(
            guild, tournament, match_type, match_index, t1, t2,
            winning_team_index, temp_stats, session,
        )


async def _publish_match_result_card_impl(
    guild: discord.Guild,
    tournament,
    match_type: str,
    match_index: int,
    t1: int,
    t2: int,
    winning_team_index: int,
    temp_stats: dict,
    session: dict,
) -> "discord.Message | None":
    """Сгенерировать PNG «РЕЗУЛЬТАТ МАТЧА» и опубликовать в RESULT_CHANNEL_ID.

    Использует уже рассчитанные данные (ELO/победитель не пересчитываются).
    Обновляет существующий result_message_id или отправляет новое сообщение.
    Возвращает сообщение с результатом (нужно для ссылки в карточке матча).
    """
    try:
        from utils.match_result_renderer import (
            MatchResultData,
            ResultPlayer,
            TeamTotals,
            render_match_result,
        )
        from storage.match_number_store import match_number_store
        from storage.player_stats_store import player_stats_store

        match_id = f"{tournament.id}_{_session_key(match_type, match_index)}"

        # user_id по имени: турнир + все известные stats
        user_ids: dict[str, int] = dict(tournament.player_user_ids)
        try:
            for st in await player_stats_store.get_all(guild.id):
                user_ids.setdefault(st.name, st.user_id)
        except Exception:
            pass

        async def _player(name: str, team: str) -> ResultPlayer:
            kd = temp_stats.get(name, {}) or {}
            uid = user_ids.get(name, 0)
            stats = await player_stats_store.get(guild.id, uid) if uid else None
            return ResultPlayer(
                user_id=uid,
                name=name,
                team=team,
                avatar_url=await _member_avatar_url(guild, uid),
                sr=float(stats.skill_rating) if stats else 0.0,
                elo=int(stats.elo) if stats else 0,
                kills=int(kd.get("kills", 0) or 0),
                deaths=int(kd.get("deaths", 0) or 0),
                assists=int(kd.get("assists", 0) or 0),
                damage=int(kd.get("damage", 0) or 0),
                elo_change=int(getattr(stats, "last_elo_change", 0) or 0) if stats else 0,
            )

        team_a_names = team_roster(tournament, t1)
        team_b_names = team_roster(tournament, t2)
        players = await asyncio.gather(
            *[_player(n, "A") for n in team_a_names],
            *[_player(n, "B") for n in team_b_names],
        )
        cards_a = [p for p in players if p.team == "A"][:4]
        cards_b = [p for p in players if p.team == "B"][:4]

        def _totals(cards: list[ResultPlayer]) -> TeamTotals:
            return TeamTotals(
                kills=sum(p.kills for p in cards),
                damage=sum(p.damage for p in cards),
                elo_change=sum(p.elo_change for p in cards),
            )

        totals_a, totals_b = _totals(cards_a), _totals(cards_b)

        # MVP: для multi-room — kills → damage → assists → user_id
        # (стабильно при полном равенстве). Для классики — урон, как раньше.
        all_cards = cards_a + cards_b
        if session.get("mode") == "rooms":
            mvp = max(
                all_cards,
                key=lambda p: (
                    p.kills, p.damage, p.assists, p.user_id or 0
                ),
                default=None,
            )
        else:
            mvp = max(all_cards, key=lambda p: p.damage, default=None)
        mvp_name = ""
        mvp_kda = ""
        mvp_damage = ""
        if mvp is not None and (
            mvp.damage > 0 or mvp.kills > 0 or mvp.assists > 0
        ):
            mvp.is_mvp = True
            mvp_name = mvp.name
            mvp_kda = f"{mvp.kills} / {mvp.deaths} / {mvp.assists}"
            mvp_damage = _fmt_num(mvp.damage)

        # Длительность матча: «Начало» из карточки матча (started_at
        # в match_channels) → момент публикации результата. Фолбэк —
        # created_at OCR-сессии (время загрузки скриншота).
        duration = "—"
        start_ts: float | None = None
        for _ch_id, _m in (tournament.match_channels or {}).items():
            if (
                _m.get("match_type") == match_type
                and _m.get("match_index") == match_index
                and _m.get("started_at")
            ):
                try:
                    start_ts = float(_m["started_at"])
                except (TypeError, ValueError):
                    start_ts = None
                break
        if start_ts is None:
            created_raw = session.get("created_at")
            if created_raw:
                try:
                    created = datetime.fromisoformat(str(created_raw))
                    start_ts = created.timestamp()
                except Exception:
                    start_ts = None
        if start_ts is not None:
            end_ts = session.get("result_sent_at")
            if not end_ts:
                end_ts = time.time()
            duration = _format_duration(end_ts - start_ts)

        # Счёт раундов: OCR «N VS M» идёт в порядке «левая:правая
        # панель» скриншота — привязываем к реальным командам через
        # panel-маппинг. «Команда A : Команда B» на карточке —
        # независимо от того, кто победил.
        # Ranked rooms-режим: итогового счёта раундов НЕТ — блок
        # показывает командные убийства с честной подписью и число
        # сыгранных комнат; счёт OCR в rooms не используется.
        rooms_count = len(session.get("rooms") or []) \
            if session.get("mode") == "rooms" else 0
        if session.get("mode") == "rooms":
            score_a, score_b = totals_a.kills, totals_b.kills
            score_label = (
                f"УБИЙСТВА · КОМНАТ: {rooms_count}"
                if rooms_count else "УБИЙСТВА"
            )
        else:
            from services.ocr_service import ordered_round_score

            ordered = ordered_round_score(session, t1, t2)
            score_a, score_b, score_label = _resolve_card_score(
                session, totals_a.kills, totals_b.kills,
                ordered_score=ordered,
            )
        if score_label == "СЧЁТ МАТЧА":
            win_score = score_a if winning_team_index == t1 else score_b
            lose_score = score_b if winning_team_index == t1 else score_a
            if win_score <= lose_score:
                if session.get("mode") == "rooms":
                    score_a, score_b = totals_a.kills, totals_b.kills
                    score_label = "УБИЙСТВА КОМАНД"
                else:
                    raise ValueError(
                        f"OCR score inconsistent with winner: "
                        f"{score_a}:{score_b} (team {winning_team_index} won)"
                    )

        # Реальный сквозной номер матча (тот же, что в embed запуска)
        match_num = await match_number_store.get_or_assign(match_id)

        data = MatchResultData(
            match_num=f"#{match_num}",
            duration=duration,
            score_a=score_a,
            score_b=score_b,
            score_label=score_label,
            winner="A" if winning_team_index == t1 else "B",
            team_a=cards_a,
            team_b=cards_b,
            totals_a=totals_a,
            totals_b=totals_b,
            mvp_name=mvp_name,
            mvp_kda=mvp_kda,
            mvp_damage=mvp_damage,
        )

        png = _RESULT_CARD_CACHE.get(match_id)
        if png is None:
            png = await render_match_result(data)
            _RESULT_CARD_CACHE[match_id] = png
            if len(_RESULT_CARD_CACHE) > _RESULT_CARD_CACHE_MAX:
                _RESULT_CARD_CACHE.pop(next(iter(_RESULT_CARD_CACHE)))

        channel = await _result_channel(guild, None)
        if channel is None:
            return None
        card = _result_card(
            tournament, match_type, match_index, t1, t2, match_num=match_num,
            duration=duration,
            rooms_count=rooms_count,
        )
        msg_id = session.get("result_message_id")

        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(
                    view=card,
                    attachments=[
                        discord.AttachmentInput(
                            io.BytesIO(png), filename=SHOT_FILENAME
                        )
                    ],
                )
                if not session.get("result_sent_at"):
                    session["result_sent_at"] = msg.created_at.timestamp()
                    store.set(tournament)
                return msg
            except Exception as e:
                logger.warning("Result card edit failed: %s", e)

        file = discord.File(io.BytesIO(png), filename=SHOT_FILENAME)
        bot_msg = await channel.send(view=card, file=file)
        session["result_message_id"] = bot_msg.id
        session["result_sent_at"] = bot_msg.created_at.timestamp()
        store.set(tournament)
        return bot_msg
    except Exception:
        logger.exception("Match result card publish failed")
        return None



class OCRUploadCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        # Сначала проверяем, не является ли канал выделенным каналом матча
        # (MM-флоу): скриншот в нём автоматически привязывается к матчу.
        # Pending-заявку читаем всегда: в матч-канале лежит и кнопка
        # «Неправильный скриншот» — без слияния флаг reupload теряется
        # и повторная загрузка отклоняется как «уже загружен скриншот».
        pending = pop_pending_upload(
            message.guild.id, message.channel.id, message.author.id
        )
        if pending is None:
            pending = pop_pending_upload_any(
                message.guild.id, message.author.id, message.channel.id
            )

        meta = None
        for t in store.get_by_guild(message.guild.id):
            mc = (getattr(t, "match_channels", None) or {}).get(
                str(message.channel.id)
            )
            if mc:
                meta = {
                    "match_type": mc["match_type"],
                    "match_index": mc["match_index"],
                    "tchannel": t.channel_id,
                    "match_channel": True,
                }
                # Флаги из pending того же матча (reupload/squad/tchannel)
                if pending and (
                    pending.get("match_type") == meta["match_type"]
                    and pending.get("match_index") == meta["match_index"]
                ):
                    for k in ("reupload", "squad_team", "squad_uid"):
                        if pending.get(k):
                            meta[k] = pending[k]
                    meta["tchannel"] = (
                        pending.get("tchannel") or meta["tchannel"]
                    )
                break

        # Затем точное совпадение канала, затем любой канал сервера —
        # скриншот можно прислать и в канал турнира, и в канал результатов
        if meta is None:
            meta = pending
        if not meta:
            return

        # Канал матча MM: скриншоты принимает только кнопка
        # «Отправить результат» (модалка → ModalUploadShim).
        # Обычные картинки в чате канала игнорируем.
        if meta.get("match_channel") and not isinstance(
            message, ModalUploadShim
        ):
            if message.attachments:
                await message.reply(
                    replace_emojis(
                        "📸 Скриншот результата отправляется кнопкой "
                        "«Отправить результат» — обычная картинка в чат "
                        "не запускает обработку."
                    )
                )
            return

        # Ждём именно изображение
        attachment = next(
            (
                a
                for a in message.attachments
                if (a.content_type or "").startswith("image/")
                or any(a.filename.lower().endswith(e) for e in IMAGE_EXTS)
            ),
            None,
        )
        if attachment is None:
            # Не спамим на обычные сообщения — отвечаем только если
            # в сообщении вообще есть вложения (но не картинка)
            if message.attachments:
                await message.reply(
                    replace_emojis(
                        "❌ Нужен скриншот — прикрепите изображение."
                    )
                )
            # Возвращаем pending, чтобы пользователь мог дослать картинку
            register_pending_upload(
                message.guild.id, message.channel.id, message.author.id,
                meta["match_type"], meta["match_index"],
                tchannel_id=meta.get("tchannel", message.channel.id),
                reupload=meta.get("reupload", False),
                squad_team=meta.get("squad_team"),
                squad_uid=meta.get("squad_uid"),
            )
            return

        if attachment.size and attachment.size > MAX_ATTACH:
            await message.reply(
                replace_emojis("❌ Файл слишком большой (макс. 10 МБ).")
            )
            return

        # Турнир ищем по каналу, откуда нажали кнопку (tchannel),
        # а не по каналу, куда прислали скриншот
        tchannel_id = meta.get("tchannel") or message.channel.id
        tournament = store.get(message.guild.id, tchannel_id)
        if not tournament:
            await message.reply(replace_emojis("❌ Турнир не найден."))
            return

        match_type = meta["match_type"]
        match_index = meta["match_index"]
        try:
            t1, t2 = _match_tuple(tournament, match_type, match_index)
        except Exception:
            await message.reply(replace_emojis("❌ Матч не найден."))
            return

        # Скриншот из выделенного канала матча — только капитаны/орги
        if meta.get("match_channel"):
            from views.ocr_match_view import _can_use_ocr

            if not _can_use_ocr(message.author, tournament, [t1, t2]):
                await message.reply(
                    replace_emojis(
                        "❌ Скриншот может отправить капитан команды "
                        "или организатор."
                    )
                )
                return

        key = _session_key(match_type, match_index)
        existing = tournament.ocr_sessions.get(key)
        session_active = bool(
            existing and existing.get("status") not in ("completed", "cancelled")
        )
        is_reupload = bool(meta.get("reupload")) and session_active

        # Ranked MM classic: замену комплекта («Неправильный
        # скриншот») может выполнить только его отправитель, капитан
        # зафиксированной команды-победителя или администратор —
        # чужой капитан не может перехватить загрузку и сбросить
        # сопоставление. Squad/rooms имеют собственные правила.
        if (
            is_reupload
            and getattr(tournament, "mm_search_channel_id", None)
            and existing.get("mode") not in ("squad", "rooms")
            and not _mm_upload_allowed(existing, tournament, message.author)
        ):
            await message.reply(
                replace_emojis(
                    "❌ Этот комплект может заменить только его "
                    "отправитель, капитан команды-победителя "
                    "или администратор."
                )
            )
            return

        # --- Мультикомнатный сбор (OCR V2) -------------------------------
        # Комнаты заполняются ЯВНО по номеру через кнопку «Добавить
        # скриншоты» на сообщении прогресса — картинка в чат без
        # указания комнаты не принимается (иначе порядок файлов
        # неоднозначен).
        if (
            existing
            and existing.get("mode") == "rooms"
            and existing.get("status") == "collecting"
        ):
            await message.reply(
                replace_emojis(
                    "📸 Скриншоты загружаются кнопкой «Добавить "
                    "скриншоты» под сообщением прогресса — там указывается "
                    "номер комнаты."
                )
            )
            return

        # Новый скриншот не должен перезаписывать активную сессию —
        # только явная кнопка «Неправильный скриншот» (reupload=True).
        # Исключения: squad-режим — второй капитан загружает свой
        # отряд; pending-клейм — его владелец может повторно отправить
        # файл (напр. после рестарта бота во время OCR).
        if session_active and not is_reupload:
            if _is_pending_claim_owner(existing, message.author.id):
                pass  # владелец клейма — продолжаем как обычно
            elif existing.get("mode") == "squad":
                loaded = len(existing.get("squads", {})) + len(
                    existing.get("squad_pool", [])
                )
                if loaded >= 2:
                    await message.reply(
                        replace_emojis(
                            "⚠️ Скриншоты обоих отрядов уже загружены — "
                            "используйте «Неправильный скриншот» для замены."
                        )
                    )
                    return
            else:
                await message.reply(
                    replace_emojis(
                        "⚠️ Для этого матча уже загружен скриншот — "
                        "завершите сопоставление, нажмите «Неправильный "
                        "скриншот» или вызовите организатора."
                    )
                )
                return

        # Ranked MM: атомарно закрепляем право на новый комплект ДО
        # длительного OCR — победитель = команда первого ПРИНЯТОГО
        # файла, а не первого завершившего распознавание. Пустой
        # pending-клейм снимается при ошибке OCR
        # (_release_pending_upload_claim), админ-отмена работает как
        # обычно (статус awaiting_ocr не терминальный).
        if (
            getattr(tournament, "mm_search_channel_id", None)
            and not session_active
        ):
            async with _room_lock(key, getattr(tournament, "id", "") or ""):
                tournament = (
                    store.get(message.guild.id, tchannel_id) or tournament
                )
                cur = tournament.ocr_sessions.get(key)
                cur_active = bool(
                    cur
                    and cur.get("status")
                    not in ("completed", "cancelled", "finalizing")
                )
                if not cur_active:
                    tournament.ocr_sessions[key] = {
                        "match_type": match_type,
                        "match_index": match_index,
                        "status": "awaiting_ocr",
                        "mode": "pending",
                        "teams": [t1, t2],
                        "panels": {},
                        "squads": {},
                        "squad_pool": [],
                        "maps": {},
                        "panel_picks": {},
                        "uploaded_by": message.author.id,
                        "uploaded_by_name": message.author.display_name,
                        "session_id": uuid.uuid4().hex[:16],
                        "match_id": (
                            f"{getattr(tournament, 'id', '?')}_{key}"
                        ),
                        "guild_id": message.guild.id,
                        "winner_pending": False,
                        "winning_team_index": _mm_winner_for_uploader(
                            tournament, t1, t2, message.author.id
                        ),
                        "created_at": datetime.now().isoformat(),
                        "gen": 1,
                        "ocr_pending": True,
                    }
                    store.set(tournament)
                    existing = tournament.ocr_sessions[key]
                    session_active = True
                    is_reupload = False

        stats_channel = await _stats_channel(message, tournament)

        # Сразу подтверждаем приём в канале матча — OCR идёт долго,
        # без сообщения игроки думают, что скриншот не дошёл, и шлют дубли.
        try:
            await message.channel.send(
                f"{message.author.mention} отправил скриншот — "
                "ожидайте обработки результата."
            )
        except Exception:
            logger.debug("upload ack send failed", exc_info=True)

        try:
            data = await attachment.read()
            from services.ocr_service import (
                OCR_EXECUTOR,
                detect_winner_side,
            )

            loop = asyncio.get_running_loop()
            # Fast path: anchor-based OCR двух таблиц CS 4×4 —
            # parsed в формате cs_parser или None → полный OCR.
            parsed = await _fast_parse_cs(data)
            boxes = None
            if parsed is not None:
                # Боксы fast-пути — для score-fallback и
                # winner-диагностики (полный det+rec не нужен).
                boxes = parsed.pop("_rec_boxes", []) or []
            else:
                boxes = await loop.run_in_executor(
                    OCR_EXECUTOR, _ocr_boxes, data
                )
            if not boxes:
                async with _room_lock(key, getattr(tournament, "id", "") or ""):
                    tournament = (
                        store.get(message.guild.id, tchannel_id)
                        or tournament
                    )
                    _release_pending_upload_claim(
                        tournament, key, message.author.id
                    )
                await message.reply(
                    replace_emojis(
                        "❌ Не удалось распознать таблицу результатов. "
                        "Убедитесь, что это экран CS 4x4."
                    )
                )
                return

            # За время OCR сессия могла быть финализирована, отменена
            # или пересоздана — не перезаписываем терминальное состояние.
            tournament = store.get(message.guild.id, tchannel_id)
            cur = (
                tournament.ocr_sessions.get(key)
                if tournament else None
            )
            if (cur or {}).get("status") in (
                "completed", "cancelled", "finalizing"
            ):
                await message.reply(
                    replace_emojis(
                        "⚠️ Результат этого матча уже финализирован "
                        "или отменён — новый комплект не принимается."
                    )
                )
                return

            # Сначала пробуем детальный экран одного отряда
            if parsed is None:
                squad = await loop.run_in_executor(
                    OCR_EXECUTOR, _parse_squad, boxes
                )
                if squad is not None:
                    # _handle_squad_upload сам держит _room_lock только на
                    # время мутаций сессии — Discord API выполняется вне
                    # лока, чтобы не блокировать финализацию/отмену.
                    await self._handle_squad_upload(
                        message, tournament, t1, t2, meta,
                        match_type, match_index, key, existing,
                        session_active, is_reupload, stats_channel,
                        data, squad,
                    )
                    return

            # Классический двух-панельный экран — squad-сессия
            # классический скриншот не принимает (обе ветки: fast
            # и полный OCR).
            if session_active and existing.get("mode") == "squad":
                await message.reply(
                    replace_emojis(
                        "⚠️ Для этого матча уже загружен отряд-скриншот "
                        "(squad-режим). Классический двухкомандный экран "
                        "сюда не подойдёт — замените отряд через "
                        "«Неправильный скриншот»."
                    )
                )
                return

            if parsed is None:
                parsed = await loop.run_in_executor(
                    OCR_EXECUTOR, _parse_classic, boxes
                )
            if parsed is None:
                async with _room_lock(key, getattr(tournament, "id", "") or ""):
                    tournament = (
                        store.get(message.guild.id, tchannel_id)
                        or tournament
                    )
                    _release_pending_upload_claim(
                        tournament, key, message.author.id
                    )
                await message.reply(
                    replace_emojis(
                        "❌ Не удалось распознать таблицу результатов. "
                        "Убедитесь, что это экран CS 4x4 «Битва отрядов»."
                    )
                )
                return

            # Счёт «N VS M» мог не распознаться — повторный OCR полосы
            if parsed.get("score") is None:
                from services.ocr_service import ocr_score_fallback

                score = await loop.run_in_executor(
                    OCR_EXECUTOR, ocr_score_fallback, data, boxes
                )
                if score:
                    parsed["score"] = list(score)

            # Победитель — ЛЕВАЯ панель скриншота: результат отправляет
            # капитан победившей команды, победившая всегда слева.
            # Цветовая детекция оставлена только для диагностики —
            # итог не зависит от неё (kills/damage/KDA не решают).
            detected = await loop.run_in_executor(
                OCR_EXECUTOR, detect_winner_side, data, boxes
            )
            if detected and detected != "left":
                logger.info(
                    "winner_side: color-detect=%s overridden -> left",
                    detected,
                )
            side = "left"

            panels = {
                side_name: {
                    "name": parsed.get(name_key),
                    "rows": [
                        {
                            "name": p["name"],
                            "secondary_text": p.get("secondary_text"),
                            "kills": p["kills"],
                            "deaths": p["deaths"],
                            "assists": p["assists"],
                            # None = «не распознано» — не превращаем
                            # в 0: финализация требует все поля.
                            "dmg": p.get("dmg"),
                        }
                        for p in parsed[players_key]
                    ],
                }
                for side_name, name_key, players_key in (
                    ("left", "team_a_name", "team_a"),
                    ("right", "team_b_name", "team_b"),
                )
            }

            # Нормализуем имена из основного парсера тоже — OCR может
            # выдать «ИНЖИТИХ»/«ижинитх» до cyrillic-добива
            for side_name in ("left", "right"):
                nm = panels[side_name].get("name")
                if nm:
                    panels[side_name]["name"] = _normalize_team_name(nm)

            # Названия команд на экране кириллические («ХИЩНИКИ»), а
            # основной rec-чарсет кириллицу не знает — перепознаём
            # область названия отдельной cyrillic-моделью.
            from services.ocr_service import ocr_cyrillic_region

            for side_name, box_key in (
                ("left", "team_a_name_box"),
                ("right", "team_b_name_box"),
            ):
                region = parsed.get(box_key)
                if not region:
                    continue
                cname = await ocr_cyrillic_region(data, region)
                if cname:
                    panels[side_name]["name"] = _normalize_team_name(cname)

            parsed["winner"] = {"left": "a", "right": "b"}.get(side)

            # _room_lock: создание/замена classic-сессии атомарно
            # относительно параллельных загрузок, финализации и
            # административной отмены — «первый принятый файл»
            # определяет победителя без гонки двух капитанов.
            # Discord API (ответы, публикация, карточка) — ПОСЛЕ
            # освобождения лока.
            reply_text = None
            plan = None  # ("reupload"|"new", session)
            async with _room_lock(key, getattr(tournament, "id", "") or ""):
                # Состояние могло измениться за время OCR —
                # перечитываем актуальное под локом.
                tournament = (
                    store.get(message.guild.id, tchannel_id) or tournament
                )
                existing = tournament.ocr_sessions.get(key)
                session_active = bool(
                    existing
                    and existing.get("status")
                    not in ("completed", "cancelled", "finalizing")
                )
                is_reupload = bool(meta.get("reupload")) and session_active
                pending_mine = _is_pending_claim_owner(
                    existing, getattr(message.author, "id", "")
                )
                if session_active and not is_reupload and not pending_mine:
                    if existing.get("mode") == "squad":
                        reply_text = (
                            "⚠️ Для этого матча уже загружен "
                            "отряд-скриншот (squad-режим). "
                            "Классический двухкомандный экран "
                            "сюда не подойдёт — замените отряд "
                            "через «Неправильный скриншот»."
                        )
                    else:
                        reply_text = (
                            "⚠️ Для этого матча уже загружен "
                            "скриншот — завершите сопоставление, "
                            "нажмите «Неправильный скриншот» или "
                            "вызовите организатора."
                        )
                elif is_reupload and existing.get("mode") == "squad":
                    reply_text = (
                        "⚠️ Для этого матча загружен "
                        "отряд-скриншот (squad-режим) — "
                        "классический скриншот его не заменит."
                    )
                elif is_reupload and existing.get("message_id"):
                    # Обновляем то же сообщение: новый скриншот +
                    # сброс маппинга (публикация — после лока).
                    existing.update(
                        {
                            "status": "awaiting_mapping",
                            "panels": panels,
                            "winner_side": side,
                            "banner": parsed.get("banner"),
                            "score": parsed.get("score"),
                            "top5": parsed.get("top5", False),
                            "uploaded_by": message.author.id,
                            "uploaded_by_name": message.author.display_name,
                            "results_channel_id": stats_channel.id,
                            "maps": {}, "panel_picks": {},
                            "reuploaded_at": datetime.now().isoformat(),
                        }
                    )
                    store.set(tournament)
                    plan = ("reupload", existing)
                elif session_active and not pending_mine:
                    # Активная сессия без message_id (reupload до
                    # публикации или гонка) — перезаписывать её
                    # новой нельзя: mappings/статистика пропадут.
                    reply_text = (
                        "⚠️ Для этого матча уже идёт обработка "
                        "скриншота — дождитесь завершения или "
                        "вызовите организатора."
                    )
                else:
                    # Новая сессия (pending-клейм этого же файла
                    # перезаписывается полной).
                    tournament.ocr_sessions[key] = {
                        "match_type": match_type,
                        "match_index": match_index,
                        "status": "awaiting_mapping",
                        "teams": [t1, t2],
                        "panels": panels,
                        "winner_side": side,
                        "banner": parsed.get("banner"),
                        "score": parsed.get("score"),
                        "top5": parsed.get("top5", False),
                        "uploaded_by": message.author.id,
                        "uploaded_by_name": message.author.display_name,
                        # Ranked MM: файл уже принят — победитель
                        # фиксируется сразу по Discord ID
                        # отправителя (OCR-баннер не определяет
                        # победителя). Для обычных турниров поле
                        # не используется — победитель по OCR.
                        "session_id": uuid.uuid4().hex[:16],
                        "match_id": (
                            f"{getattr(tournament, 'id', '?')}_{key}"
                        ),
                        "guild_id": message.guild.id,
                        "winner_pending": False,
                        "winning_team_index": _mm_winner_for_uploader(
                            tournament, t1, t2, message.author.id
                        ),
                        "created_at": datetime.now().isoformat(),
                        "results_channel_id": stats_channel.id,
                        "maps": {}, "panel_picks": {},
                    }
                    store.set(tournament)
                    plan = ("new", tournament.ocr_sessions[key])

            if reply_text is not None:
                await message.reply(replace_emojis(reply_text))
                return
            if plan is None:
                return

            kind, session = plan
            view = OCRPublicView(
                message.guild.id, tournament.channel_id, tournament,
                match_type, match_index,
            )
            caption = _mapping_caption(tournament, session)
            # message_id, который может быть переиспользован
            # edit-путём — цель для нейтрализации при отмене сессии
            # во время Discord API-вызова.
            target_mid = session.get("message_id")
            new_mid = None
            if kind == "reupload":
                try:
                    await _update_results_message(
                        stats_channel, session["message_id"],
                        data, view, caption=caption,
                    )
                    await message.add_reaction("✅")
                except Exception as e:
                    logger.warning("OCR reupload edit failed: %s", e)
                    bot_msg = await _send_results_message(
                        stats_channel, data, view, caption=caption
                    )
                    new_mid = bot_msg.id
            else:
                bot_msg = await _send_results_message(
                    stats_channel, data, view, caption=caption
                )
                new_mid = bot_msg.id

            # writeback message_id под локом — только если сессия
            # всё ещё та же (не отменена/не пересоздана). Если
            # сессия умерла во время Discord API — гасим
            # устаревшее сообщение, чтобы кнопки не остались
            # активными.
            stale_mid = None
            async with _room_lock(key, getattr(tournament, "id", "") or ""):
                cur = store.get(message.guild.id, tchannel_id)
                cur_s = (
                    cur.ocr_sessions.get(key) if cur else None
                )
                if cur_s is session:
                    if new_mid is not None:
                        session["message_id"] = new_mid
                        store.set(cur)
                else:
                    stale_mid = new_mid or target_mid
            if stale_mid is not None:
                await _neutralize_stale_ocr_message(
                    stats_channel, stale_mid
                )

            # Карточка матча в cs-4x4-match-N → «идёт сопоставление»
            # (кнопка «Отправить результат» исчезает из карточки)
            try:
                from services.match_announcer import update_match_card

                await update_match_card(
                    message.guild, tournament, match_type, match_index,
                    state="processing",
                )
            except Exception as exc:
                logger.warning("Match card processing update failed: %s", exc)
        except Exception:
            logger.exception("OCR upload processing failed")
            async with _room_lock(key, getattr(tournament, "id", "") or ""):
                tournament = (
                    store.get(message.guild.id, tchannel_id) or tournament
                )
                _release_pending_upload_claim(
                    tournament, key, message.author.id
                )
            await message.reply(
                replace_emojis("❌ Ошибка при обработке скриншота.")
            )

    # ------------------------------------------------------------------
    # Squad-режим: детальный экран одного отряда
    # ------------------------------------------------------------------

    async def _publish_squad_message(
        self,
        stats_channel: discord.TextChannel,
        tournament,
        message: discord.Message,
        data: bytes,
        view,
        entry: dict[str, Any],
        match_type: str = "",
        match_index: int = 0,
        t1: int = 0,
        t2: int = 0,
    ) -> None:
        """Отправить или обновить сообщение со статой одного отряда."""
        # Карточка матча в cs-4x4-match-N → «идёт сопоставление»
        if match_type:
            try:
                from services.match_announcer import update_match_card

                await update_match_card(
                    message.guild, tournament, match_type, match_index,
                    state="processing",
                )
            except Exception as exc:
                logger.warning("Match card processing update failed: %s", exc)
        if entry.get("message_id"):
            try:
                msg = await stats_channel.fetch_message(
                    entry["message_id"]
                )
                await msg.edit(
                    view=_mapping_layout(view, _SQUAD_MAP_CAPTION),
                    attachments=[
                        discord.AttachmentInput(
                            io.BytesIO(data), filename=SHOT_FILENAME
                        )
                    ],
                )
                await message.add_reaction("✅")
                return
            except Exception as e:
                logger.warning("Squad message edit failed: %s", e)

        file = discord.File(io.BytesIO(data), filename=SHOT_FILENAME)
        bot_msg = await stats_channel.send(
            view=_mapping_layout(view, _SQUAD_MAP_CAPTION),
            file=file,
        )
        # message_id пишется вызывающим под _room_lock — здесь
        # только Discord API, без мутации состояния.
        return bot_msg.id

    async def _handle_squad_upload(
        self,
        message: discord.Message,
        tournament,
        t1: int,
        t2: int,
        meta: dict[str, Any],
        match_type: str,
        match_index: int,
        key: str,
        existing: dict | None,
        session_active: bool,
        is_reupload: bool,
        stats_channel: discord.TextChannel,
        data: bytes,
        squad: dict[str, Any],
    ) -> None:
        """Обработать скриншот детального экрана одного отряда.

        Мутации сессии — под _room_lock; Discord API (reply,
        публикация результата, карточка матча) — ПОСЛЕ
        освобождения лока: медленный или сбойный Discord не
        блокирует финализацию и административную отмену.
        """
        guild_id = message.guild.id
        tchannel_id = meta.get("tchannel") or message.channel.id
        reply_text = None
        publish = None  # (view, entry)
        session = None

        async with _room_lock(key, getattr(tournament, "id", "") or ""):
            # Состояние могло измениться за время OCR —
            # перечитываем актуальное под локом.
            tournament = store.get(guild_id, tchannel_id) or tournament
            existing = tournament.ocr_sessions.get(key)
            session_active = bool(
                existing
                and existing.get("status")
                not in ("completed", "cancelled", "finalizing")
            )
            is_reupload = bool(meta.get("reupload")) and session_active
            pending_mine = _is_pending_claim_owner(
                existing, getattr(message.author, "id", "")
            )
            eff_mode = existing.get("mode") if session_active else None

            squad_team = meta.get("squad_team")
            squad_uid = meta.get("squad_uid")

            if (
                not is_reupload
                and squad_team is not None
                and squad_team not in (t1, t2)
            ):
                reply_text = "❌ Эта команда не играет в матче."
            elif session_active and eff_mode not in ("squad", "pending"):
                reply_text = (
                    "⚠️ Для этого матча уже загружен скриншот другого "
                    "типа результатов — дождитесь завершения или вызовите "
                    "организатора."
                )
            elif (
                session_active
                and eff_mode == "pending"
                and not pending_mine
            ):
                reply_text = (
                    "⚠️ Для этого матча уже идёт обработка скриншота — "
                    "дождитесь завершения или вызовите организатора."
                )
            else:
                if session_active:
                    session = existing
                    session.setdefault("squads", {})
                    session.setdefault("squad_pool", [])
                    session.setdefault("panels", {})
                    session.setdefault("maps", {})
                    # Мой pending-клейм (первый принятый файл ещё
                    # распознаётся) → заполняем его как squad-сессию.
                    if session.get("status") == "awaiting_ocr":
                        session["mode"] = "squad"
                        session["status"] = "awaiting_mapping"
                else:
                    session = {
                        "match_type": match_type,
                        "match_index": match_index,
                        "status": "awaiting_mapping",
                        "mode": "squad",
                        "teams": [t1, t2],
                        "panels": {},
                        "squads": {},
                        "squad_pool": [],
                        "uploaded_by": message.author.id,
                        "uploaded_by_name": message.author.display_name,
                        # Ranked MM: победитель = команда капитана
                        # ПЕРВОГО принятого squad-файла; загрузка
                        # второго отряда его не меняет.
                        "session_id": uuid.uuid4().hex[:16],
                        "match_id": (
                            f"{getattr(tournament, 'id', '?')}_{key}"
                        ),
                        "guild_id": message.guild.id,
                        "winner_pending": False,
                        "winning_team_index": _mm_winner_for_uploader(
                            tournament, t1, t2, message.author.id
                        ),
                        "created_at": datetime.now().isoformat(),
                        "results_channel_id": stats_channel.id,
                        "maps": {}, "panel_picks": {},
                    }
                    tournament.ocr_sessions[key] = session

                # --- Повторная загрузка по «Неправильный скриншот» ---
                if is_reupload and squad_team is not None:
                    tkey = str(squad_team)
                    sentry = session["squads"].get(tkey)
                    if sentry is None:
                        reply_text = "❌ Отряд для этой команды не найден."
                    else:
                        sentry["rows"] = squad["players"]
                        sentry["banner"] = squad.get("banner")
                        sentry["reuploaded_at"] = (
                            datetime.now().isoformat()
                        )
                        panel = session.get("panels", {}).get(
                            f"squad_{squad_team}"
                        )
                        if panel is not None:
                            panel["rows"] = squad["players"]
                        session.get("maps", {}).pop(tkey, None)
                        if len(session.get("maps", {})) < 2:
                            session["status"] = (
                                "mapping"
                                if session.get("squads")
                                else "awaiting_mapping"
                            )
                        store.set(tournament)
                        publish = (
                            SquadAssignedView(
                                guild_id, tournament.channel_id,
                                tournament, match_type, match_index,
                                squad_team,
                            ),
                            sentry,
                        )
                elif is_reupload and squad_uid:
                    entry = next(
                        (
                            e for e in session["squad_pool"]
                            if e.get("uid") == squad_uid
                        ),
                        None,
                    )
                    if entry is None:
                        reply_text = "❌ Скриншот отряда не найден."
                    else:
                        entry["rows"] = squad["players"]
                        entry["banner"] = squad.get("banner")
                        entry["reuploaded_at"] = (
                            datetime.now().isoformat()
                        )
                        store.set(tournament)
                        publish = (
                            SquadClaimView(
                                guild_id, tournament.channel_id,
                                tournament, match_type, match_index,
                                squad_uid,
                            ),
                            entry,
                        )
                elif not is_reupload and squad_team is not None:
                    # --- Автоназначение: кнопка капитана команды ---
                    tkey = str(squad_team)
                    if tkey in session.get("squads", {}):
                        reply_text = (
                            "⚠️ У этой команды уже загружен отряд — "
                            "замените его кнопкой "
                            "«Неправильный скриншот»."
                        )
                    else:
                        entry = {
                            "rows": squad["players"],
                            "banner": squad.get("banner"),
                            "uploaded_by": message.author.id,
                            "uploaded_by_name": (
                                message.author.display_name
                            ),
                            "created_at": datetime.now().isoformat(),
                            "message_id": None,
                        }
                        session["squads"][tkey] = entry
                        session["panels"][f"squad_{squad_team}"] = {
                            "name": _team_display_name(
                                tournament, squad_team
                            ),
                            "rows": squad["players"],
                        }
                        if len(session.get("squads", {})) >= 2:
                            session["status"] = "mapping"
                        store.set(tournament)
                        publish = (
                            SquadAssignedView(
                                guild_id, tournament.channel_id,
                                tournament, match_type, match_index,
                                squad_team,
                            ),
                            entry,
                        )
                else:
                    # --- Новая загрузка отряда (без привязки) ---
                    loaded = len(session.get("squads", {})) + len(
                        session.get("squad_pool", [])
                    )
                    if loaded >= 2:
                        reply_text = (
                            "⚠️ Скриншоты обоих отрядов уже загружены — "
                            "используйте «Неправильный скриншот» "
                            "для замены."
                        )
                    else:
                        uid = uuid.uuid4().hex[:10]
                        entry = {
                            "uid": uid,
                            "rows": squad["players"],
                            "banner": squad.get("banner"),
                            "uploaded_by": message.author.id,
                            "uploaded_by_name": (
                                message.author.display_name
                            ),
                            "created_at": datetime.now().isoformat(),
                            "message_id": None,
                        }
                        session["squad_pool"].append(entry)
                        if len(session.get("squads", {})) >= 2:
                            session["status"] = "mapping"
                        store.set(tournament)
                        publish = (
                            SquadClaimView(
                                guild_id, tournament.channel_id,
                                tournament, match_type, match_index, uid,
                            ),
                            entry,
                        )

                # Пустой pending-клейм после неудачной обработки —
                # освобождаем, чтобы не блокировать других капитанов.
                if (
                    session.get("status") == "awaiting_ocr"
                    and not session.get("squads")
                    and not session.get("squad_pool")
                    and not session.get("maps")
                    and not session.get("panels")
                ):
                    tournament.ocr_sessions.pop(key, None)
                    store.set(tournament)
                    session = None
                    publish = None

        # --- Discord API вне _room_lock ---
        if reply_text is not None:
            await message.reply(replace_emojis(reply_text))
            return
        if publish is None:
            return
        view, entry = publish
        # message_id, который мог быть переиспользован edit-путём
        # внутри _publish_squad_message — цель для нейтрализации,
        # если сессия была отменена во время Discord API-вызова.
        target_mid = entry.get("message_id")
        new_mid = await self._publish_squad_message(
            stats_channel, tournament, message, data,
            view, entry,
            match_type=match_type, match_index=match_index,
            t1=t1, t2=t2,
        )
        # writeback message_id — короткая мутация под локом,
        # только если сессия всё ещё та же (не отменена/не
        # пересоздана параллельным комплектом).
        stale_mid = None
        async with _room_lock(key, getattr(tournament, "id", "") or ""):
            cur = store.get(guild_id, tchannel_id) or tournament
            cur_s = cur.ocr_sessions.get(key)
            if cur_s is session:
                if new_mid is not None and entry.get("message_id") is None:
                    entry["message_id"] = new_mid
                    store.set(cur)
            else:
                # Сессия отменена/заменена, пока Discord отвечал —
                # не сохраняем message_id и гасим устаревшее
                # сообщение (view → None, кнопки неактивны).
                stale_mid = new_mid or target_mid
        if stale_mid is not None:
            await _neutralize_stale_ocr_message(
                stats_channel, stale_mid
            )


# ---------------------------------------------------------------------------
# Мультикомнатный режим (OCR V2): 1–10 скриншотов на один матч
# ---------------------------------------------------------------------------

# Локи на матч для атомарного приёма комнат и финализации
_ROOM_LOCKS: dict[str, asyncio.Lock] = {}
_ROOM_LOCKS_MAX = 200

# Кэш OCR по хэшу файла — одинаковый скриншот не гоняем дважды
_OCR_BOX_CACHE: dict[str, list] = {}
_OCR_BOX_CACHE_MAX = 100


def _room_lock(match_key: str, tournament_id: str = "") -> asyncio.Lock:
    """Per-match лок. Ключ включает tournament_id — матчи разных
    турниров не блокируют друг друга (Tournaments 2.0 §8)."""
    scoped = f"{tournament_id}|{match_key}" if tournament_id else match_key
    lock = _ROOM_LOCKS.get(scoped)
    if lock is None:
        from utils.locks import TrackedLock
        lock = TrackedLock()
        _ROOM_LOCKS[scoped] = lock
        _evict_idle_locks(_ROOM_LOCKS, _ROOM_LOCKS_MAX)
    return lock


def _evict_idle_locks(store: dict, max_size: int) -> None:
    """Вытеснить только по-настоящему СВОБОДНЫЕ локи: не удерживаемые
    и без ожидающих acquire-задач (TrackedLock._pending). Занятый
    или ожидающий лок нельзя удалять — иначе для того же ключа
    создастся второй объект лока и сериализация сломается (P0)."""
    from utils.locks import lock_is_idle
    if len(store) <= max_size:
        return
    for k in list(store.keys()):
        if len(store) <= max_size:
            break
        lock = store.get(k)
        if lock_is_idle(lock):
            del store[k]


async def _ocr_boxes_cached(data: bytes) -> list:
    """OCR с дедупликацией по sha256 содержимого."""
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    cached = _OCR_BOX_CACHE.get(digest)
    if cached is not None:
        return cached
    from services.ocr_service import OCR_EXECUTOR

    loop = asyncio.get_running_loop()
    import time as _time

    from utils import perf_metrics

    _t0 = _time.perf_counter()
    try:
        boxes = await loop.run_in_executor(OCR_EXECUTOR, _ocr_boxes, data)
    finally:
        perf_metrics.record(
            "ocr_boxes_ms", (_time.perf_counter() - _t0) * 1000.0
        )
        perf_metrics.incr("ocr_runs")
    _OCR_BOX_CACHE[digest] = boxes
    if len(_OCR_BOX_CACHE) > _OCR_BOX_CACHE_MAX:
        _OCR_BOX_CACHE.pop(next(iter(_OCR_BOX_CACHE)))
    return boxes


async def _fast_parse_cs(data: bytes, *, enabled: bool = True):
    """Anchor-based fast OCR → parsed dict (формат cs_parser) или None.

    None — fast-путь не применим/не прошёл валидацию → вызывать
    полный RapidOCR pipeline. Отключается env OCR_FAST_ENABLED=0
    или параметром enabled=False (диагностика /ocr mode=full).
    """
    import os

    if not enabled or os.environ.get("OCR_FAST_ENABLED", "1") != "1":
        return None
    try:
        from services import fast_cs_ocr

        return await fast_cs_ocr.parse_cs_fast(data)
    except Exception:
        logger.debug("fast CS OCR failed, fallback to full OCR", exc_info=True)
        return None


async def _refine_parsed_rows(data: bytes, parsed: dict) -> None:
    """Targeted re-OCR строк с пропущенными полями (ник/K/D/A/DMG).

    Полный кадр повторно не распознаём — вырезаем только полосу строки
    и прогоняем OCR по ней. Заполняем ТОЛЬКО отсутствующие значения:
    распознанное основной моделью не перезаписываем.
    """
    from services import cs_parser
    from services.ocr_service import ocr_image_region_async
    from PIL import Image

    try:
        img_w, img_h = Image.open(io.BytesIO(data)).size
    except Exception:
        return
    boundary = float(parsed.get("boundary_x") or img_w / 2)

    for side, players_key in (("left", "team_a"), ("right", "team_b")):
        x_lo = 0.0 if side == "left" else boundary
        x_hi = boundary if side == "left" else float(img_w)
        for p in parsed.get(players_key) or []:
            needs_name = p.get("name") in (None, "", "???")
            needs_kda = p.get("kills") is None
            if not (needs_name or needs_kda):
                continue
            cy = float(p.get("cy") or 0)
            rh = float(p.get("row_h") or 40)
            if cy <= 0:
                continue
            region = (
                x_lo, max(0.0, cy - rh * 0.75),
                x_hi, min(float(img_h), cy + rh * 0.75),
            )
            try:
                sub = await ocr_image_region_async(data, region)
            except Exception:
                continue
            if not sub:
                continue
            fields = cs_parser.parse_row_fields(sub)
            if needs_name and fields.get("name"):
                p["name"] = fields["name"][:32]
            if needs_kda and fields.get("kills") is not None:
                p["kills"] = fields["kills"]
                p["deaths"] = fields.get("deaths")
                p["assists"] = fields.get("assists")
                p["dmg"] = fields.get("dmg", p.get("dmg"))
                if p.get("deaths"):
                    p["kd"] = round(p["kills"] / p["deaths"], 2)


async def _classic_pipeline(
    data: bytes, need_score: bool = True, fast: bool = True
) -> dict | None:
    """Общий классический пайплайн: OCR → parse → score fallback →
    winner detect → cyrillic names → refine строк. Возвращает
    {"panels","score","banner","top5","winner_side"} или None.

    need_score=False (rooms/Ranked): счёт раундов в итогах не
    используется — пропускаем лишний OCR-проход шапки.
    fast=False — только полный RapidOCR (диагностика /ocr mode=full).
    """
    from services.ocr_service import (
        OCR_EXECUTOR,
        detect_winner_side,
        ocr_cyrillic_region,
        ocr_score_fallback,
    )

    loop = asyncio.get_running_loop()

    # Fast path: anchor-based OCR (две таблицы ИМЯ|K/D/A|DMG).
    # Возвращает parsed в формате cs_parser или None → полный OCR.
    parsed = await _fast_parse_cs(data, enabled=fast)
    boxes = None
    if parsed is not None:
        # Распознанные боксы fast-пути — для score-fallback и
        # winner-диагностики (полный det+rec не нужен).
        boxes = parsed.pop("_rec_boxes", []) or []
    else:
        boxes = await _ocr_boxes_cached(data)
        if not boxes:
            return None

        # Сначала пробуем детальный экран одного отряда — rooms-режим
        # его не поддерживает (нужен двухпанельный экран).
        from services.cs_squad_parser import parse_squad_results

        squad = await loop.run_in_executor(OCR_EXECUTOR, _parse_squad, boxes)
        if squad is not None:
            return {"squad": squad}

        parsed = await loop.run_in_executor(OCR_EXECUTOR, _parse_classic, boxes)
        if parsed is None:
            return None

    if need_score and parsed.get("score") is None:
        score = await loop.run_in_executor(
            OCR_EXECUTOR, ocr_score_fallback, data, boxes
        )
        if score:
            parsed["score"] = list(score)

    await _refine_parsed_rows(data, parsed)

    panels = {
        side_name: {
            "name": parsed.get(name_key),
            "rows": [
                {
                    "name": p["name"],
                    "secondary_text": p.get("secondary_text"),
                    "kills": p["kills"],
                    "deaths": p["deaths"],
                    "assists": p["assists"],
                    # None = «не распознано» — не превращаем в 0.
                    "dmg": p.get("dmg"),
                }
                for p in parsed[players_key]
            ],
        }
        for side_name, name_key, players_key in (
            ("left", "team_a_name", "team_a"),
            ("right", "team_b_name", "team_b"),
        )
    }
    for side_name in ("left", "right"):
        nm = panels[side_name].get("name")
        if nm:
            panels[side_name]["name"] = _normalize_team_name(nm)

    for side_name, box_key in (
        ("left", "team_a_name_box"),
        ("right", "team_b_name_box"),
    ):
        region = parsed.get(box_key)
        if not region:
            continue
        cname = await ocr_cyrillic_region(data, region)
        if cname:
            panels[side_name]["name"] = _normalize_team_name(cname)

    return {
        "panels": panels,
        "score": parsed.get("score"),
        "banner": parsed.get("banner"),
        "top5": parsed.get("top5", False),
        "winner_side": "left",
        "sha": None,  # заполняется вызывающим
    }


async def _update_progress_message(
    channel, message_id: int, text: str, view=None
) -> None:
    """Обновить сообщение прогресса загрузки (одно, без спама)."""
    if not message_id:
        return
    try:
        msg = await channel.fetch_message(message_id)
        await msg.edit(content=text, view=view)
    except Exception as e:
        logger.warning("Progress message edit failed: %s", e)


async def _publish_room_message(
    stats_channel, tournament, message, items: list[dict],
    room: dict, match_type: str, match_index: int,
) -> None:
    """Сообщение комнаты: скриншот(ы) + кнопки сопоставления (RoomView).

    Идемпотентно: повторный вызов при уже сохранённом message_id
    ничего не отправляет — защита от дублей при частичном сбое
    (send успешен, store.set упал).
    """
    from views.ocr_match_view import RoomView

    if room.get("message_id"):
        return

    files = [
        discord.File(
            io.BytesIO(it["data"]),
            filename=f"room_{room['room_index']}_{i}{_img_ext(it['filename'])}",
        )
        for i, it in enumerate(items)
    ]
    view = RoomView(
        message.guild.id, tournament.channel_id, tournament,
        match_type, match_index, room["room_index"],
    )
    players_n = sum(
        len(p.get("rows") or [])
        for p in (room.get("panels") or {}).values()
    )
    kind_label = (
        "CS 4×4" if room.get("kind") in (None, "classic")
        else str(room.get("kind")).upper()
    )
    lines = [
        f"**Комната {room['room_index']}** · {kind_label} · "
        f"игроков {players_n}"
    ]
    if room.get("stat_conflicts"):
        n = sum(
            1 for c in room["stat_conflicts"] if not c.get("resolved")
        )
        lines.append(
            f"⚠️ Конфликт экран/отряд: {n} — нужен выбор админа."
        )
    if room.get("aux_review"):
        lines.append(
            f"👁 {len(room['aux_review'])} строк отряда — "
            "проверка админом."
        )
    text = "\n".join(lines)
    bot_msg = await stats_channel.send(
        content=replace_emojis(text), view=view, files=files
    )
    room["message_id"] = bot_msg.id
    ids = room.setdefault("message_ids", [])
    if bot_msg.id not in ids:
        ids.append(bot_msg.id)
    store.set(tournament)
    if files:
        # Основная карточка теперь хранит те же байты —
        # технические shot-сообщения удаляем, чтобы в канале
        # оставалась ОДНА карточка на комнату. Без вложений в
        # карточке архивировать нельзя — shot'ы остаются
        # единственным источником.
        await _archive_seq_shot_messages(stats_channel, room)


async def _archive_seq_shot_messages(stats_channel, room: dict) -> None:
    """Удалить технические shot-сообщения после публикации
    основной карточки комнаты с теми же вложениями.

    Вызывается ТОЛЬКО после успешной отправки room-сообщения с
    файлами — иначе потеряется единственная копия исходника.
    shot_message_ids сохраняются для аудита/отмены комплекта.
    """
    if room.get("shots_archived") or stats_channel is None:
        return
    for mid in list(room.get("shot_message_ids") or []):
        try:
            msg = await stats_channel.fetch_message(int(mid))
            await msg.delete()
        except Exception:
            logger.debug(
                "seq shot archive delete failed mid=%s", mid,
                exc_info=True,
            )
    room["shots_archived"] = True


def _img_ext(filename: str) -> str:
    import os

    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in IMAGE_EXTS else ".png"


def _room_has_panels(room: dict) -> bool:
    return bool(room.get("panels"))


def _room_files_totals(room: dict) -> tuple[int, int]:
    """(принято, требуется) скриншотов для итогов сессии.

    collecting-комната — канонический room_file_progress. После
    «Завершить комнату» pending очищается — считаем по kind и
    sha/shas: classic/mixed минимум 1 файл, squad — 2. Неизвестный
    kind с panels → required=accepted (не занижаем denominator).
    """
    if _room_has_panels(room):
        shas = {str(s) for s in room.get("shas") or []}
        if room.get("sha"):
            shas.add(str(room["sha"]))
        accepted = len(shas)
        kind = room.get("kind")
        if kind == "squad":
            required = 2
        elif kind in ("classic", "mixed"):
            required = 1
        else:
            required = accepted
        return accepted, required
    accepted, required, _rem, _cf = room_file_progress(room)
    return accepted, required


def _session_files_totals(session: dict) -> tuple[int, int]:
    """Сумма (принято, требуется) скриншотов по ВСЕМ комнатам матча.

    Комната без записи эквивалентна пустой collecting-комнате:
    room_required_files({}) → 2 — канонический лимит неизвестного
    типа, а не произвольная константа: при появлении classic-файла
    требование комнаты пересчитывается в 1.
    """
    expected = int(session.get("rooms_expected") or 0)
    by_idx = {
        int(r.get("room_index", 0)): r
        for r in session.get("rooms") or []
    }
    accepted = required = 0
    for i in range(1, expected + 1):
        room = by_idx.get(i)
        if room is None:
            required += room_required_files({})
            continue
        a, r = _room_files_totals(room)
        accepted += a
        required += r
    return accepted, required


def _session_bundle(session: dict | None) -> bool:
    """Пакетный флоу (/tournament create): 1 скриншот на комнату,
    файлы по порядку, единая кнопка «Завершить загрузку». MM и
    legacy-сессии без флага — False (старый per-room сбор)."""
    return bool(
        session
        and session.get("mode") == "rooms"
        and session.get("bundle_v2")
    )


def _bundle_room_filled(room: dict | None) -> bool:
    """У комнаты есть принятый файл: pending (collecting) или
    panels (финализирована)."""
    if not room:
        return False
    return _room_has_panels(room) or bool(room.get("pending"))


def _bundle_entry_flagged(entry: dict) -> bool:
    return bool(entry.get("needs_reupload") or entry.get("shot_pending"))


def _bundle_room_needs_reupload(room: dict | None) -> bool:
    return any(
        _bundle_entry_flagged(p)
        for p in (room or {}).get("pending") or []
    )


def _bundle_rooms_by_index(session: dict) -> dict[int, dict]:
    return {
        int(r.get("room_index", 0)): r
        for r in session.get("rooms") or []
    }


def _bundle_next_room_index(session: dict) -> int | None:
    """Первая комната 1..rooms_expected без принятого файла.

    None — все комнаты заполнены (или нужна только повторная
    отправка уже принятых файлов).
    """
    expected = int(session.get("rooms_expected") or 0)
    by_idx = _bundle_rooms_by_index(session)
    for i in range(1, expected + 1):
        if not _bundle_room_filled(by_idx.get(i)):
            return i
    return None


def _bundle_reupload_room_index(session: dict) -> int | None:
    """Комната, чей файл надо отправить повторно (байты не
    сохранены/утеряны) — приоритетнее пустых комнат."""
    for r in sorted(
        session.get("rooms") or [],
        key=lambda r: int(r.get("room_index", 0)),
    ):
        if _bundle_room_needs_reupload(r):
            return int(r.get("room_index", 0))
    return None


def _bundle_files_accepted(session: dict) -> int:
    """Принято скриншотов = комнаты с pending или panels
    (bundle: ровно 1 файл на комнату)."""
    return sum(
        1 for r in session.get("rooms") or [] if _bundle_room_filled(r)
    )


def _bundle_upload_complete(session: dict | None) -> bool:
    """Все rooms_expected комнат имеют файл и ни один не ждёт
    повторной отправки — можно показывать «Завершить загрузку»."""
    if not _session_bundle(session):
        return False
    expected = int(session.get("rooms_expected") or 0)
    if expected <= 0:
        return False
    by_idx = _bundle_rooms_by_index(session)
    for i in range(1, expected + 1):
        room = by_idx.get(i)
        if not _bundle_room_filled(room):
            return False
        if not _room_has_panels(room) and _bundle_room_needs_reupload(room):
            return False
    return True


def _bundle_upload_slots(session: dict) -> int:
    """Сколько файлов ещё можно принять: пустые комнаты +
    комнаты, ожидающие повторной отправки."""
    expected = int((session or {}).get("rooms_expected") or 0)
    if expected <= 0:
        return 0
    by_idx = _bundle_rooms_by_index(session or {})
    empty = sum(
        1 for i in range(1, expected + 1)
        if not _bundle_room_filled(by_idx.get(i))
    )
    reupload = sum(
        1 for r in (session or {}).get("rooms") or []
        if _bundle_room_needs_reupload(r)
    )
    return empty + reupload


def _rooms_completed_count(session: dict) -> int:
    return sum(
        1 for r in session.get("rooms") or [] if _room_has_panels(r)
    )


def _rooms_republish_index(session: dict) -> int | None:
    """Первая комната с panels, но без message_id — требуется
    восстановление интерфейса (publish_fail), а не новый OCR."""
    rooms = sorted(
        session.get("rooms") or [],
        key=lambda r: int(r.get("room_index") or 0),
    )
    for r in rooms:
        if _room_has_panels(r) and not r.get("message_id"):
            return int(r.get("room_index") or 0)
    return None


def _rooms_next_index(session: dict) -> int | None:
    """Первая комната без скриншотов (явное назначение по номеру)."""
    expected = int(session.get("rooms_expected") or 0)
    filled = {
        int(r.get("room_index", 0))
        for r in session.get("rooms") or []
        if _room_has_panels(r)
    }
    for i in range(1, expected + 1):
        if i not in filled:
            return i
    return None


def _rooms_progress_text(session: dict) -> str:
    total = int(session.get("rooms_expected") or 0)
    done = _rooms_completed_count(session)
    rep = _rooms_republish_index(session)
    nxt = _rooms_next_index(session)
    bundle = _session_bundle(session)
    # В bundle-режиме «Комнаты» = сколько комнат уже получили
    # свой скриншот (pending или panels); до финализации panels
    # ещё нет, поэтому completed_count здесь не подходит.
    rooms_shown = (
        _bundle_files_accepted(session) if bundle else done
    )
    # «Далее» для bundle — первая комната без файла, а не без
    # panels (panels появляются только после «Завершить
    # загрузку» для всех комнат сразу).
    shown_next = (
        _bundle_next_room_index(session) if bundle else nxt
    )
    if bundle and _bundle_upload_complete(session):
        # Все файлы приняты, финализация ещё не запущена.
        base = "комплект загружен — нажмите «Завершить загрузку»"
    elif bundle and _bundle_reupload_room_index(session) is not None:
        # Файл принят, но Discord-копия не сохранена — нужен
        # повтор той же загрузки, новые комнаты не трогаем.
        base = (
            "ожидается повторная отправка скриншота · комната "
            f"{_bundle_reupload_room_index(session)}"
        )
    elif shown_next is None:
        base = "ожидание сопоставления"
    elif rooms_shown == 0:
        base = f"загрузка · комната {shown_next}"
    else:
        base = f"обработка · комната {shown_next}"
    # Комната распознана, но интерфейс не опубликован (publish_fail)
    # — recover-путь через «Добавить скриншот» важнее основного
    # статуса, показываем его отдельным суффиксом.
    status = (
        f"{base} · восстановление интерфейса · комната {rep}"
        if rep is not None else base
    )
    shots = (
        f"\nСкриншоты: {_bundle_files_accepted(session)}/{total}"
        if bundle and total > 0 else ""
    )
    return (
        f"**Результаты матча**\n"
        f"Комнаты: {rooms_shown}/{total}"
        + (
            f" · далее: комната {shown_next}"
            if shown_next is not None else ""
        )
        + shots
        + f"\nСтатус: {status}"
    )


def _rooms_files_accepted(session: dict) -> bool:
    """Хотя бы один файл реально принят в обработку.

    True, если карточка уже переведена в processing (флаг ставится
    при приёме файлов до OCR) или есть хотя бы одна заполненная
    комната. Сессия в collecting без файлов → False.
    """
    if session.get("card_state") == "processing":
        return True
    if _rooms_completed_count(session) > 0:
        return True
    # V5: принятый, но ещё не финализированный файл — тоже «принято».
    return any(r.get("pending") for r in session.get("rooms") or [])


def rooms_session_can_continue(session: dict | None) -> bool:
    """Сессию можно продолжить через основную кнопку: rooms,
    статус collecting и ещё НИ ОДИН файл не принят."""
    return bool(
        session
        and session.get("mode") == "rooms"
        and session.get("status") == "collecting"
        and not _rooms_files_accepted(session)
    )


def match_button_hidden_for_session(session: dict | None) -> bool:
    """Нужно ли скрывать «Отправить результат» в карточке матча.

    rooms: только если файл принят (0 файлов → кнопка доступна,
    чтобы продолжить сбор). Остальные режимы: активная сессия
    всегда означает принятый скриншот → processing.
    """
    if not session:
        return False
    status = session.get("status")
    if status in (None, "completed", "cancelled"):
        return False
    if session.get("mode") == "rooms":
        return _rooms_files_accepted(session)
    return True


async def _ensure_processing_card(
    guild, tournament, match_type: str, match_index: int
) -> None:
    """Перевести карточку матча в состояние «обработка» один раз.

    Вызывается при ПРИЁМЕ файлов (не дожидаясь OCR): кнопка
    «Отправить результат» исчезает из карточки сразу, повторные
    нажатия старой кнопки отклоняются сервером.
    """
    key = _session_key(match_type, match_index)
    session = (tournament.ocr_sessions or {}).get(key)
    if not session or session.get("card_state") == "processing":
        return
    session["card_state"] = "processing"
    store.set(tournament)
    try:
        from services.match_announcer import update_match_card

        await update_match_card(
            guild, tournament, match_type, match_index, state="processing"
        )
    except Exception as exc:
        logger.warning("Match card processing update failed: %s", exc)


async def _update_rooms_progress(
    guild, tournament, key: str, match_type: str, match_index: int,
    stats_channel,
) -> None:
    """Обновить сообщение прогресса; при полном комплекте — финальный
    статус и переход в awaiting_mapping."""
    session = tournament.ocr_sessions.get(key)
    if not session:
        return
    prog_id = session.get("progress_message_id")
    total = int(session.get("rooms_expected") or 0)
    done = _rooms_completed_count(session)
    if prog_id and stats_channel is not None:
        # RoomCollectView определён в этом же модуле (ниже по файлу) —
        # импорт из views.ocr_match_view здесь невозможен (цикл).
        # Кнопки восстановления («Добавить скриншот») живут, пока
        # идёт сбор ИЛИ есть комната с panels без message_id
        # (publish_fail) — иначе recover-путь теряется.
        rep_needed = _rooms_republish_index(session) is not None
        complete = (
            done >= total and total > 0 and not rep_needed
        )
        view = None if complete else RoomCollectView(
            guild.id, tournament.channel_id, tournament,
            match_type, match_index, session.get("gen", 1),
        )
        text = (
            replace_emojis(
                f"✅ Комплект скриншотов принят: **{done}** комнат.\n"
                "Сопоставьте игроков кнопками под каждым скриншотом."
            )
            if complete else replace_emojis(_rooms_progress_text(session))
        )
        await _update_progress_message(stats_channel, prog_id, text, view=view)
    if done >= total and total > 0:
        await _finalize_rooms_if_ready(
            guild, tournament, key, match_type, match_index, stats_channel,
        )


async def _finalize_rooms_if_ready(
    guild, tournament, key: str, match_type: str, match_index: int,
    stats_channel,
) -> bool:
    """Все комнаты заполнены → перевести сессию в awaiting_mapping."""
    session = tournament.ocr_sessions.get(key)
    if not session or session.get("mode") != "rooms":
        return False
    if session.get("status") != "collecting":
        return False
    total = int(session.get("rooms_expected") or 0)
    if total <= 0:
        return False
    have = {
        int(r.get("room_index", 0))
        for r in session.get("rooms") or []
        if _room_has_panels(r)
    }
    if have != set(range(1, total + 1)):
        return False

    session["status"] = "awaiting_mapping"
    session["rooms_received"] = len(session.get("rooms") or [])
    store.set(tournament)
    return True


def _all_room_shas(session: dict) -> set[str]:
    out: set[str] = set()
    for r in session.get("rooms") or []:
        if r.get("sha"):
            out.add(r["sha"])
        out.update(r.get("shas") or [])
        # V5: sha ещё не финализированных файлов тоже заняты —
        # один и тот же скриншот не должен попасть в две комнаты.
        out.update(_pending_shas(r))
        rep = r.get("republish") or {}
        out.update(rep.get("shas") or [])
    return out


def _drop_empty_room(rooms: list, room: dict | None) -> None:
    """Убрать комнату без panels/pending после отклонённой загрузки.

    Пустая запись не должна оставаться в session["rooms"]:
    иначе она выглядит как «комната в работе» и засоряет
    republish/next-index логику. Комната с принятыми, но ещё не
    финализированными файлами (V5 pending) — НЕ пустая.
    """
    if room is None or _room_has_panels(room) or room.get("pending"):
        return
    rooms[:] = [r for r in rooms if r is not room]


def _squad_player_rows(squad: dict) -> list[dict]:
    """Строки игроков squad-панели (имя + K/D/A/DMG + extras)."""
    from views.ocr_match_view import EXTRA_STAT_KEYS

    rows = []
    for p in squad.get("players") or []:
        row = {
            "name": (p.get("name") or "???")[:32],
            "kills": p.get("kills"),
            "deaths": p.get("deaths"),
            "assists": p.get("assists"),
            "dmg": p.get("dmg"),
        }
        for k in EXTRA_STAT_KEYS:
            if p.get(k) is not None:
                row[k] = p[k]
        rows.append(row)
    return rows


def _squad_aux_entry(squad: dict, sha: str) -> dict:
    """Aux-запись squad-скриншота для смешанной комнаты."""
    return {
        "sha": sha,
        "players": _squad_player_rows(squad),
        "banner": squad.get("banner"),
    }


def _norm_player_key(name) -> str:
    """Ключ сопоставления игрока между classic и squad скриншотами."""
    import re
    import unicodedata

    s = unicodedata.normalize("NFKC", str(name or "")).lower()
    return re.sub(r"[^0-9a-zа-яё一-鿿]", "", s)


def _reconcile_mixed_squad(room: dict) -> None:
    """Сверка squad-aux скриншота с classic-панелями смешанной комнаты.

    Classic — единственный источник статистики (ничего не суммируем).
    Squad-скриншот используется только для:
    - заполнения None-полей classic-строки при УВЕРЕННОМ совпадении
      игрока: уникальный _norm_player_key И все уникальные совпадения
      отряда — в пределах ОДНОЙ панели (squad = одна команда);
    - фиксации расхождений непустых значений в room["stat_conflicts"]
      — финализация блокируется до разрешения администратором;
    - неоднозначные случаи (нет уникального совпадения, имя
      встречается на обеих панелях, отряд «размазался» по командам)
      → room["aux_review"] для ручной проверки, автозаполнения нет.
    """
    panels = room.get("panels") or {}
    index: dict[str, list] = {}
    for side in ("left", "right"):
        for i, r in enumerate(
            (panels.get(side) or {}).get("rows") or []
        ):
            key = _norm_player_key(r.get("name"))
            if key:
                index.setdefault(key, []).append((side, i, r))

    conflicts = room.setdefault("stat_conflicts", [])
    filled = room.setdefault("aux_filled", [])
    review = room.setdefault("aux_review", [])

    def _review(p, reason, candidates=None):
        review.append({
            "name": p.get("name"),
            "reason": reason,
            "squad": {
                f: p.get(f)
                for f in ("kills", "deaths", "assists", "dmg")
            },
            "candidates": candidates or [],
        })

    for aux in room.get("aux_squads") or []:
        players = aux.get("players") or []
        # Squad-скриншот — ОДИН отряд одной команды: все уникальные
        # совпадения должны лежать в одной панели. Если нет —
        # принадлежность к команде не подтверждена, автозаполнение
        # отключаем для всего aux.
        unique_sides = set()
        unique_matches = {}
        for p in players:
            key = _norm_player_key(p.get("name"))
            ms = index.get(key) or []
            if len(ms) == 1:
                unique_matches[key] = ms[0]
                unique_sides.add(ms[0][0])
        if len(unique_sides) > 1:
            for p in players:
                _review(p, "squad_spans_both_teams")
            continue

        for p in players:
            key = _norm_player_key(p.get("name"))
            ms = index.get(key) or []
            if len(ms) != 1:
                _review(
                    p, "no_unique_match",
                    [
                        {"side": s, "row": i, "name": r.get("name")}
                        for s, i, r in ms
                    ],
                )
                continue
            side, i, row = ms[0]
            for f in ("kills", "deaths", "assists", "dmg"):
                sv = p.get(f)
                if sv is None:
                    continue
                cv = row.get(f)
                if cv is None:
                    row[f] = sv
                    filled.append({
                        "side": side, "row": i,
                        "name": row.get("name"), "field": f,
                        "value": sv, "source": "squad",
                    })
                elif cv != sv:
                    row["stat_conflict"] = True
                    conflicts.append({
                        "side": side, "row": i,
                        "name": row.get("name"), "field": f,
                        "classic": cv, "squad": sv,
                        "resolved": False,
                    })
    for k in ("stat_conflicts", "aux_filled", "aux_review"):
        if not room.get(k):
            room.pop(k, None)


def _merge_room_pipes(
    room: dict, pipes: list[dict], shas: list[str]
) -> str | None:
    """Перенести распознанные pipes в panels комнаты.

    Общая логика bundle- и sequential-загрузки (V5): classic —
    основной источник, squad — aux/вторая команда, смешанная пара
    идёт через _reconcile_mixed_squad. Возвращает код ошибки
    ("invalid") или None. Вызывать под _room_lock.
    """
    classic_idx = next(
        (i for i, p in enumerate(pipes) if p.get("squad") is None), None
    )
    if classic_idx is not None:
        # Classic — основной источник статистики обеих команд.
        # Смешанная загрузка (classic + squad): squad-скриншот
        # сохраняется как aux для сверки/заполнения пропусков,
        # статистику повторно НЕ суммирует.
        pipe = pipes[classic_idx]
        room["panels"].update(pipe["panels"])
        room["kind"] = "classic" if len(pipes) == 1 else "mixed"
        room["winner_side"] = pipe.get("winner_side")
        room["banner"] = pipe.get("banner")
        room["score"] = pipe.get("score")
        room["top5"] = pipe.get("top5", False)
        room["sha"] = shas[classic_idx]
        for p, sha in zip(pipes, shas):
            if p.get("squad") is None:
                continue
            room.setdefault("aux_squads", []).append(
                _squad_aux_entry(p["squad"], sha)
            )
            room.setdefault("shas", []).append(sha)
        _reconcile_mixed_squad(room)
        return None

    # Только squad-скриншоты: каждый — отдельный отряд.
    if len(pipes) > 2:
        return "invalid"
    room["kind"] = "squad"
    panels = room.setdefault("panels", {})
    banners = room.setdefault("banners", {})
    for pipe, sha in zip(pipes, shas):
        squad = pipe["squad"]
        pkey = "squad" if "squad" not in panels else "squad2"
        if pkey in panels:
            return "invalid"  # не должно случиться при max 2 squad
        rows = _squad_player_rows(squad)
        panels[pkey] = {"name": "Отряд", "rows": rows}
        banners[pkey] = squad.get("banner")
        room.setdefault("shas", []).append(sha)
        if not room.get("banner") and squad.get("banner"):
            room["banner"] = squad.get("banner")
    if room.get("sha") is None and room["shas"]:
        room["sha"] = room["shas"][0]
    return None


# ---------------------------------------------------------------------------
# V5: последовательная загрузка — один скриншот за раз
# ---------------------------------------------------------------------------
#
# Сырые байты изображений в JSON-состояние НЕ пишутся. Байты живут:
# 1) в памяти процесса — _SEQ_FILE_CACHE до «Завершить комнату»;
# 2) в Discord — вложение shot-сообщения (room["pending"][*]
#    ["shot_message_id"]), откуда они читаются после рестарта.
# 3) для republish — _REPUBLISH_FILE_CACHE (там файлы уже
#    публиковались ранее, байты нужны только для повторной отправки).

_SEQ_FILE_CACHE: dict[tuple, dict[str, dict]] = {}
_REPUBLISH_FILE_CACHE: dict[tuple, dict[str, dict]] = {}


def _seq_cache_key(guild_id, channel_id, key: str, room_index: int):
    return (int(guild_id), int(channel_id), key, int(room_index))


def _pending_shas(room: dict) -> list[str]:
    return [
        str(p.get("sha"))
        for p in room.get("pending") or []
        if p.get("sha")
    ]


def _seq_kind_compat(existing: list[str], new_kind: str) -> bool:
    """Допустима ли пара «уже принятые виды + новый».

    Classic — не более одного на комнату; squad — максимум два;
    смешанная пара classic+squad — через aux-сверку. Всего ≤2 файла.
    """
    kinds = list(existing) + [new_kind]
    if len(kinds) > 2:
        return False
    return not (kinds.count("classic") > 1)


def _classic_pipe_complete(pipe: dict) -> bool:
    """Classic-скриншот ПОЛНОСТЬЮ распознан: обе команды по 4
    игрока И у всех восьми заполнены K/D/A/DMG.

    8 игроков найдены, но часть полей None → False: команде
    нужна дополнительная Squad-сверка для дозаполнения —
    второй файл остаётся доступен.
    """
    panels = (pipe or {}).get("panels") or {}
    left = ((panels.get("left") or {}).get("rows")) or []
    right = ((panels.get("right") or {}).get("rows")) or []
    if len(left) < 4 or len(right) < 4:
        return False
    for r in list(left) + list(right):
        for f in ("kills", "deaths", "assists", "dmg"):
            if (r or {}).get(f) is None:
                return False
    return True


def _room_reupload_only(room: dict) -> bool:
    """Загрузка разрешена ИСКЛЮЧИТЕЛЬНО из-за флагов
    needs_reupload/shot_pending — без них комната уже «полная»
    (полный Classic или 2 файла). В этом режиме принимается
    только повторная отправка ранее сохранённого SHA."""
    pending = room.get("pending") or []
    if not pending:
        return False
    if not any(
        p.get("needs_reupload") or p.get("shot_pending")
        for p in pending
    ):
        return False
    stripped = []
    for p in pending:
        q = dict(p)
        q.pop("needs_reupload", None)
        q.pop("shot_pending", None)
        stripped.append(q)
    return not room_allows_second_file({**room, "pending": stripped})


def room_allows_second_file(room: dict) -> bool:
    """Можно ли добавить ещё один скриншот в collecting-комнату.

    - needs_reupload/shot_pending → повторная отправка того же
      файла разрешена всегда;
    - пустая комната / один squad / неполный classic → второй
      файл допустим (второй отряд или сверка);
    - полный classic (обе панели по 4 строки) и 2 файла → False.
    """
    pending = room.get("pending") or []
    if any(
        p.get("needs_reupload") or p.get("shot_pending")
        for p in pending
    ):
        return True
    if not pending:
        return True
    if len(pending) >= 2:
        return False
    kinds = [p.get("kind") for p in pending]
    if "classic" in kinds:
        pipe = next(
            (
                p.get("pipe") or {}
                for p in pending
                if p.get("kind") == "classic"
            ),
            {},
        )
        return not _classic_pipe_complete(pipe)
    if kinds == ["squad"]:
        return True
    return False


def room_required_files(room: dict) -> int:
    """Сколько скриншотов НЕОБХОДИМО для завершения комнаты.

    Classic-экран покрывает обе команды сразу → 1 файл достаточен
    (неполный pipe не блокирует завершение — сверка опциональна).
    Squad-скриншот — один отряд → нужны 2 (по одному на команду).
    Пока файлов нет, тип неизвестен → лимит 2.
    """
    pending = room.get("pending") or []
    if not pending:
        return 2
    if any(p.get("kind") == "classic" for p in pending):
        return 1
    return 2


def room_file_progress(room: dict) -> tuple[int, int, int, bool]:
    """ЕДИНЫЙ источник счётчика файлов комнаты.

    Возвращает (accepted, required, remaining, can_finish):
    accepted — принято файлов; required — сколько необходимо;
    remaining — сколько осталось; can_finish — можно ли
    завершать комнату. Используется кнопками RoomFilesView,
    текстами collect/shot-сообщений и серверной проверкой
    finalize_room_collect — логика не дублируется.
    """
    pending = room.get("pending") or []
    accepted = len(pending)
    required = room_required_files(room)
    remaining = max(0, required - accepted)
    return accepted, required, remaining, bool(accepted) and remaining == 0


def room_files_complete(room: dict) -> bool:
    """Комнату можно завершать: есть файлы и обязательный
    минимум загружен."""
    return room_file_progress(room)[3]


def _room_pending_summary(pending: list) -> tuple[list[str], int]:
    """(виды файлов, распознанные игроки) для текста collect-
    сообщения: 'экран'/'отряд' + сумма строк panels/squad."""
    kinds: list[str] = []
    players = 0
    for p in pending or []:
        pipe = p.get("pipe") or {}
        if p.get("kind") == "classic":
            kinds.append("экран")
            for side in ("left", "right"):
                players += len(
                    ((pipe.get("panels") or {}).get(side) or {})
                    .get("rows") or []
                )
        else:
            kinds.append("отряд")
            players += len(
                (pipe.get("squad") or {}).get("players") or []
            )
    return kinds, players


def _new_seq_room(room_index: int) -> dict:
    return {
        "room_index": room_index,
        "sha": None,
        "shas": [],
        "kind": None,
        "status": "collecting_files",
        "pending": [],
        "collect_message_id": None,
        "collect_pending": False,
        "panels": {},
        "banners": {},
        "maps": {},
        "panel_picks": {},
        "winner_side": None,
        "banner": None,
        "score": None,
        "top5": False,
        "message_id": None,
        "message_ids": [],
        "shot_message_ids": [],
        "uploaded_at": datetime.now().isoformat(),
    }


def _find_seq_room(session: dict, room_index: int):
    return next(
        (
            r for r in session.get("rooms") or []
            if int(r.get("room_index", 0)) == room_index
        ),
        None,
    )


async def _publish_seq_shot(
    stats_channel, room: dict, pending_entry: dict,
    item: dict,
) -> bool:
    """Сообщение с принятым скриншотом — хранит байты в Discord
    для восстановления после рестарта и показывает прогресс.

    True — файл надёжно сохранён в Discord (единственный
    долговременный источник после рестарта). False — отправка
    не удалась: вызывающий код помечает entry shot_pending и
    НЕ сообщает пользователю об успехе.
    """
    # Идемпотентность: shot уже опубликован и не помечен к
    # повторной заливке — не дублируем сообщение. При
    # needs_reupload старый ID не доверяем: сообщение могло быть
    # удалено — публикуем заново и перезаписываем ID.
    if pending_entry.get("shot_message_id") and not pending_entry.get(
        "needs_reupload"
    ):
        return True
    kind_label = (
        "полный экран" if pending_entry.get("kind") == "classic"
        else "скриншот отряда"
    )
    n = len(room.get("pending") or [])
    accepted, required, _rem, _cf = room_file_progress(room)
    try:
        # Техническое сообщение-источник байтов: минимум текста —
        # публичную карточку комнаты публикует _publish_room_message.
        msg = await stats_channel.send(
            content=replace_emojis(
                f"📸 **Комната {room['room_index']}** — {kind_label} "
                f"({accepted}/{required})"
            ),
            files=[
                discord.File(
                    io.BytesIO(item["data"]),
                    filename=(
                        f"room_{room['room_index']}_shot{n}"
                        f"{_img_ext(item.get('filename') or '')}"
                    ),
                )
            ],
        )
        pending_entry["shot_message_id"] = msg.id
        # shot-сообщения — не message_ids: на них не вешается
        # RoomView (кнопки сопоставления), только метка отмены.
        ids = room.setdefault("shot_message_ids", [])
        if msg.id not in ids:
            ids.append(msg.id)
        return True
    except Exception as e:
        logger.warning("seq shot message publish failed: %s", e)
        return False


def _match_captain_ids(
    tournament, match_type: str, match_index: int, session: dict
) -> list[int]:
    """Discord ID капитанов обеих команд ТЕКУЩЕГО матча.

    Индексы команд — из свежего resolve_match_context (fallback —
    session["teams"]), имена капитанов → player_user_ids. Только
    реальные ID, дедупликация, порядок A затем B.
    """
    try:
        from models.tournament import resolve_match_context

        ctx = resolve_match_context(tournament, match_type, match_index)
        idxs = [ctx.team_a_index, ctx.team_b_index]
    except Exception:
        idxs = [int(x) for x in (session.get("teams") or [])]
    pids = getattr(tournament, "player_user_ids", None) or {}
    teams = getattr(tournament, "teams", None) or []
    out: list[int] = []
    for raw in idxs:
        try:
            i = int(raw)
        except (TypeError, ValueError):
            continue
        team = teams[i] if 0 <= i < len(teams) else {}
        cap = (team or {}).get("captain") or ""
        try:
            uid = int(pids.get(cap) or 0)
        except (TypeError, ValueError):
            uid = 0
        if uid and uid not in out:
            out.append(uid)
    return out


async def _ensure_room_collect_message(
    stats_channel, tournament, session: dict, room: dict,
    match_type: str, match_index: int,
) -> None:
    """Сообщение комнаты в режиме сбора: статус файлов + кнопки
    «Добавить скриншот» / «Завершить комнату».

    Upsert: после каждого принятого файла СУЩЕСТВУЮЩЕЕ сообщение
    редактируется (текст + актуальный набор кнопок — полный
    Classic не предлагает второй скриншот), новых сообщений
    не создаётся. Приглашение «collect_pending» сериализует
    параллельные обновления.
    """
    if room.get("collect_pending"):
        return
    room["collect_pending"] = True
    store.set(tournament)
    pending = room.get("pending") or []
    kinds, players = _room_pending_summary(pending)
    allow_add = room_allows_second_file(room)
    accepted, required, remaining, can_finish = room_file_progress(room)
    detail = " · ".join(
        ([" · ".join(dict.fromkeys(kinds))] if kinds else [])
        + ([f"игроки {players}"] if players else [])
    )
    head = f"**Комната {room['room_index']}**"
    if detail:
        head += f" — {detail}"
    else:
        head += " — сбор файлов"
    lines = [head]
    # Пинг капитанов обеих команд матча — один раз при создании
    # сообщения (edit не генерирует новых уведомлений).
    cap_ids = _match_captain_ids(tournament, match_type, match_index, session)
    if cap_ids:
        lines.append(
            "Капитаны: " + " ".join(f"<@{u}>" for u in cap_ids)
        )
    # Счётчики разделены явно: «Комнаты» — прогресс по комнатам
    # матча (rooms_expected), «Скриншоты комнаты N» — файлы
    # ТЕКУЩЕЙ комнаты (required зависит от типа: classic 1 /
    # squad 2). Без разделения «Скриншоты: 1/1» выглядело как
    # завершение всего матча при нескольких комнатах.
    sess_done = _rooms_completed_count(session)
    sess_total = int(session.get("rooms_expected") or 0)
    rooms_part = (
        f"Комнаты: {sess_done}/{sess_total} · " if sess_total > 0 else ""
    )
    if can_finish:
        if allow_add:
            suffix = (
                "все обязательные загружены, можно завершить "
                "или добавить ещё для сверки"
            )
        else:
            suffix = "все загружены. Нажмите «Завершить комнату»"
    else:
        suffix = f"осталось загрузить: {remaining}"
    lines.append(
        f"{rooms_part}Скриншоты комнаты {room['room_index']}: "
        f"{accepted}/{required} — {suffix}"
    )
    if sess_total > 0:
        tot_a, tot_r = _session_files_totals(session)
        lines.append(
            f"Всего принятых скриншотов: {tot_a}/{tot_r}"
        )
    text = replace_emojis("\n".join(lines))
    # В content только <@id> капитанов — пингуются исключительно
    # они; роли и @everyone отключены.
    mentions = discord.AllowedMentions(
        users=True, roles=False, everyone=False
    )

    from views.ocr_match_view import RoomFilesView

    gid = (
        getattr(tournament, "guild_id", None)
        or session.get("guild_id")
        or 0
    )
    view = RoomFilesView(
        gid, tournament.channel_id, tournament,
        match_type, match_index, int(room["room_index"]),
        int(session.get("gen", 1)),
        allow_add=allow_add,
        allow_done=can_finish,
    )
    cmid = room.get("collect_message_id")
    try:
        if cmid:
            try:
                msg = await stats_channel.fetch_message(int(cmid))
                await msg.edit(
                    content=text, view=view,
                    allowed_mentions=mentions,
                )
            except Exception:
                # Сообщение удалено/недоступно — создаём заново
                # (повторный пинг допустим: старого сообщения нет).
                msg = await stats_channel.send(
                    content=text, view=view,
                    allowed_mentions=mentions,
                )
                room["collect_message_id"] = msg.id
        else:
            msg = await stats_channel.send(
                content=text, view=view,
                allowed_mentions=mentions,
            )
            room["collect_message_id"] = msg.id
        room["collect_pending"] = False
        store.set(tournament)
    except Exception as e:
        room["collect_pending"] = False
        store.set(tournament)
        logger.warning("room collect message failed: %s", e)


async def _seq_pending_items(
    stats_channel, pending_entries: list, cache_key: tuple
) -> list[dict]:
    """Собрать items (filename+bytes) для _publish_room_message.

    Источники байтов: память → Discord shot-сообщение. Если оба
    недоступны (старые комнаты/сбой) — файл пропускается; сообщение
    комнаты публикуется без вложений, статистика не теряется.
    """
    items: list[dict] = []
    problems: list[dict] = []
    cache = _SEQ_FILE_CACHE.get(cache_key) or {}
    for p in pending_entries or []:
        sha = p.get("sha")
        data = None
        centry = cache.get(sha)
        if centry:
            data = centry.get("data")
        if data is None and p.get("shot_message_id") and stats_channel:
            try:
                msg = await stats_channel.fetch_message(
                    int(p["shot_message_id"])
                )
                att = (getattr(msg, "attachments", None) or [None])[0]
                if att is not None:
                    data = await att.read()
            except Exception:
                logger.debug("seq shot fetch failed", exc_info=True)
        if not data:
            problems.append({"sha": sha, "reason": "missing"})
            continue
        # Верификация: восстановленные байты обязаны совпадать
        # с sha256, зафиксированным при OCR — иначе нельзя
        # публиковать комплект с чужой/повреждённой картинкой.
        if _sha256_bytes(data) != sha:
            problems.append({"sha": sha, "reason": "sha_mismatch"})
            continue
        items.append({
            "filename": p.get("filename") or f"{sha}.png",
            "data": data,
        })
    return items, problems


async def process_room_file(
    guild: discord.Guild,
    channel,
    user,
    tournament,
    match_type: str,
    match_index: int,
    key: str,
    session: dict,
    stats_channel,
    room_index: int,
    item: dict,
) -> str:
    """Принять ОДИН скриншот в комнату (V5 sequential).

    Файл OCR'ится сразу, результат складывается в room["pending"]
    (без panels — комната «считается» только после «Завершить
    комнату»). Байты — в памяти и в shot-сообщении Discord.

    Коды: "ok" (первый файл) | "added" (второй) | "dup" |
    "invalid" | "invalid_mix" | "room_full" | "parse_fail" |
    "room_done" | "cancelled" | "publish_fail" | "error".
    """
    expected = int(session.get("rooms_expected") or 0)
    if room_index < 1 or room_index > expected:
        return "invalid"
    bundle = _session_bundle(session)

    rooms = session.setdefault("rooms", [])
    room = _find_seq_room(session, room_index)
    room_done = room is not None and _room_has_panels(room)
    needs_republish = room_done and not room.get("message_id")
    if room_done and not needs_republish:
        return "room_done"
    if room is None:
        room = _new_seq_room(room_index)
        rooms.append(room)
        rooms.sort(key=lambda r: int(r.get("room_index", 0)))

    sha = _sha256_bytes(item["data"])

    # Republish-ветка: комната уже распознана, не хватает только
    # сообщения с кнопками — копим исходные файлы по sha.
    if needs_republish:
        return await _process_republish_file(
            guild, stats_channel, tournament, key, session,
            match_type, match_index, room_index, room, item, sha,
        )

    # Дедупликация: sha не должен встречаться в других комнатах
    # и в pending этой комнаты. Исключение — повторная отправка
    # ТОГО ЖЕ файла, когда Discord-копия не сохранена
    # (shot_pending) или утеряна (needs_reupload): OCR не
    # повторяем — восстанавливаем только публикацию вложения.
    pend_entries = room.get("pending") or []
    dup_entry = next(
        (p for p in pend_entries if p.get("sha") == sha), None
    )
    if dup_entry is not None:
        if dup_entry.get("shot_pending") or dup_entry.get(
            "needs_reupload"
        ):
            return await _retry_seq_shot(
                guild, stats_channel, tournament, key, session,
                room, dup_entry, item,
            )
        _drop_empty_room(rooms, room)
        return "dup"
    # Bundle (/tournament create): ровно 1 скриншот на комнату —
    # любое pending (даже с флагами повторной отправки: тот же
    # SHA ушёл в _retry_seq_shot выше) закрывает комнату для
    # новых файлов. Проверка повторяется под _room_lock.
    if bundle and pend_entries:
        return "room_full"
    known = _all_room_shas(session)
    if room.get("sha"):
        known.discard(room["sha"])
    if sha in known:
        _drop_empty_room(rooms, room)
        return "dup"
    if len(pend_entries) >= 2:
        return "room_full"
    # Полный Classic (обе команды по 4 строки, все K/D/A/DMG)
    # — второй скриншот не нужен: устаревшая кнопка/прямой
    # вызов не должны добавлять лишний файл. needs_reupload/
    # shot_pending уже учтены внутри room_allows_second_file.
    if pend_entries and not room_allows_second_file(room):
        _drop_empty_room(rooms, room)
        return "room_full"
    # Загрузка открыта ТОЛЬКО для повтора утерянного файла
    # (reupload-флаги — единственная причина): новый SHA
    # отклоняем ДО OCR, pending не меняется. Тот же SHA уже
    # ушёл в _retry_seq_shot по dup_entry выше.
    if _room_reupload_only(room):
        _drop_empty_room(rooms, room)
        return "room_full"

    gen = session.get("gen", 1)
    try:
        pipe = await _classic_pipeline(item["data"], need_score=False)
    except Exception:
        logger.exception("Seq room file OCR pipeline failed")
        _drop_empty_room(rooms, room)
        return "error"

    # Сессия могла быть отменена/пересоздана пока шёл OCR
    fresh = store.get(guild.id, tournament.channel_id)
    fs = (fresh.ocr_sessions.get(key) if fresh else None) or {}
    if (
        fs.get("mode") != "rooms"
        or fs.get("status") != "collecting"
        or fs.get("gen") != gen
    ):
        _seq_purge_session(guild.id, tournament.channel_id, key)
        return "cancelled"

    if pipe is None:
        # Первый файл отклонён — пустую комнату убираем; при наличии
        # pending комната и ранее принятые файлы сохраняются.
        _drop_empty_room(fs.get("rooms") or [], room)
        return "parse_fail"

    kind = "squad" if pipe.get("squad") is not None else "classic"
    # Bundle: один скриншот = полный экран обеих команд. Скриншот
    # отряда покрывает только одну команду — в этом флоу не
    # принимается (MM/legacy сохраняют squad-путь).
    if bundle and kind != "classic":
        _drop_empty_room(fs.get("rooms") or [], room)
        return "invalid_classic"
    existing_kinds = [
        p.get("kind") for p in room.get("pending") or []
    ]
    if not _seq_kind_compat(existing_kinds, kind):
        _drop_empty_room(fs.get("rooms") or [], room)
        return "invalid_mix"

    # Мутация под _room_lock: параллельные загрузки двух
    # пользователей сериализуются — не больше 2 файлов, без
    # смешения статуса и двойной статистики.
    async with _room_lock(key, getattr(tournament, "id", "") or ""):
        fresh = store.get(guild.id, tournament.channel_id) or tournament
        fs = fresh.ocr_sessions.get(key) or {}
        if (
            fs.get("mode") != "rooms"
            or fs.get("status") != "collecting"
            or int(fs.get("gen", 0)) != int(gen)
        ):
            _seq_purge_session(guild.id, tournament.channel_id, key)
            return "cancelled"
        room = _find_seq_room(fs, room_index)
        if room is None or _room_has_panels(room):
            return "room_done"
        if room.get("status") != "collecting_files":
            return "closed"
        pend = room.setdefault("pending", [])
        pshas = _pending_shas(room)
        if sha in pshas or (bundle and pend) or len(pend) >= 2:
            return "dup" if sha in pshas else "room_full"
        # Гонка: состояние комнаты изменилось между проверкой и
        # локом — полный Classic не принимает второй файл, а
        # reupload-only комната — только уже известный SHA
        # (новый здесь быть не может: sha in pshas → dup выше).
        if pend and not room_allows_second_file(room):
            return "room_full"
        if pend and _room_reupload_only(room):
            return "room_full"
        ek = [p.get("kind") for p in pend]
        if not _seq_kind_compat(ek, kind):
            return "invalid_mix"
        entry = {
            "sha": sha,
            "kind": kind,
            "filename": item.get("filename") or f"{sha[:12]}.png",
            "pipe": pipe,
            "shot_message_id": None,
        }
        pend.append(entry)
        room["status"] = "collecting_files"
        room["uploaded_at"] = datetime.now().isoformat()
        fs["rooms_received"] = _rooms_completed_count(fs)
        store.set(fresh)
        tournament, session = fresh, fs

    was_first = len(room.get("pending") or []) == 1
    ck = _seq_cache_key(guild.id, tournament.channel_id, key, room_index)
    _SEQ_FILE_CACHE.setdefault(ck, {})[sha] = {
        "filename": entry["filename"], "data": item["data"],
    }

    # Shot-сообщение (байты в Discord) + сообщение сбора комнаты.
    # Без успешной отправки вложения файл не считается надёжно
    # сохранённым: entry помечается shot_pending, пользователю
    # уходит НЕ «ok», а предложение повторить отправку.
    shot_ok = await _publish_seq_shot(stats_channel, room, entry, item)
    entry["shot_pending"] = not shot_ok
    store.set(tournament)
    # Bundle: per-room сообщение сбора не создаётся — прогресс и
    # кнопки живут на рабочей карточке матча (RoomCollectView).
    # Комната получит своё сообщение с маппингом только после
    # «Завершить загрузку» → finalize_room_collect.
    if not _session_bundle(session):
        await _ensure_room_collect_message(
            stats_channel, tournament, session, room,
            match_type, match_index,
        )
    # Файл принят — карточка матча переходит в processing
    # (кнопка «Отправить результат» скрывается).
    await _ensure_processing_card(guild, tournament, match_type, match_index)
    await _update_rooms_progress(
        guild, tournament, key, match_type, match_index, stats_channel,
    )
    if not shot_ok:
        return "shot_pending"
    return "ok" if was_first else "added"


async def _seq_shot_attachment_ok(stats_channel, entry: dict) -> bool:
    """Проверить, что shot-сообщение живо и его вложение
    соответствует sha256 записи — иначе старый ID недоверенный
    (сообщение могло быть удалено/изменено)."""
    mid = entry.get("shot_message_id")
    sha = entry.get("sha")
    if not mid or not sha or stats_channel is None:
        return False
    try:
        msg = await stats_channel.fetch_message(int(mid))
        att = (getattr(msg, "attachments", None) or [None])[0]
        if att is None:
            return False
        data = await att.read()
        return bool(data) and _sha256_bytes(data) == sha
    except Exception:
        logger.debug("seq shot verify failed", exc_info=True)
        return False


async def _retry_seq_shot(
    guild, stats_channel, tournament, key, session,
    room, entry, item,
) -> str:
    """Повторная отправка того же файла для восстановления
    Discord-копии (shot_pending/needs_reupload). OCR не
    перезапускается — pipe уже сохранён в pending.

    Сериализовано _room_lock: отмена сессии и параллельные
    повторы не могут оставить рассинхрон флагов/ID.
    """
    async with _room_lock(key, getattr(tournament, "id", "") or ""):
        # Актуальное состояние — store мог обновиться, пока
        # пользователь грузил файл.
        fresh = store.get(guild.id, tournament.channel_id) or tournament
        fs = fresh.ocr_sessions.get(key) or {}
        if (
            fs.get("mode") != "rooms"
            or fs.get("status") != "collecting"
            or int(fs.get("gen", 0)) != int(session.get("gen", 0))
        ):
            _seq_purge_session(guild.id, tournament.channel_id, key)
            return "cancelled"
        fresh_room = _find_seq_room(fs, int(room.get("room_index", 0)))
        if fresh_room is None:
            return "closed"
        # Entry ищем по sha в АКТУАЛЬНОМ списке — объект room
        # мог быть подменён store.set.
        entry = next(
            (
                p for p in fresh_room.get("pending") or []
                if p.get("sha") == entry.get("sha")
            ),
            None,
        )
        if entry is None:
            return "closed"
        tournament, session, room = fresh, fs, fresh_room

        # 1) ID есть и не needs_reupload — уже надёжно.
        if entry.get("shot_message_id") and not entry.get(
            "needs_reupload"
        ):
            return "ok"
        # 2) needs_reupload/shot_pending, но сообщение живо и
        #    sha совпадает — просто снимаем флаги, без репоста.
        if await _seq_shot_attachment_ok(stats_channel, entry):
            entry["shot_pending"] = False
            entry["needs_reupload"] = False
            store.set(tournament)
            return "ok"
        # 3) Реальная потеря — публикуем тот же файл заново.
        ck = _seq_cache_key(
            guild.id, tournament.channel_id, key,
            int(room.get("room_index", 0)),
        )
        _SEQ_FILE_CACHE.setdefault(ck, {})[entry["sha"]] = {
            "filename": entry.get("filename") or item.get("filename"),
            "data": item["data"],
        }
        ok = await _publish_seq_shot(stats_channel, room, entry, item)
        entry["shot_pending"] = not ok
        if ok:
            entry["needs_reupload"] = False
        store.set(tournament)
        return "ok" if ok else "shot_pending"


def _seq_purge_session(guild_id, channel_id, key: str) -> None:
    """Очистить байтовые кэши последовательной загрузки сессии —
    при отмене/завершении/устаревании, чтобы RAM не копил
    изображения после серии неудачных матчей."""
    prefix = (int(guild_id), int(channel_id), key)
    for cache in (_SEQ_FILE_CACHE, _REPUBLISH_FILE_CACHE):
        for ck in [k for k in cache if k[:3] == prefix]:
            cache.pop(ck, None)


async def _process_republish_file(
    guild, stats_channel, tournament, key, session,
    match_type, match_index, room_index, room, item, sha,
) -> str:
    """Последовательный republish: копим файлы по sha, пока не
    наберётся исходный комплект — затем _republish_room_interface.

    Байты — только в памяти (файлы уже публиковались при OCR);
    после рестарта пользователь просто загружает их заново.
    """
    expected = list(room.get("shas") or [])
    if room.get("sha") and room["sha"] not in expected:
        expected.append(room["sha"])
    rep = room.setdefault("republish", {"shas": []})
    got = list(rep.get("shas") or [])

    ck = _seq_cache_key(
        guild.id, tournament.channel_id, key, room_index
    )
    cache = _REPUBLISH_FILE_CACHE.setdefault(ck, {})
    if sha not in expected:
        return "sha_mismatch"
    if sha in got and sha in cache:
        return "dup"
    if sha not in got:
        got.append(sha)
        rep["shas"] = got
        store.set(tournament)
    cache[sha] = {"filename": item.get("filename"), "data": item["data"]}

    if sorted(got) != sorted(expected):
        return "republish_partial"

    items = [
        {"filename": cache[s].get("filename") or f"{s}.png",
         "data": cache[s]["data"]}
        for s in expected if s in cache
    ]
    if len(items) != len(expected):
        # Часть байтов потеряна (другой рестарт/инстанс) — ждём
        # повторной загрузки недостающих файлов.
        return "republish_partial"
    res = await _republish_room_interface(
        guild, stats_channel, tournament, key, session,
        stats_channel, room_index, items, match_type, match_index,
        list(expected),
    )
    if res == "ok":
        room.pop("republish", None)
        _REPUBLISH_FILE_CACHE.pop(ck, None)
        store.set(tournament)
        return "republished"
    return res


async def finalize_room_collect(
    guild: discord.Guild,
    user,
    tournament,
    match_type: str,
    match_index: int,
    room_index: int,
    stats_channel=None,
) -> str:
    """«Завершить комнату» (V5): слить pending → panels, опубликовать
    сообщение комнаты с кнопками сопоставления, status →
    awaiting_mapping. Идемпотентно под _room_lock.

    Коды: "ok" | "publish_fail" | "done" | "empty" | "closed" |
    "forbidden" | "invalid" | "files_missing" | "incomplete".
    """
    key = _session_key(match_type, match_index)
    session = (tournament.ocr_sessions or {}).get(key)
    if not session or session.get("mode") != "rooms":
        return "closed"
    if session.get("status") in ("completed", "cancelled"):
        _seq_purge_session(guild.id, tournament.channel_id, key)
        return "closed"
    # «Завершить комнату» — капитану одной из команд матча
    # (Discord ID) ИЛИ Discord-администратору сервера.
    # Отправитель комплекта, обычные участники и организатор
    # без admin-прав — forbidden. Повторная проверка под
    # _room_lock на свежем состоянии.
    teams = list(session.get("teams") or [])
    if not teams:
        try:
            from models.tournament import resolve_match_context
            ctx = resolve_match_context(tournament, match_type, match_index)
            teams = [ctx.team_a_index, ctx.team_b_index]
        except Exception:
            try:
                teams = list(_match_tuple(tournament, match_type, match_index))
            except Exception:
                teams = []
    if not _can_finalize_room(user, tournament, teams):
        return "forbidden"

    # Сначала восстанавливаем и верифицируем исходные файлы —
    # merge в panels выполняем только когда ВСЕ байты на месте
    # и совпадают по sha256. Иначе комната остаётся в
    # collecting_files с needs_reupload — статистика не теряется,
    # пользователь может прикрепить файлы повторно.
    if stats_channel is None:
        stats_channel = await _stats_channel_for(guild, session)
    pre_t = store.get(guild.id, tournament.channel_id) or tournament
    pre_room = _find_seq_room(
        pre_t.ocr_sessions.get(key) or {}, room_index
    )
    # Идемпотентность до проверки файлов: уже завершённая
    # комната → "done", а не "empty".
    if pre_room is not None and _room_has_panels(pre_room):
        return "done"
    pend_pre = (pre_room or {}).get("pending") or []
    if not pend_pre:
        return "empty"
    # НЕЗАВИСИМЫЙ снимок sha ДО асинхронного восстановления
    # файлов: pre_room может разделять состояние с объектом,
    # который будет перечитан под локом — сравнение «объект с
    # самим собой» ничего не ловит. Снимок — плоский список.
    sha_snapshot = list(_pending_shas(pre_room or {}))
    ck = _seq_cache_key(
        guild.id, tournament.channel_id, key, room_index
    )
    items, problems = await _seq_pending_items(
        stats_channel, pend_pre, ck
    )
    # Mixed-комната: Classic — главный скриншот, идёт первым
    # во вложениях карточки; squad — дополнительный для проверки.
    kind_by_sha = {
        p.get("sha"): p.get("kind") for p in pend_pre
    }
    sha_of_item = {
        _sha256_bytes(it["data"]): it for it in items
    }
    items = [
        sha_of_item[s] for s in sha_snapshot
        if s in sha_of_item
    ]
    items.sort(
        key=lambda it: 0 if kind_by_sha.get(
            _sha256_bytes(it["data"])
        ) == "classic" else 1
    )
    if problems:
        bad = {p["sha"] for p in problems}
        for p in pend_pre:
            if p.get("sha") in bad:
                p["needs_reupload"] = True
                p["shot_pending"] = True
        store.set(pre_t)
        logger.warning(
            "seq finalize room %s: %d file(s) unrecoverable "
            "(%s) — awaiting reupload",
            room_index, len(problems),
            ",".join(sorted(bad)),
        )
        return "files_missing"

    gen = int(session.get("gen", 1))
    fresh_t = None
    async with _room_lock(key, getattr(tournament, "id", "") or ""):
        fresh_t = store.get(guild.id, tournament.channel_id) or tournament
        fs = fresh_t.ocr_sessions.get(key) or {}
        if (
            fs.get("mode") != "rooms"
            or fs.get("status") != "collecting"
            or int(fs.get("gen", 0)) != gen
        ):
            _seq_purge_session(guild.id, tournament.channel_id, key)
            return "closed"
        room = _find_seq_room(fs, room_index)
        if room is None:
            return "empty"
        # Повторная проверка прав ПОД локом на свежем состоянии:
        # между pre-check и локом сессия/команда могли измениться.
        if not _can_finalize_room(
            user, fresh_t, fs.get("teams") or teams
        ):
            return "forbidden"
        if _room_has_panels(room):
            return "done"
        pend = room.get("pending") or []
        if not pend:
            return "empty"
        # Серверная проверка полноты комплекта: кнопки можно
        # подделать/устареть — обязательный минимум файлов
        # (classic 1 / squad 2) проверяется здесь, под локом.
        if not room_files_complete(room):
            return "incomplete"
        # Pending мог измениться между снимком и локом (параллельная
        # загрузка/повтор/отмена) — сверяем АКТУАЛЬНЫЙ набор sha
        # с независимым снимком, снятым до await восстановления.
        if _pending_shas(room) != sha_snapshot:
            return "files_missing"
        pipes = [p.get("pipe") or {} for p in pend]
        shas = _pending_shas(room)
        err = _merge_room_pipes(room, pipes, shas)
        if err is not None:
            return err
        room["status"] = "awaiting_mapping"
        # shot-сообщения сохраняем отдельным списком — для метки
        # отмены; RoomView на них не вешается.
        sids = room.setdefault("shot_message_ids", [])
        for p in pend:
            mid = p.get("shot_message_id")
            if mid and mid not in sids:
                sids.append(mid)
        room["pending"] = []
        fs["rooms_received"] = _rooms_completed_count(fs)
        store.set(fresh_t)
        session, room = fs, room

    # Публикация сообщения комнаты с кнопками сопоставления —
    # байты уже верифицированы выше.
    channel = stats_channel
    ok = False
    if channel is not None:
        for attempt in range(2):
            try:
                await _publish_room_message(
                    channel, fresh_t, channel, items, room,
                    match_type, match_index,
                )
                ok = True
                break
            except Exception as e:
                logger.warning(
                    "seq room publish attempt %d failed: %s",
                    attempt + 1, e,
                )
    await _ensure_processing_card(guild, fresh_t, match_type, match_index)
    if channel is not None:
        await _update_rooms_progress(
            guild, fresh_t, key, match_type, match_index, channel,
        )
    # Техническое collect-сообщение удаляем ТОЛЬКО когда карточка
    # комнаты реально опубликована — в канале остаётся одна
    # карточка. При publish_fail сообщение НЕ трогаем как источник
    # восстановления: убираем лишь его кнопки (RoomFilesView в
    # awaiting_mapping уже нерабочие — recover идёт через
    # progress-message «Добавить скриншот»).
    published = ok or bool(room.get("message_id"))
    try:
        cmid = room.get("collect_message_id")
        if cmid and channel is not None:
            cmsg = await channel.fetch_message(int(cmid))
            if published:
                await cmsg.delete()
                room["collect_message_id"] = None
            else:
                await cmsg.edit(
                    content=replace_emojis(
                        f"**Комната {room_index}** — ⏳ интерфейс "
                        "не опубликован. Восстановление: кнопка "
                        "«Добавить скриншот» в сообщении прогресса "
                        "(те же файлы, без повторного OCR)."
                    ),
                    view=None,
                )
            store.set(fresh_t)
    except Exception:
        logger.debug("collect message finalize cleanup failed",
                     exc_info=True)
    # Байты комнаты больше не нужны для republish этой комнаты —
    # источник истины теперь panels + shot-сообщения.
    _SEQ_FILE_CACHE.pop(ck, None)
    return "ok" if ok or room.get("message_id") else "publish_fail"


async def cleanup_rooms_ocr_messages(
    guild: discord.Guild,
    tournament,
    match_type: str,
    match_index: int,
    session: dict | None = None,
) -> bool:
    """Удалить технические OCR-сообщения завершённого матча.

    Режимы: rooms (карточки комнат, collect, shot-остатки,
    confirm), squad (сообщения отрядов), classic/прочие
    (общее OCR-сообщение сессии). Удаляются ТОЛЬКО сообщения,
    привязанные к этому матчу и созданные ботом; итоговая
    рабочая карточка (progress/work_message_id) и карточка
    результата НЕ трогаются. Идемпотентно: cleanup_done
    ставится только при полном успехе — при частичном сбое
    возвращается False и recovery повторит попытку.
    """
    key = _session_key(match_type, match_index)
    sess = session or (tournament.ocr_sessions or {}).get(key)
    if not sess or sess.get("status") != "completed":
        return False
    if sess.get("cleanup_done"):
        return True
    mode = sess.get("mode")
    channel = await _stats_channel_for(guild, sess)
    if channel is None:
        return False
    mids: list[int] = []
    if mode == "rooms" or sess.get("rooms"):
        for r in sess.get("rooms") or []:
            mids.extend(r.get("message_ids") or [])
            if r.get("message_id"):
                mids.append(int(r["message_id"]))
            if r.get("collect_message_id"):
                mids.append(int(r["collect_message_id"]))
            for p in r.get("pending") or []:
                if p.get("shot_message_id"):
                    mids.append(int(p["shot_message_id"]))
            for sid in r.get("shot_message_ids") or []:
                mids.append(int(sid))
    if sess.get("confirm_message_id"):
        mids.append(int(sess["confirm_message_id"]))
    if mode == "squad":
        for sentry in (sess.get("squads") or {}).values():
            if sentry.get("message_id"):
                mids.append(int(sentry["message_id"]))
        for entry in sess.get("squad_pool") or []:
            if entry.get("message_id"):
                mids.append(int(entry["message_id"]))
    elif mode != "rooms":
        # Classic/одиночное OCR-сообщение сессии.
        if sess.get("message_id"):
            mids.append(int(sess["message_id"]))
    for sid in sess.get("shot_message_ids") or []:
        mids.append(int(sid))
    # Итоговые сообщения НЕ удаляем: прогресс/рабочая карточка
    # матча и карточка результата в канале результатов.
    keep = {
        int(sess.get("progress_message_id") or 0),
        int(sess.get("result_message_id") or 0),
    }
    ctrl = (getattr(tournament, "match_cards", None) or {}).get(key) or {}
    if ctrl.get("work_message_id"):
        keep.add(int(ctrl["work_message_id"]))
    deleted = 0
    failed = False
    for mid in dict.fromkeys(mids):
        if mid in keep or not mid:
            continue
        try:
            msg = await channel.fetch_message(mid)
        except discord.NotFound:
            continue
        except Exception as exc:
            failed = True
            logger.warning(
                "OCR cleanup fetch failed mid=%s match=%s: %s",
                mid, key, exc,
            )
            continue
        # Удаляем только сообщения бота — пользовательские
        # и чужие карточки не трогаем.
        author = getattr(msg, "author", None)
        if author is not None and not getattr(author, "bot", False):
            logger.warning(
                "OCR cleanup skip non-bot message mid=%s match=%s",
                mid, key,
            )
            continue
        try:
            await msg.delete()
            deleted += 1
        except discord.NotFound:
            pass
        except discord.Forbidden:
            failed = True
            logger.warning(
                "rooms cleanup forbidden mid=%s match=%s",
                mid, key,
            )
        except Exception as exc:
            failed = True
            logger.warning(
                "OCR cleanup delete failed mid=%s match=%s: %s",
                mid, key, exc,
            )
    if failed:
        # Частичный сбой — cleanup_done НЕ ставим: recovery
        # после рестарта повторит удаление оставшихся сообщений.
        return False
    sess["cleanup_done"] = True
    try:
        store.set(tournament)
    except Exception:
        pass
    logger.info(
        "OCR cleanup done: match=%s mode=%s deleted=%d",
        key, mode, deleted,
    )
    return True


async def _stats_channel_for(guild, session):
    """Канал прогресса/результатов комнаты.

    Приоритет — ``ocr_channel_id`` (канал, выбранный при создании
    сессии: OCR_MAPPING_CHANNEL_ID для турниров, канал матча для
    MM). Для старых сессий без него — ``progress_channel_id``:
    опубликованные сообщения НЕ переносятся в другой канал.
    """
    for key in ("ocr_channel_id", "progress_channel_id"):
        ch_id = session.get(key)
        if ch_id:
            try:
                return guild.get_channel(int(ch_id)) or await guild.fetch_channel(
                    int(ch_id)
                )
            except Exception:
                pass
    return None


async def process_room_bundle(
    guild: discord.Guild,
    channel,
    user,
    tournament,
    match_type: str,
    match_index: int,
    key: str,
    session: dict,
    stats_channel,
    room_index: int,
    items: list[dict],
) -> str:
    """Обработать скриншоты ОДНОЙ явно выбранной комнаты.

    items: [{"filename": str, "data": bytes}] — 1 файл (полный экран
    или один отряд) либо 2 файла (по одному скриншоту на команду).
    Комната определяется room_index, а не порядком загрузки.

    Коды: "ok" | "dup" | "invalid" | "parse_fail" | "room_done" |
    "cancelled" | "publish_fail" | "error".
    """
    import hashlib

    expected = int(session.get("rooms_expected") or 0)
    if room_index < 1 or room_index > expected:
        return "invalid"
    if not items or len(items) > 2:
        return "invalid"

    rooms = session.setdefault("rooms", [])
    room = next(
        (r for r in rooms if int(r.get("room_index", 0)) == room_index),
        None,
    )
    room_done = room is not None and _room_has_panels(room)
    # Комната распознана, но сообщение с кнопками не опубликовано —
    # восстанавливаем только интерфейс, без повторного OCR/статистики.
    needs_republish = room_done and not room.get("message_id")
    if room_done and not needs_republish:
        return "room_done"
    if room is None:
        room = {
            "room_index": room_index,
            "sha": None,
            "shas": [],
            "kind": None,
            "status": "awaiting_mapping",
            "panels": {},
            "banners": {},
            "maps": {},
            "panel_picks": {},
            "winner_side": None,
            "banner": None,
            "score": None,
            "top5": False,
            "message_id": None,
            "message_ids": [],
            "uploaded_at": datetime.now().isoformat(),
        }
        rooms.append(room)
        rooms.sort(key=lambda r: int(r.get("room_index", 0)))

    # Дедупликация по sha256 — по всему комплекту
    shas = [_sha256_bytes(it["data"]) for it in items]
    known = _all_room_shas(session) - set(room.get("shas") or [])
    if room.get("sha"):
        known.discard(room["sha"])
    if any(s in known for s in shas):
        _drop_empty_room(rooms, room)
        return "dup"

    if needs_republish:
        return await _republish_room_interface(
            guild, channel, tournament, key, session, stats_channel,
            room_index, items, match_type, match_index, shas,
        )

    gen = session.get("gen", 1)
    pipes = []
    try:
        for it in items:
            pipe = await _classic_pipeline(it["data"], need_score=False)
            pipes.append(pipe)
    except Exception:
        logger.exception("Room bundle OCR pipeline failed")
        _drop_empty_room(rooms, room)
        return "error"

    # Сессия могла быть отменена/пересоздана пока шёл OCR
    fresh = store.get(guild.id, tournament.channel_id)
    fs = (fresh.ocr_sessions.get(key) if fresh else None) or {}
    if (
        fs.get("mode") != "rooms"
        or fs.get("status") != "collecting"
        or fs.get("gen") != gen
    ):
        return "cancelled"

    if any(p is None for p in pipes):
        _drop_empty_room(rooms, room)
        return "parse_fail"

    kinds = ["squad" if p.get("squad") is not None else "classic"
             for p in pipes]
    if len(items) == 2 and kinds == ["classic", "classic"]:
        # Два полных экрана в одной комнате — недопустимо.
        # classic+squad — поддерживаемая смешанная загрузка.
        _drop_empty_room(rooms, room)
        return "invalid_mix"
    if len(items) == 1 and kinds[0] not in ("classic", "squad"):
        _drop_empty_room(rooms, room)
        return "invalid"

    # --- Сборка panels комнаты ---
    err = _merge_room_pipes(room, pipes, shas)
    if err is not None:
        _drop_empty_room(rooms, room)
        return err

    session["rooms_received"] = _rooms_completed_count(session)
    store.set(tournament)

    # Файлы приняты и распознаны — прячем «Отправить результат»
    # в карточке матча. Вызываем только на успешном пути: при
    # отклонённом комплекте (invalid/parse_fail/invalid_mix)
    # кнопка и кнопки комнат не должны блокироваться.
    await _ensure_processing_card(guild, tournament, match_type, match_index)

    # Сообщение комнаты: все её скриншоты + кнопки маппинга.
    # Публикация обязательна: без сообщения с кнопками комплект
    # нельзя завершить — при сбое повторяем один раз, иначе
    # возвращаем publish_fail (panels уже сохранены, повторная
    # загрузка тех же файлов восстановит интерфейс через
    # needs_republish-ветку выше).
    for attempt in range(2):
        try:
            await _publish_room_message(
                stats_channel, tournament, channel, items, room,
                match_type, match_index,
            )
            break
        except Exception as e:
            logger.warning(
                "Room bundle message publish attempt %d failed: %s",
                attempt + 1, e,
            )
    else:
        # Комната заполнена — прогресс/финализация комплекта
        # выполняются и при publish_fail (восстановление интерфейса
        # доступно и в awaiting_mapping).
        await _update_rooms_progress(
            guild, tournament, key, match_type, match_index,
            stats_channel,
        )
        return "publish_fail"

    await _update_rooms_progress(
        guild, tournament, key, match_type, match_index, stats_channel,
    )
    return "ok"


async def _find_room_message_id(
    stats_channel, guild_id: int, channel_id: int,
    match_type: str, match_index: int, room_index: int,
) -> int | None:
    """Найти уже опубликованное сообщение комнаты по custom_id
    кнопок маппинга (room_map:gid:cid:mt:mi:room:team).

    Сценарий: Discord send успешен, но store.set(message_id)
    упал — при повторной попытке нельзя слать дубль. Ищем в
    истории канала сообщение с кнопками этой комнаты.
    """
    prefix = (
        f"room_map:{guild_id}:{channel_id}:{match_type}:"
        f"{match_index}:{room_index}:"
    )
    try:
        async for msg in stats_channel.history(limit=100):
            for row in getattr(msg, "components", None) or []:
                for comp in getattr(row, "children", []) or []:
                    cid = getattr(comp, "custom_id", "") or ""
                    if cid.startswith(prefix):
                        return msg.id
    except Exception:
        logger.debug("room message lookup failed", exc_info=True)
    return None


async def _republish_room_interface(
    guild: discord.Guild,
    channel,
    tournament,
    key: str,
    session: dict,
    stats_channel,
    room_index: int,
    items: list[dict],
    match_type: str,
    match_index: int,
    shas: list[str],
) -> str:
    """Повторно опубликовать ТОЛЬКО интерфейс комнаты (скриншоты +
    кнопки сопоставления), если OCR уже завершён, но сообщение не
    было отправлено.

    Статистика не пересчитывается и не дублируется: panels/sha уже
    сохранены в сессии, ELO начисляется только при финализации.
    Отменённые/финализированные сессии и устаревшие gen пропускаются.
    """
    fresh = store.get(guild.id, tournament.channel_id)
    fs = (fresh.ocr_sessions.get(key) if fresh else None) or {}
    if (
        fs.get("mode") != "rooms"
        or fs.get("status") not in ("collecting", "awaiting_mapping")
        or fs.get("gen") != session.get("gen", 1)
    ):
        return "cancelled"
    room = next(
        (
            r for r in fs.get("rooms") or []
            if int(r.get("room_index", 0)) == room_index
        ),
        None,
    )
    if room is None or not _room_has_panels(room) or room.get("message_id"):
        return "room_done"

    # SHA-верификация: восстанавливаем ТОЛЬКО исходный комплект
    # файлов — иначе можно опубликовать чужой скриншот со старой
    # статистикой. Classic: room["sha"]; squad: room["shas"]
    # (1–2 файла, порядок не важен).
    expected = list(room.get("shas") or [])
    if room.get("sha") and room["sha"] not in expected:
        expected.append(room["sha"])
    if sorted(shas) != sorted(expected):
        logger.warning(
            "Room %s republish rejected: sha mismatch "
            "(got %s, expected %s)",
            room_index, shas, expected,
        )
        return "sha_mismatch"

    # Идемпотентность: если send прошёл, а сохранение message_id
    # упало — сообщение уже есть в канале, дубль не нужен.
    existing = await _find_room_message_id(
        stats_channel, guild.id, tournament.channel_id,
        match_type, match_index, room_index,
    )
    if existing is not None:
        room["message_id"] = existing
        ids = room.setdefault("message_ids", [])
        if existing not in ids:
            ids.append(existing)
        store.set(fresh)
        await _update_rooms_progress(
            guild, fresh, key, match_type, match_index, stats_channel,
        )
        return "ok"

    for attempt in range(2):
        try:
            await _publish_room_message(
                stats_channel, fresh, channel, items, room,
                match_type, match_index,
            )
            break
        except Exception as e:
            logger.warning(
                "Room interface republish attempt %d failed: %s",
                attempt + 1, e,
            )
    else:
        return "publish_fail"

    # Карточка восстановлена — техническое collect-сообщение
    # (publish_fail-ветка finalize его сохраняла) больше не нужно:
    # удаляем, в канале остаётся одна публичная карточка комнаты.
    cmid = room.get("collect_message_id")
    if cmid and stats_channel is not None:
        try:
            cmsg = await stats_channel.fetch_message(int(cmid))
            await cmsg.delete()
            room["collect_message_id"] = None
            store.set(fresh)
        except Exception:
            logger.debug("republish collect cleanup failed",
                         exc_info=True)

    await _update_rooms_progress(
        guild, fresh, key, match_type, match_index, stats_channel,
    )
    return "ok"


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


async def _maybe_delete_match_channel(
    guild: discord.Guild, tournament, match_type: str, match_index: int
) -> None:
    """Удалить выделенный канал матча после фиксации результата.

    Вызывается из finalize_ocr_match — к этому моменту карточка
    сопоставления и результат уже опубликованы, канал больше не
    нужен. Раньше удаление происходило при загрузке скриншота, но
    для MM-матчей сообщение сопоставления живёт в этом же канале —
    канал удалялся до отправки (404 Unknown Channel).
    """
    channels = getattr(tournament, "match_channels", None) or {}
    if not channels:
        return
    session = (tournament.ocr_sessions or {}).get(
        _session_key(match_type, match_index)
    ) or {}
    if session.get("mode") == "squad":
        loaded = len(session.get("squads", {})) + len(
            session.get("squad_pool", [])
        )
        if loaded < 2:
            return
    elif not session:
        return

    # Кандидаты: все каналы из match_channels + preset-канал MM
    # (match_channels хранит одну запись на канал — последняя
    # анонсированная, поэтому preset проверяем отдельно).
    candidate_ids = {int(c) for c in channels}
    preset = getattr(tournament, "mm_preset_channel_id", 0) or 0
    if preset:
        candidate_ids.add(int(preset))

    for cid in candidate_ids:
        mc = channels.get(str(cid)) or {}
        is_pool = cid in MM_MATCH_CHANNEL_POOL
        if (
            not is_pool
            and (
                mc.get("match_type") != match_type
                or mc.get("match_index") != match_index
            )
        ):
            continue
        try:
            channel = guild.get_channel(cid)
            if channel is None:
                channel = await guild.fetch_channel(cid)
            if channel is None:
                channels.pop(str(cid), None)
                continue
            if is_pool:
                # Канал пула не удаляем — возвращаем слот через единый
                # менеджер (lockdown overwrites + очистка истории → FREE).
                # Сбрасываем, когда завершены все матчи, анонсированные
                # именно в этот канал.
                if _pool_channel_idle(tournament, cid, mc):
                    from services import match_channel_pool
                    await match_channel_pool.release_channel(channel)
                    channels.pop(str(cid), None)
            else:
                await channel.delete(reason="Результат матча загружен")
                logger.info("Match channel %s deleted", cid)
                channels.pop(str(cid), None)
        except Exception as exc:
            logger.warning("Failed to delete match channel %s: %s", cid, exc)
            channels.pop(str(cid), None)
    tournament.match_channels = channels
    store.set(tournament)


def _pool_channel_idle(tournament, channel_id: int, entry: dict) -> bool:
    """True, если канал пула можно сбрасывать: все матчи, идущие
    в этом канале, завершены.

    entry — запись tournament.match_channels для канала. Список
    matches заполняется анонсером; для старых записей без него
    берём матч из match_type/match_index, а для preset-канала MM
    без записи — все матчи текущей фазы.
    """
    pairs = entry.get("matches") or []
    if not pairs:
        mt, mi = entry.get("match_type"), entry.get("match_index")
        if mt is not None and mi is not None:
            pairs = [(mt, mi)]
    if not pairs and getattr(tournament, "mm_preset_channel_id", 0) == channel_id:
        try:
            from services.match_announcer import _phase_matches

            pairs = [
                (mt, mi) for mt, mi, _a, _b in _phase_matches(tournament)
            ]
        except Exception as exc:
            logger.warning("Pool idle check failed: %s", exc)
            return False
    if not pairs:
        return True
    try:
        from views.ocr_match_view import match_is_finished

        return all(
            match_is_finished(tournament, mt, mi) for mt, mi in pairs
        )
    except Exception as exc:
        logger.warning("Pool idle check failed: %s", exc)
        return False


async def _reset_pool_channel(channel) -> None:
    """Вернуть канал пула в дефолт: overwrites категории + чистая история."""
    try:
        await channel.edit(
            overwrites={}, reason="MM match channel pool reset")
    except Exception as exc:
        logger.warning("Pool channel %s overwrites reset failed: %s",
                       channel.id, exc)
    try:
        for p in await channel.pins():
            try:
                await p.unpin()
            except Exception:
                pass
    except Exception:
        pass
    try:
        while True:
            msgs = [m async for m in channel.history(limit=100)]
            if not msgs:
                break
            try:
                await channel.delete_messages(
                    msgs, reason="MM channel pool reset")
            except Exception:
                for m in msgs:
                    try:
                        await m.delete()
                    except Exception:
                        pass
            if len(msgs) < 100:
                break
    except Exception as exc:
        logger.warning("Pool channel %s purge failed: %s",
                       channel.id, exc)
    logger.info("Pool channel %s reset to defaults", channel.id)


# ---------------------------------------------------------------------------
# UI мультикомнатной загрузки: выбор числа комнат → сбор файлов → прогресс
# ---------------------------------------------------------------------------

async def create_rooms_session(
    guild: discord.Guild,
    channel: discord.abc.Messageable,
    user: discord.User,
    tournament,
    match_type: str,
    match_index: int,
    count: int,
):
    """Создать rooms-сессию и сообщение прогресса. None — уже есть сессия."""
    key = _session_key(match_type, match_index)
    # Лок на матч: два капитана могут нажать «Отправить результат»
    # одновременно — без лока создались бы две сессии.
    async with _room_lock(key, getattr(tournament, "id", "") or ""):
        tournament = (
            store.get(guild.id, tournament.channel_id) or tournament
        )
        existing = tournament.ocr_sessions.get(key)
        if existing and existing.get("status") not in ("completed", "cancelled"):
            return None
        t1, t2 = _match_tuple(tournament, match_type, match_index)
        # Пакетный флоу (bundle): 1 скриншот на комнату, файлы
        # привязываются по порядку, единая кнопка «Завершить
        # загрузку» — для обычных турниров /tournament create
        # и MM/Ranked. Для MM ocr_channel = канал матча (см.
        # ниже): все сообщения остаются внутри канала матча,
        # OCR-канал 1570473950218158180 не используется.
        is_plain = not getattr(tournament, "mm_search_channel_id", 0)
        session = {
            "match_type": match_type,
            "match_index": match_index,
            "mode": "rooms",
            "status": "collecting",
            "rooms_expected": int(count),
            "rooms_received": 0,
            "rooms": [],
            "bundle_v2": True,
            "gen": 1,
            "uploaded_by": user.id,
            "uploaded_by_name": getattr(user, "display_name", user.name),
            # Идентификаторы жизненного цикла (ТЗ Ranked MM):
            # session_id/generation инвалидируют устаревшие callbacks,
            # winner_pending → winning_team_index фиксируется первым
            # принятым файлом (_rooms_claim_bundle).
            "session_id": uuid.uuid4().hex[:16],
            "match_id": f"{getattr(tournament, 'id', '?')}_{key}",
            "guild_id": guild.id,
            "winner_pending": True,
            "winning_team_index": None,
            "teams": [t1, t2],
            "maps": {},
            "panel_picks": {},
            "card_state": "active",
            "created_at": datetime.now().isoformat(),
        }
        tournament.ocr_sessions[key] = session

        # Канал OCR-сообщений комнаты: MM-матчи — канал матча
        # (channel), обычные турниры — OCR_MAPPING_CHANNEL_ID.
        # ID фиксируется в сессии: все shot/collect/карточки
        # одной комнаты живут в одном канале, старые сессии не
        # переносятся.
        ocr_channel = channel
        if not getattr(tournament, "mm_search_channel_id", 0):
            try:
                ocr_channel = (
                    guild.get_channel(OCR_MAPPING_CHANNEL_ID)
                    or await guild.fetch_channel(OCR_MAPPING_CHANNEL_ID)
                )
            except Exception as exc:
                logger.warning(
                    "OCR_MAPPING_CHANNEL_ID resolve failed: %s", exc
                )
                ocr_channel = None
            if ocr_channel is None:
                ocr_channel = channel
        session["ocr_channel_id"] = getattr(ocr_channel, "id", channel.id)

        view = RoomCollectView(
            guild.id, tournament.channel_id, tournament,
            match_type, match_index, session["gen"],
        )
        prog_text = replace_emojis(_rooms_progress_text(session))
        # Переиспользуем рабочее сообщение матча через единую точку
        # ensure_work_card — одно сообщение на матч, дубли
        # исключены per-match lock'ом и reconciliation-сканом.
        # Для MM отправляем новое в канале матча.
        if is_plain:
            from services.match_announcer import ensure_work_card

            _f, ctrl, msg, created = await ensure_work_card(
                guild, tournament, match_type, match_index,
                ocr_channel, view=view, text=prog_text,
            )
            if not created and msg is not None:
                try:
                    await msg.edit(content=prog_text, view=view)
                except Exception:
                    pass
            if ctrl is not None:
                # Сообщение перешло под управление progress-флоу —
                # sig контрольного текста недействителен, после
                # завершения/отката refresh обязан отредактировать.
                ctrl.pop("control_sig", None)
        else:
            msg = await ocr_channel.send(
                content=prog_text,
                view=view,
            )
        session["progress_message_id"] = msg.id
        session["progress_channel_id"] = ocr_channel.id
        store.set(tournament)
        return session


def _is_strict_admin(user) -> bool:
    """Строгий админ для полного отката: ТОЛЬКО server administrator.

    BOT_OWNER_ID намеренно НЕ даёт доступа — владелец бота без
    administrator-прав на конкретном сервере не может откатывать
    чужие матчи. guild_permissions может отсутствовать у моков
    и DM-user — тогда False.
    """
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and getattr(perms, "administrator", False))


def _mm_winner_for_uploader(tournament, t1, t2, user_id):
    """Команда Discord-пользователя среди команд матча (Ranked MM).

    Победитель = команда капитана, отправившего первый принятый
    файл. Не-участник (админ) → None: команду выберет организатор
    явно при подтверждении. None и для не-MM турниров.
    """
    if not getattr(tournament, "mm_search_channel_id", None):
        return None
    try:
        from views.ocr_match_view import _team_of_user

        return _team_of_user(tournament, (t1, t2), user_id)
    except Exception:
        return None


def _mm_upload_allowed(existing: dict, tournament, user) -> bool:
    """Ranked MM classic: замену/дозагрузку может делать только
    отправитель комплекта, капитан зафиксированной команды-
    победителя или Discord-администратор — чужой капитан не может
    перехватить загрузку и сбросить сопоставление."""
    uid = int(getattr(user, "id", 0) or 0)
    if uid and uid == int(existing.get("uploaded_by") or 0):
        return True
    if _is_strict_admin(user):
        return True
    wti = existing.get("winning_team_index")
    try:
        wi = int(wti) if wti is not None else None
    except (TypeError, ValueError):
        wi = None
    if wi is not None:
        try:
            from views.ocr_match_view import _uploaded_by_is_captain_of

            if _uploaded_by_is_captain_of(tournament, wi, uid):
                return True
        except Exception:
            pass
    return False


def _release_pending_upload_claim(
    tournament, key: str, user_id
) -> bool:
    """Снять пустой pending-клейм после неудачного OCR.

    Клейм создаётся ДО распознавания, чтобы победителем стал первый
    ПРИНЯТЫЙ файл, а не первый завершивший OCR. Если OCR/валидация
    не удалась и данных в сессии нет — клейм снимается: другие
    капитаны не блокируются навсегда, повторная загрузка возможна.
    Вызывать под _room_lock. True — клейм снят.
    """
    s = tournament.ocr_sessions.get(key)
    if not s or s.get("status") != "awaiting_ocr":
        return False
    try:
        if int(s.get("uploaded_by") or 0) != int(user_id):
            return False
    except (TypeError, ValueError):
        return False
    if (
        s.get("squads")
        or s.get("squad_pool")
        or s.get("maps")
        or s.get("panels")
    ):
        return False
    tournament.ocr_sessions.pop(key, None)
    store.set(tournament)
    return True


def _is_pending_claim_owner(session, user_id) -> bool:
    """True — сессия является pending-клеймом, принадлежащим user_id.

    Pending-клейм (status="awaiting_ocr", mode="pending") создаётся
    до OCR и переживает рестарт: владелец может повторно отправить
    файл, чужие капитаны — нет. Вызывать под _room_lock.
    """
    if not session or session.get("status") != "awaiting_ocr":
        return False
    if session.get("mode") != "pending":
        return False
    try:
        return int(session.get("uploaded_by") or 0) == int(user_id)
    except (TypeError, ValueError):
        return False


def _rooms_bundle_claimed(session: dict) -> bool:
    """Комплект считается занятым после первого принятого файла.

    До этого любой капитан или администратор может начать/продолжить
    пустую сессию — победитель зафиксируется отправителем первого
    файла. Для legacy-сессий без winner_pending занятость выводится
    из наличия комнат/победителя.
    """
    if session.get("winner_pending") is False:
        return True
    if session.get("winning_team_index") is not None:
        return True
    return bool(session.get("rooms") or session.get("rooms_received"))


def _rooms_file_submit_allowed(tournament, session: dict, user) -> bool:
    """Права на отправку файлов в rooms-комплект.

    Ranked MM (mm_search_channel_id): незанятый комплект могут начать
    капитан любой из команд или администратор; занятый — только его
    отправитель, капитан уже зафиксированной команды-победителя или
    Discord-администратор. Обычные турниры — прежнее правило
    (капитан/организатор любой из команд матча).
    """
    teams = session.get("teams") or []
    if not getattr(tournament, "mm_search_channel_id", None):
        from views.ocr_match_view import _can_use_ocr

        return _can_use_ocr(user, tournament, teams)
    uid = int(getattr(user, "id", 0) or 0)
    if _rooms_bundle_claimed(session):
        if uid and uid == int(session.get("uploaded_by") or 0):
            return True
        if _is_strict_admin(user):
            return True
        wti = session.get("winning_team_index")
        try:
            wi = int(wti) if wti is not None else None
        except (TypeError, ValueError):
            wi = None
        if wi is not None:
            from views.ocr_match_view import _uploaded_by_is_captain_of

            if _uploaded_by_is_captain_of(tournament, wi, uid):
                return True
        else:
            # Комплект занял не-участник (админ/орг): победитель ещё
            # не зафиксирован — капитаны могут помогать с загрузкой,
            # команду-победителя явно выберет администратор.
            from views.ocr_match_view import _is_match_captain

            if any(_is_match_captain(user, tournament, i) for i in teams):
                return True
        return False
    # Незанятый комплект: только капитан одной из команд матча или
    # Discord-администратор — посторонний «организатор» не должен
    # занять комплект и заблокировать капитанов до админ-отката.
    from views.ocr_match_view import _is_match_captain

    if any(_is_match_captain(user, tournament, i) for i in teams):
        return True
    return _is_strict_admin(user)


async def _rooms_claim_bundle(
    guild: discord.Guild,
    tournament,
    key: str,
    session: dict,
    user,
):
    """Зафиксировать отправителя первого принятого файла комплекта.

    Возвращает (status, tournament, session): «ok» — можно слать файлы
    (tournament/session могут быть свежими из store), «forbidden» —
    чужой занятый комплект, «stale» — сессия устарела/отменена.
    Победитель = команда отправителя и дальше не меняется.
    """
    if _rooms_bundle_claimed(session):
        if _rooms_file_submit_allowed(tournament, session, user):
            return "ok", tournament, session
        return "forbidden", tournament, session
    async with _room_lock(key, getattr(tournament, "id", "") or ""):
        fresh = store.get(guild.id, tournament.channel_id) or tournament
        s = fresh.ocr_sessions.get(key)
        if (
            not s
            or s.get("mode") != "rooms"
            or s.get("status") != "collecting"
            or int(s.get("gen", 0)) != int(session.get("gen", 0))
        ):
            return "stale", tournament, session
        if _rooms_bundle_claimed(s):
            # Параллельная загрузка успела первой — обычная проверка
            # прав на уже занятый комплект.
            if _rooms_file_submit_allowed(fresh, s, user):
                return "ok", fresh, s
            return "forbidden", fresh, s
        # Defense in depth: права перепроверяются и на незанятом
        # комплекте (вызывающий код уже проверил, но состояние могло
        # измениться между проверкой и захватом лока).
        if not _rooms_file_submit_allowed(fresh, s, user):
            return "forbidden", fresh, s
        s["winner_pending"] = False
        # Запоминаем инициатора комплекта: если ЕГО первый файл будет
        # отклонён (invalid/parse_fail), claim откатывается и права
        # возвращаются исходному состоянию.
        s.setdefault("creator_id", s.get("uploaded_by"))
        s.setdefault("creator_name", s.get("uploaded_by_name"))
        s["uploaded_by"] = int(getattr(user, "id", 0) or 0)
        s["uploaded_by_name"] = (
            getattr(user, "display_name", None)
            or getattr(user, "name", "")
            or str(user.id)
        )
        try:
            from views.ocr_match_view import _team_of_user

            teams = s.get("teams") or []
            winner = (
                _team_of_user(fresh, teams, user.id) if teams else None
            )
        except Exception:
            winner = None
        if winner is not None:
            s["winning_team_index"] = int(winner)
        store.set(fresh)
        return "ok", fresh, s


# Коды process_room_bundle, при которых файл НЕ принят: если это
# был первый файл незанятого комплекта, claim откатывается —
# отправитель не должен навсегда занять комплект и закрепить за
# собой команду-победителя, а карточка матча не должна терять
# кнопку отправки.
_BUNDLE_REJECT_CODES = frozenset(
    {"invalid", "invalid_mix", "parse_fail", "error", "dup"}
)


async def _rooms_release_claim(
    guild: discord.Guild,
    channel_id: int,
    key: str,
    session: dict,
    tournament_id: str = "",
) -> None:
    """Откат фиксации комплекта после отклонённой первой загрузки.

    Срабатывает только если в сессии всё ещё нет ни одной комнаты
    с panels — иначе комплект уже легитимно занят ранее принятым
    файлом и трогать его нельзя.
    """
    async with _room_lock(key, tournament_id):
        fresh = store.get(guild.id, channel_id)
        s = (fresh.ocr_sessions.get(key) if fresh else None)
        if (
            not s
            or s.get("mode") != "rooms"
            or s.get("status") != "collecting"
            or int(s.get("gen", 0)) != int(session.get("gen", 0))
        ):
            return
        rooms = s.get("rooms") or []
        if any(
            _room_has_panels(r) or r.get("pending") for r in rooms
        ):
            return
        # Убираем пустые комнаты-огрызки отклонённых попыток.
        s["rooms"] = [
            r for r in rooms
            if _room_has_panels(r) or r.get("pending")
        ]
        s["rooms_received"] = _rooms_completed_count(s)
        s["winner_pending"] = True
        s["winning_team_index"] = None
        s["uploaded_by"] = s.get("creator_id", s.get("uploaded_by"))
        s["uploaded_by_name"] = s.get(
            "creator_name", s.get("uploaded_by_name")
        )
        store.set(fresh)


async def cancel_rooms_session(
    guild: discord.Guild,
    user,
    tournament,
    match_type: str,
    match_index: int,
) -> str:
    """Полный административный откат OCR-комплекта любого режима
    (rooms/classic/squad). «ok»|«none»|«done»|«forbidden»."""
    from config import BOT_OWNER_ID

    key = _session_key(match_type, match_index)
    session = tournament.ocr_sessions.get(key)
    if not session:
        return "none"
    if session.get("status") in ("completed", "cancelled"):
        return "done"
    # Строгая проверка: только Discord-администратор сервера или
    # владелец бота — не роль «организатор» турнира.
    if not _is_strict_admin(user):
        return "forbidden"

    async with _room_lock(key, getattr(tournament, "id", "") or ""):
        # Повторная проверка под локом — статус мог измениться
        tournament = store.get(guild.id, tournament.channel_id) or tournament
        session = tournament.ocr_sessions.get(key)
        if not session:
            return "none"
        if session.get("status") in ("completed", "cancelled"):
            return "done"
        # Финализация держит тот же _room_lock — сюда можно попасть
        # только до её старта или после полного завершения. Дополнительно
        # проверяем processed_elo_matches на случай рассинхрона статусов.
        from views.ocr_match_view import match_is_finished

        if match_is_finished(tournament, match_type, match_index):
            return "done"
        session["gen"] = int(session.get("gen", 1)) + 1
        session["status"] = "cancelled"
        # Удаляем сессию целиком — поздние OCR-задачи увидят
        # отсутствие/несовпадение gen и не запишут результат.
        tournament.ocr_sessions.pop(key, None)
        _seq_purge_session(guild.id, tournament.channel_id, key)
        store.set(tournament)

    # Карточка матча → обратно в активное состояние
    try:
        from services.match_announcer import update_match_card

        await update_match_card(
            guild, tournament, match_type, match_index, state="active"
        )
    except Exception as e:
        logger.warning("rooms cancel card update failed: %s", e)

    # Гасим сообщения комнат и прогресса (best effort).
    # Канал — сохранённый в сессии (ocr_channel_id), fallback —
    # progress_channel_id для старых сессий.
    channel = None
    for ck in ("ocr_channel_id", "progress_channel_id"):
        try:
            cid = int(session.get(ck) or 0)
            if cid:
                channel = guild.get_channel(cid) or await guild.fetch_channel(cid)
                if channel is not None:
                    break
        except Exception:
            pass
    # progress_message_id для обычных турниров == work_message_id:
    # это рабочая карточка матча, а не временное сообщение. Её
    # нельзя гасить текстом «комплект отменён» — финальное
    # состояние «Ожидание скриншотов» + кнопка загрузки
    # выставляется через update_match_card/refresh_match_control.
    _mc_entry = (getattr(tournament, "match_cards", None) or {}).get(key) or {}
    _work_mid = _mc_entry.get("work_message_id")
    if channel is not None:
        try:
            prog_id = session.get("progress_message_id")
            if prog_id and (
                not _work_mid or int(prog_id) != int(_work_mid)
            ):
                prog = await channel.fetch_message(prog_id)
                await prog.edit(
                    content=replace_emojis(
                        "❌ Комплект отменён · можно загрузить заново."
                    ),
                    view=None,
                )
        except Exception as e:
            logger.warning("rooms progress edit failed: %s", e)
        for room in session.get("rooms") or []:
            # Все сообщения комнаты: карточка результата, shot-
            # сообщения со скриншотами, техническое collect-
            # сообщение. Скриншоты отменённого комплекта не
            # должны оставаться в канале — сообщения с
            # вложениями удаляем; если удалить нельзя — снимаем
            # вложения и кнопки, оставляя короткий статус.
            mids = list(room.get("message_ids") or [])
            mids += list(room.get("shot_message_ids") or [])
            # V5: до финализации shot-id лежат в pending —
            # без них скриншоты collecting-комнаты остались бы.
            for p in room.get("pending") or []:
                if p.get("shot_message_id"):
                    mids.append(p["shot_message_id"])
            if room.get("message_id"):
                mids.append(room["message_id"])
            if room.get("collect_message_id"):
                mids.append(room["collect_message_id"])
            for mid in dict.fromkeys(mids):
                try:
                    msg = await channel.fetch_message(mid)
                    if getattr(msg, "attachments", None):
                        try:
                            await msg.delete()
                            continue
                        except Exception:
                            pass
                    await msg.edit(
                        content=replace_emojis(
                            f"**Комната {room.get('room_index')}** — "
                            "❌ комплект отменён."
                        ),
                        view=None,
                        attachments=[],
                    )
                except Exception as e:
                    logger.warning(
                        "room message cancel cleanup failed: %s", e
                    )
    # Аудит отмены: администратор, match_id, время — в лог и Discord.
    match_id = f"{tournament.id}_{key}"
    logger.info(
        "rooms OCR bundle cancelled by admin: user_id=%s match_id=%s",
        getattr(user, "id", "?"), match_id,
    )
    try:
        log_ch = guild.get_channel(OCR_STATS_CHANNEL_ID)
        if log_ch is None:
            log_ch = await guild.fetch_channel(OCR_STATS_CHANNEL_ID)
        await log_ch.send(
            content=replace_emojis(
                "🛑 **Комплект скриншотов отменён**\n"
                f"Администратор: {getattr(user, 'mention', None) or user} "
                f"(`{getattr(user, 'id', '?')}`)\n"
                f"Матч: `{match_id}`\n"
                f"Время: {datetime.now():%d.%m.%Y %H:%M:%S}"
            )
        )
    except Exception as e:
        logger.debug("rooms cancel audit log failed: %s", e)
    return "ok"


class RoomCountSelectView(View):
    """«СКОЛЬКО КОМНАТ БЫЛО СЫГРАНО?» — выбор 1..10 (ephemeral)."""

    def __init__(
        self, guild_id: int, channel_id: int, tournament,
        match_type: str, match_index: int,
    ):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.tournament = tournament
        self.select = Select(
            placeholder="Количество комнат (1–10)",
            options=[
                discord.SelectOption(label=f"{n}", value=str(n))
                for n in range(1, 11)
            ],
            min_values=1,
            max_values=1,
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        try:
            count = int((self.select.values or ["1"])[0])
        except (TypeError, ValueError):
            count = 1
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        session = await create_rooms_session(
            interaction.guild, interaction.channel, interaction.user,
            tournament, self.match_type, self.match_index, count,
        )
        if session is None:
            await interaction.response.send_message(
                replace_emojis(
                    "⚠️ Для этого матча уже идёт загрузка скриншотов."
                ),
                ephemeral=True,
            )
            return
        # Модалка первой комнаты — первый ответ на select-interaction.
        # При сбое открытия сессия НЕ отменяется: она остаётся
        # «collecting» и доступна для повтора через «Добавить
        # скриншоты» / отмены — OCR-комплект не блокируется.
        try:
            await interaction.response.send_modal(
                RoomUploadModal(
                    self.guild_id, self.channel_id, tournament,
                    self.match_type, self.match_index, 1, session["gen"],
                )
            )
        except Exception as exc:
            logger.error(
                "Не удалось открыть модалку загрузки комнаты 1 "
                "(match %s_%s): %s",
                self.match_type, self.match_index, exc, exc_info=True,
            )
            try:
                await interaction.response.send_message(
                    replace_emojis(
                        "⚠️ Не удалось открыть окно загрузки файлов — "
                        "сессия активна, повторите через кнопку "
                        "«Добавить скриншоты»."
                    ),
                    ephemeral=True,
                )
            except Exception:
                pass


class RoomUploadModal(Modal):
    """Один скриншот ОДНОЙ явно выбранной комнаты (V5 sequential).

    За раз принимается ровно 1 файл; второй скриншот той же комнаты
    добавляется отдельной загрузкой через «Добавить скриншот»,
    завершение — кнопкой «Завершить комнату».
    """

    def __init__(
        self, guild_id: int, channel_id: int, tournament,
        match_type: str, match_index: int, room_index: int, gen: int,
    ):
        # FileUpload в discord.py 2.7 не принимает label — текст
        # задаётся через Label-обёртку (как в ScreenshotUploadModal).
        self.upload = Label(
            text=f"Скриншот комнаты {room_index} (1 файл)",
            component=FileUpload(
                required=True,
                min_values=1,
                max_values=1,
            ),
        )
        super().__init__(title=f"Комната {room_index}", timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.room_index = room_index
        self.gen = gen
        self.add_item(self.upload)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        upload = self.upload.component
        files = list(getattr(upload, "values", None) or [])
        await interaction.response.defer(ephemeral=True)
        if not files:
            await interaction.followup.send(
                replace_emojis("❌ Файлы не получены — попробуйте ещё раз."),
                ephemeral=True,
            )
            return
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.followup.send(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        key = _session_key(self.match_type, self.match_index)
        session = tournament.ocr_sessions.get(key)
        # awaiting_mapping допускаем: модалка открывается только для
        # комнаты, требующей восстановления интерфейса (publish_fail) —
        # process_room_bundle проверит SHA и опубликует без OCR.
        if (
            not session
            or session.get("mode") != "rooms"
            or session.get("status") not in ("collecting", "awaiting_mapping")
            or int(session.get("gen", 0)) != int(self.gen)
        ):
            await interaction.followup.send(
                replace_emojis(
                    "⚠️ Сессия загрузки устарела или отменена — "
                    "начните заново."
                ),
                ephemeral=True,
            )
            return
        # Права — кто может слать файлы в этот комплект
        if not _rooms_file_submit_allowed(
            tournament, session, interaction.user
        ):
            await interaction.followup.send(
                replace_emojis(
                    "❌ Этот комплект может продолжить только его "
                    "отправитель, капитан команды-победителя "
                    "или администратор."
                ),
                ephemeral=True,
            )
            return
        items = []
        for f in files:
            try:
                data = await f.read()
            except Exception:
                data = None
            if not data:
                await interaction.followup.send(
                    replace_emojis("❌ Не удалось прочитать файл."),
                    ephemeral=True,
                )
                return
            if len(data) > MAX_ATTACH:
                await interaction.followup.send(
                    replace_emojis("❌ Файл слишком большой (макс. 10 МБ)."),
                    ephemeral=True,
                )
                return
            items.append({"filename": f.filename, "data": data})
        await _accept_room_file(
            interaction, tournament, session, key,
            self.channel_id, self.match_type, self.match_index,
            self.room_index, items[0],
        )


class BundleUploadModal(Modal):
    """Дозагрузка скриншотов bundle-комплекта (/tournament create).

    Принимает до max_files изображений за раз; файлы привязываются
    к комнатам по порядку: сначала комнаты, ожидающие повторной
    отправки (needs_reupload/shot_pending), затем пустые — по
    возрастанию room_index. Одна комната = один файл.
    """

    def __init__(
        self, guild_id: int, channel_id: int, tournament,
        match_type: str, match_index: int, gen: int,
        max_files: int,
    ):
        self.upload = Label(
            text=(
                # label ≤ 45 символов (Discord 50035).
                f"Скриншоты по порядку (до {max_files})"
            ),
            component=FileUpload(
                required=True,
                min_values=1,
                max_values=max(1, min(int(max_files), 10)),
            ),
        )
        super().__init__(title="Добавить скриншоты", timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.gen = gen
        self.max_files = max_files
        self.add_item(self.upload)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        files = list(
            getattr(self.upload.component, "values", None) or []
        )
        if not files:
            await interaction.followup.send(
                replace_emojis("❌ Файлы не получены — попробуйте ещё раз."),
                ephemeral=True,
            )
            return
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.followup.send(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        key = _session_key(self.match_type, self.match_index)
        session = (tournament.ocr_sessions or {}).get(key)
        if (
            not _session_bundle(session)
            or session.get("status") != "collecting"
            or int(session.get("gen", 0)) != int(self.gen)
        ):
            await interaction.followup.send(
                replace_emojis(
                    "⚠️ Сессия загрузки устарела или отменена — "
                    "начните заново."
                ),
                ephemeral=True,
            )
            return
        if not _rooms_file_submit_allowed(
            tournament, session, interaction.user
        ):
            await interaction.followup.send(
                replace_emojis(
                    "❌ Этот комплект может продолжить только его "
                    "отправитель, капитан команды-победителя "
                    "или администратор."
                ),
                ephemeral=True,
            )
            return

        items = []
        for f in files:
            try:
                data = await f.read()
            except Exception:
                data = None
            if not data:
                await interaction.followup.send(
                    replace_emojis("❌ Не удалось прочитать файл."),
                    ephemeral=True,
                )
                return
            if len(data) > MAX_ATTACH:
                await interaction.followup.send(
                    replace_emojis("❌ Файл слишком большой (макс. 10 МБ)."),
                    ephemeral=True,
                )
                return
            items.append({"filename": f.filename, "data": data})

        # Целевые комнаты: сначала ожидающие повторной отправки,
        # затем пустые — строго по порядку room_index.
        expected = int(session.get("rooms_expected") or 0)
        by_idx = _bundle_rooms_by_index(session)
        targets: list[int] = []
        for r in sorted(
            session.get("rooms") or [],
            key=lambda r: int(r.get("room_index", 0)),
        ):
            ridx = int(r.get("room_index", 0))
            if _bundle_room_needs_reupload(r):
                targets.append(ridx)
        for i in range(1, expected + 1):
            if not _bundle_room_filled(by_idx.get(i)):
                targets.append(i)
        targets = list(dict.fromkeys(targets))

        slots = len(targets)
        if slots <= 0:
            await interaction.followup.send(
                replace_emojis("✅ Комплект уже заполнен."),
                ephemeral=True,
            )
            return
        if len(items) > slots:
            await interaction.followup.send(
                replace_emojis(
                    f"⚠️ Свободных комнат осталось {slots}, а "
                    f"файлов {len(items)} — лишние не будут "
                    "приняты. Беру первые "
                    f"{slots} по порядку."
                ),
                ephemeral=True,
            )
        assignments = [
            (ridx, item)
            for ridx, item in zip(targets, items[:slots])
        ]
        await _accept_bundle_files(
            interaction, tournament, session, key,
            self.channel_id, self.match_type, self.match_index,
            assignments,
        )


async def _accept_room_file(
    interaction: discord.Interaction,
    tournament,
    session: dict,
    key: str,
    channel_id: int,
    match_type: str,
    match_index: int,
    room_index: int,
    item: dict,
) -> None:
    """Общий код приёма одного файла комнаты: claim отправителя,
    process_room_file, откат claim при отклонении, ephemeral-
    результат. Используется RoomUploadModal и RoomStartModal —
    OCR-пайплайн здесь не дублируется."""
    # Первый принятый файл фиксирует отправителя и его команду
    # как победителя (Ranked MM); у занятого комплекта права
    # перепроверяются под _room_lock — гонка двух капитанов.
    was_unclaimed = not _rooms_bundle_claimed(session)
    status, tournament, session = await _rooms_claim_bundle(
        interaction.guild, tournament, key, session, interaction.user
    )
    if status == "stale":
        await interaction.followup.send(
            replace_emojis(
                "⚠️ Сессия загрузки устарела или отменена — "
                "начните заново."
            ),
            ephemeral=True,
        )
        return
    if status == "forbidden":
        await interaction.followup.send(
            replace_emojis(
                "❌ Этот комплект может продолжить только его "
                "отправитель или администратор."
            ),
            ephemeral=True,
        )
        return
    # Канал комнаты — сохранённый в сессии (ocr_channel_id →
    # progress_channel_id), а не «текущий» канал interaction:
    # все shot/collect/карточки одной комнаты в одном канале.
    stats_channel = await _stats_channel_for(interaction.guild, session)
    if stats_channel is None:
        stats_channel = await _stats_channel(interaction, tournament)
    if stats_channel is None:
        stats_channel = interaction.channel
    res = await process_room_file(
        guild=interaction.guild,
        channel=stats_channel,
        user=interaction.user,
        tournament=tournament,
        match_type=match_type,
        match_index=match_index,
        key=key,
        session=session,
        stats_channel=stats_channel,
        room_index=room_index,
        item=item,
    )
    # Отклонённый ПЕРВЫЙ файл не должен навсегда занимать
    # комплект: откатываем claim (победитель/права) и убираем
    # пустую комнату — кнопка «Отправить результат» и загрузка
    # остаются доступными.
    if res in _BUNDLE_REJECT_CODES and was_unclaimed:
        await _rooms_release_claim(
            interaction.guild, channel_id, key, session,
            getattr(tournament, "id", "") or "",
        )
    # Единый счётчик комнаты: сообщаем, сколько файлов ещё
    # обязательно загрузить (или что комплект полон).
    ok_extra = ""
    remaining = 0
    if res in ("ok", "added"):
        try:
            r = next(
                (
                    x for x in session.get("rooms") or []
                    if int(x.get("room_index", 0)) == room_index
                ),
                None,
            )
            if r is not None:
                _a, _req, remaining, _cf = room_file_progress(r)
        except Exception:
            pass
    messages = {
        "ok": (
            f"✅ Скриншот принят в комнату {room_index}. "
            + (
                f"Осталось загрузить ещё {remaining}."
                if remaining
                else "Все скриншоты приняты — можно завершить "
                "комнату."
            )
        ),
        "added": (
            f"✅ Скриншот принят в комнату {room_index}. "
            + (
                f"Осталось загрузить ещё {remaining}."
                if remaining
                else "Все скриншоты приняты — можно завершить "
                "комнату."
            )
        ),
        "shot_pending": (
            "⚠️ Статистика распознана и сохранена, но "
            "скриншот не удалось опубликовать в канал — "
            "отправьте тот же файл ещё раз кнопкой «Добавить "
            "скриншот» (OCR повторно не запустится)."
        ),
        "files_missing": (
            "⚠️ Часть исходных скриншотов комнаты утеряна "
            "(сообщение с файлом недоступно) — прикрепите их "
            "повторно кнопкой «Добавить скриншот»: "
            "распознанная статистика сохранена."
        ),
        "room_full": (
            f"⚠️ В комнате {room_index} достаточно "
            "скриншотов — больше добавить нельзя. "
            + (
                "Комплект этой комнаты заполнен."
                if _session_bundle(session)
                else "Завершите комнату."
            )
        ),
        "invalid_classic": (
            "❌ Нужен полный экран результатов — скриншот "
            "покрывает обе команды сразу. Скриншот одного "
            "отряда в этом режиме не подходит."
        ),
        "dup": "⚠️ Этот скриншот уже загружен в комплект.",
        "invalid": (
            "❌ Не удалось определить тип скриншота — нужен "
            "полный экран CS 4x4 или скриншот отряда."
        ),
        "invalid_mix": (
            "❌ Второй полный скриншот в одной комнате не "
            "принимается — первый файл сохранён. Варианты: "
            "1 полный скриншот (+ опционально 1 скриншот "
            "отряда для сверки) или 1–2 скриншота отрядов."
        ),
        "room_done": (
            f"ℹ️ Комната {room_index} уже заполнена — "
            "замена только через откат администратором."
        ),
        "republish_partial": (
            "ℹ️ Скриншот учтён для восстановления интерфейса — "
            "загрузите остальные файлы этой комнаты."
        ),
        "republished": (
            "✅ Интерфейс комнаты восстановлен — сопоставление "
            "игроков доступно под сообщением комнаты."
        ),
        "parse_fail": (
            "❌ Не удалось распознать таблицу результатов — "
            "проверьте, что это экран CS 4x4.\n"
            "Ранее принятые скриншоты комнаты сохранены — "
            "повторите загрузку кнопкой «Добавить скриншот»."
        ),
        "cancelled": "⚠️ Сессия загрузки была отменена.",
        "sha_mismatch": (
            "❌ Файлы не совпадают с исходными скриншотами этой "
            "комнаты — восстановление интерфейса отклонено, "
            "статистика не изменена. Загрузите именно те файлы, "
            "которые распознавались."
        ),
        "publish_fail": (
            "❌ Статистика распознана, но сообщение с кнопками "
            "не удалось опубликовать. Повторите загрузку тех же "
            "скриншотов — интерфейс восстановится без повторного "
            "подсчёта. Если ошибка повторится — администратору."
        ),
    }
    await interaction.followup.send(
        replace_emojis(
            messages.get(
                res, "❌ Ошибка обработки — попробуйте ещё раз."
            )
        ),
        ephemeral=True,
    )


# Короткие причины для сводки пакетной загрузки — полные тексты
# живут в _accept_room_file, здесь только то, что нужно в итоге.
_BUNDLE_RESULT_SHORT = {
    "ok": "принят",
    "added": "принят",
    "dup": "дубликат",
    "invalid": "не распознан",
    "invalid_classic": "нужен полный экран (не скриншот отряда)",
    "invalid_mix": "конфликт типов файлов",
    "room_full": "комната уже заполнена",
    "room_done": "комната уже завершена",
    "parse_fail": "не удалось распознать таблицу",
    "shot_pending": "принят, но файл не сохранился — повторите",
    "publish_fail": "статистика сохранена, интерфейс не опубликован",
    "cancelled": "сессия отменена",
    "sha_mismatch": "файл не совпадает с оригиналом",
    "republish_partial": "учтён для восстановления",
    "republished": "интерфейс восстановлен",
    "files_missing": "исходный файл утерян — нужна повторная отправка",
    "closed": "сессия закрыта",
    "empty": "нет файлов",
    "incomplete": "комплект неполный",
    "forbidden": "нет прав",
    "error": "ошибка обработки",
}


async def _accept_bundle_files(
    interaction: discord.Interaction,
    tournament,
    session: dict,
    key: str,
    channel_id: int,
    match_type: str,
    match_index: int,
    assignments: list[tuple[int, dict]],
) -> None:
    """Пакетная приёмка файлов bundle-комплекта (/tournament create).

    assignments — пары (room_index, item) в порядке загрузки:
    файл j привязан к своей комнате, порядок не меняется.
    Claim отправителя фиксируется один раз, процессинг идёт через
    общий process_room_file (OCR/дедуп/блокировки — без
    дублирования), пользователю — один ephemeral-итог.
    """
    was_unclaimed = not _rooms_bundle_claimed(session)
    status, tournament, session = await _rooms_claim_bundle(
        interaction.guild, tournament, key, session, interaction.user
    )
    if status == "stale":
        await interaction.followup.send(
            replace_emojis(
                "⚠️ Сессия загрузки устарела или отменена — "
                "начните заново."
            ),
            ephemeral=True,
        )
        return
    if status == "forbidden":
        await interaction.followup.send(
            replace_emojis(
                "❌ Этот комплект может продолжить только его "
                "отправитель или администратор."
            ),
            ephemeral=True,
        )
        return
    stats_channel = await _stats_channel_for(interaction.guild, session)
    if stats_channel is None:
        stats_channel = await _stats_channel(interaction, tournament)
    if stats_channel is None:
        stats_channel = interaction.channel

    results: list[tuple[int, str]] = []
    for room_index, item in assignments:
        # store.get возвращает живой объект — состояние комнат
        # обновляется между итерациями без ручной передачи.
        fresh_t = (
            store.get(interaction.guild.id, channel_id) or tournament
        )
        fs = (fresh_t.ocr_sessions or {}).get(key) or session
        res = await process_room_file(
            guild=interaction.guild,
            channel=stats_channel,
            user=interaction.user,
            tournament=fresh_t,
            match_type=match_type,
            match_index=match_index,
            key=key,
            session=fs,
            stats_channel=stats_channel,
            room_index=room_index,
            item=item,
        )
        results.append((room_index, res))
        tournament = fresh_t
        session = fs

    # Ни один файл не принят и комплект был свободен — claim
    # откатываем, кнопка загрузки остаётся доступной.
    if (
        was_unclaimed
        and results
        and all(r in _BUNDLE_REJECT_CODES for _, r in results)
    ):
        await _rooms_release_claim(
            interaction.guild, channel_id, key, session,
            getattr(tournament, "id", "") or "",
        )

    accepted = [
        r for r, res in results if res in ("ok", "added")
    ]
    lines = []
    if accepted:
        lines.append(
            "✅ Принято скриншотов: "
            f"{len(accepted)} — комнаты "
            + ", ".join(str(r) for r in accepted)
            + "."
        )
    for r, res in results:
        if res in ("ok", "added"):
            continue
        short = _BUNDLE_RESULT_SHORT.get(res, "ошибка")
        lines.append(f"⚠️ Комната {r}: {short}.")
    exp = int(session.get("rooms_expected") or 0)
    filled = _bundle_files_accepted(session)
    if filled < exp:
        lines.append(
            f"Загружено {filled}/{exp} — остальные комнаты "
            "можно дозагрузить кнопкой «Добавить скриншоты»."
        )
    elif _bundle_upload_complete(session):
        lines.append(
            "Комплект полный — нажмите «Завершить загрузку»."
        )
    await interaction.followup.send(
        replace_emojis("\n".join(lines) or "⚠️ Файлы не приняты."),
        ephemeral=True,
    )


class RoomStartModal(Modal):
    """Старт rooms-загрузки одним окном: количество комнат
    (String Select, по умолчанию 1) + скриншот комнаты 1.

    Сессия создаётся ТОЛЬКО при отправке всей формы — выбор в
    списке сам по себе ничего не запускает, закрытие окна
    безопасно. OCR-пайплайн не дублируется: файл уходит в общий
    _accept_room_file → process_room_file.
    """

    def __init__(
        self, guild_id: int, channel_id: int,
        match_type: str, match_index: int, bundle: bool = False,
    ):
        options = []
        for n in range(1, 11):
            if n == 1:
                label = "1 комната — обычно"
            elif n == 2:
                label = "2 комнаты"
            elif n == 3:
                label = "3 комнаты"
            else:
                label = f"{n} комнат"
            options.append(discord.SelectOption(
                label=label, value=str(n), default=(n == 1),
            ))
        self.count_select = Label(
            text="Сколько комнат было сыграно?",
            component=Select(
                placeholder="Количество комнат",
                options=options,
                min_values=1,
                max_values=1,
            ),
        )
        # Bundle (/tournament create): до 10 файлов за раз —
        # файлы привязываются к комнатам по порядку, не больше
        # выбранного числа комнат. MM — строго 1 файл.
        self.upload = Label(
            text=(
                # label ≤ 45 символов (Discord 50035).
                "Скриншоты комнат (по порядку)"
                if bundle else "Скриншот комнаты 1 (1 файл)"
            ),
            component=FileUpload(
                required=True,
                min_values=1,
                max_values=10 if bundle else 1,
            ),
        )
        super().__init__(title="Отправить результат", timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.bundle = bundle
        self.add_item(self.count_select)
        self.add_item(self.upload)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from views.ocr_match_view import (
            _can_use_ocr, match_is_finished,
        )

        await interaction.response.defer(ephemeral=True)
        # Количество комнат: не меняли выбор → 1; диапазон 1..10.
        try:
            count = int(
                (self.count_select.component.values or ["1"])[0]
            )
        except (TypeError, ValueError, IndexError):
            count = 1
        count = min(max(count, 1), 10)

        files = list(
            getattr(self.upload.component, "values", None) or []
        )
        if not files:
            await interaction.followup.send(
                replace_emojis(
                    "❌ Прикрепите скриншот — без файла "
                    "загрузка не начинается."
                ),
                ephemeral=True,
            )
            return
        # Bundle: файлов не больше выбранного числа комнат —
        # каждый файл = одна комната, порядок = порядок вложений.
        if self.bundle and len(files) > count:
            await interaction.followup.send(
                replace_emojis(
                    f"❌ Файлов ({len(files)}) больше, чем "
                    f"комнат ({count}) — по одному скриншоту "
                    "на комнату. Уберите лишние и отправьте "
                    "снова."
                ),
                ephemeral=True,
            )
            return
        items = []
        for f in files[:1] if not self.bundle else files:
            try:
                data = await f.read()
            except Exception:
                data = None
            if not data:
                await interaction.followup.send(
                    replace_emojis("❌ Не удалось прочитать файл."),
                    ephemeral=True,
                )
                return
            if len(data) > MAX_ATTACH:
                await interaction.followup.send(
                    replace_emojis(
                        "❌ Файл слишком большой (макс. 10 МБ)."
                    ),
                    ephemeral=True,
                )
                return
            items.append({"filename": f.filename, "data": data})

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.followup.send(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        key = _session_key(self.match_type, self.match_index)
        # Повторная проверка прав и отсутствия активной сессии —
        # состояние могло измениться после открытия окна.
        try:
            t1, t2 = _match_tuple(
                tournament, self.match_type, self.match_index
            )
        except Exception:
            t1 = t2 = None
        phase = getattr(getattr(tournament, "phase", None), "value", "")
        active = (
            (self.match_type == "qualifier" and phase == "qualifiers")
            or (self.match_type == "semifinal" and phase == "semifinals")
            or (self.match_type == "final" and phase == "final")
        )
        if (
            phase and not active
        ) or match_is_finished(
            tournament, self.match_type, self.match_index
        ):
            await interaction.followup.send(
                replace_emojis(
                    "❌ Матч не активен или результат уже "
                    "зафиксирован."
                ),
                ephemeral=True,
            )
            return
        if t1 is not None and not _can_use_ocr(
            interaction.user, tournament, [t1, t2]
        ):
            await interaction.followup.send(
                replace_emojis(
                    "❌ Скриншот может отправить капитан команды "
                    "или организатор."
                ),
                ephemeral=True,
            )
            return
        existing = (tournament.ocr_sessions or {}).get(key)
        if existing and existing.get("status") not in (
            "completed", "cancelled", "finalizing",
        ):
            await interaction.followup.send(
                replace_emojis(
                    "⚠️ Для этого матча уже идёт загрузка "
                    "скриншотов — повторная отправка не требуется."
                ),
                ephemeral=True,
            )
            return

        # Сессия создаётся только здесь — serialize через
        # _room_lock внутри create_rooms_session: два капитана,
        # отправившие форму одновременно, получат один комплект.
        session = await create_rooms_session(
            interaction.guild, interaction.channel, interaction.user,
            tournament, self.match_type, self.match_index, count,
        )
        if session is None:
            await interaction.followup.send(
                replace_emojis(
                    "⚠️ Для этого матча уже идёт загрузка "
                    "скриншотов — продолжите через сообщение "
                    "прогресса."
                ),
                ephemeral=True,
            )
            return
        # Свежий tournament после store.set внутри create_...
        tournament = (
            store.get(self.guild_id, self.channel_id) or tournament
        )
        if self.bundle and _session_bundle(session):
            # Файл j → комната j+1 (сессия новая, комнаты пустые);
            # порядок вложений = порядок комнат.
            assignments = [
                (j + 1, item) for j, item in enumerate(items)
            ]
            await _accept_bundle_files(
                interaction, tournament, session, key,
                self.channel_id, self.match_type, self.match_index,
                assignments,
            )
            return
        await _accept_room_file(
            interaction, tournament, session, key,
            self.channel_id, self.match_type, self.match_index,
            1, items[0],
        )


async def _rooms_reply(interaction: discord.Interaction, text: str) -> None:
    """Ответ на rooms-interaction после раннего defer.

    Предпочитает edit_original_response (defer уже выполнен);
    при недоступности — followup, в крайнем случае send_message.
    Ошибки отправки логируются, но не пробрасываются — результат
    отмены уже зафиксирован в store.
    """
    content = replace_emojis(text)
    try:
        if interaction.response.is_done():
            await interaction.edit_original_response(content=content)
            return
    except Exception:
        pass
    try:
        await interaction.followup.send(content, ephemeral=True)
    except Exception:
        try:
            await interaction.response.send_message(content, ephemeral=True)
        except Exception:
            logger.debug("rooms reply failed entirely", exc_info=True)


class RoomCollectView(View):
    """Кнопки на сообщении прогресса: дозагрузка + админ-откат.

    Bundle (/tournament create): «Добавить скриншоты» пока
    комплект не полон, затем «✅ Завершить загрузку» — кнопки
    вычисляются из актуального состояния сессии при каждом
    пересоздании View (edit прогресса / restart recovery).
    """

    def __init__(
        self, guild_id: int, channel_id: int, tournament,
        match_type: str, match_index: int, gen: int,
    ):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.gen = gen
        session = (
            (getattr(tournament, "ocr_sessions", None) or {}).get(
                _session_key(match_type, match_index)
            )
            if tournament else None
        )
        bundle = _session_bundle(session)
        complete = _bundle_upload_complete(session)
        slots = _bundle_upload_slots(session)
        if not bundle or (not complete and slots > 0):
            add_btn = Button(
                label="📸 Добавить скриншоты",
                style=discord.ButtonStyle.primary,
                custom_id=(
                    f"rooms_add:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{gen}"
                ),
                row=0,
            )
            add_btn.callback = self._on_add
            self.add_item(add_btn)
        if bundle and complete:
            done_btn = Button(
                label="✅ Завершить загрузку",
                style=discord.ButtonStyle.success,
                custom_id=(
                    f"rooms_finish:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{gen}"
                ),
                row=0,
            )
            done_btn.callback = self._on_finish
            self.add_item(done_btn)
        b = Button(
            label="❌ Отменить комплект",
            style=discord.ButtonStyle.danger,
            custom_id=(
                f"rooms_cancel:{guild_id}:{channel_id}:{match_type}:"
                f"{match_index}:{gen}"
            ),
            row=0,
        )
        b.callback = self._on_cancel
        self.add_item(b)

    async def _on_add(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        key = _session_key(self.match_type, self.match_index)
        session = (tournament.ocr_sessions.get(key) if tournament else None)
        rep = _rooms_republish_index(session) if session else None
        status = session.get("status") if session else None
        # collecting — обычный сбор; awaiting_mapping — только если
        # есть комната, требующая восстановления интерфейса.
        allowed = bool(
            session
            and session.get("mode") == "rooms"
            and int(session.get("gen", 0)) == int(self.gen)
            and (
                status == "collecting"
                or (status == "awaiting_mapping" and rep is not None)
            )
        )
        if not allowed:
            await interaction.response.send_message(
                replace_emojis("⚠️ Сессия загрузки завершена или отменена."),
                ephemeral=True,
            )
            return
        # Bundle-сбор (collecting): пакетная модалка — файлы
        # распределяются по комнатам по порядку. Режим
        # восстановления интерфейса (awaiting_mapping + rep)
        # остаётся по-комнатным — там нужен конкретный room_index.
        if _session_bundle(session) and status == "collecting":
            slots = _bundle_upload_slots(session)
            if slots <= 0:
                await interaction.response.send_message(
                    replace_emojis("✅ Комплект уже заполнен."),
                    ephemeral=True,
                )
                return
            if not _rooms_file_submit_allowed(
                tournament, session, interaction.user
            ):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Этот комплект может продолжить только "
                        "его отправитель, капитан команды-"
                        "победителя или администратор."
                    ),
                    ephemeral=True,
                )
                return
            try:
                await interaction.response.send_modal(
                    BundleUploadModal(
                        self.guild_id, self.channel_id, tournament,
                        self.match_type, self.match_index,
                        int(session.get("gen", 1)), slots,
                    )
                )
            except Exception as exc:
                logger.error(
                    "Не удалось открыть bundle-модалку "
                    "(match %s_%s): %s",
                    self.match_type, self.match_index, exc,
                    exc_info=True,
                )
            return
        # Комната без опубликованного интерфейса — приоритетнее
        # следующей пустой: иначе пользователь не сможет восстановить
        # publish_fail-комнату (она считается «заполненной»).
        nxt = rep if rep is not None else _rooms_next_index(session)
        if nxt is None:
            await interaction.response.send_message(
                replace_emojis(
                    "✅ Комплект заполнен — продолжайте сопоставление "
                    "игроков под сообщениями комнат."
                    if status == "awaiting_mapping"
                    else "✅ Комплект уже полон."
                ),
                ephemeral=True,
            )
            return
        # Права — как при загрузке через кнопку
        if not _rooms_file_submit_allowed(
            tournament, session, interaction.user
        ):
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Этот комплект может продолжить только его "
                    "отправитель, капитан команды-победителя "
                    "или администратор."
                ),
                ephemeral=True,
            )
            return
        try:
            await interaction.response.send_modal(
                RoomUploadModal(
                    self.guild_id, self.channel_id, tournament,
                    self.match_type, self.match_index, nxt,
                    int(session.get("gen", 1)),
                )
            )
        except Exception as exc:
            logger.error(
                "Не удалось открыть модалку загрузки комнаты %s "
                "(match %s_%s): %s",
                nxt, self.match_type, self.match_index, exc,
                exc_info=True,
            )
            try:
                await interaction.response.send_message(
                    replace_emojis(
                        "⚠️ Не удалось открыть окно загрузки файлов — "
                        "повторите попытку."
                    ),
                    ephemeral=True,
                )
            except Exception:
                pass

    async def _on_finish(self, interaction: discord.Interaction) -> None:
        """«Завершить загрузку» (bundle): финализировать ВСЕ
        комнаты подряд через существующий finalize_room_collect —
        merge pending→panels, публикация сообщений с маппингом,
        переход сессии в awaiting_mapping."""
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            pass
        tournament = store.get(self.guild_id, self.channel_id)
        key = _session_key(self.match_type, self.match_index)
        session = (
            (tournament.ocr_sessions or {}).get(key)
            if tournament else None
        )
        if (
            not _session_bundle(session)
            or session.get("status") != "collecting"
            or int(session.get("gen", 0)) != int(self.gen)
        ):
            await _rooms_reply(
                interaction,
                "⚠️ Сессия загрузки устарела или отменена.",
            )
            return
        # Серверная проверка полноты комплекта — устаревшую View
        # обойти нельзя: требуется файл в каждой комнате и ни
        # одного ожидающего повторной отправки.
        if not _bundle_upload_complete(session):
            await _rooms_reply(
                interaction,
                "❌ Комплект неполный — загрузите скриншоты "
                "всех комнат перед завершением.",
            )
            return
        # Права — как у «Завершить комнату»: капитан команды
        # матча или администратор (проверяется и внутри
        # finalize_room_collect на свежем состоянии).
        teams = list(session.get("teams") or [])
        if teams and not _can_finalize_room(
            interaction.user, tournament, teams
        ):
            await _rooms_reply(
                interaction,
                "❌ Завершить загрузку может капитан команды "
                "или администратор.",
            )
            return
        stats_channel = await _stats_channel_for(
            interaction.guild, session
        )
        if stats_channel is None:
            stats_channel = await _stats_channel(
                interaction, tournament
            )
        expected = int(session.get("rooms_expected") or 0)
        done_rooms: list[int] = []
        failed: list[tuple[int, str]] = []
        for i in range(1, expected + 1):
            # Свежий объект на каждую комнату: finalize пишет
            # store.set — состояние не должно быть устаревшим.
            t = store.get(self.guild_id, self.channel_id) or tournament
            res = await finalize_room_collect(
                interaction.guild, interaction.user, t,
                self.match_type, self.match_index, i,
                stats_channel=stats_channel,
            )
            tournament = t
            if res in ("ok", "done", "publish_fail"):
                done_rooms.append(i)
            else:
                failed.append((i, res))
        # Финальный прогресс: все panels → awaiting_mapping,
        # прогресс-сообщение показывает «комплект принят».
        t = store.get(self.guild_id, self.channel_id) or tournament
        try:
            await _update_rooms_progress(
                interaction.guild, t, key,
                self.match_type, self.match_index, stats_channel,
            )
        except Exception:
            logger.warning(
                "rooms finish progress update failed", exc_info=True
            )
        if failed:
            detail = ", ".join(
                f"комната {r} ({_BUNDLE_RESULT_SHORT.get(res, res)})"
                for r, res in failed
            )
            await _rooms_reply(
                interaction,
                f"⚠️ Финализировано комнат: {len(done_rooms)}. "
                f"Не удалось: {detail}. Повторите через "
                "«Добавить скриншоты».",
            )
        else:
            await _rooms_reply(
                interaction,
                "✅ Загрузка завершена — все комнаты обработаны. "
                "Сопоставьте игроков кнопками под сообщениями "
                "комнат.",
            )

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        # Отвечаем Discord сразу: cancel_rooms_session делает
        # JsonStore-записи, редактирование сообщений и аудит —
        # суммарно может превысить 3-секундное окно interaction
        # (discord.errors.NotFound 10062 Unknown interaction).
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            # Interaction уже отвечен/протух — дальше отвечать
            # всё равно некуда, но отмену выполнять безопасно.
            pass
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await _rooms_reply(interaction, "❌ Турнир не найден.")
            return
        try:
            res = await cancel_rooms_session(
                interaction.guild, interaction.user, tournament,
                self.match_type, self.match_index,
            )
        except Exception:
            logger.exception(
                "rooms cancel failed (match %s_%s)",
                self.match_type, self.match_index,
            )
            await _rooms_reply(
                interaction,
                "⚠️ Не удалось отменить комплект — попробуйте ещё раз "
                "или обратитесь к администратору.",
            )
            return
        if res == "forbidden":
            await _rooms_reply(
                interaction, "❌ Отменить комплект может только администратор."
            )
        elif res == "done":
            await _rooms_reply(
                interaction, "ℹ️ Сессия уже завершена или отменена."
            )
        elif res == "none":
            await _rooms_reply(interaction, "ℹ️ Активной загрузки нет.")
        else:
            await _rooms_reply(
                interaction, "✅ Комплект скриншотов отменён."
            )


class ModalUploadShim:
    """Минимальный shim discord.Message для вызова on_message из модалки.

    Модалка с FileUpload (views/ocr_match_view.py) не имеет объекта
    Message — этот шим предоставляет те же атрибуты (guild, channel,
    author, attachments, reply, add_reaction), чтобы весь пайплайн
    on_message работал без изменений.
    """

    def __init__(
        self,
        guild: discord.Guild,
        channel: discord.abc.GuildChannel,
        user,
        attachment: discord.Attachment,
        reply_fn,
    ) -> None:
        self.guild = guild
        self.channel = channel
        self.author = user
        self.attachments = [attachment]
        self._reply_fn = reply_fn

    async def reply(self, content=None, **kwargs):
        return await self._reply_fn(content)

    async def add_reaction(self, emoji: str) -> None:
        return None


async def setup(bot) -> None:
    # Прогрев OCR-моделей в фоне — первый инференс ~4с на холодную
    try:
        from services.ocr_service import warmup

        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, warmup)
    except Exception:
        logger.debug("OCR warmup schedule failed", exc_info=True)
    await bot.add_cog(OCRUploadCog(bot))
