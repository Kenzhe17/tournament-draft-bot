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
import uuid
from datetime import datetime
from typing import Any

import discord
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
    "Капитаны нажимают свои кнопки ниже и сопоставляют игровые ники "
    "с участниками Discord. После сопоставления обеих команд результат "
    "зафиксируется автоматически."
)

_SQUAD_MAP_CAPTION = (
    "Капитан нажимает свою кнопку ниже и сопоставляет игровые ники "
    "с участниками Discord. После сопоставления обеих команд результат "
    "зафиксируется автоматически."
)


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
) -> discord.Message:
    """Новое сообщение: инструкция по сопоставлению + скриншот + кнопки.

    Без embed и итоговых данных — результат публикуется только после
    полного сопоставления (publish_match_result_card).
    """
    file = discord.File(io.BytesIO(data), filename=SHOT_FILENAME)
    return await channel.send(
        view=_mapping_layout(view, _OCR_MAP_CAPTION),
        file=file,
    )


async def _update_results_message(
    channel: discord.TextChannel,
    message_id: int,
    data: bytes,
    view: OCRPublicView,
) -> discord.Message:
    """Обновить существующее сообщение (повторный скриншот).

    При любой ошибке редактирования кидает исключение — вызывающий
    код отправит новое сообщение.
    """
    msg = await channel.fetch_message(message_id)
    await msg.edit(
        view=_mapping_layout(view, _OCR_MAP_CAPTION),
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


def _result_card(
    tournament, match_type: str, match_index: int, t1: int, t2: int,
    match_num: int = 0,
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

        # MVP — максимальный урон (та же логика, что в /statistic)
        all_cards = cards_a + cards_b
        mvp = max(all_cards, key=lambda p: p.damage, default=None)
        mvp_name = ""
        mvp_kda = ""
        mvp_damage = ""
        if mvp is not None and (mvp.damage > 0 or mvp.kills > 0):
            mvp.is_mvp = True
            mvp_name = mvp.name
            mvp_kda = f"{mvp.kills} / {mvp.deaths} / {mvp.assists}"
            mvp_damage = _fmt_num(mvp.damage)

        # Длительность: от загрузки скриншота до фиксации результата
        duration = "— : —"
        created_raw = session.get("created_at")
        if created_raw:
            try:
                created = datetime.fromisoformat(str(created_raw))
                secs = max(0, int((datetime.now() - created).total_seconds()))
                duration = f"{secs // 60:02d}:{secs % 60:02d}"
            except Exception:
                pass

        # Финальный счёт: распознанный «N VS M» со скриншота,
        # иначе fallback — суммы убийств (squad-скрины без счёта)
        score = session.get("score")
        if isinstance(score, (list, tuple)) and len(score) == 2:
            try:
                score_a, score_b = int(score[0]), int(score[1])
            except (TypeError, ValueError):
                score_a, score_b = totals_a.kills, totals_b.kills
        else:
            score_a, score_b = totals_a.kills, totals_b.kills

        # Реальный сквозной номер матча (тот же, что в embed запуска)
        match_num = await match_number_store.get_or_assign(match_id)

        data = MatchResultData(
            match_num=f"#{match_num}",
            duration=duration,
            score_a=score_a,
            score_b=score_b,
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
            tournament, match_type, match_index, t1, t2, match_num=match_num
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
                return msg
            except Exception as e:
                logger.warning("Result card edit failed: %s", e)

        file = discord.File(io.BytesIO(png), filename=SHOT_FILENAME)
        bot_msg = await channel.send(view=card, file=file)
        session["result_message_id"] = bot_msg.id
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
                break

        # Затем точное совпадение канала, затем любой канал сервера —
        # скриншот можно прислать и в канал турнира, и в канал результатов
        if meta is None:
            meta = pop_pending_upload(
                message.guild.id, message.channel.id, message.author.id
            )
        if meta is None:
            meta = pop_pending_upload_any(
                message.guild.id, message.author.id, message.channel.id
            )
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

            # Победитель по цветовой подсветке панели (CPU-bound)
            side = await loop.run_in_executor(
                OCR_EXECUTOR, detect_winner_side, data, boxes
            )

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
                    panels[side_name]["name"] = cname

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
                        "maps": {},
                        "reuploaded_at": datetime.now().isoformat(),
                    }
                )
                store.set(tournament)

                # View строим ПОСЛЕ сброса maps — кнопки снова активны
                view = OCRPublicView(
                    message.guild.id, tournament.channel_id, tournament,
                    match_type, match_index,
                )
                try:
                    await _update_results_message(
                        stats_channel, existing["message_id"],
                        data, view,
                    )
                    await message.add_reaction("✅")
                except Exception as e:
                    logger.warning("OCR reupload edit failed: %s", e)
                    bot_msg = await _send_results_message(
                        stats_channel, data, view
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
                "maps": {},
            }
            store.set(tournament)

            bot_msg = await _send_results_message(
                stats_channel, data, view
            )

            session = tournament.ocr_sessions.get(key)
            if session is not None:
                session["message_id"] = bot_msg.id
                store.set(tournament)

            # Карточка матча в cs-4x4-match-N → «идёт сопоставление»
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
                "maps": {},
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
                # Канал пула не удаляем — возвращаем в дефолт:
                # сбрасываем overwrites и чистим историю. Но только
                # когда все матчи турнира завершены — канал общий
                # для всех матчей MM.
                if await _pool_channel_idle(tournament, cid):
                    await _reset_pool_channel(channel)
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


async def _pool_channel_idle(tournament, channel_id: int) -> bool:
    """True, если канал пула можно сбрасывать: нет незавершённых
    матчей турнира, которые ещё используют его как preset-канал."""
    if getattr(tournament, "mm_preset_channel_id", 0) != channel_id:
        return True
    try:
        from services.match_announcer import _phase_matches
        from views.ocr_match_view import match_is_finished

        for mt, mi, _a, _b in _phase_matches(tournament):
            if not match_is_finished(tournament, mt, mi):
                return False
    except Exception as exc:
        logger.warning("Pool idle check failed: %s", exc)
        return False
    return True


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
    await bot.add_cog(OCRUploadCog(bot))
