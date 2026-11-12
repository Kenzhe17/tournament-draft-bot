"""Тесты Components V2 статистики Free Fire (/ff stats) и кулдауна /ff info."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord

from services.freefire_service import FreeFireError
from utils import ff_stats_formatter as sfmt
from views.freefire_stats_view import FreeFireStatsView

OWNER_ID = 42

SAMPLE_BR = {
    "stats": {
        "solostats": {
            "kills": 100,
            "gamesplayed": 50,
            "wins": 5,
            "detailedstats": {
                "deaths": 80,
                "damage": 50000,
                "headshotkills": 20,
                "topntimes": 3,
                "survivaltime": 36000,
                "highestkills": 7,
                "distancetravelled": 120000,
            },
        },
        "duostats": {"kills": 10, "gamesplayed": 5, "wins": 1},
        "quadstats": {"kills": 5, "gamesplayed": 2, "wins": 0},
    }
}

SAMPLE_CS = {
    "stats": {
        "csstats": {
            "kills": 40,
            "gamesplayed": 20,
            "wins": 8,
            "detailedstats": {"deaths": 30, "damage": 9000, "assists": 12},
        }
    }
}


def _make_view(data=SAMPLE_BR, fetch=None, **kwargs):
    return FreeFireStatsView(
        uid="535606728",
        region="ru",
        owner_id=OWNER_ID,
        data=data,
        fetch=fetch if fetch is not None else AsyncMock(),
        timeout=None,
        **kwargs,
    )


def _interaction(user_id=OWNER_ID, custom_id="ffs:br_career"):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        data={"custom_id": custom_id},
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
            defer=AsyncMock(),
        ),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(
            send=AsyncMock(), edit_message=AsyncMock()
        ),
    )


def _press(view, custom_id, user_id=OWNER_ID):
    for child in view.walk_children():
        if (
            isinstance(child, discord.ui.Button)
            and child.custom_id == custom_id
        ):
            interaction = _interaction(user_id, custom_id)
            return child, interaction, child.callback(interaction)
    raise AssertionError(f"button {custom_id} not found")


def _container(view):
    for child in view.children:
        if isinstance(child, discord.ui.Container):
            return child
    raise AssertionError("no container")


def _all_text(view):
    return "\n".join(
        item.content
        for item in _container(view).walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def _component_count(view):
    total = 0

    def walk(item):
        nonlocal total
        total += 1
        for ch in getattr(item, "children", []) or []:
            walk(ch)

    for ch in view.children:
        walk(ch)
    return total


# ------------------------------------------------------- stats formatter


def test_stats_modes_cover_all_five():
    keys = [k for k, *_ in sfmt.STATS_MODES]
    assert keys == [
        "br_career", "br_normal", "br_ranked", "cs_career", "cs_ranked"
    ]
    for key, gm, mm, _ in sfmt.STATS_MODES:
        assert gm in ("br", "cs")
        assert mm in ("CAREER", "NORMAL", "RANKED")


def test_stats_markdown_br_and_cs():
    br = sfmt.stats_markdown(SAMPLE_BR["stats"])
    assert "Соло" in br and "Дуо" in br and "Сквад" in br
    assert "K/D" in br
    cs = sfmt.stats_markdown(SAMPLE_CS["stats"])
    assert "Clash Squad" in cs
    assert "Топ:" not in cs  # BR-only метрики не должны быть в CS
    assert sfmt.stats_markdown({}) == "Данные отсутствуют."


# ------------------------------------------------------------- stats view


@pytest.mark.asyncio
async def test_stats_view_renders_br_and_buttons():
    view = _make_view()
    text = _all_text(view)
    assert "СТАТИСТИКА" in text
    assert "Соло" in text
    assert "K/D" in text
    rows = [
        c
        for c in _container(view).children
        if isinstance(c, discord.ui.ActionRow)
    ]
    assert len(rows) == 2
    labels = [b.label for b in rows[0].children]
    assert labels == [
        "BR · Карьера", "BR · Обычные", "BR · Рейтинг",
        "CS · Карьера", "CS · Рейтинг",
    ]
    active = [
        b for b in rows[0].children
        if b.style == discord.ButtonStyle.primary
    ]
    assert len(active) == 1 and active[0].label == "BR · Карьера"


@pytest.mark.asyncio
async def test_stats_cached_mode_no_refetch():
    fetch = AsyncMock()
    view = _make_view(fetch=fetch)
    # br_career уже в кэше (initial data) — переключение бесплатно
    _, interaction, coro = _press(view, "ffs:br_career")
    await coro
    interaction.response.edit_message.assert_awaited_once()
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_stats_lazy_fetch_uncached_mode():
    fetch = AsyncMock(return_value=SAMPLE_CS)
    view = _make_view(fetch=fetch)
    _, interaction, coro = _press(view, "ffs:cs_ranked")
    await coro
    fetch.assert_awaited_once_with("cs_ranked")
    interaction.response.defer.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once()
    assert view.mode == "cs_ranked"
    assert "Clash Squad" in _all_text(view)
    # cs_career — новый запрос
    _, _, coro2 = _press(view, "ffs:cs_career")
    await coro2
    assert fetch.await_count == 2
    assert fetch.await_args_list[-1].args == ("cs_career",)
    # cs_ranked уже в кэше — повторный запрос не нужен
    _, _, coro3 = _press(view, "ffs:cs_ranked")
    await coro3
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_stats_fetch_error_cached_and_shown():
    fetch = AsyncMock(side_effect=FreeFireError("rate limit", "RATE_LIMIT"))
    view = _make_view(fetch=fetch)
    _, _, coro = _press(view, "ffs:cs_career")
    await coro
    assert "rate limit" in _all_text(view)
    # Повторный клик по упавшему режиму — без нового запроса
    _, _, coro2 = _press(view, "ffs:cs_career")
    await coro2
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_stats_refresh_updates_current_mode():
    fetch = AsyncMock(return_value=SAMPLE_CS)
    view = _make_view(fetch=fetch)
    _, interaction, coro = _press(view, "ffs:refresh")
    await coro
    fetch.assert_awaited_once_with("br_career")
    interaction.edit_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_stats_refresh_cooldown_and_error_keeps_data():
    fetch = AsyncMock(side_effect=FreeFireError("429", "RATE_LIMIT"))
    view = _make_view(fetch=fetch)
    old_entry = view._cache["br_career"]
    _, _, coro = _press(view, "ffs:refresh")
    await coro
    assert view._cache["br_career"] is old_entry  # старые данные целы
    assert "429" in view._notice
    # cooldown: второй refresh сразу — ephemeral followup после defer
    _, i2, coro2 = _press(view, "ffs:refresh")
    await coro2
    i2.response.defer.assert_awaited_once()
    i2.followup.send.assert_awaited_once()
    assert i2.followup.send.await_args.kwargs.get("ephemeral")
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_stats_non_owner_denied():
    view = _make_view()
    for cid in ("ffs:br_career", "ffs:cs_ranked", "ffs:refresh"):
        _, interaction, coro = _press(view, cid, user_id=999)
        await coro
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args.kwargs.get(
            "ephemeral"
        )
        interaction.response.send_message.reset_mock()


@pytest.mark.asyncio
async def test_stats_expired_and_timeout():
    view = _make_view(expired=True)
    for child in view.walk_children():
        if isinstance(child, discord.ui.Button):
            assert child.disabled is True
    message = SimpleNamespace(edit=AsyncMock())
    view2 = _make_view()
    view2.bound_message = message
    view2._cache["cs_career"] = {"ok": True, "data": SAMPLE_CS}
    await view2.on_timeout()
    message.edit.assert_awaited_once()
    sent = message.edit.await_args.kwargs["view"]
    assert sent._expired is True
    assert "cs_career" in sent._cache  # кэш переживает timeout


@pytest.mark.asyncio
async def test_stats_component_limits_all_modes():
    view = _make_view()
    for key, *_ in sfmt.STATS_MODES:
        view.mode = key
        view._cache[key] = {"ok": True, "data": SAMPLE_BR}
        view._render()
        assert len(_container(view).children) <= 10
        assert _component_count(view) <= 40
        for item in _container(view).walk_children():
            if isinstance(item, discord.ui.TextDisplay):
                assert len(item.content) <= 2000


# ------------------------------------------------------- FF cooldown store


def test_ff_cooldown_store(tmp_path, monkeypatch):
    import storage.ff_cooldowns as mod

    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    store = mod.FFCooldownStore("test_cd.json")
    assert store.seconds_left(1) == 0
    store.mark_used(1)
    left = store.seconds_left(1)
    assert 6 * 3600 - 5 < left <= 6 * 3600
    # Другой пользователь не затронут
    assert store.seconds_left(2) == 0
    # Персистентность: новый экземпляр читает тот же файл
    store2 = mod.FFCooldownStore("test_cd.json")
    assert store2.seconds_left(1) == left


def test_ff_cooldown_expired(tmp_path, monkeypatch):
    import storage.ff_cooldowns as mod

    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    store = mod.FFCooldownStore("test_cd.json")
    store._data["7"] = time.time() - mod.COOLDOWN_SECONDS - 1
    assert store.seconds_left(7) == 0


def test_ff_info_and_stats_cooldowns_are_independent(tmp_path, monkeypatch):
    import storage.ff_cooldowns as mod

    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    info = mod.FFCooldownStore("ff_info_cooldowns.json")
    stats = mod.FFCooldownStore("ff_stats_cooldowns.json")
    info.mark_used(1)
    assert info.seconds_left(1) > 0
    assert stats.seconds_left(1) == 0
