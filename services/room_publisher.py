"""Публикация данных игровых комнат обычных турниров.

Формат (возвращённый старый стиль):
- CV2-карточка «КОМНАТА ОТКРЫТА» в ROOM_DATA_CHANNEL_ID:
  названия команд, ростеры <@id> всех 8 участников, правила
  матча (если заданы), пароль, кнопка «ID» (ephemeral-ответ
  с копируемым ID).
- Уведомление в ROOM_OPEN_NOTIFY_CHANNEL_ID: «КОМНАТА ОТКРЫТА»,
  «Команда A vs Команда B», упоминания 8 участников, ссылка
  на канал данных и на конкретную карточку. ID/пароль в
  уведомлении НЕ публикуются.

Гарантии:
- Одна актуальная запись на match_key (room_store);
- карточка создаётся один раз, далее редактируется по
  data_message_id — повторных карточек нет;
- уведомление отправляется только при первой публикации
  (notify_required + notification_message_id == 0) — при
  изменении ID/пароля повторного уведомления нет;
- при ошибке публикации пользователь НЕ получает ложный
  успех; card_pending запускает безопасный retry после
  рестарта (retry_pending_rooms).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import discord

from config import ROOM_DATA_CHANNEL_ID, ROOM_OPEN_NOTIFY_CHANNEL_ID
from storage.room_store import room_store
from utils.design.layouts import DesignView

if TYPE_CHECKING:
    from models.tournament import Tournament

logger = logging.getLogger(__name__)

_RULES_LABELS = {
    "kazakhstan": "Kazakhstan",
    "brazil": "Brazil",
}

# Persistent-кнопка «ID» на карточке комнаты.
# custom_id: rcard:id:{gid}:{cid}:{tid}:{phase}:{idx}
T_ROOM_ID = re.compile(
    r"^rcard:id:(?P<gid>\d+):(?P<cid>\d+):"
    r"(?P<tid>[^:]+):(?P<phase>qualifier|semifinal|final):(?P<idx>\d+)$"
)


# ---------------------------------------------------------------------------
# Составы и названия
# ---------------------------------------------------------------------------


def _team_display_name(tournament: "Tournament", idx: int) -> str:
    team = (
        tournament.teams[idx]
        if 0 <= idx < len(tournament.teams)
        else {}
    )
    captain = team.get("captain", f"Команда {idx + 1}")
    return str(tournament.team_names.get(idx, captain))


def _roster_mentions(
    tournament: "Tournament", team: dict[str, Any]
) -> str:
    """Ростер как <@uid>-упоминания; без ID — сохранённое имя."""
    from utils.embeds import team_roster_names

    parts = []
    for name in team_roster_names(team):
        uid = tournament.player_user_ids.get(name)
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            uid = 0
        parts.append(f"<@{uid}>" if uid > 0 else name)
    return " ".join(parts) if parts else "—"


def _match_teams(
    tournament: "Tournament", m: dict[str, Any]
) -> tuple[dict, dict]:
    a, b = m["teams"]
    teams = tournament.teams or []
    t1 = teams[a] if 0 <= a < len(teams) else {}
    t2 = teams[b] if 0 <= b < len(teams) else {}
    return t1, t2


def _rules_label(tournament: "Tournament") -> str:
    ruleset = (getattr(tournament, "ruleset", "") or "").strip()
    if not ruleset:
        return ""
    return _RULES_LABELS.get(ruleset.lower(), ruleset)


# ---------------------------------------------------------------------------
# CV2-карточка «КОМНАТА ОТКРЫТА»
# ---------------------------------------------------------------------------


def room_card_custom_id(
    guild_id: int, channel_id: int, tournament, m: dict[str, Any]
) -> str:
    return (
        f"rcard:id:{guild_id}:{channel_id}:{tournament.id}:"
        f"{m['phase']}:{m['index']}"
    )


def build_room_card_view(
    tournament: "Tournament",
    m: dict[str, Any],
    room: dict[str, Any],
) -> DesignView:
    """CV2-карточка данных комнаты для канала ROOM_DATA_CHANNEL_ID."""
    t1, t2 = _match_teams(tournament, m)
    name1 = _team_display_name(tournament, m["teams"][0])
    name2 = _team_display_name(tournament, m["teams"][1])
    rules = _rules_label(tournament)

    view = DesignView(timeout=None)
    view.text("## КОМНАТА ОТКРЫТА")
    view.text(f"**{name1}**\n{_roster_mentions(tournament, t1)}")
    view.text(f"**{name2}**\n{_roster_mentions(tournament, t2)}")
    if rules:
        view.text(f"**Правила:** {rules}")
    view.text(f"**Пароль:** `{room.get('password', '')}`")
    view.row(discord.ui.Button(
        style=discord.ButtonStyle.primary,
        label="ID",
        custom_id=room_card_custom_id(
            tournament.guild_id, tournament.channel_id, tournament, m
        ),
    ))
    return view


# ---------------------------------------------------------------------------
# Уведомление в общий чат
# ---------------------------------------------------------------------------


def build_room_notify_view(
    tournament: "Tournament",
    m: dict[str, Any],
    data_channel_id: int,
    data_message_id: int,
) -> DesignView:
    """CV2-уведомление «КОМНАТА ОТКРЫТА» для общего чата.

    Упоминания 8 участников (пингуют), ссылки на канал данных
    и на конкретную карточку. ID/пароль не раскрываются.
    """
    t1, t2 = _match_teams(tournament, m)
    name1 = _team_display_name(tournament, m["teams"][0])
    name2 = _team_display_name(tournament, m["teams"][1])

    lines = [
        "## КОМНАТА ОТКРЫТА",
        f"**{name1} vs {name2}**",
        _roster_mentions(tournament, t1),
        _roster_mentions(tournament, t2),
        f"Данные комнаты: <#{data_channel_id}>",
    ]
    if data_message_id:
        jump = (
            f"https://discord.com/channels/"
            f"{tournament.guild_id}/{data_channel_id}/{data_message_id}"
        )
        lines.append(f"Карточка: {jump}")

    view = DesignView(timeout=None)
    view.text("\n".join(lines))
    return view


# ---------------------------------------------------------------------------
# Публикация / редактирование
# ---------------------------------------------------------------------------


async def _get_channel(
    bot: discord.Client, channel_id: int
) -> discord.abc.GuildChannel | None:
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("room publish: channel %s fetch failed: %s",
                           channel_id, exc)
            return None
    return channel


async def _send_or_edit_card(
    bot: discord.Client,
    data_channel,
    tournament: "Tournament",
    m: dict[str, Any],
    room: dict[str, Any],
) -> tuple[bool, int]:
    """Создать или отредактировать карточку. → (ok, message_id)."""
    card_view = build_room_card_view(tournament, m, room)
    mentions = discord.AllowedMentions(users=True, roles=False,
                                       everyone=False)

    existing_id = int(room.get("data_message_id") or 0)
    if existing_id:
        try:
            msg = await data_channel.fetch_message(existing_id)
            await msg.edit(
                view=card_view, allowed_mentions=mentions
            )
            return True, existing_id
        except discord.NotFound:
            pass  # карточка удалена — отправим новую ниже
        except Exception as exc:  # noqa: BLE001
            logger.warning("room card edit failed: %s", exc)
            return False, existing_id

    try:
        msg = await data_channel.send(view=card_view,
                                      allowed_mentions=mentions)
        return True, int(getattr(msg, "id", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("room card send failed: %s", exc)
        return False, 0


async def _send_notification(
    bot: discord.Client,
    notify_channel,
    tournament: "Tournament",
    m: dict[str, Any],
    data_channel_id: int,
    data_message_id: int,
) -> tuple[bool, int]:
    """Отправить уведомление в общий чат → (ok, message_id)."""
    view = build_room_notify_view(
        tournament, m, data_channel_id, data_message_id
    )
    mentions = discord.AllowedMentions(users=True, roles=False,
                                       everyone=False)
    try:
        msg = await notify_channel.send(view=view,
                                        allowed_mentions=mentions)
        return True, int(getattr(msg, "id", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("room notify send failed: %s", exc)
        return False, 0


async def publish_room(
    bot: discord.Client,
    tournament: "Tournament",
    m: dict[str, Any],
    room: dict[str, Any],
) -> dict[str, Any]:
    """Опубликовать/обновить карточку комнаты и уведомление.

    Возвращает {"card": bool, "notified": bool, "data_message_id": int}.
    Безопасно для повторных вызовов: карточка редактируется по
    data_message_id, уведомление — только при notify_required
    и отсутствии notification_message_id.
    """
    gid, cid = tournament.guild_id, tournament.channel_id
    key = m["key"]
    result = {"card": False, "notified": False, "data_message_id": 0}

    data_channel = await _get_channel(bot, ROOM_DATA_CHANNEL_ID)
    if data_channel is None:
        room_store.set_publish_state(
            gid, cid, key, card_pending=True)
        return result

    ok, msg_id = await _send_or_edit_card(
        bot, data_channel, tournament, m, room
    )
    if not ok:
        room_store.set_publish_state(
            gid, cid, key, card_pending=True)
        return result

    # Обновить room-снимок и сохранить message_id.
    room = dict(room)
    room["data_message_id"] = msg_id
    room_store.set_publish_state(
        gid, cid, key, data_message_id=msg_id, card_pending=False)
    result["card"] = True
    result["data_message_id"] = msg_id

    # Уведомление — только при первой публикации.
    needs_notify = bool(room.get("notify_required")) and not int(
        room.get("notification_message_id") or 0)
    if needs_notify:
        notify_channel = await _get_channel(
            bot, ROOM_OPEN_NOTIFY_CHANNEL_ID)
        if notify_channel is not None:
            ok_n, n_msg_id = await _send_notification(
                bot, notify_channel, tournament, m,
                ROOM_DATA_CHANNEL_ID, msg_id,
            )
            if ok_n and n_msg_id:
                room_store.set_publish_state(
                    gid, cid, key,
                    notification_message_id=n_msg_id,
                    notify_required=False,
                )
                result["notified"] = True
            else:
                # Карточка есть, уведомление — нет: флаг pending,
                # recovery дошлёт его без дубля карточки.
                room_store.set_publish_state(
                    gid, cid, key, card_pending=True)
        else:
            room_store.set_publish_state(
                gid, cid, key, card_pending=True)

    return result


async def retry_pending_rooms(bot: discord.Client) -> int:
    """Восстановление после рестарта: довернуть pending-публикации.

    Для каждой записи с card_pending находит турнир в store и
    повторяет publish_room — карточка редактируется/создаётся
    по data_message_id, уведомление уходит только если его
    ещё не было (notify_required).
    """
    from storage.json_store import store as tournament_store

    resolved = 0
    for item in room_store.pending_rooms():
        gid, cid, key = (
            item["guild_id"], item["channel_id"], item["key"])
        try:
            tournament = tournament_store.get(gid, cid)
        except Exception:
            tournament = None
        if tournament is None:
            continue
        # Восстановить match-словарь из ключа матча.
        parts = key.split(":")
        # key = {tid}:{phase}:{idx}; tid может содержать ':'? — нет.
        if len(parts) < 3:
            continue
        phase, idx_s = parts[-2], parts[-1]
        try:
            idx = int(idx_s)
        except ValueError:
            continue
        from views.room_view import iter_tournament_matches
        m = next(
            (x for x in iter_tournament_matches(tournament)
             if x["phase"] == phase and x["index"] == idx),
            None,
        )
        if m is None:
            continue
        room = room_store.get_active_room(gid, cid, key)
        if room is None:
            continue
        try:
            res = await publish_room(bot, tournament, m, room)
            if res["card"]:
                resolved += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("room retry failed key=%s: %s", key, exc)
    if resolved:
        logger.info("room publish recovery: %d записей", resolved)
    return resolved


# ---------------------------------------------------------------------------
# Persistent-кнопка «ID» на карточке
# ---------------------------------------------------------------------------


class RoomIdBtn(discord.ui.DynamicItem[discord.ui.Button],
                template=T_ROOM_ID):
    """Кнопка «ID» — ephemeral-ответ с ID комнаты (копируемый)."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="ID",
            custom_id=custom_id,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(str(interaction.data.get("custom_id")
                       or item.custom_id))

    async def callback(self, interaction: discord.Interaction) -> None:
        custom_id = str(interaction.data.get("custom_id") or "")
        m = T_ROOM_ID.match(custom_id)
        if not m:
            await interaction.response.send_message(
                "Кнопка устарела.", ephemeral=True)
            return
        gid, cid = int(m.group("gid")), int(m.group("cid"))
        tid, phase = m.group("tid"), m.group("phase")
        idx = int(m.group("idx"))
        key = f"{tid}:{phase}:{idx}"
        room = room_store.get_active_room(gid, cid, key)
        if not room or not room.get("room_id"):
            await interaction.response.send_message(
                "Данные комнаты не найдены.", ephemeral=True)
            return
        # Отвечаем ТОЛЬКО значением — удобно копировать.
        await interaction.response.send_message(
            str(room["room_id"]), ephemeral=True)


class RoomCardTemplateView(discord.ui.View):
    """Регистрируется один раз в setup_hook — dispatch по шаблону."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(RoomIdBtn("rcard:id:0:0:0:qualifier:0"))
