"""Squad Hub 2.0 — сервисный слой: защита активных турниров,
публичная карточка Squad Hub, рекрутинговые карточки, сводная
статистика игроков (SR/ELO).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import discord

from models.team import (
    JOIN_APPLICATIONS, JOIN_CLOSED, JOIN_MODE_LABELS, JOIN_OPEN,
    JOIN_PASSWORD, PURPOSE_LABELS, TEAM_MAX_MEMBERS, Team,
)
from storage import squad_store
from storage.team_store import team_store, TeamStoreError

logger = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()

# Debounce/coalescing обновления публичных карточек:
# guild_id -> asyncio.Task активного worker'а
_HUB_TASKS: dict[int, asyncio.Task] = {}
_HUB_GUILDS: dict[int, discord.Guild] = {}
_HUB_COALESCE_SECONDS = 1.5

# Подтверждение передачи капитанства: (guild_id, team_id) -> deadline
_PENDING_TRANSFERS: dict[tuple[int, str], tuple[int, float]] = {}
_TRANSFER_TTL = 300.0  # 5 минут на подтверждение


# ----------------------------------------------------------------------
# Защита активных турниров (BR + Cash Cup)
# ----------------------------------------------------------------------

async def is_team_in_active_tournament(guild_id: int,
                                       team_id: str) -> bool:
    """True — команда зафиксирована в активном BR или Cash Cup.

    BR: team_id в registered_teams не-завершённого и не-тестового
    турнира. Cash Cup: br_team_id совпадает с командой в статусе
    registration/running.
    """
    try:
        from models.br_tournament import BRTournamentPhase
        from storage.br_store import br_store
        for brt in br_store.all():
            if (brt and not getattr(brt, "is_test", False)
                    and int(getattr(brt, "guild_id", 0)) == int(guild_id)
                    and team_id in (brt.registered_teams or [])
                    and brt.phase not in (
                        BRTournamentPhase.COMPLETE,
                        BRTournamentPhase.CANCELLED)):
                return True
    except Exception as exc:
        logger.warning("BR protection check failed: %s", exc)

    try:
        from services.cashcup_store import cashcup_store
        state = await cashcup_store.active(guild_id)
        if state:
            for t in state.get("teams") or []:
                if t.get("br_team_id") == team_id:
                    return True
    except Exception as exc:
        logger.warning("CashCup protection check failed: %s", exc)

    return False


async def active_tournament_reason(guild_id: int, team_id: str) -> str | None:
    """Текст причины блокировки или None, если команда свободна."""
    if not await is_team_in_active_tournament(guild_id, team_id):
        return None
    return (
        "Команда зарегистрирована в активном турнире — состав, "
        "капитанство и расформирование временно заблокированы. "
        "После завершения турнира управление снова станет доступным."
    )


# ----------------------------------------------------------------------
# Статистика игроков (SR/ELO) — из существующих систем
# ----------------------------------------------------------------------

async def player_brief(guild_id: int, user_id: int,
                       guild: discord.Guild | None = None) -> dict[str, Any]:
    """Сводка по игроку: имя, SR, ELO. Нет данных — 'Не указан'."""
    name = f"<@{user_id}>"
    if guild is not None:
        m = guild.get_member(user_id)
        if m is not None:
            name = m.display_name
    sr = None
    elo = None
    try:
        from storage.player_stats_store import player_stats_store
        stats = await player_stats_store.get(guild_id, user_id)
        if stats is not None:
            sr = stats.skill_rating
            elo = stats.elo
            if stats.name:
                name = stats.name
    except Exception as exc:
        logger.debug("player_brief stats failed: %s", exc)
    return {"user_id": user_id, "name": name, "sr": sr, "elo": elo}


async def team_avg_sr(guild_id: int, members: list[int],
                      guild: discord.Guild | None = None) -> float | None:
    """Средний SR состава. None — нет данных ни у кого."""
    vals = []
    for uid in members:
        try:
            from storage.player_stats_store import player_stats_store
            stats = await player_stats_store.get(guild_id, uid)
            if stats is not None and stats.games > 0:
                vals.append(stats.skill_rating)
        except Exception:
            continue
    if not vals:
        return None
    return round(sum(vals) / len(vals), 1)


def fmt_sr(sr: float | None) -> str:
    return f"{sr:.1f}" if sr is not None else "Не указан"


def fmt_elo(elo: int | None) -> str:
    return str(elo) if elo is not None else "—"


# ----------------------------------------------------------------------
# Передача капитанства (подтверждение новым капитаном)
# ----------------------------------------------------------------------


def request_captain_transfer(guild_id: int, team_id: str,
                             new_captain_id: int) -> None:
    _PENDING_TRANSFERS[(int(guild_id), team_id)] = (
        int(new_captain_id), datetime.now(timezone.utc).timestamp()
        + _TRANSFER_TTL)


def pending_transfer_for(guild_id: int, team_id: str,
                         user_id: int) -> bool:
    """True — для этого пользователя есть ожидающее подтверждение."""
    item = _PENDING_TRANSFERS.get((int(guild_id), team_id))
    if item is None:
        return False
    cid, deadline = item
    if datetime.now(timezone.utc).timestamp() > deadline:
        _PENDING_TRANSFERS.pop((int(guild_id), team_id), None)
        return False
    return cid == int(user_id)


def consume_transfer_request(guild_id: int, team_id: str,
                             user_id: int) -> bool:
    """Атомарно забрать запрос на передачу (подтверждение)."""
    item = _PENDING_TRANSFERS.get((int(guild_id), team_id))
    if item is None:
        return False
    cid, deadline = item
    if datetime.now(timezone.utc).timestamp() > deadline or cid != int(user_id):
        return False
    _PENDING_TRANSFERS.pop((int(guild_id), team_id), None)
    return True


def cancel_transfer_request(guild_id: int, team_id: str) -> None:
    _PENDING_TRANSFERS.pop((int(guild_id), team_id), None)


# ----------------------------------------------------------------------
# Публичная карточка Squad Hub
# ----------------------------------------------------------------------


def hub_channel_id(guild_id: int | None) -> int:
    """Настроенный канал Squad Hub (Control Center / settings)."""
    try:
        from services.discord_cfg import channel_id as _cid
        return int(_cid(guild_id, "discord.squad_hub_channel_id", 0) or 0)
    except Exception:
        return 0


def recruitment_channel_id(guild_id: int | None) -> int:
    try:
        from services.discord_cfg import channel_id as _cid
        return int(_cid(guild_id, "discord.recruitment_channel_id", 0) or 0)
    except Exception:
        return 0


async def _hub_stats(guild_id: int) -> dict[str, int]:
    return await squad_store.squad_hub_stats(guild_id)


def render_hub_card(stats: dict[str, int]) -> discord.ui.LayoutView:
    """Публичная карточка Squad Hub (Components V2)."""
    from utils.design import colors
    lines = [
        "## r!z3 | SQUAD HUB",
        "",
        "Найди команду. Собери состав. Играй вместе.",
        "",
        f"**Всего команд:** {stats.get('teams', 0)}  ",
        f"**Ведут набор:** {stats.get('recruiting', 0)}  ",
        f"**Ищут команду:** {stats.get('lfg', 0)}",
    ]
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Colour(
        colors.BRAND))
    container.add_item(discord.ui.TextDisplay(content="\n".join(lines)))
    row = discord.ui.ActionRow()
    for label, cid, style in (
        ("Найти команду", "squad:find", discord.ButtonStyle.primary),
        ("Создать команду", "squad:create", discord.ButtonStyle.success),
        ("Ищу команду", "squad:lfg", discord.ButtonStyle.secondary),
        ("Моя команда", "squad:mine", discord.ButtonStyle.secondary),
        ("Приглашения", "squad:invites", discord.ButtonStyle.secondary),
    ):
        row.add_item(discord.ui.Button(
            style=style, label=label, custom_id=cid))
    container.add_item(row)
    view.add_item(container)
    return view


async def ensure_hub_card(bot: discord.Client,
                          guild: discord.Guild) -> discord.Message | None:
    """Создать карточку Squad Hub, если её ещё нет.

    Канал берётся из настроек; канал не создаётся автоматически.
    """
    state = await squad_store.get_hub_state(guild.id)
    if state:
        ch = guild.get_channel(state["channel_id"])
        if ch is not None:
            try:
                msg = await ch.fetch_message(state["message_id"])
                return msg
            except (discord.NotFound, discord.Forbidden):
                pass
    channel_id = hub_channel_id(guild.id)
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        return None
    stats = await _hub_stats(guild.id)
    msg = await channel.send(
        view=render_hub_card(stats), allowed_mentions=_NO_MENTIONS)
    await squad_store.set_hub_state(guild.id, channel.id, msg.id)
    return msg


async def refresh_hub_card(guild: discord.Guild) -> None:
    """Обновить счётчики карточки (debounced через schedule)."""
    schedule_hub_refresh(guild)


def schedule_hub_refresh(guild: discord.Guild) -> None:
    """Отложенное обновление карточки: несколько изменений подряд
    схлопываются в одну правку сообщения (coalescing window)."""
    gid = int(guild.id)
    _HUB_GUILDS[gid] = guild
    task = _HUB_TASKS.get(gid)
    if task is not None and not task.done():
        return  # worker уже запланирован — он перечитает состояние
    _HUB_TASKS[gid] = asyncio.get_event_loop().create_task(
        _hub_refresh_worker(gid))


async def _hub_refresh_worker(gid: int) -> None:
    try:
        await asyncio.sleep(_HUB_COALESCE_SECONDS)
        guild = _HUB_GUILDS.get(gid)
        if guild is None:
            return
        await _do_hub_refresh(guild)
    except Exception as exc:
        logger.warning("hub refresh failed: %s", exc)
    finally:
        _HUB_TASKS.pop(gid, None)


async def _do_hub_refresh(guild: discord.Guild) -> None:
    state = await squad_store.get_hub_state(guild.id)
    if not state:
        return
    channel = guild.get_channel(state["channel_id"])
    if channel is None:
        return
    try:
        message = await channel.fetch_message(state["message_id"])
    except (discord.NotFound, discord.Forbidden):
        await squad_store.clear_hub_state(guild.id)
        return
    stats = await _hub_stats(guild.id)
    try:
        await message.edit(
            view=render_hub_card(stats), allowed_mentions=_NO_MENTIONS)
    except (discord.NotFound, discord.Forbidden) as exc:
        logger.info("hub card edit skipped: %s", exc)


async def restore_hub_views(bot: discord.Client) -> int:
    """Восстановить persistent views карточек Squad Hub после рестарта.

    Возвращает число восстановленных сообщений.
    """
    from views.squad_hub_view import SquadHubView, RecruitmentCardView
    restored = 0
    for guild in bot.guilds:
        state = await squad_store.get_hub_state(guild.id)
        if state:
            ch = guild.get_channel(state["channel_id"])
            if ch is not None:
                try:
                    await ch.fetch_message(state["message_id"])
                    bot.add_view(SquadHubView(),
                                 message_id=state["message_id"])
                    restored += 1
                except (discord.NotFound, discord.Forbidden):
                    pass
        # Рекрутинговые карточки команд
        for team in team_store.get_by_guild(guild.id):
            if team.recruitment_message_id and team.recruitment_channel_id:
                ch = guild.get_channel(team.recruitment_channel_id)
                if ch is not None:
                    try:
                        await ch.fetch_message(team.recruitment_message_id)
                        bot.add_view(
                            RecruitmentCardView(team.team_id),
                            message_id=team.recruitment_message_id)
                        restored += 1
                    except (discord.NotFound, discord.Forbidden):
                        pass
    return restored


# ----------------------------------------------------------------------
# Рекрутинговая карточка команды
# ----------------------------------------------------------------------

async def publish_recruitment_card(bot: discord.Client,
                                   guild: discord.Guild,
                                   team: Team,
                                   *,
                                   create_if_missing: bool = True
                                   ) -> discord.Message | None:
    """Опубликовать/обновить карточку набора команды.

    Одна актуальная карточка на команду: при повторной публикации
    редактируется существующее сообщение (channel_id/message_id
    сохранены в teams). ``create_if_missing=False`` — только
    обновить уже опубликованную карточку (для синхронизации
    состава без явной публикации капитаном).
    """
    from views.team_recruit_view import render_recruit_card

    fresh = await team_store.refresh_from_db(team.team_id) or team
    has_card = bool(fresh.recruitment_channel_id
                    and fresh.recruitment_message_id)
    if not has_card and not create_if_missing:
        return None

    channel_id = recruitment_channel_id(guild.id)
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        return None
    stats = await _recruit_stats(guild, fresh)
    view = render_recruit_card(guild, fresh, stats)

    if (fresh.recruitment_channel_id
            and fresh.recruitment_message_id):
        old_ch = guild.get_channel(fresh.recruitment_channel_id)
        if old_ch is not None:
            try:
                msg = await old_ch.fetch_message(
                    fresh.recruitment_message_id)
                await msg.edit(view=view, allowed_mentions=_NO_MENTIONS)
                return msg
            except (discord.NotFound, discord.Forbidden):
                pass

    msg = await channel.send(view=view, allowed_mentions=_NO_MENTIONS)
    fresh.recruitment_channel_id = channel.id
    fresh.recruitment_message_id = msg.id
    await team_store.update_team(fresh)
    return msg


async def _recruit_stats(guild: discord.Guild,
                         team: Team) -> dict[str, Any]:
    avg_sr = await team_avg_sr(guild.id, team.members, guild)
    captain = guild.get_member(team.captain_id)
    return {
        "avg_sr": avg_sr,
        "captain_name": (captain.display_name if captain else
                         f"<@{team.captain_id}>"),
    }


async def sync_recruit_card(bot: discord.Client,
                            guild: discord.Guild,
                            team_id: str) -> None:
    """Обновить рекрутинговую карточку после изменения состава —
    только если карточка уже опубликована."""
    try:
        team = await team_store.refresh_from_db(team_id)
        if team is not None and (team.recruitment_channel_id
                                 or team.recruitment_message_id):
            await publish_recruitment_card(
                bot, guild, team, create_if_missing=False)
    except Exception as exc:
        logger.info("recruit card sync failed: %s", exc)


async def close_recruitment_card(bot: discord.Client,
                                 guild: discord.Guild,
                                 team: Team) -> None:
    """Обновить карточку после заполнения/закрытия набора."""
    try:
        await publish_recruitment_card(bot, guild, team)
    except Exception as exc:
        logger.info("recruit card refresh failed: %s", exc)


async def archive_recruitment_card(bot: discord.Client,
                                   guild: discord.Guild,
                                   team_name: str) -> None:
    """Пометить карточку расформированной команды."""
    # Карточка удаляется из teams каскадом — ищем по сохранённым id
    # невозможно после delete; вызыется ДО disband с team объектом.
    pass  # реализовано в потоке расформирования


# ----------------------------------------------------------------------
# Утилиты для видов
# ----------------------------------------------------------------------


async def member_line(guild: discord.Guild, team: Team,
                      index: int) -> str:
    """Строка участника для карточки команды."""
    uid = team.members[index]
    m = guild.get_member(uid)
    name = m.display_name if m else f"<@{uid}>"
    brief = await player_brief(guild.id, uid, guild)
    role = "Капитан" if uid == team.captain_id else "Участник"
    return f"{index + 1:02d}. {name} — {role} · SR {fmt_sr(brief['sr'])}"


def join_mode_allows_view(team: Team) -> bool:
    return True


def can_apply(team: Team) -> bool:
    return (team.recruiting and not team.is_full()
            and team.join_mode == JOIN_APPLICATIONS)


def can_join_open(team: Team) -> bool:
    return (team.recruiting and not team.is_full()
            and team.join_mode == JOIN_OPEN)


def needs_password(team: Team) -> bool:
    return (team.recruiting and not team.is_full()
            and team.join_mode == JOIN_PASSWORD)
