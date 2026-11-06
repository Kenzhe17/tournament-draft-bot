"""OcrApply — применение OCR-результатов к legacy драфт-турниру.

Матчит имена игроков из battles-recon (Free Fire ники) с именами
в tournament.teams (Discord-ники), показывает предпросмотр
организатору и по подтверждению пишет kills/deaths в
tournament.temp_match_stats — откуда их забирает прежний
ConfirmFinalWinnerButton.callback (Elo/история/монеты).
"""

from __future__ import annotations

import logging
import re

import discord

from arbiter.models import Match
from arbiter.services.ocr_service import normalize_results

logger = logging.getLogger(__name__)


def _norm(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9а-яА-Я]", "", (name or "").lower())


def _team_player_names(tournament, team_idx: int) -> list[str]:
    """Все имена игроков команды (captain + circles + players list)."""
    if team_idx >= len(tournament.teams):
        return []
    team = tournament.teams[team_idx]
    names: list[str] = []
    captain = team.get("captain")
    if captain:
        names.append(captain)
    for c in range(1, 5):
        p = team.get(f"circle{c}")
        if p and p not in names:
            names.append(p)
    for p in team.get("players", []) or []:
        if p and p not in names:
            names.append(p)
    return names


def build_roster_map(tournament, team_a: int, team_b: int) -> dict[str, str]:
    """norm(name) -> каноническое имя игрока для обеих команд."""
    roster: dict[str, str] = {}
    for idx in (team_a, team_b):
        for name in _team_player_names(tournament, idx):
            roster.setdefault(_norm(name), name)
    return roster


def match_ocr_players(tournament, team_a: int, team_b: int,
                      ocr_players: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    """Сопоставить OCR-игроков с roster.

    Возвращает (matched, unmatched):
      matched   = {canonical_name: {"kills","deaths","assists","damage","ocr_name"}}
      unmatched = [ocr player dicts без совпадения]
    """
    roster = build_roster_map(tournament, team_a, team_b)
    matched: dict[str, dict] = {}
    unmatched: list[dict] = []
    for p in ocr_players:
        key = _norm(p.get("name", ""))
        canonical = roster.get(key)
        if canonical and canonical not in matched:
            matched[canonical] = {
                "kills": p.get("kills", 0),
                "deaths": p.get("deaths", 0),
                "assists": p.get("assists", 0),
                "damage": p.get("damage", 0),
                "ocr_name": p.get("name", "?"),
            }
        else:
            unmatched.append(p)
    return matched, unmatched


async def write_stats_to_tournament(guild_id: int, tournament_id: str,
                                    match_type: str, match_index: int,
                                    stats: dict[str, dict]) -> bool:
    """Записать kills/deaths в temp_match_stats свежего турнира из store."""
    from storage.json_store import store
    tournament = None
    for t in store.get_by_guild(guild_id):
        if t and t.id == tournament_id:
            tournament = t
            break
    if tournament is None:
        logger.warning("ocr_apply: tournament %s not found", tournament_id)
        return False
    match_id = f"{tournament.id}_{match_type}_{match_index}"
    bucket = tournament.temp_match_stats.setdefault(match_id, {})
    for player, kd in stats.items():
        bucket[player] = {"kills": int(kd.get("kills", 0)),
                          "deaths": int(kd.get("deaths", 0))}
    store.set(tournament)
    logger.info("ocr_apply: wrote %d players to %s", len(stats), match_id)
    return True
