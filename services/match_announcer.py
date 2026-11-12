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
    с новым состоянием результата. Не-MM турниры: единая точка
    обновления публичной карточки матча и рабочего OCR-сообщения.
    """
    if not getattr(tournament, "mm_search_channel_id", 0):
        try:
            await update_public_match_card(
                guild, tournament, match_type, match_index
            )
            await refresh_match_control(
                guild, tournament, match_type, match_index
            )
        except Exception as exc:
            logger.warning("match card public update failed: %s", exc)
        return
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


_CARD_LOCKS: dict[str, "asyncio.Lock"] = {}
_CARD_LOCKS_MAX = 200


def _card_lock(key: str) -> "asyncio.Lock":
    """Per-match asyncio.Lock — сериализует fetch+send карточки,
    чтобы два параллельных апдейта не создали дубликат."""
    import asyncio

    lock = _CARD_LOCKS.get(key)
    if lock is None:
        if len(_CARD_LOCKS) >= _CARD_LOCKS_MAX:
            _CARD_LOCKS.pop(next(iter(_CARD_LOCKS)))
        lock = asyncio.Lock()
        _CARD_LOCKS[key] = lock
    return lock


def _mc_key(match_type: str, match_index: int) -> str:
    return f"{match_type}_{match_index}"


def _bracket_winner_index(
    tournament: Tournament, match_type: str, match_index: int
) -> int | None:
    """Зафиксированный победитель матча в сетке (None — не определён)."""
    try:
        if match_type == "qualifier":
            w = (tournament.qualifier_winners or [])[match_index]
        elif match_type == "semifinal":
            w = (tournament.semifinal_winners or [])[match_index]
        elif match_type == "final":
            w = tournament.winner_team_index
        else:
            return None
    except (IndexError, TypeError):
        return None
    try:
        return int(w) if w is not None else None
    except (TypeError, ValueError):
        return None


def _rooms_played(tournament: Tournament, match_type: str,
                  match_index: int) -> int:
    """Количество сыгранных комнат матча (для публичной карточки)."""
    session = (tournament.ocr_sessions or {}).get(
        _mc_key(match_type, match_index)
    ) or {}
    rooms = session.get("rooms") or []
    if rooms:
        return len(rooms)
    if session.get("mode") in ("squad", "classic") or session.get(
        "message_id"
    ):
        return 1
    return 0


async def update_public_match_card(
    guild: discord.Guild,
    tournament: Tournament,
    match_type: str,
    match_index: int,
) -> None:
    """Legacy-очистка: отдельные публичные карточки матчей больше
    не публикуются — результаты показываются в основном embed
    турнирной сетки. Если в match_cards сохранился
    public_message_id, подтверждённое сообщение бота в канале
    турнира удаляется, поля очищаются."""
    if getattr(tournament, "mm_search_channel_id", 0):
        return
    key = _mc_key(match_type, match_index)
    lock_key = f"{getattr(tournament, 'id', '?')}:{key}"
    async with _card_lock(lock_key):
        await _update_public_match_card_impl(
            guild, tournament, match_type, match_index
        )


async def cleanup_public_match_cards(
    guild: discord.Guild,
    tournament: Tournament,
) -> None:
    """Удалить все legacy публичные карточки матчей турнира —
    sweep для восстановления после рестарта."""
    if getattr(tournament, "mm_search_channel_id", 0):
        return
    for mkey in list((getattr(tournament, "match_cards", None) or {})):
        try:
            mt, mi_s = mkey.rsplit("_", 1)
            await update_public_match_card(
                guild, tournament, mt, int(mi_s)
            )
        except Exception as exc:
            logger.warning(
                "public card cleanup failed for %s: %s", mkey, exc
            )


async def _update_public_match_card_impl(
    guild: discord.Guild,
    tournament: Tournament,
    match_type: str,
    match_index: int,
) -> None:
    """Удалить сохранённую legacy публичную карточку матча.

    Новые публичные карточки НЕ создаются. Удаление выполняется
    только для сообщения бота в канале турнира, не совпадающего
    с основным сообщением сетки. При ошибке API ID сохраняется
    для повторной попытки."""
    if getattr(tournament, "mm_search_channel_id", 0):
        return
    key = _mc_key(match_type, match_index)
    mcards = getattr(tournament, "match_cards", None)
    if not mcards:
        return
    entry = mcards.get(key)
    if not entry:
        return
    mid = entry.get("public_message_id")
    if not mid:
        return
    channel_id = int(
        entry.get("public_channel_id") or tournament.channel_id or 0
    )
    try:
        channel = guild.get_channel(channel_id)
        if channel is None:
            channel = await guild.fetch_channel(channel_id)
    except Exception:
        channel = None
    if channel is not None:
        try:
            msg = await channel.fetch_message(int(mid))
        except discord.NotFound:
            msg = None
        except Exception as exc:
            # Временная ошибка API — ID сохраняем для retry.
            logger.warning(
                "legacy public card fetch failed mid=%s: %s", mid, exc
            )
            return
        if msg is not None:
            author = getattr(msg, "author", None)
            is_own = author is None or getattr(author, "bot", False)
            is_main = (
                int(getattr(tournament, "message_id", 0) or 0)
                == int(mid)
            )
            if not is_own or is_main:
                logger.warning(
                    "legacy public card mid=%s skipped: "
                    "not a bot match card", mid,
                )
                entry.pop("public_message_id", None)
                entry.pop("public_channel_id", None)
                entry.pop("sig", None)
                try:
                    store.set(tournament)
                except Exception:
                    pass
                return
            try:
                await msg.delete()
            except discord.NotFound:
                pass
            except Exception as exc:
                logger.warning(
                    "legacy public card delete failed mid=%s: %s",
                    mid, exc,
                )
                return
    entry.pop("public_message_id", None)
    entry.pop("public_channel_id", None)
    entry.pop("sig", None)
    try:
        store.set(tournament)
    except Exception:
        pass


def replace_emojis_safe(text: str) -> str:
    try:
        from config import replace_emojis

        return replace_emojis(text)
    except Exception:
        return text


def _control_content(
    tournament: Tournament, match_type: str, match_index: int
) -> tuple[str, bool]:
    """(текст, finished) рабочего сообщения матча в OCR-канале."""
    from cogs.ocr_upload import _captain_name, _match_label
    from views.ocr_match_view import _match_tuple, match_is_finished

    t1, t2 = _match_tuple(tournament, match_type, match_index)
    name_a = _captain_name(tournament, t1)
    name_b = _captain_name(tournament, t2)
    label = _match_label(match_type, match_index)
    finished = (
        _bracket_winner_index(tournament, match_type, match_index)
        is not None
        or match_is_finished(tournament, match_type, match_index)
    )
    session = (tournament.ocr_sessions or {}).get(
        _mc_key(match_type, match_index)
    )
    if finished:
        status = "Результаты подтверждены"
        rooms_str = str(_rooms_played(tournament, match_type, match_index))
    elif session and session.get("rooms_expected"):
        done = sum(
            1 for r in session.get("rooms") or []
            if r.get("panels") or r.get("pending")
        )
        rooms_str = f"{done}/{int(session['rooms_expected'])}"
        status = {
            "collecting": "Сбор скриншотов",
            "awaiting_mapping": "Сопоставление игроков",
        }.get(str(session.get("status")), "Обработка OCR")
    else:
        rooms_str = "—"
        status = "Ожидание скриншотов"

    # Капитаны — только реальные Discord ID, один пинг на создание.
    cap_ids: list[int] = []
    try:
        from cogs.ocr_upload import _match_captain_ids

        cap_ids = _match_captain_ids(
            tournament, match_type, match_index, session or {}
        )
    except Exception:
        cap_ids = []
    lines = [
        f"**{label} — {name_a} vs {name_b}**",
        "",
        f"Комнаты: {rooms_str}",
        f"Статус: {status}",
    ]
    if finished:
        winner_idx = _bracket_winner_index(
            tournament, match_type, match_index
        )
        if winner_idx is not None:
            lines.append(
                f"🏆 Победитель: "
                f"{_captain_name(tournament, winner_idx)}"
            )
    if cap_ids:
        lines.append(
            "Капитаны: " + " ".join(f"<@{u}>" for u in cap_ids)
        )
    return "\n".join(lines), finished


def match_control_view(
    guild_id: int,
    channel_id: int,
    tournament: Tournament,
    match_type: str,
    match_index: int,
    finished: bool = False,
):
    """Persistent view рабочего сообщения: кнопка «Загрузить
    скриншоты» (MatchScreenshotButton → RoomStartModal); для
    завершённого матча — «📊 Статистика» (ephemeral)."""
    from discord.ui import View
    from views.ocr_match_view import MatchScreenshotButton

    view = View(timeout=None)
    if finished:
        view.add_item(
            MatchStatsButton(
                guild_id, channel_id, tournament,
                match_type, match_index,
            )
        )
    else:
        view.add_item(
            MatchScreenshotButton(
                guild_id, channel_id, tournament,
                match_type, match_index,
                "📸 Загрузить скриншоты",
            )
        )
    return view


class MatchStatsButton(discord.ui.Button):
    """«📊 Статистика» на итоговой рабочей карточке — ephemeral
    K/D/A/Damage по обеим командам из сохранённой сессии."""

    def __init__(
        self, guild_id: int, channel_id: int, tournament,
        match_type: str, match_index: int,
    ):
        super().__init__(
            label="📊 Статистика",
            style=discord.ButtonStyle.secondary,
            custom_id=(
                f"match_stats:{guild_id}:{channel_id}:"
                f"{match_type}:{match_index}"
            ),
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.json_store import store
        from views.ocr_match_view import (
            _match_tuple, _rooms_aggregate_stats,
            _team_display_name, team_roster,
        )

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Турнир не найден.", ephemeral=True
            )
            return
        key = f"{self.match_type}_{self.match_index}"
        session = (tournament.ocr_sessions or {}).get(key) or {}
        if not session.get("rooms"):
            await interaction.response.send_message(
                "ℹ️ Статистика недоступна.", ephemeral=True
            )
            return
        try:
            t1, t2 = _match_tuple(
                tournament, self.match_type, self.match_index
            )
            stats = _rooms_aggregate_stats(
                session, tournament, t1, t2
            ) or {}
        except Exception:
            stats = {}
        lines = []
        for tidx in (t1, t2):
            lines.append(f"**{_team_display_name(tournament, tidx)}**")
            for name in team_roster(tournament, tidx):
                s = stats.get(name) or {}
                lines.append(
                    f"{name} — {s.get('kills', '—')}/"
                    f"{s.get('deaths', '—')}/{s.get('assists', '—')} — "
                    f"{s.get('dmg', '—')}"
                )
            lines.append("")
        await interaction.response.send_message(
            "\n".join(lines)[:1900], ephemeral=True
        )


async def _ocr_work_channel(guild: discord.Guild):
    from config import OCR_MAPPING_CHANNEL_ID

    try:
        ch = guild.get_channel(OCR_MAPPING_CHANNEL_ID)
        if ch is None:
            ch = await guild.fetch_channel(OCR_MAPPING_CHANNEL_ID)
        return ch
    except Exception:
        return None


async def send_match_control(
    guild: discord.Guild,
    tournament: Tournament,
    match_type: str,
    match_index: int,
    bot=None,
) -> None:
    """Создать рабочее сообщение матча в OCR-канале (одно на матч).

    Идемпотентно: при существующем work_message_id сообщение
    только обновляется, дубль не отправляется.
    """
    if getattr(tournament, "mm_search_channel_id", 0):
        return
    key = _mc_key(match_type, match_index)
    mcards = getattr(tournament, "match_cards", None)
    if mcards is None:
        mcards = {}
        try:
            tournament.match_cards = mcards
        except Exception:
            pass
    entry = mcards.setdefault(key, {})
    channel = await _ocr_work_channel(guild)
    if channel is None:
        return
    try:
        text, finished = _control_content(
            tournament, match_type, match_index
        )
    except Exception as exc:
        logger.warning("match control text failed: %s", exc)
        return
    view = match_control_view(
        tournament.guild_id, tournament.channel_id, tournament,
        match_type, match_index, finished=finished,
    )
    mentions = discord.AllowedMentions(
        users=True, roles=False, everyone=False
    )
    import hashlib

    csig = hashlib.sha1(text.encode()).hexdigest()[:16]
    mid = entry.get("work_message_id")
    msg = None
    if mid:
        try:
            msg = await channel.fetch_message(int(mid))
        except Exception:
            msg = None
    try:
        if msg is not None:
            if entry.get("control_sig") != csig:
                await msg.edit(
                    content=replace_emojis_safe(text), view=view,
                    allowed_mentions=mentions,
                )
                entry["control_sig"] = csig
        else:
            msg = await channel.send(
                content=replace_emojis_safe(text), view=view,
                allowed_mentions=mentions,
            )
            entry["work_message_id"] = msg.id
            entry["work_channel_id"] = channel.id
            entry["control_sig"] = csig
            if bot is not None:
                try:
                    bot.add_view(view, message_id=msg.id)
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("match control send failed: %s", exc)
        return
    try:
        store.set(tournament)
    except Exception:
        pass


async def refresh_match_control(
    guild: discord.Guild,
    tournament: Tournament,
    match_type: str,
    match_index: int,
) -> None:
    """Актуализировать рабочее сообщение матча.

    Пропускается, пока жива активная OCR-сессия — тогда сообщение
    принадлежит progress/confirm-флоу. Без сессии показывает
    «Ожидание скриншотов» + кнопку (после отката) или «Завершён».
    """
    if getattr(tournament, "mm_search_channel_id", 0):
        return
    key = _mc_key(match_type, match_index)
    entry = (getattr(tournament, "match_cards", None) or {}).get(key)
    session = (tournament.ocr_sessions or {}).get(key)
    if session and session.get("status") not in (
        None, "completed", "cancelled"
    ):
        return
    if entry and entry.get("work_message_id"):
        await send_match_control(guild, tournament, match_type,
                                 match_index)
    else:
        # Legacy-матч без карточки — создаём рабочее сообщение,
        # только если матч актуален (фаза живая, не завершён).
        from views.ocr_match_view import match_is_finished

        phase = getattr(getattr(tournament, "phase", None), "value", "")
        phase_ok = (
            (match_type == "qualifier" and phase == "qualifiers")
            or (match_type == "semifinal" and phase == "semifinals")
            or (match_type == "final" and phase == "final")
        )
        if not phase_ok:
            return
        if match_is_finished(
            tournament, match_type, match_index
        ) or _bracket_winner_index(
            tournament, match_type, match_index
        ) is not None:
            return
        await send_match_control(guild, tournament, match_type,
                                 match_index)


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
            else:
                # Обычный турнир: одно публичное сообщение матча
                # в канале турнира + одно рабочее сообщение в
                # OCR-канале (кнопка «Загрузить скриншоты»).
                try:
                    await update_public_match_card(
                        guild, tournament, match_type, match_index
                    )
                    await send_match_control(
                        guild, tournament, match_type, match_index,
                        bot=bot,
                    )
                    store.set(tournament)
                except Exception as exc:
                    logger.warning(
                        "match cards announce failed: %s", exc
                    )

            await match_number_store.mark_announced(key)
            logger.info(
                "Match #%d announced: %s %s vs %s (tournament %s)",
                number, label, name_a, name_b, tournament.id,
            )
        except Exception as exc:
            logger.error("Match announce failed for %s: %s", key, exc)
