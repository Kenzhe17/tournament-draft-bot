from config import NOTIFICATION_CHANNEL_ID, ROOM_DATA_CHANNEL_ID, replace_emojis
"""Modal form for entering room ID and password."""

import logging
import time
import discord
from utils.design import text_card
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from models.tournament import Tournament


# Cooldown для отправки комнаты (1 секунда)
room_submission_cooldowns = {}  # guild_id -> timestamp


class RoomModal(discord.ui.Modal, title="Комната игры"):
    """Modal for entering room ID and password."""

    room_id = discord.ui.TextInput(
        label="ID комнаты",
        placeholder="123456",
        required=True,
        max_length=20
    )

    room_password = discord.ui.TextInput(
        label="Пароль",
        placeholder="123",
        required=True,
        max_length=20
    )

    def __init__(self, match_type: str, match_index: int, team1_index: int, team2_index: int):
        super().__init__()
        self.match_type = match_type
        self.match_index = match_index
        self.team1_index = team1_index
        self.team2_index = team2_index

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Handle room ID and password submission."""
        from storage.json_store import store

        # Defer immediately to avoid timeout
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.DiscordServerError):
            # Interaction expired or server error, continue anyway
            pass

        # Check cooldown (1 second)
        guild_id = interaction.guild_id
        current_time = time.time()
        if guild_id in room_submission_cooldowns:
            elapsed = current_time - room_submission_cooldowns[guild_id]
            if elapsed < 1:
                try:
                    await interaction.followup.send(
                        f"❌ Подождите {1 - elapsed:.1f} секунд перед отправкой комнаты.",
                        ephemeral=True
                    )
                except (discord.NotFound, discord.DiscordServerError):
                    pass
                return

        tournament = store.get(interaction.guild_id, interaction.channel_id)
        if not tournament:
            try:
                await interaction.followup.send(replace_emojis("❌ Нет активного турнира."), ephemeral=True)
            except (discord.NotFound, discord.DiscordServerError):
                pass
            return

        # Validate inputs
        if not self.room_id.value or not self.room_password.value:
            try:
                await interaction.followup.send(replace_emojis("❌ ID и пароль комнаты не могут быть пустыми."), ephemeral=True)
            except (discord.NotFound, discord.DiscordServerError):
                pass
            return

        # Check if room data already exists for this match
        existing_room = None
        if self.match_type == "qualifier":
            existing_room = tournament.qualifier_rooms.get(self.match_index)
        elif self.match_type == "semifinal":
            existing_room = tournament.semifinal_rooms.get(self.match_index)
        elif self.match_type == "final":
            existing_room = tournament.final_room

        if existing_room:
            try:
                await interaction.followup.send(
                    replace_emojis("❌ Данные комнаты уже были отправлены другим пользователем."),
                    ephemeral=True
                )
            except (discord.NotFound, discord.DiscordServerError):
                pass
            return

        # Check cooldown (1 second)
        if guild_id in room_submission_cooldowns:
            elapsed = current_time - room_submission_cooldowns[guild_id]
            if elapsed < 1:
                try:
                    await interaction.followup.send(
                        f"❌ Подождите {1 - elapsed:.1f} секунд перед отправкой комнаты.",
                        ephemeral=True
                    )
                except (discord.NotFound, discord.DiscordServerError):
                    pass
                return

        # Set cooldown BEFORE sending (to prevent simultaneous submissions)
        room_submission_cooldowns[guild_id] = time.time()

        # Store room data based on match type
        room_data = {"id": self.room_id.value, "password": self.room_password.value}

        if self.match_type == "qualifier":
            tournament.qualifier_rooms[self.match_index] = room_data
        elif self.match_type == "semifinal":
            tournament.semifinal_rooms[self.match_index] = room_data
        elif self.match_type == "final":
            tournament.final_room = room_data

        store.set(tournament)

        # Update tournament message
        from bot import TournamentBot
        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        # Send DM notifications to team members
        card_ok = await send_room_dm_notifications(
            bot, tournament, self.match_type, self.match_index,
            self.team1_index, self.team2_index,
            self.room_id.value, self.room_password.value,
        )

        # Confirm to user — ТОЛЬКО при успешной публикации карточки.
        # При неудаче данные комнаты уже сохранены и выставлен
        # card_pending — карточка будет переотправлена без
        # создания второй комнаты (bot.setup_hook recovery).
        try:
            if card_ok:
                await interaction.followup.send(
                    replace_emojis("✅ Комната создана и данные отправлены."),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    replace_emojis(
                        "⚠️ Комната создана, данные сохранены, но "
                        "карточка не опубликована — она будет "
                        "отправлена автоматически."
                    ),
                    ephemeral=True,
                )
        except (discord.NotFound, discord.DiscordServerError):
            pass


def _team_roster(tournament: Any, team_data: dict) -> list[str]:
    """Имена состава: players[] или circle1-4."""
    players = [p for p in team_data.get("players", []) if p]
    if players:
        return players
    return [
        team_data.get(f"circle{c}") for c in range(1, 5)
        if team_data.get(f"circle{c}")
    ]


def _team_line(tournament: Any, team_data: dict) -> str:
    """Строка вида '<@p1> <@p2> <@p3> <@p4>'."""
    parts = []
    for name in _team_roster(tournament, team_data):
        uid = tournament.player_user_ids.get(name)
        parts.append(f"<@{uid}>" if uid and uid > 0 else name)
    return " ".join(parts) if parts else "*"


def _room_mentions(tournament: Any, team_data: dict) -> str:
    """Упоминания состава команды (<@id> там, где известен user_id)."""
    return _team_line(tournament, team_data)


async def _resolve_mm_match_channel(
    bot: Any, tournament: Any, match_type: str, match_index: int
):
    """Канал матча (cs-4x4-match-N), где анонсирован данный матч."""
    for ch_id, mc in (getattr(tournament, "match_channels", None) or {}).items():
        bound = mc.get("matches") or []
        direct = (
            mc.get("match_type") == match_type
            and mc.get("match_index") == match_index
        )
        if [match_type, match_index] in bound or direct:
            ch = bot.get_channel(int(ch_id))
            if ch is None:
                try:
                    ch = await bot.fetch_channel(int(ch_id))
                except Exception:
                    ch = None
            if ch is not None:
                return ch
    preset = int(getattr(tournament, "mm_preset_channel_id", 0) or 0)
    if preset:
        ch = bot.get_channel(preset)
        if ch is None:
            try:
                ch = await bot.fetch_channel(preset)
            except Exception:
                ch = None
        if ch is not None:
            return ch
    return None


# ---------------------------------------------------------------------------
# «🔓 Комната открыта»: embed + ephemeral-кнопка ID комнаты.
# Пароль выводится прямо в карточке; ID — только по кнопке и только
# капитанам команд матча, организаторам и администраторам.
# ---------------------------------------------------------------------------

_ROOM_DATA_TEMPLATE = (
    r"rd:(?P<gid>\d+):(?P<cid>\d+):(?P<mtype>qualifier|semifinal|final):"
    r"(?P<midx>\d+):(?P<t1>-?\d+):(?P<t2>-?\d+):(?P<what>id|pass)"
)

_ROOM_DATA_DENIED = (
    "❌ Получить данные комнаты могут только участники матча, "
    "организаторы или администраторы."
)


class _RoomDataButton(discord.ui.Button):
    async def callback(self, interaction: discord.Interaction) -> None:
        await _room_data_press(interaction)


class RoomDataDynamicItem(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_ROOM_DATA_TEMPLATE,
):
    """Persistent-шаблон кнопок данных комнаты: custom_id кодирует
    guild/channel/match/команды — состояние не хранится в виде."""

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        what = match.group("what")
        label = "📋 ID" if what == "id" else "🔑 Пароль"
        concrete = getattr(item, "custom_id", None) or str(
            interaction.data.get("custom_id") or ""
        )
        btn = _RoomDataButton(
            style=discord.ButtonStyle.secondary,
            label=label,
            custom_id=concrete,
        )
        return cls(btn)


class RoomDataTemplateView(discord.ui.View):
    """Регистрируется один раз в bot.setup_hook — dispatch по шаблону."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            RoomDataDynamicItem(
                _RoomDataButton(
                    style=discord.ButtonStyle.secondary,
                    label="📦 Данные комнаты",
                    custom_id="rd:0:0:qualifier:0:0:0:id",
                )
            )
        )


def _room_data_buttons(
    guild_id: int,
    channel_id: int,
    match_type: str,
    match_index: int,
    team1_index: int,
    team2_index: int,
) -> list[discord.ui.Button]:
    """Кнопка «Получить ID» — custom_id кодирует контекст.

    Пароль больше не запрашивается кнопкой — он показан в карточке.
    Суффикс `:id` сохранён для совместимости с persistent-шаблоном
    и старыми карточками.
    """
    base = (
        f"rd:{guild_id}:{channel_id}:{match_type}:{match_index}:"
        f"{team1_index}:{team2_index}"
    )
    return [
        discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="📋 ID",
            custom_id=f"{base}:id",
        ),
    ]


def _roster_line(
    tournament: Any,
    team_data: dict,
    *,
    ping: bool,
    display: dict | None = None,
) -> str:
    """Строка ростера: ping=True → <@id>-упоминания (MM);
    ping=False → отображаемые имена без упоминаний (турниры)."""
    if ping:
        return _room_mentions(tournament, team_data) or "—"
    names = _team_roster(tournament, team_data)
    if display:
        names = [display.get(n, n) for n in names]
    return " ".join(names) if names else "—"


def _room_card_view(
    tournament: Any,
    team1_data: dict,
    team2_data: dict,
    name1: str,
    name2: str,
    match_type: str,
    match_index: int,
    team1_index: int,
    team2_index: int,
    *,
    ping: bool = True,
    display: dict | None = None,
) -> discord.ui.LayoutView:
    """Components V2 карточка «🔓 Комната открыта».

    Структура: заголовок, имя команды 1, её ростер, имя команды 2,
    её ростер, пароль. ID — только по кнопке.
    ping=True (MM): <@id> пингуют игроков через allowed_mentions.
    ping=False (турниры): обычные имена, AllowedMentions.none().
    Одна отправка = одно уведомление; сообщение не редактируется
    и не пересылается → повторных пингов после рестарта нет.
    """
    from utils.design.layouts import DesignView

    if match_type == "qualifier":
        room_data = (tournament.qualifier_rooms or {}).get(match_index) or {}
    elif match_type == "semifinal":
        room_data = (tournament.semifinal_rooms or {}).get(match_index) or {}
    else:
        room_data = tournament.final_room or {}
    password = str(room_data.get("password", "") or "")

    view = DesignView(timeout=None)
    lines = [
        "🔓 Комната открыта",
        "",
        name1,
        _roster_line(tournament, team1_data, ping=ping, display=display),
        "",
        name2,
        _roster_line(tournament, team2_data, ping=ping, display=display),
        "",
        f"Пароль: `{password}`",
    ]
    view.lines(*lines)
    view.row(
        *_room_data_buttons(
            tournament.guild_id, tournament.channel_id,
            match_type, match_index, team1_index, team2_index,
        )
    )
    return view


async def _room_data_press(interaction: discord.Interaction) -> None:
    """Callback кнопок «Получить ID/пароль» — ephemeral-ответ."""
    import re

    custom_id = str(interaction.data.get("custom_id") or "")
    m = re.match(_ROOM_DATA_TEMPLATE, custom_id)
    if not m:
        await interaction.response.send_message(
            "⚠️ Кнопка устарела — данные комнаты недоступны.",
            ephemeral=True,
        )
        return

    gid = int(m.group("gid"))
    cid = int(m.group("cid"))
    match_type = m.group("mtype")
    match_index = int(m.group("midx"))
    enc_t1, enc_t2 = int(m.group("t1")), int(m.group("t2"))
    what = m.group("what")

    from storage.json_store import store
    tournament = store.get(gid, cid)
    if tournament is None:
        await interaction.response.send_message(
            "⚠️ Турнир не найден — данные комнаты недоступны.",
            ephemeral=True,
        )
        return

    # Данные комнаты из АКТУАЛЬНОГО состояния турнира.
    if match_type == "qualifier":
        room_data = (tournament.qualifier_rooms or {}).get(match_index)
    elif match_type == "semifinal":
        room_data = (tournament.semifinal_rooms or {}).get(match_index)
    else:
        room_data = tournament.final_room
    if not room_data:
        await interaction.response.send_message(
            "⚠️ Данные комнаты не найдены или уже удалены.",
            ephemeral=True,
        )
        return

    user = interaction.user
    uid = getattr(user, "id", 0)
    allowed = False

    # Администратор сервера — по реальным Discord-правам.
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and getattr(perms, "administrator", False):
        allowed = True

    # Организатор (org-роли/админы проекта) — через is_org_check.
    if not allowed and interaction.guild is not None:
        try:
            from utils.permissions import is_org_check
            allowed = bool(is_org_check(user, interaction.guild))
        except Exception:
            allowed = False

    # Участники матча — ЛЮБОЙ из 8 игроков обеих команд, проверка
    # по Discord ID из player_user_ids. Пара команд — свежая из
    # resolve_match_context; при неудаче — индексы из custom_id.
    if not allowed:
        try:
            from models.tournament import resolve_match_context
            ctx = resolve_match_context(tournament, match_type, match_index)
            team_ids = [ctx.team_a_index, ctx.team_b_index]
        except Exception:
            team_ids = [enc_t1, enc_t2]
        try:
            from views.ocr_match_view import team_roster
            pids = getattr(tournament, "player_user_ids", None) or {}
            teams = getattr(tournament, "teams", None) or []
            participant_ids = set()
            for i in team_ids:
                if not (0 <= int(i) < len(teams)):
                    continue
                for name in team_roster(tournament, int(i)):
                    mapped = pids.get(name)
                    try:
                        if mapped:
                            participant_ids.add(int(mapped))
                    except (TypeError, ValueError):
                        continue
            allowed = uid in participant_ids
        except Exception:
            allowed = False

    if not allowed:
        await interaction.response.send_message(
            replace_emojis(_ROOM_DATA_DENIED), ephemeral=True
        )
        return

    # Ответ — ТОЛЬКО значение, без заголовков/markdown/лишнего текста.
    value = str(
        room_data.get("id", "") if what == "id"
        else room_data.get("password", "")
    )
    await interaction.response.send_message(value, ephemeral=True)


async def send_room_dm_notifications(
    bot: Any,
    tournament: Any,
    match_type: str,
    match_index: int,
    team1_index: int,
    team2_index: int,
    room_id: str,
    room_password: str,
) -> bool:
    """Уведомление об открытой комнате. True — карточка опубликована.

    MM (/mm squad): карточка в канал матча, пинги <@id> работают.
    Обычный турнир (/tournament create): карточка в ROOM_DATA_CHANNEL_ID
    без упоминаний + анонс с пингом роли в канал уведомлений.
    При неудаче публикации выставляется room_data["card_pending"] —
    восстановление после рестарта в bot.setup_hook.
    """
    team1_data = tournament.teams[team1_index] if team1_index < len(tournament.teams) else {}
    team2_data = tournament.teams[team2_index] if team2_index < len(tournament.teams) else {}

    # Название команды = имя капитана
    name1 = team1_data.get("captain", f"Team {team1_index}")
    name2 = team2_data.get("captain", f"Team {team2_index}")

    is_mm = bool(getattr(tournament, "mm_search_channel_id", 0))

    def _room_entry() -> dict | None:
        """Запись room_data этого матча — для флага card_pending."""
        try:
            if match_type == "qualifier":
                return (tournament.qualifier_rooms or {}).get(match_index)
            if match_type == "semifinal":
                return (tournament.semifinal_rooms or {}).get(match_index)
            if match_type == "final":
                return tournament.final_room or None
        except Exception:
            return None
        return None

    def _update_room_entry(update) -> None:
        entry = _room_entry()
        if entry is None:
            return
        try:
            update(entry)
            from storage.json_store import store
            store.set(tournament)
        except Exception as exc:
            logger.warning("room card state mark failed: %s", exc)

    def _mark_pending(pending: bool) -> None:
        def _u(e):
            if pending:
                e["card_pending"] = True
            else:
                e.pop("card_pending", None)
        _update_room_entry(_u)

    def _mark_sent(message_id: int) -> None:
        def _u(e):
            e["card_message_id"] = int(message_id)
            e.pop("card_pending", None)
        _update_room_entry(_u)

    if is_mm:
        channel = await _resolve_mm_match_channel(
            bot, tournament, match_type, match_index
        )
        if channel is None:
            logger.warning(
                "MM room notify: match channel not found "
                "(%s/%s, tournament %s)",
                match_type, match_index, getattr(tournament, "id", "?"),
            )
            _mark_pending(True)
            return False
        card = _room_card_view(
            tournament, team1_data, team2_data, name1, name2,
            match_type, match_index, team1_index, team2_index,
            ping=True,
        )
        try:
            msg = await channel.send(
                view=card,
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
        except Exception as exc:
            logger.warning("MM room card send failed: %s", exc)
            _mark_pending(True)
            return False
        _mark_sent(getattr(msg, "id", 0) or 0)
        return True

    # Обычный турнир (/tournament create): карточка — строго в
    # ROOM_DATA_CHANNEL_ID, <@id>-упоминания игроков пингуют
    # участников матча (users=True, roles/everyone выключены).
    # Актуальные Discord-имена подставляет сам Discord.
    data_channel = bot.get_channel(ROOM_DATA_CHANNEL_ID)
    if data_channel is None:
        try:
            data_channel = await bot.fetch_channel(ROOM_DATA_CHANNEL_ID)
        except Exception:
            data_channel = None
    card_sent = False
    if data_channel is not None:
        try:
            card = _room_card_view(
                tournament, team1_data, team2_data, name1, name2,
                match_type, match_index, team1_index, team2_index,
                ping=True,
            )
            msg = await data_channel.send(
                view=card,
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
            card_sent = True
            card_msg_id = getattr(msg, "id", 0) or 0
        except Exception as exc:
            logger.warning("Tournament room card send failed: %s", exc)
    if card_sent:
        _mark_sent(card_msg_id)
    else:
        _mark_pending(True)

    from config import NOTIFICATION_ROLE_ID
    general_channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    if general_channel is None:
        try:
            general_channel = await bot.fetch_channel(NOTIFICATION_CHANNEL_ID)
        except Exception:
            general_channel = None
    if general_channel is not None and card_sent:
        guild_obj = getattr(general_channel, "guild", None)
        role = (
            guild_obj.get_role(NOTIFICATION_ROLE_ID)
            if guild_obj is not None else None
        )
        role_mention = role.mention if role else ""
        # Components V2: content= с view= запрещён (50035) —
        # упоминание роли и текст переносим внутрь TextDisplay
        # карточки; allowed_mentions roles=True сохраняет пинг.
        notice_text = (
            f"{role_mention}\n" if role_mention else ""
        ) + f"**Комната открыта** — данные в <#{data_channel.id}>"
        try:
            await general_channel.send(
                view=text_card(notice_text),
                allowed_mentions=discord.AllowedMentions(
                    users=False, roles=True, everyone=False
                ),
            )
        except discord.HTTPException as exc:
            # Уведомление не критично для карточки комнаты —
            # логируем и не прерываем on_submit. Повторной
            # отправки нет: карточка уже опубликована, дубль
            # пинга хуже пропущенного уведомления.
            logger.warning(
                "Room-open notification to %s failed: %s",
                getattr(general_channel, "id", "?"), exc,
            )
    return card_sent

def pending_room_cards(tournament: Any) -> list[tuple[str, int]]:
    """Матчи с room_data.card_pending — для recovery после рестарта."""
    out: list[tuple[str, int]] = []
    try:
        for mtype, rooms in (
            ("qualifier", tournament.qualifier_rooms),
            ("semifinal", tournament.semifinal_rooms),
        ):
            for midx, rd in (rooms or {}).items():
                if isinstance(rd, dict) and rd.get("card_pending"):
                    out.append((mtype, int(midx)))
        fr = tournament.final_room
        if isinstance(fr, dict) and fr.get("card_pending"):
            out.append(("final", 0))
    except Exception:
        pass
    return out


async def retry_pending_room_card(
    bot: Any,
    tournament: Any,
    match_type: str,
    match_index: int,
) -> bool:
    """Переотправка карточки «Комната открыта» после рестарта.

    Если card_message_id сохранён и сообщение живо — только
    снимаем card_pending (БЕЗ дубля). Иначе — полная отправка.
    """
    from storage.json_store import store

    if match_type == "qualifier":
        entry = (tournament.qualifier_rooms or {}).get(match_index)
    elif match_type == "semifinal":
        entry = (tournament.semifinal_rooms or {}).get(match_index)
    else:
        entry = tournament.final_room or None
    if not isinstance(entry, dict) or not entry.get("card_pending"):
        return False

    is_mm = bool(getattr(tournament, "mm_search_channel_id", 0))
    channel = None
    if is_mm:
        channel = await _resolve_mm_match_channel(
            bot, tournament, match_type, match_index
        )
    else:
        channel = bot.get_channel(ROOM_DATA_CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(ROOM_DATA_CHANNEL_ID)
            except Exception:
                channel = None

    # Сообщение уже опубликовано до падения — только снимаем флаг.
    mid = entry.get("card_message_id")
    if mid and channel is not None:
        try:
            await channel.fetch_message(int(mid))
        except Exception:
            pass  # сообщения нет — переотправляем ниже
        else:
            entry.pop("card_pending", None)
            try:
                store.set(tournament)
            except Exception as exc:
                logger.warning("room card_pending clear failed: %s", exc)
            return True

    # Полная переотправка (идемпотентно: флаг снимается только
    # при успешной публикации, card_message_id обновляется).
    try:
        from views.ocr_match_view import _match_tuple
        t1, t2 = _match_tuple(tournament, match_type, match_index)
    except Exception:
        logger.warning(
            "retry room card: cannot resolve teams %s/%s",
            match_type, match_index,
        )
        return False
    return await send_room_dm_notifications(
        bot, tournament, match_type, match_index, t1, t2, "", ""
    )
