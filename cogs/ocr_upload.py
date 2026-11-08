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

from config import get_emoji, replace_emojis
from storage.json_store import store
from views.ocr_match_view import (
    OCR_STATS_CHANNEL_ID,
    OCRPublicView,
    SquadAssignedView,
    SquadClaimView,
    _captain_mentions,
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
RESULT_CHANNEL_ID = 1568290145138900992


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


def _squad_embed(
    squad: dict[str, Any],
    match_type: str,
    match_index: int,
    tournament,
    t1: int,
    t2: int,
) -> discord.Embed:
    """Embed детального экрана ОДНОГО отряда (все колонки статистики)."""
    banner = _BANNER_LABELS.get(squad.get("banner"), "❔ результат не распознан")
    embed = discord.Embed(
        title=f"⚔️ {_match_label(match_type, match_index)} — Статистика отряда",
        description=(
            f"**Результат отряда на экране: {banner}**\n"
            f"Матч: **{_team_display_name(tournament, t1)}** vs "
            f"**{_team_display_name(tournament, t2)}**"
        ),
        color=discord.Color.gold() if squad.get("banner") == "win"
        else discord.Color.dark_grey(),
    )
    embed.set_image(url=f"attachment://{SHOT_FILENAME}")
    for i, p in enumerate(squad.get("players", []), 1):
        hs = p.get("headshots")
        hs_str = f"{_fmt_num(hs)}%" if hs is not None else "—"
        rev_x = p.get("revive_x")
        rev_str = _fmt_num(p.get("revives"))
        if rev_x is not None:
            rev_str = f"{rev_str} / {_fmt_num(rev_x)}"
        lines = [
            f"**K/D/A:** {p.get('kills', 0)}/{p.get('deaths', 0)}/"
            f"{p.get('assists', 0)}   **DMG:** {_fmt_num(p.get('dmg'))}",
            f"**Факт. урон:** {_fmt_num(p.get('fact_dmg'))}"
            f"   **Нокдауны:** {_fmt_num(p.get('knockdowns'))}",
            f"**Исцеление:** {_fmt_num(p.get('healing'))}"
            f"   **Поднятия:** {rev_str}"
            f"   **Хэдшоты:** {hs_str}",
        ]
        embed.add_field(
            name=f"{i}. {p.get('name', '?')}",
            value="\n".join(lines),
            inline=False,
        )
    embed.add_field(
        name="🧩 Следующий шаг",
        value=(
            "Это скриншот **одного отряда**. Капитан соответствующей "
            "команды нажимает кнопку **«Это <команда>»**, затем "
            "**«Сопоставить»** и привязывает ники к Discord-игрокам. "
            "Когда оба отряда сопоставлены — результат фиксируется "
            "автоматически (победитель — по баннерам ПОБЕДА/ПОРАЖЕНИЕ)."
        ),
        inline=False,
    )
    return embed


async def _stats_channel(message: discord.Message):
    """Канал результатов: заданный ID, иначе канал сообщения."""
    channel = message.guild.get_channel(OCR_STATS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await message.guild.fetch_channel(OCR_STATS_CHANNEL_ID)
        except Exception:
            channel = None
    return channel or message.channel


async def _send_results_message(
    channel: discord.TextChannel,
    data: bytes,
    embed: discord.Embed,
    view: OCRPublicView,
    mentions: str,
) -> discord.Message:
    """Новое сообщение с результатами: embed + скриншот + пинг капитанов."""
    file = discord.File(io.BytesIO(data), filename=SHOT_FILENAME)
    return await channel.send(
        view=compose(
            embed, view,
            text=f"{mentions}\n📊 Результаты матча — сопоставьте игроков.",
        ),
        file=file,
    )


async def _update_results_message(
    channel: discord.TextChannel,
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
    await msg.edit( view=compose(embed, view),
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
        parts.append(f"<@{uid}>" if uid else name)
    return " ".join(parts)


def _result_embed(
    tournament, match_type: str, match_index: int, t1: int, t2: int
) -> discord.Embed:
    """Стилизованная карточка итога матча: команды, капитаны, игроки."""
    name_a = _captain_name(tournament, t1)
    name_b = _captain_name(tournament, t2)
    embed = discord.Embed(
        title=f" {replace_emojis('winner')} ИТОГ МАТЧА — {_match_label(match_type, match_index)}",
        description=(
            f"**{name_a}** vs **{name_b}**\n\n"
            f"{get_emoji('num_1')} **{name_a}**\n"
            f"{_roster_mentions(tournament, t1)}\n\n"
            f"{get_emoji('num_2')} **{name_b}**\n"
            f"{_roster_mentions(tournament, t2)}"
        ),
        color=discord.Color.gold(),
    )
    embed.set_image(url=f"attachment://{SHOT_FILENAME}")
    return embed


async def _result_channel(guild: discord.Guild, fallback):
    """Канал итогов: RESULT_CHANNEL_ID, иначе fallback (канал статистики)."""
    channel = guild.get_channel(RESULT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(RESULT_CHANNEL_ID)
        except Exception:
            channel = None
    return channel or fallback


async def _upsert_result_message(
    guild: discord.Guild,
    tournament,
    match_type: str,
    match_index: int,
    t1: int,
    t2: int,
    data: bytes,
    fallback_channel,
) -> None:
    """Отправить/обновить итоговый пост матча в RESULT_CHANNEL_ID.

    Скриншот прикрепляется к карточке; при повторной загрузке
    сообщение редактируется (result_message_id хранится в сессии).
    """
    try:
        channel = await _result_channel(guild, fallback_channel)
        if channel is None:
            return
        embed = _result_embed(tournament, match_type, match_index, t1, t2)
        key = _session_key(match_type, match_index)
        session = tournament.ocr_sessions.get(key)
        msg_id = session.get("result_message_id") if session else None

        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(
                    view=compose(embed),
                    attachments=[
                        discord.AttachmentInput(
                            io.BytesIO(data), filename=SHOT_FILENAME
                        )
                    ],
                )
                return
            except Exception as e:
                logger.warning("Result message edit failed: %s", e)

        file = discord.File(io.BytesIO(data), filename=SHOT_FILENAME)
        bot_msg = await channel.send(view=compose(embed), file=file)
        if session is not None:
            session["result_message_id"] = bot_msg.id
            store.set(tournament)
    except Exception:
        logger.exception("Result channel publish failed")


class OCRUploadCog(commands.Cog):
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

        stats_channel = await _stats_channel(message)

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

            parsed["winner"] = {"left": "a", "right": "b"}.get(side)
            embed = _results_embed(
                parsed, match_type, match_index, tournament, t1, t2
            )
            mentions = _captain_mentions(tournament, [t1, t2])

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
                        data, embed, view,
                    )
                    await message.add_reaction("✅")
                except Exception as e:
                    logger.warning("OCR reupload edit failed: %s", e)
                    bot_msg = await _send_results_message(
                        stats_channel, data, embed, view, mentions
                    )
                    existing["message_id"] = bot_msg.id
                    store.set(tournament)
                await _upsert_result_message(
                    message.guild, tournament, match_type, match_index,
                    t1, t2, data, stats_channel,
                )
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
                "top5": parsed.get("top5", False),
                "uploaded_by": message.author.id,
                "uploaded_by_name": message.author.display_name,
                "created_at": datetime.now().isoformat(),
                "results_channel_id": stats_channel.id,
                "maps": {},
            }
            store.set(tournament)

            bot_msg = await _send_results_message(
                stats_channel, data, embed, view, mentions
            )

            session = tournament.ocr_sessions.get(key)
            if session is not None:
                session["message_id"] = bot_msg.id
                store.set(tournament)

            await _upsert_result_message(
                message.guild, tournament, match_type, match_index,
                t1, t2, data, stats_channel,
            )

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
        embed: discord.Embed,
        view,
        mentions: str,
        entry: dict[str, Any],
        match_type: str = "",
        match_index: int = 0,
        t1: int = 0,
        t2: int = 0,
    ) -> None:
        """Отправить или обновить сообщение со статой одного отряда."""
        if entry.get("message_id"):
            try:
                msg = await stats_channel.fetch_message(
                    entry["message_id"]
                )
                await msg.edit( view=compose(embed, view),
                    attachments=[
                        discord.AttachmentInput(
                            io.BytesIO(data), filename=SHOT_FILENAME
                        )
                    ],
                )
                await message.add_reaction("✅")
                if match_type:
                    await _upsert_result_message(
                        message.guild, tournament, match_type, match_index,
                        t1, t2, data, stats_channel,
                    )
                return
            except Exception as e:
                logger.warning("Squad message edit failed: %s", e)

        file = discord.File(io.BytesIO(data), filename=SHOT_FILENAME)
        bot_msg = await stats_channel.send(
            content=(
                f"{mentions}\n📊 Статистика отряда — "
                "выберите команду и сопоставьте игроков."
            ), view=compose(embed, view),
            file=file,
        )
        entry["message_id"] = bot_msg.id
        store.set(tournament)

        if match_type:
            await _upsert_result_message(
                message.guild, tournament, match_type, match_index,
                t1, t2, data, stats_channel,
            )

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

        mentions = _captain_mentions(tournament, [t1, t2])
        embed = _squad_embed(
            squad, match_type, match_index, tournament, t1, t2
        )

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
                stats_channel, tournament, message, data, embed,
                view, mentions, sentry,
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
                stats_channel, tournament, message, data, embed,
                view, mentions, entry,
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
                stats_channel, tournament, message, data, embed,
                view, mentions, entry,
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
            stats_channel, tournament, message, data, embed,
            view, mentions, entry,
            match_type=match_type, match_index=match_index,
            t1=t1, t2=t2,
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
    await bot.add_cog(OCRUploadCog(bot))
