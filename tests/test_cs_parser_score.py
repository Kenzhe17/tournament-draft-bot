"""Тесты распознавания счёта матча «N VS M» / «N:M» в cs_parser."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.cs_parser import parse_match_score


def _box(text, cx, cy):
    return {"text": text, "cx": cx, "cy": cy, "x0": 0, "y0": 0,
            "x1": 0, "y1": 0}


def test_score_vs_separate_boxes():
    boxes = [
        _box("5", 400, 100), _box("VS", 500, 100), _box("2", 600, 100),
        _box("3/1/2", 200, 400), _box("4/0/1", 800, 400),
    ]
    assert parse_match_score(boxes) == (5, 2)


def test_score_glued_vs():
    boxes = [_box("5 VS 2", 500, 100), _box("3/1/2", 200, 400)]
    assert parse_match_score(boxes) == (5, 2)


def test_score_plain_colon_in_header():
    """«5:2» одним боксом ВЫШЕ строк K/D/A — принимается."""
    boxes = [
        _box("5:2", 500, 100),
        _box("3/1/2", 200, 400), _box("4/0/1", 800, 400),
    ]
    assert parse_match_score(boxes) == (5, 2)


def test_score_plain_colon_below_kda_rejected():
    """«12:30» ниже/на уровне K/D/A — таймер, НЕ счёт."""
    boxes = [
        _box("3/1/2", 200, 300), _box("4/0/1", 800, 300),
        _box("12:30", 500, 350),
    ]
    assert parse_match_score(boxes) is None


def test_score_dash_and_pipe():
    boxes = [
        _box("4-1", 500, 100),
        _box("3/1/2", 200, 400),
    ]
    assert parse_match_score(boxes) == (4, 1)


def test_no_score_returns_none():
    boxes = [_box("3/1/2", 200, 400), _box("4/0/1", 800, 400)]
    assert parse_match_score(boxes) is None
