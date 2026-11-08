"""Публикация embed'ов «Матч #N начался» в канал результатов.

Вызывается при входе турнира в фазу с матчами (QUALIFIERS /
SEMIFINALS / FINAL). Для каждого матча фазыembed отправляется
один раз — факт отправки хранится в match_number_store (announced).
Номер матча — глобальный сквозной из match_number_store.
"""

from __future__ import annotations

import logging

import discord

from models.tournament import Tournament, TournamentPhase
from storage.match_number_store import match_number_store

logger = logging.getLogger(__name__)


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


async def announce_phase_matches(tournament: Tournament, guild: discord.Guild) -> None:
    """Отправить embed запуска для всех ещё не анонсированных матчей фазы."""
    matches = _phase_matches(tournament)
    if not matches:
        return

    # Ленивые импорты — избегаем модульных циклов
    from cogs.ocr_upload import RESULT_CHANNEL_ID, _captain_name, _match_label, _result_channel, _roster_mentions
    from views.ocr_match_view import _team_display_name

    channel = await _result_channel(guild, None)
    if channel is None:
        logger.warning("Match announce: канал результатов недоступен")
        return

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
        try:
            await channel.send(embed)
            await match_number_store.mark_announced(key)
            logger.info(
                "Match #%d announced: %s %s vs %s (tournament %s)",
                number, label, name_a, name_b, tournament.id,
            )
        except Exception as exc:
            logger.error("Match announce failed for %s: %s", key, exc)
