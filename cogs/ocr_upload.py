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
from discord.ui import Button, FileUpload, Modal, Select, View
from utils.design import compose, text_card
from discord.ext import commands

from config import get_emoji, replace_emojis, MM_MATCH_CHANNEL_POOL
from storage.json_store import store
from views.ocr_match_view import (
    OCR_STATS_CHANNEL_ID,
    OCRPublicView,
    SquadAssignedView,
    SquadClaimView,
    _match_tuple,
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
RESULT_CHANNEL_ID = 1563149967303118949


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
    """V2-карточка итога матча: номер, команды, игроки, скриншот.

    Состав — мелким текстом (-#). Для MM-турниров заголовок без эмодзи.
    """
    from utils.design.formatters import muted
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
    view.sep()
    if duration:
        view.text(f"**Длительность матча:** {duration}")
    if rooms_count:
        view.text(f"**Комнат сыграно:** {rooms_count}")
    view.text(muted(f"{name_a}\n{_roster_mentions(tournament, t1)}"))
    view.text(muted(f"{name_b}\n{_roster_mentions(tournament, t2)}"))
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
        # Исключение: squad-режим — второй капитан загружает свой отряд.
        if session_active and not is_reupload:
            if existing.get("mode") == "squad":
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
            boxes = await loop.run_in_executor(OCR_EXECUTOR, _ocr_boxes, data)
            if not boxes:
                await message.reply(
                    replace_emojis(
                        "❌ Не удалось распознать таблицу результатов. "
                        "Убедитесь, что это экран CS 4x4."
                    )
                )
                return

            # Сначала пробуем детальный экран одного отряда
            squad = await loop.run_in_executor(OCR_EXECUTOR, _parse_squad, boxes)
            if squad is not None:
                await self._handle_squad_upload(
                    message, tournament, t1, t2, meta,
                    match_type, match_index, key, existing,
                    session_active, is_reupload, stats_channel,
                    data, squad,
                )
                return

            # Классический двух-панельный экран
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

            parsed = await loop.run_in_executor(OCR_EXECUTOR, _parse_classic, boxes)
            if parsed is None:
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
                            "kills": p["kills"],
                            "deaths": p["deaths"],
                            "assists": p["assists"],
                            "dmg": p.get("dmg") or 0,
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

            tournament = (
                store.get(message.guild.id, tchannel_id) or tournament
            )
            view = OCRPublicView(
                message.guild.id, tournament.channel_id, tournament,
                match_type, match_index,
            )

            if is_reupload and existing.get("message_id"):
                # Обновляем то же сообщение: новый скриншот + сброс маппинга
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

                # View строим ПОСЛЕ сброса maps — кнопки снова активны
                view = OCRPublicView(
                    message.guild.id, tournament.channel_id, tournament,
                    match_type, match_index,
                )
                caption = _mapping_caption(tournament, existing)
                try:
                    await _update_results_message(
                        stats_channel, existing["message_id"],
                        data, view, caption=caption,
                    )
                    await message.add_reaction("✅")
                except Exception as e:
                    logger.warning("OCR reupload edit failed: %s", e)
                    bot_msg = await _send_results_message(
                        stats_channel, data, view, caption=caption
                    )
                    existing["message_id"] = bot_msg.id
                    store.set(tournament)
                try:
                    from services.match_announcer import update_match_card

                    await update_match_card(
                        message.guild, tournament, match_type, match_index,
                        state="processing",
                    )
                except Exception as exc:
                    logger.warning("Match card processing update failed: %s", exc)
                return

            # Новая сессия
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
                "created_at": datetime.now().isoformat(),
                "results_channel_id": stats_channel.id,
                "maps": {}, "panel_picks": {},
            }
            store.set(tournament)

            bot_msg = await _send_results_message(
                stats_channel, data, view,
                caption=_mapping_caption(
                    tournament, tournament.ocr_sessions[key]
                ),
            )

            session = tournament.ocr_sessions.get(key)
            if session is not None:
                session["message_id"] = bot_msg.id
                store.set(tournament)

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
        entry["message_id"] = bot_msg.id
        store.set(tournament)

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
        """Обработать скриншот детального экрана одного отряда."""
        guild_id = message.guild.id

        if session_active and existing.get("mode") != "squad":
            await message.reply(
                replace_emojis(
                    "⚠️ Для этого матча уже загружен скриншот другого "
                    "типа результатов — дождитесь завершения или вызовите "
                    "организатора."
                )
            )
            return

        if session_active:
            session = existing
            session.setdefault("squads", {})
            session.setdefault("squad_pool", [])
            session.setdefault("panels", {})
            session.setdefault("maps", {})
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
                "created_at": datetime.now().isoformat(),
                "results_channel_id": stats_channel.id,
                "maps": {}, "panel_picks": {},
            }
            tournament.ocr_sessions[key] = session

        # --- Повторная загрузка по кнопке «Неправильный скриншот» ---
        squad_team = meta.get("squad_team")
        squad_uid = meta.get("squad_uid")
        if is_reupload and squad_team is not None:
            tkey = str(squad_team)
            sentry = session["squads"].get(tkey)
            if sentry is None:
                await message.reply(
                    replace_emojis("❌ Отряд для этой команды не найден.")
                )
                return
            sentry["rows"] = squad["players"]
            sentry["banner"] = squad.get("banner")
            sentry["reuploaded_at"] = datetime.now().isoformat()
            panel = session.get("panels", {}).get(f"squad_{squad_team}")
            if panel is not None:
                panel["rows"] = squad["players"]
            # Сопоставление этой команды недействительно — сбрасываем
            session.get("maps", {}).pop(tkey, None)
            if len(session.get("maps", {})) < 2:
                session["status"] = (
                    "mapping" if session.get("squads")
                    else "awaiting_mapping"
                )
            store.set(tournament)
            view = SquadAssignedView(
                guild_id, tournament.channel_id, tournament,
                match_type, match_index, squad_team,
            )
            await self._publish_squad_message(
                stats_channel, tournament, message, data,
                view, sentry,
                match_type=match_type, match_index=match_index,
                t1=t1, t2=t2,
            )
            return

        if is_reupload and squad_uid:
            entry = next(
                (
                    e for e in session["squad_pool"]
                    if e.get("uid") == squad_uid
                ),
                None,
            )
            if entry is None:
                await message.reply(
                    replace_emojis("❌ Скриншот отряда не найден.")
                )
                return
            entry["rows"] = squad["players"]
            entry["banner"] = squad.get("banner")
            entry["reuploaded_at"] = datetime.now().isoformat()
            store.set(tournament)
            view = SquadClaimView(
                guild_id, tournament.channel_id, tournament,
                match_type, match_index, squad_uid,
            )
            await self._publish_squad_message(
                stats_channel, tournament, message, data,
                view, entry,
                match_type=match_type, match_index=match_index,
                t1=t1, t2=t2,
            )
            return

        # --- Автоназначение: скриншот прислан по кнопке капитана ---
        if not is_reupload and squad_team is not None:
            if squad_team not in (t1, t2):
                await message.reply(
                    replace_emojis("❌ Эта команда не играет в матче.")
                )
                return
            tkey = str(squad_team)
            if tkey in session.get("squads", {}):
                await message.reply(
                    replace_emojis(
                        "⚠️ У этой команды уже загружен отряд — "
                        "замените его кнопкой «Неправильный скриншот»."
                    )
                )
                return
            entry = {
                "rows": squad["players"],
                "banner": squad.get("banner"),
                "uploaded_by": message.author.id,
                "uploaded_by_name": message.author.display_name,
                "created_at": datetime.now().isoformat(),
                "message_id": None,
            }
            session["squads"][tkey] = entry
            session["panels"][f"squad_{squad_team}"] = {
                "name": _team_display_name(tournament, squad_team),
                "rows": squad["players"],
            }
            if len(session.get("squads", {})) >= 2:
                session["status"] = "mapping"
            store.set(tournament)

            view = SquadAssignedView(
                guild_id, tournament.channel_id, tournament,
                match_type, match_index, squad_team,
            )
            await self._publish_squad_message(
                stats_channel, tournament, message, data,
                view, entry,
                match_type=match_type, match_index=match_index,
                t1=t1, t2=t2,
            )
            return

        # --- Новая загрузка отряда (без привязки — выбор команды кнопкой) ---
        loaded = len(session.get("squads", {})) + len(
            session.get("squad_pool", [])
        )
        if loaded >= 2:
            await message.reply(
                replace_emojis(
                    "⚠️ Скриншоты обоих отрядов уже загружены — "
                    "используйте «Неправильный скриншот» для замены."
                )
            )
            return

        uid = uuid.uuid4().hex[:10]
        entry = {
            "uid": uid,
            "rows": squad["players"],
            "banner": squad.get("banner"),
            "uploaded_by": message.author.id,
            "uploaded_by_name": message.author.display_name,
            "created_at": datetime.now().isoformat(),
            "message_id": None,
        }
        session["squad_pool"].append(entry)
        if len(session.get("squads", {})) >= 2:
            session["status"] = "mapping"
        store.set(tournament)

        view = SquadClaimView(
            guild_id, tournament.channel_id, tournament,
            match_type, match_index, uid,
        )
        await self._publish_squad_message(
            stats_channel, tournament, message, data,
            view, entry,
            match_type=match_type, match_index=match_index,
            t1=t1, t2=t2,
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


def _room_lock(match_key: str) -> asyncio.Lock:
    lock = _ROOM_LOCKS.get(match_key)
    if lock is None:
        lock = asyncio.Lock()
        _ROOM_LOCKS[match_key] = lock
        if len(_ROOM_LOCKS) > _ROOM_LOCKS_MAX:
            _ROOM_LOCKS.pop(next(iter(_ROOM_LOCKS)))
    return lock


async def _ocr_boxes_cached(data: bytes) -> list:
    """OCR с дедупликацией по sha256 содержимого."""
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    cached = _OCR_BOX_CACHE.get(digest)
    if cached is not None:
        return cached
    from services.ocr_service import OCR_EXECUTOR

    loop = asyncio.get_running_loop()
    boxes = await loop.run_in_executor(OCR_EXECUTOR, _ocr_boxes, data)
    _OCR_BOX_CACHE[digest] = boxes
    if len(_OCR_BOX_CACHE) > _OCR_BOX_CACHE_MAX:
        _OCR_BOX_CACHE.pop(next(iter(_OCR_BOX_CACHE)))
    return boxes


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


async def _classic_pipeline(data: bytes, need_score: bool = True) -> dict | None:
    """Общий классический пайплайн: OCR → parse → score fallback →
    winner detect → cyrillic names → refine строк. Возвращает
    {"panels","score","banner","top5","winner_side"} или None.

    need_score=False (rooms/Ranked): счёт раундов в итогах не
    используется — пропускаем лишний OCR-проход шапки.
    """
    from services.ocr_service import (
        OCR_EXECUTOR,
        detect_winner_side,
        ocr_cyrillic_region,
        ocr_score_fallback,
    )

    loop = asyncio.get_running_loop()
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
                    "kills": p["kills"],
                    "deaths": p["deaths"],
                    "assists": p["assists"],
                    "dmg": p.get("dmg") or 0,
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
    room: dict, match_type: str, match_index: int, t1: int, t2: int,
) -> None:
    """Сообщение комнаты: скриншот(ы) + кнопки сопоставления (RoomView)."""
    from views.ocr_match_view import RoomView

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
    text = (
        f"**Комната {room['room_index']}**\n"
        f"Распознано игроков: {players_n}"
    )
    bot_msg = await stats_channel.send(
        content=replace_emojis(text), view=view, files=files
    )
    room["message_id"] = bot_msg.id
    room.setdefault("message_ids", []).append(bot_msg.id)
    store.set(tournament)


def _img_ext(filename: str) -> str:
    import os

    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in IMAGE_EXTS else ".png"


def _room_has_panels(room: dict) -> bool:
    return bool(room.get("panels"))


def _rooms_completed_count(session: dict) -> int:
    return sum(
        1 for r in session.get("rooms") or [] if _room_has_panels(r)
    )


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
    nxt = _rooms_next_index(session)
    if nxt is None:
        status = "Комплект полный — ожидается сопоставление игроков"
    elif done == 0:
        status = f"Сбор скриншотов — отправьте комнату {nxt}"
    else:
        status = f"Обработка OCR / сбор — отправьте комнату {nxt}"
    return (
        f"**ОБРАБОТКА РЕЗУЛЬТАТОВ**\n"
        f"Комнат: {total}\n"
        f"Заполнено: {done}/{total}\n"
        f"Статус: {status}\n"
        "Скриншоты приняты. Повторная отправка не требуется — "
        "продолжайте через «Добавить скриншоты»."
    )


def _rooms_files_accepted(session: dict) -> bool:
    """Хотя бы один файл реально принят в обработку.

    True, если карточка уже переведена в processing (флаг ставится
    при приёме файлов до OCR) или есть хотя бы одна заполненная
    комната. Сессия в collecting без файлов → False.
    """
    if session.get("card_state") == "processing":
        return True
    return _rooms_completed_count(session) > 0


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
        from views.ocr_match_view import RoomCollectView

        complete = done >= total and total > 0
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
    return out


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
    "cancelled" | "error".
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
    if room is not None and _room_has_panels(room):
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
        return "dup"

    # Файлы приняты в обработку — сразу прячем «Отправить результат»
    # в карточке матча, не дожидаясь завершения OCR.
    await _ensure_processing_card(guild, tournament, match_type, match_index)

    gen = session.get("gen", 1)
    pipes = []
    try:
        for it in items:
            pipe = await _classic_pipeline(it["data"], need_score=False)
            pipes.append(pipe)
    except Exception:
        logger.exception("Room bundle OCR pipeline failed")
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
        return "parse_fail"

    kinds = ["squad" if p.get("squad") is not None else "classic"
             for p in pipes]
    if len(items) == 2 and kinds != ["squad", "squad"]:
        # Два файла в комнате — только два squad-скриншота команд
        return "invalid_mix"
    if len(items) == 1 and kinds[0] not in ("classic", "squad"):
        return "invalid"

    # --- Сборка panels комнаты ---
    from views.ocr_match_view import EXTRA_STAT_KEYS

    if kinds[0] == "classic":
        pipe = pipes[0]
        room["panels"].update(pipe["panels"])
        room["kind"] = "classic"
        room["winner_side"] = pipe.get("winner_side")
        room["banner"] = pipe.get("banner")
        room["score"] = pipe.get("score")
        room["top5"] = pipe.get("top5", False)
        room["sha"] = shas[0]
    else:
        room["kind"] = "squad"
        panels = room.setdefault("panels", {})
        banners = room.setdefault("banners", {})
        for pipe, sha in zip(pipes, shas):
            squad = pipe["squad"]
            pkey = "squad" if "squad" not in panels else "squad2"
            if pkey in panels:
                return "invalid"  # не должно случиться при max 2 squad
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
            panels[pkey] = {"name": "Отряд", "rows": rows}
            banners[pkey] = squad.get("banner")
            room.setdefault("shas", []).append(sha)
            if not room.get("banner") and squad.get("banner"):
                room["banner"] = squad.get("banner")
        if room.get("sha") is None and room["shas"]:
            room["sha"] = room["shas"][0]

    session["rooms_received"] = _rooms_completed_count(session)
    store.set(tournament)

    # Сообщение комнаты: все её скриншоты + кнопки маппинга
    try:
        await _publish_room_message(
            stats_channel, tournament, channel, items, room,
            match_type, match_index,
        )
    except Exception as e:
        logger.warning("Room bundle message publish failed: %s", e)

    await _update_rooms_progress(
        guild, tournament, key, match_type, match_index, stats_channel,
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
    async with _room_lock(key):
        tournament = (
            store.get(guild.id, tournament.channel_id) or tournament
        )
        existing = tournament.ocr_sessions.get(key)
        if existing and existing.get("status") not in ("completed", "cancelled"):
            return None
        t1, t2 = _match_tuple(tournament, match_type, match_index)
        session = {
            "match_type": match_type,
            "match_index": match_index,
            "mode": "rooms",
            "status": "collecting",
            "rooms_expected": int(count),
            "rooms_received": 0,
            "rooms": [],
            "gen": 1,
            "uploaded_by": user.id,
            "uploaded_by_name": getattr(user, "display_name", user.name),
            "teams": [t1, t2],
            "maps": {},
            "panel_picks": {},
            "card_state": "active",
            "created_at": datetime.now().isoformat(),
        }
        tournament.ocr_sessions[key] = session

        view = RoomCollectView(
            guild.id, tournament.channel_id, tournament,
            match_type, match_index, session["gen"],
        )
        msg = await channel.send(
            content=replace_emojis(_rooms_progress_text(session)),
            view=view,
        )
        session["progress_message_id"] = msg.id
        session["progress_channel_id"] = channel.id
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


async def cancel_rooms_session(
    guild: discord.Guild,
    user,
    tournament,
    match_type: str,
    match_index: int,
) -> str:
    """Полный административный откат комплекта. «ok»|«none»|«done»|«forbidden»."""
    from config import BOT_OWNER_ID

    key = _session_key(match_type, match_index)
    session = tournament.ocr_sessions.get(key)
    if not session or session.get("mode") != "rooms":
        return "none"
    if session.get("status") in ("completed", "cancelled"):
        return "done"
    # Строгая проверка: только Discord-администратор сервера или
    # владелец бота — не роль «организатор» турнира.
    if not _is_strict_admin(user):
        return "forbidden"

    async with _room_lock(key):
        # Повторная проверка под локом — статус мог измениться
        tournament = store.get(guild.id, tournament.channel_id) or tournament
        session = tournament.ocr_sessions.get(key)
        if not session or session.get("mode") != "rooms":
            return "none"
        if session.get("status") in ("completed", "cancelled"):
            return "done"
        session["gen"] = int(session.get("gen", 1)) + 1
        session["status"] = "cancelled"
        # Удаляем сессию целиком — поздние OCR-задачи увидят
        # отсутствие/несовпадение gen и не запишут результат.
        tournament.ocr_sessions.pop(key, None)
        store.set(tournament)

    # Карточка матча → обратно в активное состояние
    try:
        from services.match_announcer import update_match_card

        await update_match_card(
            guild, tournament, match_type, match_index, state="active"
        )
    except Exception as e:
        logger.warning("rooms cancel card update failed: %s", e)

    # Гасим сообщения комнат и прогресса (best effort)
    channel = None
    try:
        channel = guild.get_channel(
            int(session.get("progress_channel_id") or 0)
        ) or await guild.fetch_channel(
            int(session.get("progress_channel_id") or 0)
        )
    except Exception:
        pass
    if channel is not None:
        try:
            prog_id = session.get("progress_message_id")
            if prog_id:
                prog = await channel.fetch_message(prog_id)
                await prog.edit(
                    content=replace_emojis(
                        "❌ Комплект скриншотов отменён администратором. "
                        "Можно загрузить заново."
                    ),
                    view=None,
                )
        except Exception as e:
            logger.warning("rooms progress edit failed: %s", e)
        for room in session.get("rooms") or []:
            mids = list(room.get("message_ids") or [])
            if room.get("message_id"):
                mids.append(room["message_id"])
            for mid in dict.fromkeys(mids):
                try:
                    msg = await channel.fetch_message(mid)
                    await msg.edit(
                        content=replace_emojis(
                            f"**Комната {room.get('room_index')}** — "
                            "❌ комплект отменён администратором."
                        ),
                        view=None,
                    )
                except Exception as e:
                    logger.warning("room message cancel edit failed: %s", e)
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
        # Модалка первой комнаты — первый ответ на select-interaction
        await interaction.response.send_modal(
            RoomUploadModal(
                self.guild_id, self.channel_id, tournament,
                self.match_type, self.match_index, 1, session["gen"],
            )
        )


class RoomUploadModal(Modal):
    """Скриншоты ОДНОЙ явно выбранной комнаты.

    1 файл — полный двухпанельный экран ИЛИ один squad-скриншот
    команды. 2 файла — по одному squad-скриншоту на каждую команду.
    """

    def __init__(
        self, guild_id: int, channel_id: int, tournament,
        match_type: str, match_index: int, room_index: int, gen: int,
    ):
        upload = FileUpload(
            label=f"Скриншоты комнаты {room_index} (1–2 файла)",
            required=True,
            min_values=1,
            max_values=2,
        )
        super().__init__(title=f"Комната {room_index}", timeout=600)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.room_index = room_index
        self.gen = gen
        self.add_item(upload)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        upload = self.children[0]
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
        if (
            not session
            or session.get("mode") != "rooms"
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
        # Права — как при загрузке через кнопку
        from views.ocr_match_view import _can_use_ocr

        t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
        if not _can_use_ocr(interaction.user, tournament, [t1, t2]):
            await interaction.followup.send(
                replace_emojis(
                    "❌ Скриншоты может отправить капитан команды "
                    "или организатор."
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
        stats_channel = await _stats_channel(interaction, tournament)
        if stats_channel is None:
            stats_channel = interaction.channel
        res = await process_room_bundle(
            guild=interaction.guild,
            channel=stats_channel,
            user=interaction.user,
            tournament=tournament,
            match_type=self.match_type,
            match_index=self.match_index,
            key=key,
            session=session,
            stats_channel=stats_channel,
            room_index=self.room_index,
            items=items,
        )
        messages = {
            "ok": (
                f"✅ Комната {self.room_index} обработана. "
                "Следующая — кнопкой «Добавить скриншоты»."
            ),
            "dup": "⚠️ Этот скриншот уже загружен в комплект.",
            "invalid": (
                "❌ В одной комнате: либо 1 полный скриншот, либо "
                "1–2 скриншота отрядов (максимум 2 файла)."
            ),
            "invalid_mix": (
                "❌ Смешанная загрузка не принимается: полный экран "
                "вместе со скриншотом отряда в одной комнате. "
                "Загрузите либо один полный скриншот, либо два "
                "скриншота отрядов."
            ),
            "room_done": (
                f"ℹ️ Комната {self.room_index} уже заполнена — "
                "замена только через откат администратором."
            ),
            "parse_fail": (
                "❌ Не удалось распознать таблицу результатов — "
                "проверьте, что это экран CS 4x4.\n"
                "Комната не засчитана: повторите загрузку кнопкой "
                "«Добавить скриншоты» под сообщением прогресса "
                "или обратитесь к администратору."
            ),
            "cancelled": "⚠️ Сессия загрузки была отменена.",
        }
        await interaction.followup.send(
            replace_emojis(messages.get(res, "❌ Ошибка обработки — попробуйте ещё раз.")),
            ephemeral=True,
        )


class RoomCollectView(View):
    """Кнопки на сообщении прогресса: дозагрузка + админ-откат."""

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
        if (
            not session
            or session.get("mode") != "rooms"
            or session.get("status") != "collecting"
            or int(session.get("gen", 0)) != int(self.gen)
        ):
            await interaction.response.send_message(
                replace_emojis("⚠️ Сессия загрузки завершена или отменена."),
                ephemeral=True,
            )
            return
        nxt = _rooms_next_index(session)
        if nxt is None:
            await interaction.response.send_message(
                replace_emojis("✅ Комплект уже полон."), ephemeral=True
            )
            return
        # Права — капитан/орг (как при отправке скриншота)
        from views.ocr_match_view import _can_use_ocr

        teams = session.get("teams") or []
        if not _can_use_ocr(interaction.user, tournament, teams):
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Скриншоты может отправить капитан команды "
                    "или организатор."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            RoomUploadModal(
                self.guild_id, self.channel_id, tournament,
                self.match_type, self.match_index, nxt,
                int(session.get("gen", 1)),
            )
        )

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        res = await cancel_rooms_session(
            interaction.guild, interaction.user, tournament,
            self.match_type, self.match_index,
        )
        if res == "forbidden":
            await interaction.response.send_message(
                replace_emojis("❌ Отменить комплект может только администратор."),
                ephemeral=True,
            )
        elif res == "done":
            await interaction.response.send_message(
                replace_emojis("ℹ️ Сессия уже завершена или отменена."),
                ephemeral=True,
            )
        elif res == "none":
            await interaction.response.send_message(
                replace_emojis("ℹ️ Активной загрузки нет."), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                replace_emojis("✅ Комплект скриншотов отменён."),
                ephemeral=True,
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
