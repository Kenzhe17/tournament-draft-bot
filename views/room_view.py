"""Данные комнат турнира (ID + пароль Free Fire) — Components V2.

Одна кнопка «Данные комнаты» в карточке матча открывает Modal
(ID + пароль — без номера комнаты). Отправлять могут только
участники конкретного матча (по user_id).

Публикация — services/room_publisher.py:
- CV2-карточка «КОМНАТА ОТКРЫТА» в канале данных комнат
  (названия команд, 8 участников через mentions, правила,
  пароль, кнопка «ID»);
- отдельное уведомление в общий чат (без ID/пароля);
- при изменении данных карточка редактируется, повторного
  уведомления нет; при ошибке — честный статус + безопасный
  retry после рестарта.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import discord

from models.tournament import TournamentPhase
from storage.json_store import store
from storage.room_store import match_key, room_store
from utils.design import compose, text_card
from utils.permissions import can_manage_tournament

if TYPE_CHECKING:
    from models.tournament import Tournament

logger = logging.getLogger(__name__)

# Временное хранилище «ожидающих подтверждения» правок комнаты:
# {(gid, cid, uid): {match, tournament_id, room_id, password, ts}}
# Одна актуальная запись на match_key — номер комнаты убран.
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
# Ephemeral-выбор матча
# ---------------------------------------------------------------------------


class _MatchSelect(discord.ui.View):
    """Ephemeral-выбор матча для отправки данных комнаты."""

    def __init__(self, matches: list[dict[str, Any]], tournament,
                 guild_id: int, channel_id: int):
        super().__init__(timeout=180)
        self.matches = matches
        self.tournament = tournament
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
            custom_id="troom:pick",
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
        await interaction.response.send_modal(
            RoomModal(self.guild_id, self.channel_id,
                      self.tournament, self.matches[idx])
        )


# ---------------------------------------------------------------------------
# Modal отправки/изменения комнаты
# ---------------------------------------------------------------------------

class RoomModal(discord.ui.Modal):
    """ID + пароль комнаты. Одна актуальная запись на матч."""

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
        self.add_item(self.room_input)
        self.add_item(self.pass_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        room_id = (self.room_input.value or "").strip()
        password = (self.pass_input.value or "").strip()
        if not room_id or not password:
            await interaction.response.send_message(
                "Укажите ID и пароль комнаты.", ephemeral=True)
            return

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

        existing = room_store.get_active_room(
            self.guild_id, self.channel_id, fresh["key"])
        if existing and existing.get("room_id"):
            # Изменение существующей комнаты — с подтверждением.
            token = (self.guild_id, self.channel_id, interaction.user.id)
            _PENDING_EDITS[token] = {
                "match": fresh,
                "tournament_id": str(tournament.id),
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
                        f"Данные комнаты для **{fresh['label']}** "
                        "уже существуют.\n"
                        "Заменить ID и пароль?"
                    ),
                ),
                ephemeral=True,
            )
            return

        await _save_room(
            interaction, tournament, fresh, room_id, password)


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
        pending["password"], edit_reply=True)


async def _save_room(
    interaction: discord.Interaction,
    tournament: "Tournament",
    m: dict[str, Any],
    room_id: str,
    password: str,
    *,
    edit_reply: bool = False,
) -> None:
    gid, cid = interaction.guild_id, interaction.channel_id
    result = room_store.upsert_room(
        gid, cid, m["key"], room_id, password,
        interaction.user.id,
        tournament_id=str(tournament.id),
    )
    room = result["room"]
    logger.info(
        "Room %s match=%s by=%s (created=%s)",
        "saved" if result["created"] else "updated",
        m["key"], interaction.user.id, result["created"],
    )

    # Аудит: факт отправки/изменения — БЕЗ пароля и ID в логи.
    try:
        from services import tournament_audit_service as audit_svc
        await audit_svc.record_interaction(
            interaction,
            channel_id=cid,
            tournament_id=str(tournament.id),
            match_id=m["key"],
            action=(
                audit_svc.A.ROOM_SUBMIT if result["created"]
                else audit_svc.A.ROOM_UPDATE
            ),
            source="room",
            after={
                "match": m["label"],
                "teams": list(m.get("teams", ())),
            },
            dedup_key=None,
        )
    except Exception as exc:
        logger.warning("room audit failed: %s", exc)

    # Публикация карточки + уведомления (services/room_publisher).
    publish = {"card": False, "notified": False}
    try:
        from services.room_publisher import publish_room
        publish = await publish_room(
            interaction.client, tournament, m,
            room_store.get_active_room(gid, cid, m["key"]) or room,
        )
    except Exception as exc:
        logger.warning("room publish failed key=%s: %s", m["key"], exc)

    if publish["card"]:
        detail = (
            "Карточка комнаты опубликована в канале данных."
            if publish["notified"] or not result["created"]
            else "Карточка комнаты опубликована, уведомление отправлено."
        )
    else:
        detail = (
            "Данные сохранены, но публикация карточки не удалась — "
            "будет повторена автоматически."
        )
    text = (
        f"Данные комнаты сохранены для **{m['label']}**.\n{detail}"
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


class RoomDataButton(discord.ui.Button):
    """«Данные комнаты» — Modal отправки ID/пароля (участники)."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            label="Данные комнаты",
            style=discord.ButtonStyle.primary,
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
            matches, tournament,
            self.guild_id, self.channel_id)
        await interaction.response.send_message(
            view=compose(text="Выберите матч:", view=view),
            ephemeral=True,
        )


def room_buttons(guild_id: int, channel_id: int) -> list[discord.ui.Button]:
    """Одна кнопка «Данные комнаты» для фазовых View турнира."""
    return [RoomDataButton(guild_id, channel_id)]
