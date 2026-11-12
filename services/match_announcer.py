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
import time

import discord

from models.tournament import Tournament, TournamentPhase
from storage.json_store import store
from storage.match_number_store import match_number_store

logger = logging.getLogger(__name__)

ORG_ROLE_IDS = ()  # заполняется лениво из config

# Правила матча (казахский) — ephemeral-карточка по кнопке «📜 Правила»
_MATCH_RULES_TEXT = (
    "**Ойын ережелері:**\n\n"
    "Ойын басталуын 5 минут күтеміз. Егер осы уақыт ішінде қарсылас "
    "келмесе, ойынды бар құраммен бастаймыз. Мысалы: 4:3.\n\n"
    "Егер ойын барысында бір ойыншы AFK болып, ойыннан шығып кетсе, "
    "матчты соңына дейін жалғастырамыз. Нәтиже скриншотқа тіркеледі.\n\n"
    "Бөлме міндетті түрде 5 раундтан тұруы керек. Бұл бот арқылы қай "
    "команданың жеңгенін анықтау үшін қажет.\n\n"
    "Жеңілген команда скриншот жібермейді. Скриншотты тек жеңімпаз "
    "команда жібереді.\n\n"
    "Комната міндетті түрде 5р форматында ашылуы керек.\n\n"
    "0:0 кезінде ойыннан шығуға рұқсат етіледі. Ал 1-раунд өткеннен "
    "кейін ойыннан шығуға болмайды."
)


class MatchRulesButton(discord.ui.Button):
    """Кнопка «📜 Правила» — ephemeral V2-карточка с правилами матча."""

    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="📜 Правила",
            custom_id="match_rules",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.design import text_card

        await interaction.response.send_message(
            view=text_card(_MATCH_RULES_TEXT, title="📜 Ойын ережелері"),
            ephemeral=True,
        )


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


def _roster_names(tournament: Tournament, team_index: int) -> str:
    """Состав команды обычными никами — БЕЗ <@id>-упоминаний.

    Для карточек обычных турниров (/tournament create): пинги
    игрокам не нужны, Discord-упоминания не используются.
    """
    from views.ocr_match_view import team_roster

    return " ".join(team_roster(tournament, team_index))


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

    # Единый менеджер пула: атомарный слот + overwrites + очистка.
    # Временные каналы не создаются — при полном пуле возвращается None,
    # и карточка уходит в fallback-канал результатов.
    from services import match_channel_pool
    from views.ocr_match_view import team_roster

    user_ids = []
    seen: set[int] = set()
    for idx in (t1, t2):
        for name in team_roster(tournament, idx):
            uid = tournament.player_user_ids.get(name)
            if uid and uid not in seen:
                seen.add(uid)
                user_ids.append(uid)
    role_ids = [
        r for r in (ORG_ROLE_ID, ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID)
        if r
    ]
    channel, err = await match_channel_pool.acquire_channel(
        guild,
        user_ids,
        role_ids=role_ids,
        organizer_id=tournament.organizer_id or 0,
        owner=f"mm-ann:{guild.id}:{tournament.guild_id}:{number}",
    )
    if channel is None:
        logger.warning(
            "Match channel acquire failed (%s) for match #%s", err, number
        )
    return channel


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
    result_url: str = "",
    started_at: float | None = None,
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

    if state == "done":
        # Финальное состояние: весь текст заменяется ссылкой на
        # опубликованный результат, изображение (match_found.png)
        # остаётся на месте.
        if with_png:
            view.component(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media="attachment://match_found.png")
                )
            )
            view.sep()
        if result_url:
            view.text(
                f"Результат этого матча — [результат матча]({result_url})"
            )
        else:
            view.text("Результат этого матча зафиксирован.")
        return view

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
        f"**Начало:** <t:{int(started_at or time.time())}:t>"
    )
    view.sep()

    if state == "processing":
        view.text(
            "**РЕЗУЛЬТАТ МАТЧА**\n"
            "Статус результата: **Скриншот получен — идёт "
            "сопоставление игроков**\n"
            "Капитаны сопоставляют ники со скриншота Discord-игрокам.\n"
            "Скриншот должен быть в разрешении **16:9**."
        )
    else:
        view.text(
            "**РЕЗУЛЬТАТ МАТЧА**\n"
            "Статус результата: **Ожидается**\n"
            "После окончания матча капитан **ПОБЕДИВШЕЙ** команды должен "
            "загрузить скриншот через кнопку «Отправить результат».\n"
            "Скриншот должен быть в разрешении **16:9**."
        )
    if state != "done":
        # Все кнопки — в одной нижней строке карточки
        buttons = []
        if state == "active":
            buttons.append(
                MatchScreenshotButton(
                    tournament.guild_id,
                    tournament.channel_id,
                    tournament,
                    match_type,
                    match_index,
                    "Отправить результат",
                )
            )
        # Кнопка комнаты — вместо отдельного турнирного embed
        from views.room_buttons import RoomButton

        buttons.append(
            RoomButton(
                match_type, match_index, t1, t2, name_a, name_b,
            )
        )
        buttons.append(MatchRulesButton())
        view.row(*buttons)
    return view


async def update_match_card(
    guild: discord.Guild,
    tournament: Tournament,
    match_type: str,
    match_index: int,
    state: str = "active",
    winner_name: str = "",
    score: tuple[int, int] | None = None,
    result_url: str = "",
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
        result_url=result_url,
        started_at=float(meta.get("started_at") or 0) or None,
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
        # Обычные турниры — состав обычными никами без <@id>;
        # MM сохраняет упоминания (пинги нужны для поиска матча).
        roster_fn = _roster_mentions if is_mm else _roster_names
        embed = discord.Embed(
            title=f" ⚔️ МАТЧ #{number} НАЧАЛСЯ — {label}",
            description=(
                f"**{name_a}** vs **{name_b}**\n\n"
                f"**{name_a}**\n{roster_fn(tournament, t1)}\n\n"
                f"**{name_b}**\n{roster_fn(tournament, t2)}\n\n"
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
                started_at = time.time()
                view = _match_started_view(
                    tournament, number, label, name_a, name_b, t1, t2,
                    match_type, match_index,
                    with_png=card_png is not None,
                    started_at=started_at,
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
                # Кнопки скриншота и комнаты в карточке — persistent view
                # на случай рестарта
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
                        persist.add_item(MatchRulesButton())
                        bot.add_view(persist)
                    except Exception as exc:
                        logger.warning("Match screenshot view register failed: %s", exc)
                # Регистрируем канал: скриншот в нём = загрузка результата.
                # card_message_id/has_png — для динамического обновления
                # карточки после OCR (processing/done). matches — все
                # матчи, анонсированные в этот канал (для очистки пула).
                mc_entry = tournament.match_channels.get(str(channel.id), {})
                mc_entry.update({
                    "match_type": match_type,
                    "match_index": match_index,
                    "card_message_id": match_msg.id,
                    "has_png": card_png is not None,
                    "started_at": started_at,
                })
                bound = mc_entry.setdefault("matches", [])
                pair = [match_type, match_index]
                if pair not in bound:
                    bound.append(pair)
                tournament.match_channels[str(channel.id)] = mc_entry
                store.set(tournament)
                logger.info(
                    "Match #%d channel %s announced (%s)",
                    number, channel.id, label,
                )
            elif channel is not None:
                # Обычный турнир: без пингов — только embed с
                # обычными никами, allowed_mentions полностью off.
                await channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                mc_entry = tournament.match_channels.get(str(channel.id), {})
                mc_entry.update({
                    "match_type": match_type,
                    "match_index": match_index,
                })
                bound = mc_entry.setdefault("matches", [])
                pair = [match_type, match_index]
                if pair not in bound:
                    bound.append(pair)
                tournament.match_channels[str(channel.id)] = mc_entry
                store.set(tournament)
                logger.info(
                    "Match #%d channel %s created (%s)",
                    number, channel.id, label,
                )
            elif is_mm:
                # MM без выделенного канала (нет прав/категории) —
                # fallback в канал результатов.
                # Обычные турниры: анонс-эмбед не отправляем — матчи
                # отображаются в панели турнира.
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
