"""Cog: приём скриншотов результатов матчей для OCR-сопоставления.

Капитан нажимает «📸 Скриншот» на матче (views/ocr_match_view.py),
затем отправляет изображение сообщением в канал турнира — этот cog
ловит аттачмент, прогоняет OCR и публикует сообщение с кнопками
сопоставления игроков.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import discord
from discord.ext import commands

from config import replace_emojis
from storage.json_store import store
from views.ocr_match_view import (
    OCRPublicView,
    _match_tuple,
    _session_key,
    _team_display_name,
    pop_pending_upload,
    register_pending_upload,
)

logger = logging.getLogger(__name__)

MAX_ATTACH = 10 * 1024 * 1024
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


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


class OCRUploadCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        meta = pop_pending_upload(
            message.guild.id, message.channel.id, message.author.id
        )
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
            await message.reply(
                replace_emojis(
                    "❌ Нужен скриншот — прикрепите изображение "
                    "следующим сообщением."
                )
            )
            # Возвращаем pending, чтобы пользователь мог дослать картинку
            register_pending_upload(
                message.guild.id, message.channel.id, message.author.id,
                meta["match_type"], meta["match_index"],
            )
            return

        if attachment.size and attachment.size > MAX_ATTACH:
            await message.reply(
                replace_emojis("❌ Файл слишком большой (макс. 10 МБ).")
            )
            return

        tournament = store.get(message.guild.id, message.channel.id)
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

        # Не перезаписываем активную OCR-сессию этого матча
        key = _session_key(match_type, match_index)
        existing = tournament.ocr_sessions.get(key)
        if existing and existing.get("status") not in ("completed", "cancelled"):
            await message.reply(
                replace_emojis(
                    "⚠️ Для этого матча уже загружен скриншот — "
                    "завершите сопоставление или попросите организатора "
                    "его отменить."
                )
            )
            return

        try:
            data = await attachment.read()
            from services.ocr_service import detect_winner_side

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _ocr_and_parse, data)
            if result is None:
                await message.reply(
                    replace_emojis(
                        "❌ Не удалось распознать таблицу результатов. "
                        "Убедитесь, что это экран CS 4x4 «Битва отрядов»."
                    )
                )
                return

            parsed, boxes = result
            # Победитель по цветовой подсветке панели (CPU-bound)
            side = await loop.run_in_executor(
                None, detect_winner_side, data, boxes
            )

            key = _session_key(match_type, match_index)
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

            tournament = (
                store.get(message.guild.id, message.channel.id) or tournament
            )
            tournament.ocr_sessions[key] = {
                "match_type": match_type,
                "match_index": match_index,
                "status": "awaiting_mapping",
                "teams": [t1, t2],
                "panels": panels,
                "winner_side": side,
                "banner": parsed.get("banner"),
                "top5": parsed.get("top5", False),
                "uploaded_by": message.author.id,
                "uploaded_by_name": message.author.display_name,
                "created_at": datetime.now().isoformat(),
                "maps": {},
            }
            store.set(tournament)

            # Публичное сообщение с результатами и кнопками маппинга
            from cogs.statistic import _build_embed

            parsed["winner"] = {"left": "a", "right": "b"}.get(side)
            embed = _build_embed(parsed)
            embed.add_field(
                name="🧩 Следующий шаг",
                value=(
                    f"Матч: **{_match_label(match_type, match_index)}** "
                    f"({_team_display_name(tournament, t1)} vs "
                    f"{_team_display_name(tournament, t2)})\n"
                    "Капитаны команд нажимают свои кнопки ниже и "
                    "сопоставляют игроков."
                ),
                inline=False,
            )
            view = OCRPublicView(
                message.guild.id, message.channel.id, tournament,
                match_type, match_index,
            )
            bot_msg = await message.channel.send(embed=embed, view=view)

            session = tournament.ocr_sessions.get(key)
            if session is not None:
                session["message_id"] = bot_msg.id
                store.set(tournament)

        except Exception:
            logger.exception("OCR upload processing failed")
            await message.reply(
                replace_emojis("❌ Ошибка при обработке скриншота.")
            )


async def setup(bot) -> None:
    await bot.add_cog(OCRUploadCog(bot))
