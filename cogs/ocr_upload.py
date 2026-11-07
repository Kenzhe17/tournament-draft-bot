"""Обработка скриншотов результатов матчей для OCR-сопоставления.

Два способа прислать скриншот:
1. Модалка с FileUpload (views/ocr_match_view.py) — основной путь.
2. Сообщение с изображением в канале турнира/результатов — этот cog
   ловит аттачмент через on_message (fallback для старых версий
   discord.py без FileUpload и для ручной отправки).

После OCR публикуется/обновляется сообщение в канале результатов
OCR_STATS_CHANNEL_ID с embed'ом (скриншот внутри), пингом капитанов
и кнопками сопоставления.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

import discord
from discord.ext import commands

from config import replace_emojis
from storage.json_store import store
from views.ocr_match_view import (
    OCR_STATS_CHANNEL_ID,
    OCRPublicView,
    _captain_mentions,
    _match_tuple,
    _session_key,
    _team_display_name,
    pop_pending_upload,
    pop_pending_upload_any,
    register_pending_upload,
)

logger = logging.getLogger(__name__)

MAX_ATTACH = 10 * 1024 * 1024
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
SHOT_FILENAME = "match_result.png"

ReplyFn = Callable[[str], Awaitable[None]]


def _match_label(match_type: str, match_index: int) -> str:
    if match_type == "final":
        return "Финал"
    if match_type == "semifinal":
        return f"Полуфинал #{match_index + 1}"
    return f"Отборочный #{match_index + 1}"


def _ocr_and_parse(data: bytes) -> tuple[dict[str, Any], list] | None:
    """OCR + парсинг таблицы (синхронно, запускать в executor)."""
    from services.ocr_service import _run_ocr
    from services import cs_parser

    boxes = _run_ocr(data)
    if not boxes:
        return None
    parsed = cs_parser.parse_cs_results(boxes)
    if parsed is None or (not parsed["team_a"] and not parsed["team_b"]):
        logger.warning(
            "OCR parse failed: %d boxes | %s",
            len(boxes),
            " | ".join(b["text"] for b in boxes[:60]),
        )
        return None
    return parsed, boxes


def _results_embed(
    parsed: dict[str, Any],
    match_type: str,
    match_index: int,
    tournament,
    t1: int,
    t2: int,
) -> discord.Embed:
    """Embed с распознанной таблицей + инструкцией по сопоставлению."""
    from cogs.statistic import _build_embed

    embed = _build_embed(parsed)
    embed.set_image(url=f"attachment://{SHOT_FILENAME}")
    embed.add_field(
        name="🧩 Следующий шаг",
        value=(
            f"Матч: **{_match_label(match_type, match_index)}** "
            f"({_team_display_name(tournament, t1)} vs "
            f"{_team_display_name(tournament, t2)})\n"
            "Капитаны нажимают **свои** кнопки ниже и сопоставляют "
            "игроков своей команды. После сопоставления обеих команд "
            "результат зафиксируется автоматически."
        ),
        inline=False,
    )
    return embed


async def _stats_channel(guild: discord.Guild, fallback: discord.abc.GuildChannel):
    """Канал результатов: заданный ID, иначе переданный канал."""
    channel = guild.get_channel(OCR_STATS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(OCR_STATS_CHANNEL_ID)
        except Exception:
            channel = None
    return channel or fallback


async def _send_results_message(
    channel: discord.abc.GuildChannel,
    data: bytes,
    embed: discord.Embed,
    view: OCRPublicView,
    mentions: str,
) -> discord.Message:
    """Новое сообщение с результатами: embed + скриншот + пинг капитанов."""
    file = discord.File(io.BytesIO(data), filename=SHOT_FILENAME)
    return await channel.send(
        content=f"{mentions}\n📊 Результаты матча — сопоставьте игроков.",
        embed=embed,
        view=view,
        file=file,
    )


async def _update_results_message(
    channel: discord.abc.GuildChannel,
    message_id: int,
    data: bytes,
    embed: discord.Embed,
    view: OCRPublicView,
) -> discord.Message:
    """Обновить существующее сообщение (повторный скриншот).

    При любой ошибке редактирования кидает исключение — вызывающий
    код отправит новое сообщение.
    """
    msg = await channel.fetch_message(message_id)
    await msg.edit(
        embed=embed,
        view=view,
        attachments=[
            discord.AttachmentInput(io.BytesIO(data), filename=SHOT_FILENAME)
        ],
    )
    return msg


async def process_match_screenshot(
    bot,
    guild: discord.Guild,
    user,
    attachment: discord.Attachment,
    match_type: str,
    match_index: int,
    tchannel_id: int,
    reupload: bool,
    reply: ReplyFn,
) -> bool:
    """Общий пайплайн: attachment → OCR → сессия → сообщение в канале
    результатов. reply(text) — способ ответить пользователю
    (эфемерно для модалки, reply для сообщений)."""

    async def say(text: str) -> None:
        await reply(replace_emojis(text))

    if attachment.size and attachment.size > MAX_ATTACH:
        await say("❌ Файл слишком большой (макс. 10 МБ).")
        return False

    tournament = store.get(guild.id, tchannel_id)
    if not tournament:
        await say("❌ Турнир не найден.")
        return False

    try:
        t1, t2 = _match_tuple(tournament, match_type, match_index)
    except Exception:
        await say("❌ Матч не найден.")
        return False

    key = _session_key(match_type, match_index)
    existing = tournament.ocr_sessions.get(key)
    session_active = bool(
        existing and existing.get("status") not in ("completed", "cancelled")
    )
    is_reupload = bool(reupload) and session_active

    # Новый скриншот не должен перезаписывать активную сессию —
    # только явная кнопка «Неправильный скриншот» (reupload=True)
    if session_active and not is_reupload:
        await say(
            "⚠️ Для этого матча уже загружен скриншот — "
            "завершите сопоставление, нажмите «Неправильный "
            "скриншот» или вызовите организатора."
        )
        return False

    stats_channel = await _stats_channel(guild, guild.get_channel(tchannel_id))

    try:
        data = await attachment.read()
        from services.ocr_service import detect_winner_side

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _ocr_and_parse, data)
        if result is None:
            await say(
                "❌ Не удалось распознать таблицу результатов. "
                "Убедитесь, что это экран CS 4x4 «Битва отрядов»."
            )
            return False

        parsed, boxes = result
        # Победитель по цветовой подсветке панели (CPU-bound)
        side = await loop.run_in_executor(
            None, detect_winner_side, data, boxes
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

        parsed["winner"] = {"left": "a", "right": "b"}.get(side)
        embed = _results_embed(
            parsed, match_type, match_index, tournament, t1, t2
        )
        mentions = _captain_mentions(tournament, [t1, t2])

        tournament = store.get(guild.id, tchannel_id) or tournament

        if is_reupload and existing.get("message_id"):
            # Обновляем то же сообщение: новый скриншот + сброс маппинга
            existing.update(
                {
                    "status": "awaiting_mapping",
                    "panels": panels,
                    "winner_side": side,
                    "banner": parsed.get("banner"),
                    "top5": parsed.get("top5", False),
                    "uploaded_by": user.id,
                    "uploaded_by_name": user.display_name,
                    "results_channel_id": stats_channel.id,
                    "maps": {},
                    "reuploaded_at": datetime.now().isoformat(),
                }
            )
            store.set(tournament)

            # View строим ПОСЛЕ сброса maps — кнопки снова активны
            view = OCRPublicView(
                guild.id, tournament.channel_id, tournament,
                match_type, match_index,
            )
            try:
                await _update_results_message(
                    stats_channel, existing["message_id"],
                    data, embed, view,
                )
                await say(
                    f"✅ Скриншот обновлён — результаты в "
                    f"<#{stats_channel.id}>."
                )
            except Exception as e:
                logger.warning("OCR reupload edit failed: %s", e)
                bot_msg = await _send_results_message(
                    stats_channel, data, embed, view, mentions
                )
                existing["message_id"] = bot_msg.id
                store.set(tournament)
                await say(
                    f"✅ Новый скриншот обработан — результаты в "
                    f"<#{stats_channel.id}>."
                )
            return True

        # Новая сессия
        tournament.ocr_sessions[key] = {
            "match_type": match_type,
            "match_index": match_index,
            "status": "awaiting_mapping",
            "teams": [t1, t2],
            "panels": panels,
            "winner_side": side,
            "banner": parsed.get("banner"),
            "top5": parsed.get("top5", False),
            "uploaded_by": user.id,
            "uploaded_by_name": user.display_name,
            "created_at": datetime.now().isoformat(),
            "results_channel_id": stats_channel.id,
            "maps": {},
        }
        store.set(tournament)

        # View после создания сессии — статус/кнопки актуальны
        view = OCRPublicView(
            guild.id, tournament.channel_id, tournament,
            match_type, match_index,
        )
        bot_msg = await _send_results_message(
            stats_channel, data, embed, view, mentions
        )

        session = tournament.ocr_sessions.get(key)
        if session is not None:
            session["message_id"] = bot_msg.id
            store.set(tournament)

        await say(
            f"✅ Скриншот распознан — результаты в <#{stats_channel.id}>. "
            "Капитаны уже получили уведомление."
        )
        return True

    except Exception:
        logger.exception("OCR upload processing failed")
        await say("❌ Ошибка при обработке скриншота.")
        return False


class OCRUploadCog(commands.Cog):
    """Fallback: приём скриншотов сообщением с картинкой."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        # Сначала точное совпадение канала, затем любой канал сервера —
        # скриншот можно прислать и в канал турнира, и в канал результатов
        meta = pop_pending_upload(
            message.guild.id, message.channel.id, message.author.id
        )
        if not meta:
            meta = pop_pending_upload_any(message.guild.id, message.author.id)
        if not meta:
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
                    replace_emojis("❌ Нужен скриншот — прикрепите изображение.")
                )
            # Возвращаем pending, чтобы пользователь мог дослать картинку
            register_pending_upload(
                message.guild.id, message.channel.id, message.author.id,
                meta["match_type"], meta["match_index"],
                tchannel_id=meta.get("tchannel", message.channel.id),
                reupload=meta.get("reupload", False),
            )
            return

        tchannel_id = meta.get("tchannel") or message.channel.id

        async def reply(text: str) -> None:
            await message.reply(text)

        await process_match_screenshot(
            self.bot, message.guild, message.author, attachment,
            meta["match_type"], meta["match_index"],
            tchannel_id, meta.get("reupload", False), reply,
        )


async def setup(bot) -> None:
    await bot.add_cog(OCRUploadCog(bot))
