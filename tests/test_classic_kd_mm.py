"""Тесты r!z3 CLASSIC: строгий K/D, счёт до 5, CV2-карточка MM-матча.

Покрывает:
- utils.kd_stat.parse_kd — только ``K/D`` (два целых >= 0 через /).
- utils.kd_stat.parse_match_score — Казахстан, до 5 побед.
- views.mm_match_view.build_mm_match_view — CV2 без PNG/OCR.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import kd_stat
from models.tournament import Tournament


# ---------------------------------------------------------------------------
# parse_kd
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["7/4", "12/3", "0/5", "10/0", " 8 / 2 "])
def test_parse_kd_valid(text):
    kills, deaths = kd_stat.parse_kd(text)
    assert kills >= 0 and deaths >= 0


@pytest.mark.parametrize(
    "text",
    [
        "7/4/2",          # лишнее поле (старый K/D/A)
        "7/4/2/1500",     # старый K/D/A/DMG
        "-1/4",           # отрицательные
        "7/-4",
        "abc/def",        # текст
        "7 4",            # нет разделителя
        "7:4",            # неверный разделитель
        "7/",             "/4", "", "   ",
        "7,4",
        "7.5/4",
        "999/4",          # выше MAX_KD_VALUE
    ],
)
def test_parse_kd_invalid(text):
    assert kd_stat.parse_kd(text) is None


def test_parse_kd_values():
    assert kd_stat.parse_kd("12/3") == (12, 3)
    assert kd_stat.parse_kd("0/5") == (0, 5)


# ---------------------------------------------------------------------------
# parse_match_score — Казахстан, до 5 побед
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("5:0", (5, 0)),
        ("5:1", (5, 1)),
        ("5:2", (5, 2)),
        ("5:3", (5, 3)),
        ("5:4", (5, 4)),
        ("0:5", (5, 0)),   # зеркально
        ("4:5", (5, 4)),
        (" 5 : 3 ", (5, 3)),
    ],
)
def test_parse_match_score_valid(text, expected):
    assert kd_stat.parse_match_score(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "6:0", "5:5", "4:4", "3:2", "5", "5-3", "a:b", "",
        "-5:3", "5.0:3",
    ],
)
def test_parse_match_score_invalid(text):
    assert kd_stat.parse_match_score(text) is None


# ---------------------------------------------------------------------------
# MM match card — CV2, три состояния
# ---------------------------------------------------------------------------


def _mm_tournament(state: str = "active") -> Tournament:
    t = Tournament(guild_id=1, channel_id=500)
    t.id  # noqa: B018 — __post_init__
    t.teams = [
        {"players": ["P1", "P2", "P3", "P4"], "captain": "P1"},
        {"players": ["P5", "P6", "P7", "P8"], "captain": "P5"},
    ]
    t.player_user_ids = {
        "P1": 11, "P2": 12, "P3": 13, "P4": 14,
        "P5": 15, "P6": 16, "P7": 17, "P8": 18,
    }
    t.match_channels = {
        "500": {
            "state": state,
            "match_num": 7,
            "card_message_id": 999,
            "status_text": "ожидается",
        }
    }
    return t


def _walk(view):
    """Все элементы LayoutView рекурсивно."""
    out = []

    def rec(item):
        out.append(item)
        for ch in getattr(item, "children", []) or []:
            rec(ch)

    rec(view)
    return out


def _custom_ids(view) -> set[str]:
    return {
        getattr(i, "custom_id", "")
        for i in _walk(view)
        if getattr(i, "custom_id", "")
    }


def _texts(view) -> str:
    return "\n".join(
        getattr(i, "content", "")
        for i in _walk(view)
        if getattr(i, "content", "")
    )


def test_mm_match_view_active_state():
    from views.mm_match_view import build_mm_match_view

    view = build_mm_match_view(_mm_tournament("active"), match_num=7)
    ids = _custom_ids(view)
    assert "mmatch:kd:1:500:0" in ids or any(i.startswith("mmatch:kd:") for i in ids)
    # Нет кнопок подтверждения в активном состоянии
    assert not any(i.startswith("mmatch:win:") for i in ids)
    # Classic: без кнопки «Правила» и блока «ИНФОРМАЦИЯ»
    assert "mmatch:rules" not in ids
    text = _texts(view)
    assert "НАЧАЛСЯ" in text
    assert "ИНФОРМАЦИЯ" not in text
    assert "Kazakhstan" not in text


def test_mm_match_view_done_state():
    from views.mm_match_view import build_mm_match_view

    t = _mm_tournament("done")
    t.match_channels["500"].update(
        winner="a", score="5:3",
        elo_lines=["P1: +25"],
    )
    view = build_mm_match_view(t, match_num=7)
    ids = _custom_ids(view)
    assert not any(i.startswith("mmatch:kd:") for i in ids)
    text = _texts(view)
    assert "ЗАВЕРШЁН" in text
    assert "5:3" in text
    assert "подтверждено" in text


def test_mm_match_view_check_state():
    from views.mm_match_view import build_mm_match_view

    t = _mm_tournament("check")
    view = build_mm_match_view(t, match_num=7)
    ids = _custom_ids(view)
    # CHECK-флоу 2.0: Select победителя + редактирование + отмена
    # (без «Правила»); legacy-кнопки mmatch:win:* на новых карточках
    # не выводятся — старые сообщения обрабатываются шаблоном.
    assert any(i.startswith("mmatch:winsel:") for i in ids)
    assert not any(i.startswith("mmatch:win:") for i in ids)
    assert any(i.startswith("mmatch:edit:") for i in ids)
    assert any(i.startswith("mmatch:cancel:") for i in ids)
    assert "mmatch:rules" not in ids
    text = _texts(view)
    assert "ПРОВЕРКА РЕЗУЛЬТАТА" in text
