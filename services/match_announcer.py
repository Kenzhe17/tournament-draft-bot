"""Публикация анонсов матчей текущей фазы турнира (r!z3 CLASSIC).

Обычные турниры: одно CV2-сообщение на матч в канале турнира
(``tournament.match_cards[match_key]`` хранит message_id). K/D и
подтверждение результата живут в основном сообщении турнира
(views/matches_view.py) — здесь только анонс и кнопка «Правила».

MM-матчи (/mm squad) анонсируются отдельно — views/mm_match_view.py
публикует карточку в выделенный канал пула; здесь они пропускаются.

Никаких PNG, OCR-карточек, голосований за формат и служебных
сообщений.
"""

from __future__ import annotations

import logging
import time

import discord

from models.tournament import Tournament, TournamentPhase
from storage.json_store import store
from storage.match_number_store import match_number_store
from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)

_MATCH_LABELS = {
    "qualifier": "Отборочный матч",
    "semifinal": "Полуфинал",
    "final": "Финал",
}

# Правила матча — Kazakhstan, до 5 победных раундов
MATCH_RULES_TEXT = (
    "**Формат:** 4×4, до **5 победных раундов**.\n"
    "**Победа:** команда, первой набравшая 5 раундов "
    "(допустимые счета: 5:0, 5:1, 5:2, 5:3, 5:4).\n\n"
    "**Статистика:** капитаны команд заполняют K/D каждого своего "
    "игрока в формате `K/D` (например `7/4`) через кнопку "
    "«Заполнить статистику» в сообщении турнира.\n\n"
    "**Результат:** после заполнения статистики организатор "
    "выбирает победителя и счёт. Автоматического подтверждения нет."
)


class MatchRulesButton(discord.ui.Button):
    """Кнопка «📜 Правила» — ephemeral CV2-карточка с правилами."""

    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Правила",
            custom_id="match_rules",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = DesignView(title="Правила матча", timeout=60)
        view.text(MATCH_RULES_TEXT)
        await interaction.response.send_message(
            view=view, ephemeral=True,
        )


class MatchRulesView(discord.ui.View):
    """Persistent view для кнопки «📜 Правила» в карточках матчей."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(MatchRulesButton())


def _phase_matches(tournament: Tournament) -> list[tuple[str, int, int, int]]:
    """Матчи текущей фазы: (match_type, match_index, team_a, team_b)."""
    phase = tournament.phase
    out: list[tuple[str, int, int, int]] = []
    if phase == TournamentPhase.QUALIFIERS:
        for i, (a, b) in enumerate(
            getattr(tournament, "qualifier_matches", None) or []
        ):
            out.append(("qualifier", i, a, b))
    elif phase == TournamentPhase.SEMIFINALS:
        for i, (a, b) in enumerate(
            getattr(tournament, "semifinal_matches", None) or []
        ):
            out.append(("semifinal", i, a, b))
    elif phase == TournamentPhase.FINAL:
        ft = getattr(tournament, "final_teams", None)
        if ft and len(ft) == 2:
            out.append(("final", 0, ft[0], ft[1]))
    return out


def _match_key(tournament: Tournament, match_type: str, match_index: int) -> str:
    """Ключ матча — тот же формат, что match_id в карточке результата."""
    return f"{tournament.id}_{match_type}_{match_index}"


def _match_label(match_type: str, match_index: int) -> str:
    return _MATCH_LABELS.get(match_type, match_type)


def _team_display(tournament: Tournament, team_index: int) -> str:
    """Название команды для анонса (без пингов)."""
    name = tournament.team_names.get(team_index)
    if name:
        return name
    if team_index < len(tournament.teams):
        data = tournament.teams[team_index] or {}
        return data.get("captain") or f"Команда {team_index + 1}"
    return f"Команда {team_index + 1}"


def _roster_names(tournament: Tournament, team_index: int) -> str:
    """Состав команды обычными никами — без <@id>-упоминаний."""
    if team_index >= len(tournament.teams):
        return "—"
    data = tournament.teams[team_index] or {}
    players = data.get("players") or []
    if not players:
        players = [
            data.get(f"circle{c}") for c in range(1, 5)
            if data.get(f"circle{c}")
        ]
    return "\n".join(f"• {p}" for p in players) or "—"


def _match_card_view(
    tournament: Tournament,
    number: int,
    label: str,
    name_a: str,
    name_b: str,
    t1: int,
    t2: int,
) -> DesignView:
    view = DesignView(
        title=f"МАТЧ #{number} — {label}",
        timeout=None,
    )
    view.text(f"**{name_a}** vs **{name_b}**")
    view.text(f"**{name_a}**\n{_roster_names(tournament, t1)}")
    view.text(f"**{name_b}**\n{_roster_names(tournament, t2)}")
    return view


async def refresh_announced_cards(bot: "discord.Client") -> None:
    """Startup-cleanup: удалить legacy-карточки анонсов матчей.

    В канале турнира остаётся одно публичное сообщение; ранее
    анонсированные карточки матчей удаляются адресно по
    сохранённым message_id. Сообщения участников не трогаем.
    """
    for tournament in store.all():
        if tournament.phase == TournamentPhase.COMPLETE:
            continue
        if not (tournament.match_cards or {}):
            continue
        if getattr(tournament, "mm_search_channel_id", 0):
            continue
        guild = bot.get_guild(tournament.guild_id)
        if guild is None:
            continue
        try:
            await delete_announced_cards(tournament, guild)
        except Exception as exc:
            logger.warning(
                "Announced cards cleanup failed (tournament %s): %s",
                tournament.id, exc)


async def delete_announced_cards(
    tournament: Tournament,
    guild: discord.Guild,
) -> int:
    """Удалить ранее анонсированные карточки матчей (адресно).

    В канале турнира должно оставаться только одно публичное
    сообщение — сама карточка турнира. Карточки анонсов матчей
    удаляются по сохранённым message_id; сообщения участников
    и матчей MM (другой канал/хранилище) не затрагиваются.
    """
    mcards = tournament.match_cards or {}
    if not mcards:
        return 0
    channel = guild.get_channel(tournament.channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(tournament.channel_id)
        except Exception:
            channel = None
    if not isinstance(channel, discord.TextChannel):
        return 0
    removed = 0
    for key, entry in list(mcards.items()):
        mid = entry.get("message_id")
        if not mid:
            continue
        try:
            msg = await channel.fetch_message(int(mid))
            await msg.delete()
            removed += 1
        except discord.NotFound:
            pass
        except Exception as exc:
            logger.warning(
                "Announced card delete failed (%s): %s", key, exc)
    tournament.match_cards = {}
    try:
        store.set(tournament)
    except Exception as exc:
        logger.warning("match_cards cleanup persist failed: %s", exc)
    if removed:
        logger.info(
            "Deleted %d announced match cards (tournament %s)",
            removed, tournament.id,
        )
    return removed


async def announce_phase_matches(
    tournament: Tournament,
    guild: discord.Guild,
    bot: "discord.Client | None" = None,
) -> None:
    """LEGACY: отдельные карточки анонсов больше не создаются.

    В канале турнира остаётся одно публичное сообщение; все
    действия — через его кнопки, ephemeral-карточки и Modal.
    Функция сохранена для совместимости вызовов: при каждом
    обновлении удаляет ранее анонсированные карточки матчей.
    """
    matches = _phase_matches(tournament)
    if not matches:
        return
    # MM-матчи публикуют собственную карточку в канале пула —
    # здесь их не дублируем и не трогаем.
    if getattr(tournament, "mm_search_channel_id", 0):
        return
    await delete_announced_cards(tournament, guild)
    return
