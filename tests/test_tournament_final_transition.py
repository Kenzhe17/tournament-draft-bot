"""Регрессия: после подтверждения обоих победителей полуфиналов
фаза турнира переходит в FINAL, final_teams заполняется из
semifinal_winners, pending-состояние сбрасывается, состояние
сериализуется/восстанавливается без потерь."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.tournament import Tournament, TournamentPhase


def _tournament() -> Tournament:
    t = Tournament(guild_id=1, channel_id=2)
    t.teams = [
        {"captain": f"Cap{i}", "players": [f"Cap{i}", f"P{i}a", f"P{i}b", f"P{i}c"]}
        for i in range(4)
    ]
    t.semifinal_matches = [(0, 3), (1, 2)]
    t.semifinal_winners = [None, None]
    t.semifinal_pending_winners = [None, None]
    t.phase = TournamentPhase.SEMIFINALS
    t.final_teams = []
    return t


def test_first_semifinal_winner_keeps_phase():
    t = _tournament()
    done = t.confirm_semifinal_winner(0, 0)
    assert done is False
    assert t.phase == TournamentPhase.SEMIFINALS
    assert t.semifinal_winners == [0, None]
    assert t.final_teams == []


def test_second_semifinal_winner_advances_to_final():
    t = _tournament()
    t.confirm_semifinal_winner(0, 0)
    done = t.confirm_semifinal_winner(1, 2)
    assert done is True
    assert t.phase == TournamentPhase.FINAL
    assert t.final_teams == [0, 2]
    assert t.semifinal_winners == [0, 2]
    assert t.semifinal_pending_winners == [None, None]
    assert t.final_pending_winner is None
    assert t.betting_phase == "final"
    # Проигравшие выбыли
    assert 3 in t.eliminated_teams
    assert 1 in t.eliminated_teams


def test_final_transition_survives_serialization():
    t = _tournament()
    t.confirm_semifinal_winner(0, 3)
    t.confirm_semifinal_winner(1, 1)
    assert t.phase == TournamentPhase.FINAL

    data = t.to_dict()
    t2 = Tournament.from_dict(data)
    assert t2.phase == TournamentPhase.FINAL
    assert list(t2.final_teams) == [3, 1]
    assert t2.semifinal_winners == [3, 1]


def test_duplicate_confirm_does_not_regress():
    """Повторное подтверждение того же победителя не откатывает фазу."""
    t = _tournament()
    t.confirm_semifinal_winner(0, 0)
    t.confirm_semifinal_winner(1, 2)
    assert t.phase == TournamentPhase.FINAL
    # finalize_ocr_match защищён проверкой semifinal_winners[i] is None,
    # но даже прямой повтор не должен ломать final_teams
    t.confirm_semifinal_winner(1, 2)
    assert t.phase == TournamentPhase.FINAL
    assert t.final_teams == [0, 2]
