"""HOTFIX регрессии Components V2: пагинация и /robgroup.

- Пагинация: self.view внутри callback — это _ComposedLayoutView;
  состояние (current_page и т.п.) обязано жить на исходном View
  и переживать каждый edit_message.
- /robgroup: после join лобби обязано сохранять все кнопки
  (Вступить / Начать штурм / Отмена), лимит 6 и идемпотентный штурм.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord

import cogs.rob as rob
from models.match_history import MatchHistory
from utils.design.adapter import compose
from views.history_view import HistoryView


# ---------------- helpers ----------------


def _interaction(user_id: int):
    response = SimpleNamespace(
        send_message=AsyncMock(),
        edit_message=AsyncMock(),
        defer=AsyncMock(),
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=response,
        message=SimpleNamespace(edit=AsyncMock(), delete=AsyncMock()),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _find_button(layout, label: str):
    for child in layout.walk_children():
        if isinstance(child, discord.ui.Button) and child.label == label:
            return child
    raise AssertionError(f"button {label!r} not found")


def _has_button(layout, label: str) -> bool:
    try:
        _find_button(layout, label)
        return True
    except AssertionError:
        return False


# ---------------- HistoryView pagination ----------------


def _match(i: int) -> MatchHistory:
    return MatchHistory(
        guild_id=1,
        user_id=42,
        match_id=f"m{i}",
        tournament_id="t1",
        date=datetime(2024, 1, 1),
        match_type="qualifier",
        match_index=i,
        team_index=0,
        team_name="A",
        teammates=[],
        opponent_team_index=1,
        opponent_team_name="B",
        opponents=[],
        kills=5,
        deaths=2,
        result="win",
        elo_change=10,
    )


def _history_view() -> HistoryView:
    # 3 матча + страница статистики = 4 страницы
    return HistoryView(
        guild_id=1,
        user_id=42,
        matches=[_match(i) for i in range(3)],
        stats={"total_matches": 3, "wins": 2, "losses": 1},
        user_name="tester",
    )


async def _press(layout, label: str, user_id: int = 42):
    """Нажать кнопку на последнем отправленном layout, вернуть новый view."""
    btn = _find_button(layout, label)
    interaction = _interaction(user_id)
    await btn.callback(interaction)
    interaction.response.edit_message.assert_awaited_once()
    return interaction.response.edit_message.await_args.kwargs["view"]


@pytest.mark.asyncio
async def test_history_pagination_next_and_prev():
    view = _history_view()
    layout = compose(discord.Embed(description="history"), view)

    assert view.current_page == 0
    # page 0 -> 1 -> 2 -> 3
    for expected in (1, 2, 3):
        layout = await _press(layout, "Вперёд")
        assert view.current_page == expected
        # Кнопки навигации обязаны остаться после каждого edit
        assert _has_button(layout, "Назад")
        assert _has_button(layout, "Вперёд")

    # На последней странице «Вперёд» отключена
    assert _find_button(layout, "Вперёд").disabled is True
    assert _find_button(layout, "Назад").disabled is False

    # 3 -> 2 -> 1 -> 0
    for expected in (2, 1, 0):
        layout = await _press(layout, "Назад")
        assert view.current_page == expected
        assert _has_button(layout, "Назад")
        assert _has_button(layout, "Вперёд")

    # На первой странице «Назад» — disabled-заглушка без callback
    prev = _find_button(layout, "Назад")
    assert prev.disabled is True


@pytest.mark.asyncio
async def test_history_pagination_state_shared_across_edits():
    """Каждый edit пересобирает LayoutView — состояние не должно сбрасываться."""
    view = _history_view()
    layout = compose(discord.Embed(description="history"), view)

    layout = await _press(layout, "Вперёд")
    layout = await _press(layout, "Вперёд")
    assert view.current_page == 2

    # Кнопки в новом layout принадлежат новой обёртке с тем же delegate
    nxt = _find_button(layout, "Вперёд")
    assert nxt.view is layout
    assert getattr(nxt.view, "source_view", None) is view


# ---------------- /robgroup lobby ----------------


def _make_lobby() -> rob.RobGroupView:
    view = rob.RobGroupView(
        guild_id=1, leader_id=1000, victim_id=2000, victim_name="victim"
    )
    view.add_item(rob.RobGroupJoinButton(1, view))
    view.add_item(rob.RobGroupStartButton(1, view))
    view.add_item(rob.RobGroupCancelButton(1, view))
    view.message_id = 5550001
    rob._robgroup_lobbies[view.message_id] = {
        "leader_id": view.leader_id,
        "victim_id": view.victim_id,
        "members": view.members,
        "start_time": view.start_time,
    }
    return view


@pytest.fixture
def _rob_env(monkeypatch):
    """Моки балансов/питомцев для robgroup без БД."""
    monkeypatch.setattr(
        rob.user_balance_store, "get_balance", AsyncMock(return_value=10000)
    )
    monkeypatch.setattr(
        rob.user_balance_store, "add_balance", AsyncMock(return_value=10000)
    )
    monkeypatch.setattr(
        rob.user_balance_store, "subtract_balance", AsyncMock(return_value=10000)
    )
    monkeypatch.setattr(
        rob.user_bank_store, "get_bank_balance", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        rob.inventory_store, "get_player_inventory", lambda *a, **k: []
    )

    import storage.pet_store as pet_mod

    monkeypatch.setattr(
        pet_mod.pet_store, "get_user_pet", AsyncMock(return_value=None)
    )
    rob._robgroup_cooldowns.clear()
    yield
    rob._robgroup_lobbies.pop(5550001, None)
    rob._robgroup_cooldowns.clear()


async def _join(view_layout, user_id: int):
    btn = _find_button(view_layout, "Вступить в банду")
    interaction = _interaction(user_id)
    await btn.callback(interaction)
    return interaction


def _assert_lobby_buttons(layout):
    for label in ("Вступить в банду", "Начать штурм", "Отмена"):
        assert _has_button(layout, label), f"missing button {label!r}"


@pytest.mark.asyncio
async def test_robgroup_join_keeps_all_buttons(_rob_env):
    view = _make_lobby()
    embed = await view.build_embed_async()
    layout = compose(embed, view)
    _assert_lobby_buttons(layout)

    # Второй игрок вступает
    inter = await _join(layout, 3001)
    inter.response.edit_message.assert_awaited_once()
    layout = inter.response.edit_message.await_args.kwargs["view"]
    assert 3001 in view.members
    _assert_lobby_buttons(layout)

    # Третий игрок вступает — кнопки снова на месте
    inter = await _join(layout, 3002)
    layout = inter.response.edit_message.await_args.kwargs["view"]
    assert 3002 in view.members
    _assert_lobby_buttons(layout)
    assert len(view.members) == 3


@pytest.mark.asyncio
async def test_robgroup_join_limits_and_rules(_rob_env):
    view = _make_lobby()
    embed = await view.build_embed_async()
    layout = compose(embed, view)

    # Жертва не может вступить
    inter = await _join(layout, view.victim_id)
    inter.response.send_message.assert_awaited_once()
    assert view.victim_id not in view.members

    # Лимит 6: лидер + 5 joins = 6, седьмой отклоняется
    layout = compose(embed, view)
    for uid in (3001, 3002, 3003, 3004, 3005):
        inter = await _join(layout, uid)
        layout = inter.response.edit_message.await_args.kwargs["view"]
    assert len(view.members) == 6

    inter = await _join(layout, 3006)
    inter.response.send_message.assert_awaited_once()
    assert 3006 not in view.members
    assert len(view.members) == 6

    # Повторный join того же игрока — «уже в банде» (новое лобби)
    view2 = _make_lobby()
    view2.message_id = 5550002
    rob._robgroup_lobbies[5550002] = {"leader_id": 1000}
    try:
        embed2 = await view2.build_embed_async()
        layout2 = compose(embed2, view2)
        await _join(layout2, 3001)
        inter = await _join(layout2, 3001)
        inter.response.send_message.assert_awaited_once()
        assert len(view2.members) == 2
    finally:
        rob._robgroup_lobbies.pop(5550002, None)


@pytest.mark.asyncio
async def test_robgroup_concurrent_joins_respect_limit(_rob_env):
    view = _make_lobby()
    embed = await view.build_embed_async()
    layout = compose(embed, view)

    async def join(uid):
        btn = _find_button(layout, "Вступить в банду")
        interaction = _interaction(uid)
        await btn.callback(interaction)
        return interaction

    uids = list(range(3001, 3011))  # 10 претендентов на 5 мест
    results = await asyncio.gather(*(join(uid) for uid in uids))

    assert len(view.members) == 6  # лидер + 5, лимит не превышен
    joined = [uid for uid, r in zip(uids, results)
              if r.response.edit_message.await_count == 1]
    assert len(joined) == 5


@pytest.mark.asyncio
async def test_robgroup_start_only_leader_and_once(_rob_env, monkeypatch):
    view = _make_lobby()
    embed = await view.build_embed_async()
    layout = compose(embed, view)

    await _join(layout, 3001)
    # Отдельное лобби для сценария штурма
    view2 = _make_lobby()
    view2.message_id = 5550003
    rob._robgroup_lobbies[5550003] = {"leader_id": 1000}
    try:
        embed2 = await view2.build_embed_async()
        layout2 = compose(embed2, view2)
        inter = await _join(layout2, 3001)
        layout2 = inter.response.edit_message.await_args.kwargs["view"]

        # Не-лидер не может начать штурм
        start_btn = _find_button(layout2, "Начать штурм")
        inter = _interaction(9999)
        await start_btn.callback(inter)
        inter.response.send_message.assert_awaited_once()
        assert view2.finished is False

        # Лидер запускает — результат считается один раз
        monkeypatch.setattr(rob.random, "random", lambda: 0.99)  # провал ветки
        start_btn = _find_button(layout2, "Начать штурм")
        inter = _interaction(view2.leader_id)
        await start_btn.callback(inter)
        assert view2.finished is True
        inter.response.edit_message.assert_awaited_once()
        final_view = inter.response.edit_message.await_args.kwargs["view"]
        assert not _has_button(final_view, "Начать штурм")
        assert not _has_button(final_view, "Вступить в банду")

        sub_calls = rob.user_balance_store.subtract_balance.await_count
        add_calls = rob.user_balance_store.add_balance.await_count

        # Повторный клик по «Начать штурм» — баланс не трогается
        # повторно (отклоняется по finished-флагу либо cooldown).
        start_btn2 = _find_button(layout2, "Начать штурм")
        inter2 = _interaction(view2.leader_id)
        await start_btn2.callback(inter2)
        inter2.response.edit_message.assert_not_awaited()
        assert rob.user_balance_store.subtract_balance.await_count == sub_calls
        assert rob.user_balance_store.add_balance.await_count == add_calls
    finally:
        rob._robgroup_lobbies.pop(5550003, None)


@pytest.mark.asyncio
async def test_robgroup_cancel_and_timeout_finish_lobby(_rob_env):
    # Отмена лидером
    view = _make_lobby()
    embed = await view.build_embed_async()
    layout = compose(embed, view)
    cancel_btn = _find_button(layout, "Отмена")

    inter = _interaction(9999)  # не лидер
    await cancel_btn.callback(inter)
    inter.response.send_message.assert_awaited_once()
    assert view.finished is False

    inter = _interaction(view.leader_id)
    await cancel_btn.callback(inter)
    assert view.finished is True
    inter.response.edit_message.assert_awaited_once()

    # Таймаут помечает лобби завершённым
    view2 = _make_lobby()
    view2.message_id = 5550004
    view2.original_message = None
    await view2.on_timeout()
    assert view2.finished is True
    assert 5550004 not in rob._robgroup_lobbies
    rob._robgroup_lobbies.pop(5550004, None)
