"""Регрессии добавления/удаления/обмена/перемещения игроков(org).

- AdminAddButton открывает ephemeral CV2-карточку с UserSelect,
  ручной ввод ника (AdminAddModal) удалён.
- Идентификация по Discord user_id: одинаковые display_name у разных
  пользователей не смешиваются.
- Проверки: VAC, другой активный турнир, повторная регистрация,
  лимит круга, фаза турнира, права org.
- Игрок уже в другом круге → предложение перемещения, без дубликата.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord

import services.vac_service as vac_service
import views.setup_view as sv
from models.tournament import (
    FormationMode,
    Tournament,
    TournamentPhase,
)

GID = 111
CID = 222
ORG_ID = 424242


# ------------------------------------------------------------------ mocks


class FakeStore:
    def __init__(self, tournaments=()):
        self._t = {(t.guild_id, t.channel_id): t for t in tournaments}

    def get(self, guild_id, channel_id):
        return self._t.get((guild_id, channel_id))

    def set(self, tournament):
        self._t[(tournament.guild_id, tournament.channel_id)] = tournament

    def get_by_guild(self, guild_id):
        return [
            t for (g, _), t in self._t.items() if g == guild_id
        ]

    def delete(self, guild_id, channel_id):
        self._t.pop((guild_id, channel_id), None)


def _member(uid, name):
    return SimpleNamespace(
        id=uid,
        display_name=name,
        display_avatar=SimpleNamespace(url=f"https://cdn/a/{uid}.png"),
    )


def _guild(members):
    return SimpleNamespace(
        id=GID,
        roles=[],
        get_role=lambda rid: None,
        get_member=lambda uid: members.get(uid),
        fetch_member=AsyncMock(
            side_effect=lambda uid: members.get(uid)
        ),
    )


def _user(uid=ORG_ID, admin=True):
    return SimpleNamespace(
        id=uid,
        display_name="Org",
        guild_permissions=SimpleNamespace(administrator=admin),
    )


def _interaction(user=None, guild=None, resolved=None):
    user = user or _user()
    guild = guild or _guild({})
    return SimpleNamespace(
        user=user,
        guild=guild,
        guild_id=GID,
        channel_id=CID,
        client=SimpleNamespace(
            update_tournament_message=AsyncMock()
        ),
        data={"resolved": {"users": resolved or {}}},
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
            send_modal=AsyncMock(),
            defer=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _tournament(**kw):
    kw.setdefault("guild_id", GID)
    kw.setdefault("channel_id", CID)
    return Tournament(**kw)


def _find(view, cls):
    """Первый элемент cls в LayoutView/View рекурсивно."""
    for child in view.walk_children():
        if isinstance(child, cls):
            return child
    raise AssertionError(f"{cls.__name__} not found")


def _find_all(view, cls):
    return [c for c in view.walk_children() if isinstance(c, cls)]


def _last_view(mock):
    """view= из последнего edit_message/send_message вызова."""
    call = mock.call_args
    assert call is not None
    kwargs = call.kwargs
    if "view" in kwargs:
        return kwargs["view"]
    # позиционный вызов compose(...) без view= — редкость
    for arg in call.args:
        if hasattr(arg, "walk_children"):
            return arg
    raise AssertionError("view not found in call")


def _texts(view):
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == 10:
                out.append(node.get("content", ""))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for i in node:
                walk(i)

    walk(view.to_components())
    return out


@pytest.fixture
def env(monkeypatch):
    """Изоляция: fake store, VAC выключен."""
    store = FakeStore()
    monkeypatch.setattr(sv, "store", store)
    monkeypatch.setattr(
        vac_service, "check_vac",
        AsyncMock(side_effect=lambda g, u: env.vac),
    )
    env = SimpleNamespace(store=store, vac=None)
    return env


def _use_store(env, *tournaments):
    env.store._t.clear()
    for t in tournaments:
        env.store._t[(t.guild_id, t.channel_id)] = t
    return env.store


# ------------------------------------------------------------- button flow


@pytest.mark.asyncio
async def test_add_button_opens_cv2_user_select(env):
    t = _tournament()
    _use_store(env, t)
    interaction = _interaction()

    btn = sv.AdminAddButton(GID, CID, 4, "Круг 4")
    await btn.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True
    view = kwargs["view"]
    select = _find(view, discord.ui.UserSelect)
    assert select.min_values == 1 and select.max_values == 1
    # Никаких модалок/ручного ввода
    interaction.response.send_modal.assert_not_awaited()


@pytest.mark.asyncio
async def test_pick_user_shows_confirm_and_adds(env):
    t = _tournament()
    store = _use_store(env, t)
    members = {555: _member(555, "Newbie")}
    guild = _guild(members)
    interaction = _interaction(guild=guild)
    interaction.data["resolved"]["users"] = {
        "555": {"username": "newbie", "global_name": "Newbie"}
    }

    view = sv.AdminAddUserView(GID, CID, 4)
    select = _find(view, discord.ui.UserSelect)
    select._values = ["555"]
    await select.callback(interaction)

    confirm_view = _last_view(interaction.response.edit_message)
    texts = "\n".join(_texts(confirm_view))
    assert "Newbie" in texts
    assert "Круг 4" in texts

    add_btn = _find(confirm_view, discord.ui.Button)
    assert add_btn.label == "Добавить"
    await add_btn.callback(interaction)

    assert "Newbie" in t.circle4
    assert t.player_user_ids["Newbie"] == 555
    interaction.client.update_tournament_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_blocked_by_vac(env):
    t = _tournament()
    _use_store(env, t)
    env.vac = {
        "expires_at": datetime.now() + timedelta(hours=2),
        "reason": "smurf",
        "level": 1,
    }
    members = {555: _member(555, "Newbie")}
    interaction = _interaction(guild=_guild(members))
    interaction.data["resolved"]["users"] = {"555": {"username": "n"}}

    view = sv.AdminAddUserView(GID, CID, 4)
    select = _find(view, discord.ui.UserSelect)
    select._values = ["555"]
    await select.callback(interaction)

    confirm_view = _last_view(interaction.response.edit_message)
    add_btn = _find(confirm_view, discord.ui.Button)
    await add_btn.callback(interaction)

    assert t.circle4 == []
    # Последний edit_message — карточка с VAC-текстом
    last = interaction.response.edit_message.call_args.kwargs["view"]
    assert "VAC" in "\n".join(_texts(last))


@pytest.mark.asyncio
async def test_add_blocked_by_other_active_tournament(env):
    t = _tournament()
    other = _tournament(channel_id=999)
    other.id = f"{GID}_other_tournament"  # id иначе может совпасть по ms
    other.player_user_ids["OtherName"] = 555
    other.circle4.append("OtherName")
    _use_store(env, t, other)

    members = {555: _member(555, "Newbie")}
    interaction = _interaction(guild=_guild(members))
    interaction.data["resolved"]["users"] = {"555": {"username": "n"}}

    view = sv.AdminAddUserView(GID, CID, 4)
    select = _find(view, discord.ui.UserSelect)
    select._values = ["555"]
    await select.callback(interaction)

    confirm_view = _last_view(interaction.response.edit_message)
    add_btn = _find(confirm_view, discord.ui.Button)
    await add_btn.callback(interaction)

    assert t.circle4 == []
    last = interaction.response.edit_message.call_args.kwargs["view"]
    assert "другом активном турнире" in "\n".join(_texts(last))


@pytest.mark.asyncio
async def test_add_blocked_by_circle_limit(env):
    t = _tournament()
    # EIGHT → captain_count=2, лимит круга 2
    t.circle4.extend(["A", "B"])
    t.player_user_ids.update({"A": 1, "B": 2})
    _use_store(env, t)

    members = {555: _member(555, "Newbie")}
    interaction = _interaction(guild=_guild(members))
    interaction.data["resolved"]["users"] = {"555": {"username": "n"}}

    view = sv.AdminAddUserView(GID, CID, 4)
    select = _find(view, discord.ui.UserSelect)
    select._values = ["555"]
    await select.callback(interaction)

    confirm_view = _last_view(interaction.response.edit_message)
    add_btn = _find(confirm_view, discord.ui.Button)
    await add_btn.callback(interaction)

    assert len(t.circle4) == 2
    last = interaction.response.edit_message.call_args.kwargs["view"]
    assert "заполнен" in "\n".join(_texts(last))


@pytest.mark.asyncio
async def test_add_blocked_outside_setup(env):
    t = _tournament(phase=TournamentPhase.DRAFT)
    _use_store(env, t)
    interaction = _interaction()

    btn = sv.AdminAddButton(GID, CID, 4, "Круг 4")
    await btn.callback(interaction)
    interaction.response.send_message.assert_awaited_once()
    assert "не в фазе" in str(
        interaction.response.send_message.call_args
    )


@pytest.mark.asyncio
async def test_org_permission_required(env):
    t = _tournament()
    _use_store(env, t)
    interaction = _interaction(user=_user(admin=False))

    btn = sv.AdminAddButton(GID, CID, 4, "Круг 4")
    await btn.callback(interaction)
    interaction.response.send_message.assert_awaited_once()
    interaction.response.send_message.reset_mock()

    # И на select-действии тоже проверяем права
    view = sv.AdminAddUserView(GID, CID, 4)
    select = _find(view, discord.ui.UserSelect)
    select._values = ["555"]
    interaction2 = _interaction(user=_user(admin=False))
    await select.callback(interaction2)
    interaction2.response.send_message.assert_awaited_once()
    interaction2.response.edit_message.assert_not_awaited()


# ------------------------------------------------------- duplicates & moves


@pytest.mark.asyncio
async def test_same_display_name_different_users(env):
    """Два пользователя с одинаковым display_name — разные записи."""
    t = _tournament()
    t.circle4.append("Same")
    t.player_user_ids["Same"] = 111
    _use_store(env, t)

    members = {222: _member(222, "Same")}
    interaction = _interaction(guild=_guild(members))
    interaction.data["resolved"]["users"] = {"222": {"username": "same2"}}

    view = sv.AdminAddUserView(GID, CID, 4)
    select = _find(view, discord.ui.UserSelect)
    select._values = ["222"]
    await select.callback(interaction)

    confirm_view = _last_view(interaction.response.edit_message)
    add_btn = _find(confirm_view, discord.ui.Button)
    await add_btn.callback(interaction)

    # Оба в турнире, идентичность по разным user_id
    assert len(t.circle4) == 2
    names = {n: t.player_user_ids[n] for n in t.circle4}
    assert names["Same"] == 111
    other = [n for n in t.circle4 if n != "Same"][0]
    assert "Same" in other           # суффикс, не голое имя
    assert t.player_user_ids[other] == 222

    # Резолв токенов не путает пользователей
    entries = dict(sv._player_select_entries(t))
    assert sv._resolve_player_token(t, entries["Same"]) == "Same"
    assert sv._resolve_player_token(t, entries[other]) == other


@pytest.mark.asyncio
async def test_move_between_circles_with_confirm(env):
    t = _tournament()
    t.circle2.append("Mover")
    t.player_user_ids["Mover"] = 555
    _use_store(env, t)

    members = {555: _member(555, "Mover")}
    interaction = _interaction(guild=_guild(members))
    interaction.data["resolved"]["users"] = {"555": {"username": "m"}}

    # Пик пользователя, который уже в Круге 2, для добавления в Круг 3
    view = sv.AdminAddUserView(GID, CID, 3)
    select = _find(view, discord.ui.UserSelect)
    select._values = ["555"]
    await select.callback(interaction)

    move_view = _last_view(interaction.response.edit_message)
    texts = "\n".join(_texts(move_view))
    assert "Круг 2" in texts and "Круг 3" in texts
    assert "Переместить" in texts

    move_btn = _find(move_view, discord.ui.Button)
    assert move_btn.label == "Переместить"
    await move_btn.callback(interaction)

    assert "Mover" not in t.circle2
    assert "Mover" in t.circle3
    assert t.player_user_ids["Mover"] == 555
    interaction.client.update_tournament_message.assert_awaited()


@pytest.mark.asyncio
async def test_same_circle_no_duplicate_no_move(env):
    t = _tournament()
    t.circle4.append("Here")
    t.player_user_ids["Here"] = 555
    _use_store(env, t)

    members = {555: _member(555, "Here")}
    interaction = _interaction(guild=_guild(members))
    interaction.data["resolved"]["users"] = {"555": {"username": "h"}}

    view = sv.AdminAddUserView(GID, CID, 4)
    select = _find(view, discord.ui.UserSelect)
    select._values = ["555"]
    await select.callback(interaction)

    last = interaction.response.edit_message.call_args.kwargs["view"]
    assert "уже находится" in "\n".join(_texts(last))
    assert t.circle4.count("Here") == 1
    # Никаких кнопок подтверждения
    assert _find_all(last, discord.ui.Button) == []


# ------------------------------------------- delete/swap/move by Discord ID


@pytest.mark.asyncio
async def test_delete_resolves_by_user_id(env):
    """Удаление выбирает по user_id — при одинаковых именах удаляется
    именно выбранный Discord-пользователь."""
    t = _tournament()
    t.circle4.extend(["Same", "Same ·0222"])
    t.player_user_ids["Same"] = 111
    t.player_user_ids["Same ·0222"] = 222
    _use_store(env, t)

    interaction = _interaction()
    btn = sv.DeletePlayerButton(GID, CID)
    await btn.callback(interaction)

    view = _last_view(interaction.response.send_message)
    select = _find(view, discord.ui.Select)
    # value — user_id, а не имя
    values = {o.value for o in select.options}
    assert "111" in values and "222" in values

    select._values = ["222"]
    await select.callback(interaction)

    assert "Same ·0222" not in t.circle4
    assert "Same" in t.circle4
    assert t.player_user_ids.get("Same") == 111


@pytest.mark.asyncio
async def test_swap_resolves_by_user_id(env):
    t = _tournament()
    t.circle2.extend(["P1", "P2"])
    t.player_user_ids.update({"P1": 11, "P2": 22})
    _use_store(env, t)

    interaction = _interaction()
    btn = sv.SwapPlayersButton(GID, CID)
    await btn.callback(interaction)

    view = _last_view(interaction.response.send_message)
    select1 = _find(view, discord.ui.Select)
    select1._values = ["11"]
    await select1.callback(interaction)

    view2 = _last_view(interaction.response.send_message)
    select2 = _find(view2, discord.ui.Select)
    # Первый игрок исключён из второго списка
    assert all(o.value != "11" for o in select2.options)
    select2._values = ["22"]
    await select2.callback(interaction)

    assert t.circle2 == ["P2", "P1"]


@pytest.mark.asyncio
async def test_move_player_resolves_by_user_id(env):
    t = _tournament()
    t.circle2.append("Movable")
    t.player_user_ids["Movable"] = 33
    _use_store(env, t)

    interaction = _interaction()
    btn = sv.MovePlayerButton(GID, CID)
    await btn.callback(interaction)

    view = _last_view(interaction.response.send_message)
    select = _find(view, discord.ui.Select)
    assert select.options[0].value == "33"
    select._values = ["33"]
    await select.callback(interaction)

    # Второй шаг — выбор круга
    view2 = _last_view(interaction.response.send_message)
    circle_select = _find(view2, discord.ui.Select)
    circle_select._values = ["circle3"]
    await circle_select.callback(interaction)

    assert "Movable" not in t.circle2
    assert "Movable" in t.circle3
