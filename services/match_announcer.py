"""Публикация карточек «Матч #N начался».

Два режима:
- MM-турниры (mm_search_channel_id > 0): канал создаётся заранее на
  этапе ready-check с именем «cs-4x4-match-N» (tournament.mm_preset_channel_id);
  если его нет — создаётся здесь. Карточка Components V2 с PNG
  «НАЙДЕН МАТЧ» публикуется в канал; канал регистрируется в
  tournament.match_channels — скриншот, присланный в него,
  автоматически привязывается к матчу (cogs/ocr_upload.py), после
  загрузки канал удаляется.
- Обычные турниры: embed уходит в канал результатов (RESULT_CHANNEL_ID).

Факт отправки/создания хранится в match_number_store (announced).
"""

from __future__ import annotations

import io
import logging

import discord

from models.tournament import Tournament, TournamentPhase
from storage.json_store import store
from storage.match_number_store import match_number_store

logger = logging.getLogger(__name__)

ORG_ROLE_IDS = ()  # заполняется лениво из config


def _phase_matches(tournament: Tournament) -> list[tuple[str, int, int, int]]:
    """Матчи текущей фазы: (match_type, match_index, team_a, team_b)."""
    phase = tournament.phase
    out: list[tuple[str, int, int, int]] = []
    if phase == TournamentPhase.QUALIFIERS:
        for i, (a, b) in enumerate(getattr(tournament, "qualifier_matches", None) or []):
            out.append(("qualifier", i, a, b))
    elif phase == TournamentPhase.SEMIFINALS:
        for i, (a, b) in enumerate(getattr(tournament, "semifinal_matches", None) or []):
            out.append(("semifinal", i, a, b))
    elif phase == TournamentPhase.FINAL:
        ft = getattr(tournament, "final_teams", None)
        if ft and len(ft) == 2:
            out.append(("final", 0, ft[0], ft[1]))
    return out


def _match_key(tournament: Tournament, match_type: str, match_index: int) -> str:
    """Ключ матча — тот же формат, что match_id в карточке результата."""
    return f"{tournament.id}_{match_type}_{match_index}"


def _match_mentions(tournament: Tournament, t1: int, t2: int) -> str:
    """Ментions всех игроков матча."""
    from views.ocr_match_view import team_roster

    parts = []
    for idx in (t1, t2):
        for name in team_roster(tournament, idx):
            uid = tournament.player_user_ids.get(name)
            if uid and uid > 0:
                parts.append(f"<@{uid}>")
    return " ".join(dict.fromkeys(parts))


async def _create_match_channel(
    guild: discord.Guild,
    tournament: Tournament,
    match_number: int,
    t1: int,
    t2: int,
) -> discord.TextChannel | None:
    """Создать приватный канал матча в категории канала /mm squad."""
    search_channel_id = getattr(tournament, "mm_search_channel_id", 0) or 0
    if not search_channel_id:
        return None
    search_channel = guild.get_channel(search_channel_id)
    if search_channel is None:
        try:
            search_channel = await guild.fetch_channel(search_channel_id)
        except Exception:
            search_channel = None
    if search_channel is None:
        return None
    category = getattr(search_channel, "category", None)

    # Кому виден канал: игроки матча + организатор + орги
    from config import ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID, ORG_ROLE_ID
    from views.ocr_match_view import team_roster

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    allow = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, read_message_history=True
    )
    seen: set[int] = set()
    for idx in (t1, t2):
        for name in team_roster(tournament, idx):
            uid = tournament.player_user_ids.get(name)
            if uid and uid not in seen:
                seen.add(uid)
                overwrites[discord.Object(id=uid)] = allow
    if tournament.organizer_id:
        overwrites[discord.Object(id=tournament.organizer_id)] = allow
    for rid in (ORG_ROLE_ID, ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID):
        if rid:
            overwrites[discord.Object(id=rid)] = allow

    try:
        channel = await guild.create_text_channel(
            name=f"cs-4x4-match-{match_number}",
            category=category,
            overwrites=overwrites,
            reason=f"MM match #{match_number}",
        )
        return channel
    except Exception as exc:
        logger.error("Failed to create match channel: %s", exc)
        return None


def _match_started_view(
    tournament: Tournament,
    number: int,
    label: str,
    name_a: str,
    name_b: str,
    t1: int,
    t2: int,
    match_type: str,
    match_index: int,
    with_png: bool,
    state: str = "active",
    winner_name: str = "",
    score: tuple[int, int] | None = None,
) -> "discord.ui.LayoutView":
    """V2-карточка матча для MM-флоу (без эмодзи).

    state: active — ждём скриншот; processing — скриншот получен,
    идёт сопоставление; done — результат зафиксирован.
    """
    import time

    from utils.design.layouts import DesignView
    from cogs.ocr_upload import _roster_mentions as _mentions_fn
    from views.ocr_match_view import MatchScreenshotButton, team_roster

    roster_a = team_roster(tournament, t1)
    roster_b = team_roster(tournament, t2)

    view = DesignView(title=f"МАТЧ · {name_a} vs {name_b}", timeout=None)
    view.text(f"**КОМАНДА A — {name_a}**\n{_mentions_fn(tournament, t1)}")
    view.text(f"**КОМАНДА B — {name_b}**\n{_mentions_fn(tournament, t2)}")
    view.sep()
    if with_png:
        view.component(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media="attachment://match_found.png")
            )
        )
        view.sep()
    view.text(
        "**ИНФОРМАЦИЯ О МАТЧЕ**\n"
        "**Режим:** CS 4×4\n"
        "**Тип:** Ranked\n"
        f"**Начало:** <t:{int(time.time())}:t>"
    )
    view.sep()

    if state == "done":
        lines = ["**РЕЗУЛЬТАТ МАТЧА**", "Статус результата: **Завершён**"]
        if winner_name:
            lines.append(f"Победитель: **{winner_name}**")
        if score:
            lines.append(f"Счёт: {score[0]} : {score[1]}")
        lines.append(
            "Итог опубликован в общем канале результатов."
        )
        view.text("\n".join(lines))
    elif state == "processing":
        view.text(
            "**РЕЗУЛЬТАТ МАТЧА**\n"
            "Статус результата: **Скриншот получен — идёт "
            "сопоставление игроков**\n"
            "Капитаны сопоставляют ники со скриншота Discord-игрокам."
        )
    else:
        view.text(
            "**РЕЗУЛЬТАТ МАТЧА**\n"
            "Статус результата: **Ожидается**\n"
            "После окончания игры капитан ПОБЕДИВШЕЙ команды должен "
            "отправить скриншот."
        )
        view.row(
            MatchScreenshotButton(
                tournament.guild_id,
                tournament.channel_id,
                tournament,
                match_type,
                match_index,
                "Отправить результат",
            )
        )
    if state != "done":
        # Кнопка комнаты — вместо отдельного турнирного embed
        from views.room_buttons import RoomButton

        view.row(
            RoomButton(
                match_type, match_index, t1, t2, name_a, name_b,
            )
        )
    return view


async def update_match_card(
    guild: discord.Guild,
    tournament: Tournament,
    match_type: str,
    match_index: int,
    state: str = "active",
    winner_name: str = "",
    score: tuple[int, int] | None = None,
) -> None:
    """Обновить карточку матча в канале cs-4x4-match-N (MM-флоу).

    Ищет канал по tournament.match_channels, пересобирает V2-карточку
    с новым состоянием результата. Не-MM турниры молча пропускаются.
    """
    channel_id = 0
    meta: dict | None = None
    for ch_id, m in (tournament.match_channels or {}).items():
        if (
            m.get("match_type") == match_type
            and m.get("match_index") == match_index
        ):
            channel_id = int(ch_id)
            meta = m
            break
    if not meta or not meta.get("card_message_id"):
        return

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            return
    if not isinstance(channel, discord.TextChannel):
        return

    from cogs.ocr_upload import _captain_name, _match_label
    from views.ocr_match_view import _match_tuple
    from storage.match_number_store import match_number_store

    try:
        t1, t2 = _match_tuple(tournament, match_type, match_index)
    except Exception:
        return
    name_a = _captain_name(tournament, t1)
    name_b = _captain_name(tournament, t2)
    label = _match_label(match_type, match_index)
    try:
        number = await match_number_store.get_or_assign(
            _match_key(tournament, match_type, match_index)
        )
    except Exception:
        number = 0

    view = _match_started_view(
        tournament, number, label, name_a, name_b, t1, t2,
        match_type, match_index,
        with_png=bool(meta.get("has_png")),
        state=state,
        winner_name=winner_name,
        score=score,
    )
    try:
        msg = await channel.fetch_message(int(meta["card_message_id"]))
        # Вложения не трогаем — match_found.png остаётся на месте
        await msg.edit(view=view)
    except Exception as exc:
        logger.warning("Match card update failed: %s", exc)


async def announce_phase_matches(
    tournament: Tournament,
    guild: discord.Guild,
    bot: "discord.Client | None" = None,
) -> None:
    """Анонсировать все ещё не анонсированные матчи текущей фазы."""
    matches = _phase_matches(tournament)
    if not matches:
        return

    # Ленивые импорты — избегаем модульных циклов
    from cogs.ocr_upload import (
        RESULT_CHANNEL_ID,
        _captain_name,
        _match_label,
        _result_channel,
        _roster_mentions,
    )

    is_mm = bool(getattr(tournament, "mm_search_channel_id", 0))
    fallback_channel = await _result_channel(guild, None) if not is_mm else None

    # Канал, созданный на этапе ready-check (cs-4x4-match-N)
    preset_channel: discord.TextChannel | None = None
    preset_id = int(getattr(tournament, "mm_preset_channel_id", 0) or 0)
    if is_mm and preset_id:
        preset_channel = guild.get_channel(preset_id)
        if preset_channel is None:
            try:
                preset_channel = await guild.fetch_channel(preset_id)
            except Exception:
                preset_channel = None
        if preset_channel is not None and not isinstance(
            preset_channel, discord.TextChannel
        ):
            preset_channel = None

    # PNG карточки «НАЙДЕН МАТЧ», если MM-флоу его прокинул
    card_png: bytes | None = getattr(tournament, "mm_card_png", None)

    for match_type, match_index, t1, t2 in matches:
        key = _match_key(tournament, match_type, match_index)
        if await match_number_store.is_announced(key):
            continue

        number = await match_number_store.get_or_assign(key)

        name_a = _captain_name(tournament, t1)
        name_b = _captain_name(tournament, t2)
        label = _match_label(match_type, match_index)
        embed = discord.Embed(
            title=f" ⚔️ МАТЧ #{number} НАЧАЛСЯ — {label}",
            description=(
                f"**{name_a}** vs **{name_b}**\n\n"
                f"**{name_a}**\n{_roster_mentions(tournament, t1)}\n\n"
                f"**{name_b}**\n{_roster_mentions(tournament, t2)}\n\n"
                "Матч идёт — по окончании капитан загружает скриншот статистики."
            ),
            color=discord.Color.blurple(),
        )

        channel = preset_channel
        if channel is None and is_mm:
            channel = await _create_match_channel(guild, tournament, number, t1, t2)

        try:
            if channel is not None and is_mm:
                # V2-карточка «Матч начался» + PNG «Найден матч»
                view = _match_started_view(
                    tournament, number, label, name_a, name_b, t1, t2,
                    match_type, match_index,
                    with_png=card_png is not None,
                )
                match_msg = await channel.send(
                    view=view,
                    file=(
                        discord.File(io.BytesIO(card_png), filename="match_found.png")
                        if card_png
                        else None
                    ),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                # Закрепляем карточку матча — состав и статус всегда под рукой
                try:
                    await match_msg.pin(reason=f"MM match #{number} card")
                except Exception as exc:
                    logger.warning("Match card pin failed: %s", exc)
                # Кнопка скриншота в карточке — persistent view на случай рестарта
                if bot is not None:
                    try:
                        from views.ocr_match_view import MatchScreenshotButton
                        from views.room_buttons import RoomButton

                        persist = discord.ui.View(timeout=None)
                        persist.add_item(
                            MatchScreenshotButton(
                                tournament.guild_id,
                                tournament.channel_id,
                                tournament,
                                match_type,
                                match_index,
                                "Отправить результат",
                            )
                        )
                        persist.add_item(
                            RoomButton(
                                match_type, match_index, t1, t2,
                                name_a, name_b,
                            )
                        )
                        bot.add_view(persist)
                    except Exception as exc:
                        logger.warning("Match screenshot view register failed: %s", exc)
                # Регистрируем канал: скриншот в нём = загрузка результата.
                # card_message_id/has_png — для динамического обновления
                # карточки после OCR (processing/done).
                tournament.match_channels[str(channel.id)] = {
                    "match_type": match_type,
                    "match_index": match_index,
                    "card_message_id": match_msg.id,
                    "has_png": card_png is not None,
                }
                store.set(tournament)
                logger.info(
                    "Match #%d channel %s announced (%s)",
                    number, channel.id, label,
                )
            elif channel is not None:
                mentions = _match_mentions(tournament, t1, t2)
                await channel.send(
                    content=mentions or None,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                tournament.match_channels[str(channel.id)] = {
                    "match_type": match_type,
                    "match_index": match_index,
                }
                store.set(tournament)
                logger.info(
                    "Match #%d channel %s created (%s)",
                    number, channel.id, label,
                )
            elif fallback_channel is not None:
                await fallback_channel.send(embed=embed)
            else:
                # MM без канала (нет прав/категории) — fallback в результаты
                result_ch = await _result_channel(guild, None)
                if result_ch is not None:
                    await result_ch.send(embed=embed)

            await match_number_store.mark_announced(key)
            logger.info(
                "Match #%d announced: %s %s vs %s (tournament %s)",
                number, label, name_a, name_b, tournament.id,
            )
        except Exception as exc:
            logger.error("Match announce failed for %s: %s", key, exc)
