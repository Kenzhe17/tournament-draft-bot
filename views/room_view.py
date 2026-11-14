"""Данные комнат турнира (ID + пароль Free Fire) — Components V2.

Отдельная система от K/D и результатов:
- отправлять могут только участники конкретного матча (по user_id);
- организаторы видят и исправляют данные всех матчей;
- в публичной карточке — только статус «Комната готова»;
- пароль виден только участникам матча и организаторам;
- все действия — кнопки/Modal/ephemeral, без новых сообщений в канале.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import discord

from models.tournament import TournamentPhase
from storage.json_store import store
from storage.room_store import match_key, room_store
from utils.design import compose, text_card
from utils.design.formatters import muted
from utils.permissions import can_manage_tournament

if TYPE_CHECKING:
    from models.tournament import Tournament

logger = logging.getLogger(__name__)

# Временное хранилище «ожидающих подтверждения» правок комнаты:
# {(gid, cid, uid): {match_key, room_no, room_id, password, ts}}
_PENDING_EDITS: dict[tuple[int, int, int], dict[str, Any]] = {}
_PENDING_TTL = 300.0

_PHASE_LABELS = {
    "qualifier": "ОТБОР",
    "semifinal": "ПОЛУФИНАЛ",
    "final": "ФИНАЛ",
}


# ---------------------------------------------------------------------------
# Перечисление матчей турнира
# ---------------------------------------------------------------------------

def iter_tournament_matches(
    tournament: "Tournament",
) -> list[dict[str, Any]]:
    """Все матчи текущей bracket-фазы турнира.

    Возвращает dict: phase, index, key (storage match_key),
    teams (a,b), active, label («МАТЧ #N — СТАДИЯ»).
    """
    out: list[dict[str, Any]] = []
    tid = str(tournament.id)
    phase = tournament.phase

    def _emit(p: str, i: int, a: int, b: int, active: bool) -> None:
        out.append({
            "phase": p,
            "index": i,
            "key": match_key(tid, p, i),
            "teams": (a, b),
            "active": active,
            "label": f"МАТЧ #{i + 1} — {_PHASE_LABELS[p]}",
        })

    if phase == TournamentPhase.QUALIFIERS:
        winners = tournament.qualifier_winners
        for i, (a, b) in enumerate(tournament.qualifier_matches):
            w = winners[i] if i < len(winners) else None
            _emit("qualifier", i, a, b, w is None)
    elif phase == TournamentPhase.SEMIFINALS:
        winners = tournament.semifinal_winners
        for i, (a, b) in enumerate(tournament.semifinal_matches):
            w = winners[i] if i < len(winners) else None
            _emit("semifinal", i, a, b, w is None)
    elif phase in (TournamentPhase.FINAL, TournamentPhase.COMPLETE):
        teams = tournament.final_teams
        if len(teams) >= 2:
            active = (
                phase == TournamentPhase.FINAL
                and tournament.winner_team_index is None
            )
            _emit("final", 0, teams[0], teams[1], active)
    return out


def _team_label(tournament: "Tournament", team_idx: int) -> str:
    team = (
        tournament.teams[team_idx]
        if 0 <= team_idx < len(tournament.teams)
        else {}
    )
    captain = team.get("captain", f"П{team_idx + 1}")
    return str(tournament.team_names.get(team_idx, captain))


def _match_title(tournament: "Tournament", m: dict[str, Any]) -> str:
    a, b = m["teams"]
    return (
        f"{m['label']}\n"
        f"**{_team_label(tournament, a)}** vs **{_team_label(tournament, b)}**"
    )


def _user_matches(
    tournament: "Tournament", user_id: int, *, active_only: bool = True
) -> list[dict[str, Any]]:
    """Матчи, в которых участвует пользователь (по user_id)."""
    try:
        team_idx = tournament.get_player_team_index(user_id)
    except Exception:
        team_idx = None
    if team_idx is None:
        return []
    matches = iter_tournament_matches(tournament)
    if active_only:
        matches = [m for m in matches if m["active"]]
    return [m for m in matches if team_idx in m["teams"]]


def _is_org(user, guild, tournament) -> bool:
    try:
        return bool(guild) and can_manage_tournament(user, guild, tournament)
    except Exception:
        return False


def _room_status_text(guild_id: int, channel_id: int, key: str) -> str:
    """Публичный статус комнаты для строки матча в карточке."""
    try:
        if room_store.has_room(guild_id, channel_id, key):
            return "Комната готова"
    except Exception:
        pass
    return "Ожидаются данные"


# ---------------------------------------------------------------------------
# Ephemeral-карточки
# ---------------------------------------------------------------------------

def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return discord.utils.format_dt(dt, style="R")
    except Exception:
        return "—"


def build_room_card(
    tournament: "Tournament",
    m: dict[str, Any],
    guild_id: int,
    channel_id: int,
    *,
    can_see_password: bool,
) -> discord.ui.LayoutView:
    """Эфемерная карточка «Данные комнаты» для одного матча."""
    rooms = room_store.get_rooms(guild_id, channel_id, m["key"])
    view = discord.ui.LayoutView(timeout=120)
    container = discord.ui.Container()
    container.add_item(
        discord.ui.TextDisplay(
            content=f"> ### {m['label']}\n{_match_title(tournament, m)}"
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(content="**ДАННЫЕ КОМНАТЫ**")
    )
    if not rooms:
        container.add_item(
            discord.ui.TextDisplay(content=muted("Ожидаются данные."))
        )
    else:
        for room in rooms:
            room_no = int(room.get("room_no", 1))
            rid = str(room.get("room_id") or "—")
            pwd = str(room.get("password") or "—")
            lines = [
                f"**Комната #{room_no}**",
                f"**ID:** `{rid}`",
            ]
            if can_see_password:
                lines.append(f"**Пароль:** `{pwd}`")
            else:
                lines.append(muted("Пароль виден участникам матча."))
            lines.append(
                f"**Отправил:** <@{room.get('submitted_by', 0)}> · "
                f"{_fmt_ts(room.get('submitted_at'))}"
            )
            if room.get("updated_at") and room.get("updated_by"):
                lines.append(
                    f"-# изменено <@{room['updated_by']}> · "
                    f"{_fmt_ts(room['updated_at'])}"
                )
            container.add_item(
                discord.ui.TextDisplay(content="\n".join(lines))
            )
    container.add_item(discord.ui.Separator())
    status = "Комната готова" if rooms else "Ожидаются данные"
    container.add_item(
        discord.ui.TextDisplay(
            content=f"**Статус:** {status}\n"
            + muted("Изменить данные может любой участник матча.")
        )
    )
    view.add_item(container)
    return view


class _MatchSelect(discord.ui.View):
    """Ephemeral-выбор матча (комната/просмотр)."""

    def __init__(self, matches: list[dict[str, Any]], tournament,
                 mode: str, guild_id: int, channel_id: int):
        super().__init__(timeout=180)
        self.matches = matches
        self.tournament = tournament
        self.mode = mode  # "send" | "data" | "manage"
        self.guild_id = guild_id
        self.channel_id = channel_id
        options = [
            discord.SelectOption(
                label=m["label"][:100],
                value=str(i),
                description=(
                    f"{_team_label(tournament, m['teams'][0])[:40]} vs "
                    f"{_team_label(tournament, m['teams'][1])[:40]}"
                )[:100],
            )
            for i, m in enumerate(matches[:25])
        ]
        if not options:
            return
        select = discord.ui.Select(
            placeholder="Выберите матч",
            options=options,
            custom_id=f"troom:pick:{mode}",
        )

        async def _cb(interaction: discord.Interaction,
                      _s=select) -> None:
            await self._on_pick(interaction, _s)

        select.callback = _cb
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction,
                       select: discord.ui.Select) -> None:
        try:
            idx = int(select.values[0])
        except (ValueError, IndexError):
            await interaction.response.send_message(
                "Некорректный выбор.", ephemeral=True)
            return
        if idx >= len(self.matches):
            await interaction.response.send_message(
                "Матч не найден.", ephemeral=True)
            return
        m = self.matches[idx]
        if self.mode == "send" or self.mode == "manage":
            await interaction.response.send_modal(
                RoomModal(self.guild_id, self.channel_id,
                          self.tournament, m)
            )
        else:  # data
            is_org = _is_org(interaction.user, interaction.guild,
                             self.tournament)
            team_idx = self.tournament.get_player_team_index(
                interaction.user.id)
            can_see = is_org or team_idx in m["teams"]
            card = build_room_card(
                self.tournament, m, self.guild_id, self.channel_id,
                can_see_password=can_see,
            )
            await interaction.response.edit_message(view=card)


# ---------------------------------------------------------------------------
# Modal отправки/изменения комнаты
# ---------------------------------------------------------------------------

class RoomModal(discord.ui.Modal):
    """ID + пароль комнаты. Поддерживает несколько комнат (room_no)."""

    def __init__(self, guild_id: int, channel_id: int,
                 tournament: "Tournament", m: dict[str, Any]):
        super().__init__(title=f"Комната — {m['label']}", timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.match = m
        self.room_input = discord.ui.TextInput(
            label="ID комнаты",
            placeholder="Например: 123456789",
            min_length=1,
            max_length=32,
        )
        self.pass_input = discord.ui.TextInput(
            label="Пароль комнаты",
            placeholder="Пароль",
            min_length=1,
            max_length=32,
        )
        self.no_input = discord.ui.TextInput(
            label="Номер комнаты (если несколько)",
            placeholder="1",
            required=False,
            max_length=2,
            default="1",
        )
        self.add_item(self.room_input)
        self.add_item(self.pass_input)
        self.add_item(self.no_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        room_id = (self.room_input.value or "").strip()
        password = (self.pass_input.value or "").strip()
        if not room_id or not password:
            await interaction.response.send_message(
                "Укажите ID и пароль комнаты.", ephemeral=True)
            return
        try:
            room_no = int((self.no_input.value or "1").strip() or "1")
        except ValueError:
            room_no = 1
        room_no = max(1, min(room_no, 99))

        # Серверная проверка прав: участник матча или организатор.
        tournament = store.get(self.guild_id, self.channel_id)
        if tournament is None or str(tournament.id) != str(self.tournament.id):
            await interaction.response.send_message(
                "Турнир не найден.", ephemeral=True)
            return
        # Актуальный список матчей — состояние могло измениться.
        fresh = next(
            (x for x in iter_tournament_matches(tournament)
             if x["key"] == self.match["key"]),
            None,
        )
        if fresh is None:
            await interaction.response.send_message(
                "Матч не найден в текущей фазе турнира.",
                ephemeral=True,
            )
            return
        org = _is_org(interaction.user, interaction.guild, tournament)
        if not fresh["active"] and not org:
            await interaction.response.send_message(
                "Матч уже завершён — данные комнаты изменить нельзя.",
                ephemeral=True,
            )
            return
        team_idx = tournament.get_player_team_index(interaction.user.id)
        if not org and team_idx not in fresh["teams"]:
            await interaction.response.send_message(
                "Вы не участвуете в этом матче.", ephemeral=True)
            return

        existing = room_store.get_room(
            self.guild_id, self.channel_id, fresh["key"], room_no)
        if existing:
            # Изменение существующей комнаты — с подтверждением.
            token = (self.guild_id, self.channel_id, interaction.user.id)
            _PENDING_EDITS[token] = {
                "match": fresh,
                "tournament_id": str(tournament.id),
                "room_no": room_no,
                "room_id": room_id,
                "password": password,
                "ts": time.time(),
            }
            confirm = discord.ui.View(timeout=120)
            ok = discord.ui.Button(
                label="Подтвердить изменение",
                style=discord.ButtonStyle.danger,
            )
            cancel = discord.ui.Button(
                label="Отмена",
                style=discord.ButtonStyle.secondary,
            )

            async def _ok(ic: discord.Interaction, _t=token) -> None:
                await _apply_room_edit(ic, _t)

            async def _cancel(ic: discord.Interaction) -> None:
                _PENDING_EDITS.pop(
                    (self.guild_id, self.channel_id, ic.user.id), None)
                await ic.response.edit_message(
                    view=text_card("-# Изменение отменено."))

            ok.callback = _ok
            cancel.callback = _cancel
            confirm.add_item(ok)
            confirm.add_item(cancel)
            await interaction.response.send_message(
                view=compose(
                    embed=None,
                    view=confirm,
                    text=(
                        f"**Комната #{room_no}** для **{fresh['label']}** "
                        "уже существует.\n"
                        "Заменить ID и пароль?"
                    ),
                ),
                ephemeral=True,
            )
            return

        await _save_room(
            interaction, tournament, fresh, room_id, password, room_no)


async def _apply_room_edit(
    interaction: discord.Interaction,
    token: tuple[int, int, int],
) -> None:
    pending = _PENDING_EDITS.pop(token, None)
    if pending is None or time.time() - pending["ts"] > _PENDING_TTL:
        await interaction.response.send_message(
            "Сессия подтверждения истекла — отправьте данные снова.",
            ephemeral=True,
        )
        return
    tournament = store.get(token[0], token[1])
    if tournament is None or str(tournament.id) != pending["tournament_id"]:
        await interaction.response.edit_message(
            view=text_card("Турнир больше не активен."))
        return
    m = pending["match"]
    org = _is_org(interaction.user, interaction.guild, tournament)
    team_idx = tournament.get_player_team_index(interaction.user.id)
    fresh = next(
        (x for x in iter_tournament_matches(tournament) if x["key"] == m["key"]),
        None,
    )
    if fresh is None or (not fresh["active"] and not org) or (
        not org and team_idx not in fresh["teams"]
    ):
        await interaction.response.edit_message(
            view=text_card("Нет прав на изменение этой комнаты."))
        return
    await _save_room(
        interaction, tournament, fresh, pending["room_id"],
        pending["password"], pending["room_no"], edit_reply=True)


async def _save_room(
    interaction: discord.Interaction,
    tournament: "Tournament",
    m: dict[str, Any],
    room_id: str,
    password: str,
    room_no: int,
    *,
    edit_reply: bool = False,
) -> None:
    gid, cid = interaction.guild_id, interaction.channel_id
    result = room_store.upsert_room(
        gid, cid, m["key"], room_id, password,
        interaction.user.id, room_no,
    )
    room = result["room"]
    logger.info(
        "Room %s match=%s room_no=%s by=%s (created=%s)",
        "saved" if result["created"] else "updated",
        m["key"], room_no, interaction.user.id, result["created"],
    )

    text = (
        f"Данные комнаты сохранены для **{m['label']}** "
        f"(комната #{room_no}).\n"
        "В канале турнира отображается только статус."
    )
    if edit_reply:
        await interaction.response.edit_message(view=text_card(text))
    else:
        await interaction.response.send_message(
            view=text_card(text), ephemeral=True)

    # Обновить основное сообщение турнира (статус «Комната готова»).
    try:
        bot = interaction.client
        if hasattr(bot, "update_tournament_message"):
            await bot.update_tournament_message(
                interaction.guild, tournament)
    except Exception as exc:
        logger.warning("room: tournament message refresh failed: %s", exc)


# ---------------------------------------------------------------------------
# Кнопки основной карточки турнира
# ---------------------------------------------------------------------------

def _load_tournament(gid: int, cid: int):
    try:
        return store.get(gid, cid)
    except Exception:
        return None


class RoomSendButton(discord.ui.Button):
    """«Отправить комнату» — участники активных матчей."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            label="Отправить комнату",
            style=discord.ButtonStyle.primary,
            custom_id=f"troom:send:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = _load_tournament(self.guild_id, self.channel_id)
        if tournament is None:
            await interaction.response.send_message(
                "Турнир не найден.", ephemeral=True)
            return
        matches = _user_matches(tournament, interaction.user.id)
        if not matches:
            await interaction.response.send_message(
                "Вы не участвуете в активных матчах турнира.",
                ephemeral=True,
            )
            return
        if len(matches) == 1:
            await interaction.response.send_modal(
                RoomModal(self.guild_id, self.channel_id,
                          tournament, matches[0])
            )
            return
        view = _MatchSelect(
            matches, tournament, "send",
            self.guild_id, self.channel_id)
        await interaction.response.send_message(
            view=compose(text="Выберите матч:", view=view),
            ephemeral=True,
        )


class RoomDataButton(discord.ui.Button):
    """«Данные комнаты» — просмотр своих матчей (пароль — участникам)."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            label="Данные комнаты",
            style=discord.ButtonStyle.secondary,
            custom_id=f"troom:data:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = _load_tournament(self.guild_id, self.channel_id)
        if tournament is None:
            await interaction.response.send_message(
                "Турнир не найден.", ephemeral=True)
            return
        matches = _user_matches(
            tournament, interaction.user.id, active_only=False)
        is_org = _is_org(interaction.user, interaction.guild, tournament)
        if not matches and not is_org:
            await interaction.response.send_message(
                "Вы не участвуете в матчах этого турнира.",
                ephemeral=True,
            )
            return
        if is_org and not matches:
            # Организатор без команды — просмотр всех матчей фазы.
            matches = iter_tournament_matches(tournament)
        if len(matches) == 1:
            m = matches[0]
            team_idx = tournament.get_player_team_index(
                interaction.user.id)
            can_see = is_org or team_idx in m["teams"]
            await interaction.response.send_message(
                view=build_room_card(
                    tournament, m, self.guild_id, self.channel_id,
                    can_see_password=can_see),
                ephemeral=True,
            )
            return
        view = _MatchSelect(
            matches, tournament, "data",
            self.guild_id, self.channel_id)
        await interaction.response.send_message(
            view=compose(text="Выберите матч:", view=view),
            ephemeral=True,
        )


class RoomManageButton(discord.ui.Button):
    """«Управление» — только организаторы: все матчи фазы."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            label="Управление",
            style=discord.ButtonStyle.secondary,
            custom_id=f"troom:manage:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = _load_tournament(self.guild_id, self.channel_id)
        if tournament is None:
            await interaction.response.send_message(
                "Турнир не найден.", ephemeral=True)
            return
        if not _is_org(interaction.user, interaction.guild, tournament):
            await interaction.response.send_message(
                "Только организаторы могут управлять комнатами.",
                ephemeral=True,
            )
            return
        matches = iter_tournament_matches(tournament)
        if not matches:
            await interaction.response.send_message(
                "В текущей фазе нет матчей.", ephemeral=True)
            return
        if len(matches) == 1:
            await interaction.response.send_modal(
                RoomModal(self.guild_id, self.channel_id,
                          tournament, matches[0])
            )
            return
        view = _MatchSelect(
            matches, tournament, "manage",
            self.guild_id, self.channel_id)
        await interaction.response.send_message(
            view=compose(text="Выберите матч:", view=view),
            ephemeral=True,
        )


def room_buttons(guild_id: int, channel_id: int) -> list[discord.ui.Button]:
    """Ряд кнопок комнат для фазовых View турнира."""
    return [
        RoomSendButton(guild_id, channel_id),
        RoomDataButton(guild_id, channel_id),
        RoomManageButton(guild_id, channel_id),
    ]
