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
    a, b, label = _resolve_card_score(
        {}, 23, 10, ordered_score=(5, 3)
    )
    assert (a, b, label) == (5, 3, "СЧЁТ МАТЧА")


def test_no_round_score_shows_kills_not_fake_score():
    """Нет счёта раундов → суммы убийств, подпись «УБИЙСТВА КОМАНД»."""
    a, b, label = _resolve_card_score({}, 23, 10)
    assert (a, b, label) == (23, 10, "УБИЙСТВА КОМАНД")


def test_kills_never_become_round_score():
    """Без привязанного к командам счёта — только убийства."""
    a, b, label = _resolve_card_score({"score": [5, 3]}, 23, 10)
    assert (a, b, label) == (23, 10, "УБИЙСТВА КОМАНД")


# --- Порядок счёта: «Команда A : Команда B», не «левый:правый» ---

def _scored_session(left_team, right_team):
    """Сессия: score=[5,2] — левая панель 5, правая 2."""
    return {
        "score": [5, 2],
        "maps": {
            str(left_team): {"panel": "left", "rows": []},
            str(right_team): {"panel": "right", "rows": []},
        },
    }


def test_score_team_a_left_wins():
    """A слева, A победил 5:2 → отображается 5:2."""
    from services.ocr_service import ordered_round_score
    s = _scored_session(0, 1)
    assert ordered_round_score(s, 0, 1) == (5, 2)


def test_score_team_b_left_wins():
    """B слева и победил 5:2 → для (A,B) отображается 2:5."""
    from services.ocr_service import ordered_round_score
    s = _scored_session(1, 0)  # team1=1 слева, team2=0 справа
    assert ordered_round_score(s, 0, 1) == (2, 5)


def test_score_b_left_team_order():
    """Проигравшая A имеет меньше очков: B(слева)=5, A(справа)=2 → A:B = 2:5."""
    from services.ocr_service import ordered_round_score
    s = _scored_session(1, 0)
    a, b = ordered_round_score(s, 0, 1)
    assert (a, b) == (2, 5)


def test_score_unmapped_returns_none():
    """Без panel-маппинга порядок не угадываем."""
    from services.ocr_service import ordered_round_score
    s = {"score": [5, 2], "maps": {}}
    assert ordered_round_score(s, 0, 1) is None


def test_score_missing_returns_none():
    from services.ocr_service import ordered_round_score
    assert ordered_round_score({"maps": {}}, 0, 1) is None
    assert ordered_round_score({"score": ["x", 2]}, 0, 1) is None


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


# --- Авто-выбор панели (без ручного выбора сторон) ---

def _mm_tournament():
    """Турнир матча qualifier[0] = команды 0 vs 1."""
    return SimpleNamespace(
        teams=[
            {"captain": "CapA", "players": ["CapA", "P1", "P2", "P3"]},
            {"captain": "CapB", "players": ["CapB", "Q1", "Q2", "Q3"]},
        ],
        player_user_ids={
            "CapA": 111, "P1": 113, "P2": 114, "P3": 115,
            "CapB": 222, "Q1": 223, "Q2": 224, "Q3": 225,
        },
        qualifier_matches=[(0, 1)],
        semifinal_matches=[],
        final_teams=(0, 1),
        team_names={},
    )


def _mm_session(uploaded_by=111, banner="win", mode="classic"):
    return {
        "mode": mode,
        "match_type": "qualifier",
        "match_index": 0,
        "teams": [0, 1],
        "uploaded_by": uploaded_by,
        "banner": banner,
        "maps": {},
        "panel_picks": {},
    }


def test_auto_panel_winner_uploads():
    """Капитан победившей команды грузил + баннер win → он слева."""
    from views.ocr_match_view import _auto_panel_for_team
    t = _mm_tournament()
    s = _mm_session(uploaded_by=111, banner="win")
    assert _auto_panel_for_team(t, s, 0) == "left"
    assert _auto_panel_for_team(t, s, 1) == "right"


def test_auto_panel_member_of_winner_team():
    """Любой участник команды загрузил + win → его команда слева."""
    from views.ocr_match_view import _auto_panel_for_team
    t = _mm_tournament()
    s = _mm_session(uploaded_by=113, banner="win")  # P1, не капитан
    assert _auto_panel_for_team(t, s, 0) == "left"
    assert _auto_panel_for_team(t, s, 1) == "right"


def test_auto_panel_lose_banner_flips():
    """Баннер lose → загрузившая команда на правой (проиграла)."""
    from views.ocr_match_view import _auto_panel_for_team
    t = _mm_tournament()
    s = _mm_session(uploaded_by=111, banner="lose")
    assert _auto_panel_for_team(t, s, 0) == "right"
    assert _auto_panel_for_team(t, s, 1) == "left"


def test_auto_panel_no_banner_assumes_winner():
    """Баннер не распознан — по правилу загрузки команда слева."""
    from views.ocr_match_view import _auto_panel_for_team
    t = _mm_tournament()
    s = _mm_session(uploaded_by=222, banner=None)
    assert _auto_panel_for_team(t, s, 1) == "left"
    assert _auto_panel_for_team(t, s, 0) == "right"


def test_auto_panel_unknown_uploader_none():
    """Админ/посторонний загрузил → ручной выбор стороны."""
    from views.ocr_match_view import _auto_panel_for_team
    t = _mm_tournament()
    s = _mm_session(uploaded_by=999999, banner="win")
    assert _auto_panel_for_team(t, s, 0) is None
    assert _auto_panel_for_team(t, s, 1) is None


def test_auto_panel_squad_mode_none():
    """Squad-режим — авто-сторон нет, там свои claim-кнопки."""
    from views.ocr_match_view import _auto_panel_for_team
    t = _mm_tournament()
    s = _mm_session(uploaded_by=111, banner="win", mode="squad")
    assert _auto_panel_for_team(t, s, 0) is None
