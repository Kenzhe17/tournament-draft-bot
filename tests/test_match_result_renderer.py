"""Тесты рендера карточки «РЕЗУЛЬТАТ МАТЧА»."""

import pytest
from PIL import Image
import io

from utils.match_result_renderer import (
    MatchResultData,
    ResultPlayer,
    TeamTotals,
    match_num_from_id,
    render_match_result_sync,
)


def _player(name, team, i, **kw):
    base = dict(
        user_id=1000 + i,
        name=name,
        team=team,
        sr=5.0 + i * 0.5,
        elo=2000 - i * 50,
        kills=8 - i,
        deaths=5 + i,
        assists=3,
        damage=3000 - i * 200,
        elo_change=25 - i * 3,
    )
    base.update(kw)
    return ResultPlayer(**base)


def _data(winner="A", **kw):
    a = [_player(f"PlayerA{i}", "A", i) for i in range(4)]
    b = [_player(f"PlayerB{i}", "B", i + 4) for i in range(4)]
    totals_a = TeamTotals(kills=sum(p.kills for p in a), damage=sum(p.damage for p in a), elo_change=sum(p.elo_change for p in a))
    totals_b = TeamTotals(kills=sum(p.kills for p in b), damage=sum(p.damage for p in b), elo_change=sum(p.elo_change for p in b))
    a[0].is_mvp = True
    d = dict(
        match_num="#RZ-00001",
        duration="12:34",
        score_a=totals_a.kills,
        score_b=totals_b.kills,
        winner=winner,
        team_a=a,
        team_b=b,
        totals_a=totals_a,
        totals_b=totals_b,
        mvp_name=a[0].name,
        mvp_kda=f"{a[0].kills} / {a[0].deaths} / {a[0].assists}",
        mvp_damage=f"{a[0].damage:,}".replace(",", " "),
    )
    d.update(kw)
    return MatchResultData(**d)


def test_render_size_and_format():
    png = render_match_result_sync(_data(), {})
    img = Image.open(io.BytesIO(png))
    assert img.size == (1920, 1080)
    assert img.format == "PNG"
    assert len(png) > 50_000


def test_both_winners_render():
    for winner in ("A", "B"):
        png = render_match_result_sync(_data(winner=winner), {})
        assert Image.open(io.BytesIO(png)).size == (1920, 1080)


def test_missing_stats_and_avatars():
    """Нет аватарок, нулевая статистика — не должно падать."""
    data = _data()
    for p in data.team_a + data.team_b:
        p.avatar_url = ""
        p.kills = p.deaths = p.assists = p.damage = p.elo_change = 0
        p.sr = 0.0
        p.elo = 0
    data.mvp_name = ""
    png = render_match_result_sync(data, {})
    assert Image.open(io.BytesIO(png)).size == (1920, 1080)


def test_long_nickname_and_cyrillic():
    data = _data()
    data.team_a[0].name = "ОченьДлинныйНикнеймИгрокаКоторыйНеВлезает"
    data.team_b[2].name = "ҚазақАты"
    data.mvp_name = "ОченьДлинныйНикнеймИгрокаКоторыйНеВлезает"
    png = render_match_result_sync(data, {})
    assert Image.open(io.BytesIO(png)).size == (1920, 1080)


def test_emoji_in_name_filtered():
    data = _data()
    data.team_a[0].name = "Player\U0001F600Ace\u200dX"
    png = render_match_result_sync(data, {})
    assert Image.open(io.BytesIO(png)).size == (1920, 1080)


def test_avatar_bytes_rendered():
    """Валидные байты аватарки вставляются без ошибок."""
    from PIL import Image as PILImage
    buf = io.BytesIO()
    PILImage.new("RGB", (64, 64), (200, 30, 30)).save(buf, format="PNG")
    avatars = {p.user_id: buf.getvalue() for p in _data().team_a}
    png = render_match_result_sync(_data(), avatars)
    assert Image.open(io.BytesIO(png)).size == (1920, 1080)


def test_broken_avatar_falls_back():
    data = _data()
    avatars = {p.user_id: b"not an image" for p in data.team_a + data.team_b}
    png = render_match_result_sync(data, avatars)
    assert Image.open(io.BytesIO(png)).size == (1920, 1080)


def test_match_num_stable():
    assert match_num_from_id("t_1_qualifier_0") == match_num_from_id("t_1_qualifier_0")
    assert match_num_from_id("t_1_qualifier_0") != match_num_from_id("t_1_qualifier_1")
    assert match_num_from_id("x").startswith("#RZ-")


def test_empty_teams():
    data = MatchResultData(match_num="#RZ-1", winner="A")
    png = render_match_result_sync(data, {})
    assert Image.open(io.BytesIO(png)).size == (1920, 1080)
