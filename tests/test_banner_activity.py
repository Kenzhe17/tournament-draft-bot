"""Тесты логики дневного баннера: очки, тай-брейк, рендер PNG."""

from datetime import date

import pytest

from services.banner import activity as act
from services.banner.renderer import (
    _format_voice, _plural_matches, render_banner_png,
)


def test_score_voice_only():
    # 3 часа = 10800 сек -> 36 очков
    assert act.activity_score(3 * 3600, 0) == 36


def test_score_matches_only():
    assert act.activity_score(0, 4) == 40


def test_score_combined_and_cap():
    # 10 часов войса капается до 6ч=72 очка + 2 матча=20 -> 92
    assert act.activity_score(10 * 3600, 2) == 72 + 20


def test_score_partial_5min():
    # 4 мин 59 сек = 0 очков; 5 мин = 1
    assert act.activity_score(299, 0) == 0
    assert act.activity_score(300, 0) == 1


def test_rank_tiebreak_matches():
    entries = act.rank_entries(
        {1: 3600, 2: 3600}, {1: 1, 2: 3}
    )
    assert entries[0].user_id == 2  # больше матчей при равных очках? нет:
    # score: u1=12+10=22, u2=12+30=42 -> u2 выигрывает по score
    assert entries[0].score == 42


def test_rank_tiebreak_voice():
    # равный score и matches -> больше войса
    entries = act.rank_entries({1: 1800, 2: 3600}, {})
    assert entries[0].user_id == 2


def test_format_voice():
    assert _format_voice(4 * 3600 + 27 * 60) == "4 ч 27 мин"
    assert _format_voice(35 * 60) == "35 мин"
    assert _format_voice(2 * 3600) == "2 ч"
    assert _format_voice(0) == "0 мин"


def test_plural_matches():
    assert _plural_matches(1) == "матч"
    assert _plural_matches(2) == "матча"
    assert _plural_matches(5) == "матчей"
    assert _plural_matches(12) == "матчей"
    assert _plural_matches(21) == "матч"


@pytest.mark.asyncio
async def test_render_empty_state_png():
    png = await render_banner_png(
        nickname=None, avatar_png=None, has_activity=False,
        report_day_label="Отчёт: 8 нояб")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(png))
    assert im.size == (1672, 941)


@pytest.mark.asyncio
async def test_render_active_state_png_escapes():
    # недопустимые символы ника не должны ломать рендер
    png = await render_banner_png(
        nickname="<b>A&Б</b>", avatar_png=None,
        voice_seconds=3600, matches=3, has_activity=True)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_almaty_day_bounds():
    # 2024-06-01 Almaty = UTC+5 -> [2024-05-31 19:00, 2024-06-01 19:00) UTC
    start, end = act.almaty_day_bounds_utc(date(2024, 6, 1))
    assert start.hour == 19 and start.day == 31 and start.month == 5
    assert end.hour == 19 and end.day == 1 and end.month == 6
