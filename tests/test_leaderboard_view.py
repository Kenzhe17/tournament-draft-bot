"""Регрессии лидерборда.

- Кнопки пагинации живут внутри _ComposedLayoutView (compose переносит
  items обычного View в LayoutView). Callback обязан работать с
  исходным UniversalLeaderboardView, а не с обёрткой.
- «Ваше место» считается по тем же фильтрам/сортировке, что и
  лидерборд (games > 0, user_id > 0), и отсутствует у игроков без игр.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord

import views.leaderboard_view as lbv
from models.player_stats import PlayerStats
from storage.player_stats_store import PlayerStatsStore
from utils.design.adapter import compose


def _make_view(user_id: int = 42, total_pages: int = 3) -> lbv.UniversalLeaderboardView:
    view = lbv.UniversalLeaderboardView(guild_id=1, user_id=user_id, leaderboard_type="elo")
    view._total_pages = total_pages
    view.add_item(lbv.TopCategorySelect(view))
    view.refresh_buttons()
    return view


def _find_button(layout, label: str):
    for child in layout.walk_children():
        if isinstance(child, discord.ui.Button) and child.label == label:
            return child
    raise AssertionError(f"button {label!r} not found")


def _interaction(user_id: int):
    response = SimpleNamespace(
        send_message=AsyncMock(),
        edit_message=AsyncMock(),
        defer=AsyncMock(),
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=response,
        edit_original_response=AsyncMock(),
    )


@pytest.fixture
def _fake_embed(monkeypatch):
    async def fake_build(*args, **kwargs):
        return discord.Embed(description="board")

    monkeypatch.setattr(lbv, "build_leaderboard_embed", fake_build)


@pytest.mark.asyncio
async def test_page_button_works_inside_composed_layout(_fake_embed):
    """Клик «Вперёд» на сообщении, отправленном через compose(), не падает."""
    view = _make_view(user_id=42)
    layout = compose(discord.Embed(description="board"), view)
    assert isinstance(layout, discord.ui.LayoutView)

    nxt = _find_button(layout, "Вперёд")
    assert nxt.disabled is False
    assert nxt.view is layout  # кнопка принадлежит обёртке, не исходному View

    interaction = _interaction(user_id=42)
    await nxt.callback(interaction)

    interaction.response.edit_message.assert_awaited_once()
    sent_view = interaction.response.edit_message.await_args.kwargs["view"]
    # Сообщение пересобрано из исходного View: страница сменилась,
    # кнопка «Назад» появилась.
    assert view.page == 2
    assert _find_button(sent_view, "Назад") is not None


@pytest.mark.asyncio
async def test_page_button_rejects_other_user(_fake_embed):
    view = _make_view(user_id=42)
    layout = compose(discord.Embed(description="board"), view)
    nxt = _find_button(layout, "Вперёд")

    interaction = _interaction(user_id=999)
    await nxt.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs.get("ephemeral") is True
    interaction.response.edit_message.assert_not_awaited()
    assert view.page == 1


@pytest.mark.asyncio
async def test_category_select_still_switches(_fake_embed, monkeypatch):
    async def fake_pages(guild_id, leaderboard_type, per_page):
        return 2

    monkeypatch.setattr(lbv, "_total_pages_for", fake_pages)
    view = _make_view(user_id=42)
    layout = compose(discord.Embed(description="board"), view)

    select = None
    for child in layout.walk_children():
        if isinstance(child, discord.ui.Select):
            select = child
            break
    assert select is not None

    interaction = _interaction(user_id=42)
    interaction.response.defer = AsyncMock()
    select._values = ["money"]
    await select.callback(interaction)

    assert view.leaderboard_type == "money"
    assert view.page == 1
    interaction.edit_original_response.assert_awaited_once()


# ---------- «Ваше место» ----------


def _store_with(players: list[PlayerStats]) -> PlayerStatsStore:
    store = PlayerStatsStore()
    store._use_db = False
    store._stats = {f"{p.guild_id}:{p.user_id}": p for p in players}
    return store


@pytest.mark.asyncio
async def test_rank_matches_leaderboard_order(monkeypatch):
    """Место игрока = позиции в отсортированном списке лидерборда."""
    players = [
        PlayerStats(guild_id=1, user_id=1, name="a", elo=1500, games=5),
        PlayerStats(guild_id=1, user_id=2, name="b", elo=1400, games=5),
        PlayerStats(guild_id=1, user_id=3, name="c", elo=1300, games=5),
        PlayerStats(guild_id=1, user_id=4, name="d", elo=1200, games=5),
    ]
    store = _store_with(players)
    # build_leaderboard_embed импортирует store внутри функции —
    # патчим атрибут модуля storage.player_stats_store.
    import storage.player_stats_store as pss
    monkeypatch.setattr(pss, "player_stats_store", store)
    import utils.embeds as embeds

    embed = await embeds.build_leaderboard_embed(1, page=1, leaderboard_type="elo", user_id=3)
    desc = embed.description
    assert "#3" in desc
    assert "1300 ELO" in desc


@pytest.mark.asyncio
async def test_rank_none_for_player_without_games(monkeypatch):
    """Игрок без сыгранных матчей не должен получать место."""
    players = [
        PlayerStats(guild_id=1, user_id=1, name="a", elo=1500, games=5),
        PlayerStats(guild_id=1, user_id=2, name="b", elo=1000, games=0),
    ]
    store = _store_with(players)
    import storage.player_stats_store as pss
    monkeypatch.setattr(pss, "player_stats_store", store)
    import utils.embeds as embeds

    embed = await embeds.build_leaderboard_embed(1, page=1, leaderboard_type="elo", user_id=2)
    assert "Ваше место" not in (embed.description or "")
    assert "не в рейтинге" in (embed.description or "")


@pytest.mark.asyncio
async def test_rank_tie_break_by_user_id(monkeypatch):
    """При равном ELO место детерминировано (меньший user_id выше)."""
    players = [
        PlayerStats(guild_id=1, user_id=5, name="e", elo=1200, games=3),
        PlayerStats(guild_id=1, user_id=9, name="i", elo=1200, games=3),
    ]
    store = _store_with(players)
    import storage.player_stats_store as pss
    monkeypatch.setattr(pss, "player_stats_store", store)
    import utils.embeds as embeds

    rank9 = await store.get_player_rank(1, 9)
    rank5 = await store.get_player_rank(1, 5)
    assert rank5 == 1
    assert rank9 == 2
