"""Регрессии OCR-результата: победитель = левая панель скриншота.

Правила r!z3:
- Скриншот шлёт капитан ПОБЕДИВШЕЙ команды, победитель всегда слева.
- Team A/B в модели турнира НЕ совпадают с left/right на скриншоте.
- Kills/damage/KDA не определяют победителя.
- PNG: «СЧЁТ МАТЧА» только для реального счёта раундов,
  иначе «УБИЙСТВА КОМАНД».
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cogs.ocr_upload import _resolve_card_score
from views.ocr_match_view import (
    _ocr_winning_team_index,
    _uploaded_by_is_captain_of,
)
from utils.match_result_renderer import MatchResultData, render_match_result_sync


def _tournament(captains=("CapA", "CapB"), uids=None):
    """Минимальный tournament: teams + player_user_ids."""
    uids = uids or {"CapA": 111, "CapB": 222}
    teams = [{"captain": c} for c in captains]
    return SimpleNamespace(teams=teams, player_user_ids=uids)


def _session(panel_of):
    """panel_of: {team_index: 'left'|'right'}; winner_side всегда left."""
    return {
        "mode": "classic",
        "winner_side": "left",
        "maps": {
            str(t): {"panel": p, "rows": []}
            for t, p in panel_of.items()
        },
    }


# --- Победитель = команда на ЛЕВОЙ панели, независимо от индекса ---

def test_winner_left_panel_team_a():
    """Team A слева, капитан A грузил → побеждает A (index 0)."""
    session = _session({0: "left", 1: "right"})
    assert _ocr_winning_team_index(session) == 0


def test_winner_left_panel_team_b():
    """Team B слева → побеждает B (index 1), а НЕ «team_a»."""
    session = _session({0: "right", 1: "left"})
    assert _ocr_winning_team_index(session) == 1


def test_winner_ignores_team_order():
    """Даже если индексы «наоборот», left-маппинг решает."""
    session = _session({5: "right", 9: "left"})
    assert _ocr_winning_team_index(session) == 9


# --- Проверка капитана-победителя ---

def test_captain_of_winner_ok():
    t = _tournament()
    assert _uploaded_by_is_captain_of(t, 0, 111) is True
    assert _uploaded_by_is_captain_of(t, 1, 222) is True


def test_captain_of_winner_rejects_other_team():
    """Капитан проигравшей команды НЕ может автоподтвердить."""
    t = _tournament()
    assert _uploaded_by_is_captain_of(t, 0, 222) is False


def test_captain_of_winner_rejects_unknown():
    t = _tournament()
    assert _uploaded_by_is_captain_of(t, 0, None) is False
    assert _uploaded_by_is_captain_of(t, 0, 999999) is False
    assert _uploaded_by_is_captain_of(t, 0, "abc") is False


def test_captain_of_winner_bad_index():
    t = _tournament()
    assert _uploaded_by_is_captain_of(t, 99, 111) is False
    assert _uploaded_by_is_captain_of(t, -1, 111) is False


# --- Счёт раундов vs убийства команд ---

def test_round_score_kept():
    a, b, label = _resolve_card_score({"score": [5, 3]}, 23, 10)
    assert (a, b, label) == (5, 3, "СЧЁТ МАТЧА")


def test_no_round_score_shows_kills_not_fake_score():
    """Нет счёта раундов → суммы убийств, подпись «УБИЙСТВА КОМАНД»."""
    a, b, label = _resolve_card_score({}, 23, 10)
    assert (a, b, label) == (23, 10, "УБИЙСТВА КОМАНД")


def test_invalid_score_falls_back_to_kills():
    a, b, label = _resolve_card_score({"score": ["x", None]}, 7, 4)
    assert (a, b, label) == (7, 4, "УБИЙСТВА КОМАНД")


def test_kills_never_become_round_score():
    """Даже если kills «больше», без распознанного счёта — только убийства."""
    a, b, label = _resolve_card_score({"score": None}, 23, 10)
    assert label == "УБИЙСТВА КОМАНД"


# --- PNG: label прокидывается в рендер ---

def test_renderer_accepts_score_label():
    data = MatchResultData(
        match_num="#RZ-1", score_a=23, score_b=10,
        score_label="УБИЙСТВА КОМАНД", winner="B",
    )
    png = render_match_result_sync(data, {})
    assert png[:4] == b"\x89PNG"


def test_renderer_default_label_round_score():
    data = MatchResultData(match_num="#RZ-1", score_a=5, score_b=3)
    assert data.score_label == "СЧЁТ МАТЧА"
