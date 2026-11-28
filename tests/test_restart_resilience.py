"""Регрессия: устойчивость турниров к перезапуску бота (Railway).

Проверяем, что сериализация tournaments.json -> from_dict не теряет
состояние активных турниров и НИКОГДА не переводит их в COMPLETE
сам по себе, а финализация матча не может начислить ELO/награды
дважды после рестарта в аварийном окне «БД закоммичена — JSON
ещё не сохранён».
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.tournament import (
    Tournament,
    TournamentPhase,
    TournamentSize,
)


def _full_tournament(phase: TournamentPhase) -> Tournament:
    """Турнир с репрезентативным состоянием для каждой фазы."""
    t = Tournament(guild_id=1, channel_id=100, size=TournamentSize.EIGHT)
    t.phase = phase
    t.message_id = 555
    t.draft_message_id = 777
    # Регистрации: display name + Discord user_id
    t.captains = ["Cap1"]
    t.circle1 = ["Cap1"]
    t.circle2 = ["P2"]
    t.circle3 = ["P3"]
    t.circle4 = ["Nota"]
    t.player_user_ids = {
        "Cap1": 11, "P2": 22, "P3": 33, "Nota": 44,
    }
    t.captain_order = [0]
    t.picks = {"Cap1": {"circle2": "P2"}}
    t.available = {"circle3": ["P3"], "circle4": ["Nota"]}
    t.teams = [
        {"captain": "Cap1", "circle1": "Cap1", "circle2": "P2",
         "circle3": "P3", "circle4": "Nota"},
    ]
    t.team_names = {0: "Cap1"}
    # Состояние матчей / результатов
    t.qualifier_matches = [(0, 1), (2, 3)]
    t.qualifier_winners = [0, None]
    t.semifinal_matches = [(0, 1)]
    t.semifinal_winners = [None]
    t.semifinal_pending_winners = [0]
    t.final_teams = [0, 1]
    t.final_pending_winner = 0
    t.winner_team_index = None
    t.temp_match_stats = {
        f"{t.id}_qualifier_0": {"Cap1": {"kills": 5, "deaths": 2}},
    }
    t.processed_elo_matches = {f"{t.id}_qualifier_0"}
    # OCR-сопоставления и каналы матчей
    t.ocr_sessions = {
        "qualifier_0": {"players": ["Cap1"], "state": "pending"},
    }
    t.match_channels = {"999": {"match_type": "qualifier", "match_index": 0}}
    # Драфт-таймер
    t.draft_pick_start_time = "2025-01-01T12:00:00"
    t.draft_pick_duration = 60
    # Прочие персистентные поля
    t.betting_phase = "qualifiers"
    t.betting_phase_start_time = 1735731600.0
    t.betting_results = {"Cap1": 100}
    t.earnings_awarded = False
    t.rules_vote = {"format": "kazakhstan"}
    t.organizer_id = 999
    t.mm_search_channel_id = 888
    return t


ACTIVE_PHASES = [
    TournamentPhase.SETUP,
    TournamentPhase.DRAFT,
    TournamentPhase.TEAMS,
    TournamentPhase.QUALIFIERS,
    TournamentPhase.SEMIFINALS,
    TournamentPhase.FINAL,
]


@pytest.mark.parametrize("phase", ACTIVE_PHASES)
def test_restart_roundtrip_preserves_active_state(phase):
    """to_dict -> from_dict (модель «перезапуска») сохраняет
    регистрации, составы, матчи, результаты и OCR-сессии."""
    t = _full_tournament(phase)
    restored = Tournament.from_dict(t.to_dict())

    assert restored.phase == phase
    assert restored.phase != TournamentPhase.COMPLETE
    assert restored.message_id == t.message_id
    assert restored.draft_message_id == t.draft_message_id
    # Регистрации и идентичность по user_id
    assert restored.captains == t.captains
    assert restored.circle1 == t.circle1
    assert restored.circle2 == t.circle2
    assert restored.circle3 == t.circle3
    assert restored.circle4 == t.circle4
    assert restored.player_user_ids == t.player_user_ids
    assert restored.all_players == t.all_players
    # Драфт
    assert restored.captain_order == t.captain_order
    assert restored.picks == t.picks
    assert restored.available == t.available
    # Команды и результаты матчей
    assert restored.teams == t.teams
    assert restored.team_names == t.team_names
    assert restored.qualifier_matches == [tuple(m) for m in t.qualifier_matches]
    assert restored.qualifier_winners == t.qualifier_winners
    assert restored.semifinal_matches == [tuple(m) for m in t.semifinal_matches]
    assert restored.semifinal_pending_winners == t.semifinal_pending_winners
    assert restored.final_teams == t.final_teams
    assert restored.final_pending_winner == t.final_pending_winner
    assert restored.winner_team_index is None
    assert restored.temp_match_stats == t.temp_match_stats
    assert restored.processed_elo_matches == t.processed_elo_matches
    # OCR и каналы матчей
    assert restored.ocr_sessions == t.ocr_sessions
    assert restored.match_channels == t.match_channels
    # Таймеры и ставки
    assert restored.draft_pick_start_time == t.draft_pick_start_time
    assert restored.draft_pick_duration == t.draft_pick_duration
    assert restored.betting_phase == t.betting_phase
    assert restored.betting_phase_start_time == t.betting_phase_start_time
    assert restored.betting_results == t.betting_results
    # Связи с MM и организатором
    assert restored.organizer_id == t.organizer_id
    assert restored.mm_search_channel_id == t.mm_search_channel_id
    assert restored.rules_vote == t.rules_vote


@pytest.mark.parametrize("phase", ACTIVE_PHASES)
def test_restart_never_auto_completes(phase):
    """Десериализация не должна завершать/отменять турнир:
    даже турнир в FINAL с pending-результатом остаётся FINAL."""
    t = _full_tournament(phase)
    restored = Tournament.from_dict(t.to_dict())
    assert restored.phase == phase
    assert restored.winner_team_index is None
    assert restored.phase != TournamentPhase.COMPLETE


def test_completed_tournament_stays_completed():
    """COMPLETE-турнир тоже переживает round-trip без изменений."""
    t = _full_tournament(TournamentPhase.FINAL)
    t.phase = TournamentPhase.COMPLETE
    t.winner_team_index = 0
    t.earnings_awarded = True
    restored = Tournament.from_dict(t.to_dict())
    assert restored.phase == TournamentPhase.COMPLETE
    assert restored.winner_team_index == 0
    assert restored.earnings_awarded is True


def test_earnings_awarded_flag_roundtrip_and_default():
    """Флаг защиты от двойных наград сохраняется; старые сохранения
    без поля получают False (обратная совместимость)."""
    t = Tournament(guild_id=1, channel_id=100)
    assert Tournament.from_dict(t.to_dict()).earnings_awarded is False
    t.earnings_awarded = True
    assert Tournament.from_dict(t.to_dict()).earnings_awarded is True
    # Старый формат без поля
    data = t.to_dict()
    data.pop("earnings_awarded")
    assert Tournament.from_dict(data).earnings_awarded is False


def test_draft_timer_survives_restart():
    """Дедлайн пика драфта хранится в ISO — после «рестарта»
    remaining-time считается от сохранённого start_time."""
    t = _full_tournament(TournamentPhase.DRAFT)
    restored = Tournament.from_dict(t.to_dict())
    # Старый start_time -> remaining <= 0 -> таймер сделает
    # авто-пик (штатная механика драфта, не завершение турнира).
    assert restored.get_draft_pick_remaining_time() <= 0
    # Свежий start_time -> полный таймер.
    from datetime import datetime
    restored.draft_pick_start_time = datetime.now().isoformat()
    restored.draft_pick_duration = 60
    remaining = restored.get_draft_pick_remaining_time()
    assert 0 < remaining <= 60
    # deadline_unix тоже восстанавливается
    assert restored.draft_pick_deadline_unix() is not None


# ---------------------------------------------------------------------------
# Защита от двойного начисления после рестарта
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_match_result_skips_when_stats_done(monkeypatch):
    """Если match_finalizations уже 'stats_done' (статистика
    закоммичена до падения), ELO/монеты не применяются повторно."""
    from views.kd_input_view import process_match_result
    from storage import finalize_store

    async def _fake_status(guild_id, match_id):
        assert match_id == "t1_qualifier_0"
        return "stats_done"

    monkeypatch.setattr(finalize_store, "get_status", _fake_status)

    t = _full_tournament(TournamentPhase.QUALIFIERS)
    applied = await process_match_result(
        1, t,
        {
            "match_type": "qualifier",
            "match_index": 0,
            "winning_team_index": 0,
            "team1_index": 0,
            "team2_index": 1,
            "temp_kd_data": {},
            "finalize_match_id": "t1_qualifier_0",
        },
        None,
    )
    assert applied is False


@pytest.mark.asyncio
async def test_process_match_result_skips_when_done(monkeypatch):
    from views.kd_input_view import process_match_result
    from storage import finalize_store

    async def _fake_status(guild_id, match_id):
        return "done"

    monkeypatch.setattr(finalize_store, "get_status", _fake_status)
    t = _full_tournament(TournamentPhase.FINAL)
    applied = await process_match_result(
        1, t,
        {
            "match_type": "final",
            "match_index": 0,
            "winning_team_index": 0,
            "team1_index": 0,
            "team2_index": 1,
            "temp_kd_data": {},
            "finalize_match_id": "t1_final_0",
        },
        None,
    )
    assert applied is False


@pytest.mark.asyncio
async def test_process_match_result_db_error_propagates(monkeypatch):
    """Сбой чтения finalize_store не должен трактоваться как
    «нет записи» — иначе было бы повторное начисление ELO."""
    from views.kd_input_view import process_match_result
    from storage import finalize_store

    async def _boom(guild_id, match_id):
        raise ConnectionError("db down")

    monkeypatch.setattr(finalize_store, "get_status", _boom)
    t = _full_tournament(TournamentPhase.QUALIFIERS)
    with pytest.raises(ConnectionError):
        await process_match_result(
            1, t,
            {
                "match_type": "qualifier",
                "match_index": 0,
                "winning_team_index": 0,
                "team1_index": 0,
                "team2_index": 1,
                "temp_kd_data": {},
                "finalize_match_id": "t1_qualifier_0",
            },
            None,
        )
