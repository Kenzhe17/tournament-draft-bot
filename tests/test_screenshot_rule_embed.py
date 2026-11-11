"""Пометка про скриншот 16:9 показывается только в фазах
отбор/полуфинал/финал, а не в embed настройки (/tournament create)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import utils.embeds as embeds
from models.tournament import FormationMode, RegistrationState, Tournament, TournamentPhase

RULE = "Скриншот результатов загружает только капитан"


class _FakeStatsStore:
    async def get(self, guild_id, user_id):
        return None

    async def get_many(self, guild_id, user_ids):
        return {}


@pytest.fixture(autouse=True)
def _patch_stores(monkeypatch):
    monkeypatch.setattr(embeds, "player_stats_store", _FakeStatsStore())


def _guild():
    return SimpleNamespace(id=1)


def _tournament() -> Tournament:
    t = Tournament(guild_id=1, channel_id=2)
    t.organizer_id = 7
    t.formation_mode = FormationMode.RANDOM
    t.registration = RegistrationState.OPEN
    t.players_pool = ["A", "B", "C", "D"]
    t.player_user_ids = {"A": 11, "B": 12, "C": 13, "D": 14}
    t.teams = [
        {"captain": "A", "players": ["A", "B", "C", "D"]},
        {"captain": "B", "players": ["B", "A", "C", "D"]},
        {"captain": "C", "players": ["C", "A", "B", "D"]},
        {"captain": "D", "players": ["D", "A", "B", "C"]},
    ]
    t.is_test = True  # пропускаем секцию ставок (нужна БД)
    return t


@pytest.mark.asyncio
async def test_setup_embed_has_no_screenshot_rule():
    t = _tournament()
    embed = await embeds.build_setup_embed(t, _guild())
    assert RULE not in (embed.description or "")


@pytest.mark.asyncio
async def test_qualifiers_embed_has_screenshot_rule():
    t = _tournament()
    t.phase = TournamentPhase.QUALIFIERS
    t.qualifier_matches = [(0, 1), (2, 3)]
    embed = await embeds.build_qualifiers_embed(t, _guild())
    assert RULE in (embed.description or "")


@pytest.mark.asyncio
async def test_semifinals_embed_has_screenshot_rule():
    t = _tournament()
    t.phase = TournamentPhase.SEMIFINALS
    t.semifinal_matches = [(0, 1)]
    embed = await embeds.build_semifinals_embed(t, _guild())
    assert RULE in (embed.description or "")


@pytest.mark.asyncio
async def test_final_embed_has_screenshot_rule():
    t = _tournament()
    t.phase = TournamentPhase.FINAL
    t.final_teams = [0, 1]
    embed = await embeds.build_final_embed(t, _guild())
    assert RULE in (embed.description or "")
