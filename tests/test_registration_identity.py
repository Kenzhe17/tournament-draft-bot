"""Регрессии идентификации игроков при регистрации (P0).

- Discord user_id — главный идентификатор: display_name может
  повторяться у разных пользователей и меняться после входа.
- Одинаковые display_name → уникальные сохранённые имена,
  оба игрока участвуют одновременно.
- Ник не меняется автоматически (нет user.edit(nick=...)).
- Legacy-записи без user_id привязываются по совпадающему имени.
- Отображение: реальные пользователи — <@uid>, тестовые (uid<=0)
  — текстовые имена.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord  # noqa: F401  (реальные классы Button/View)

import services.vac_service as vac_service
import views.setup_view as sv
from models.tournament import (
    FormationMode,
    RegistrationState,
    Tournament,
)
from utils.embeds import _circle_line, _player_display

GID = 111
CID = 222


# ------------------------------------------------------------------ mocks


def _user(uid, name, username=None):
    return SimpleNamespace(
        id=uid,
        display_name=name,
        name=username or name,
        primary_guild=None,
        edit=AsyncMock(),
    )


def _interaction(user, update_result=True):
    return SimpleNamespace(
        user=user,
        guild=SimpleNamespace(id=GID),
        guild_id=GID,
        channel_id=CID,
        client=SimpleNamespace(
            update_tournament_message=AsyncMock(return_value=update_result)
        ),
        data={"resolved": {"users": {}}},
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
    kw.setdefault("registration", RegistrationState.OPEN)
    kw.setdefault("phase", __import__("models.tournament", fromlist=["TournamentPhase"]).TournamentPhase.SETUP)
    return Tournament(**kw)


class FakeStore:
    def __init__(self, tournaments=()):
        self._t = {(t.guild_id, t.channel_id): t for t in tournaments}

    def get(self, guild_id, channel_id):
        return self._t.get((guild_id, channel_id))

    def set(self, tournament):
        self._t[(tournament.guild_id, tournament.channel_id)] = tournament

    def get_by_guild(self, guild_id):
        return [t for (g, _), t in self._t.items() if g == guild_id]

    def delete(self, guild_id, channel_id):
        self._t.pop((guild_id, channel_id), None)


@pytest.fixture
def env(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(sv, "store", store)
    monkeypatch.setattr(
        vac_service, "check_vac",
        AsyncMock(return_value=None),
    )
    import services.tournament_audit_service as audit_svc
    monkeypatch.setattr(
        audit_svc, "record_interaction", AsyncMock(return_value=None)
    )
    return SimpleNamespace(store=store)


def _use_store(env, *tournaments):
    env.store._t.clear()
    for t in tournaments:
        env.store._t[(t.guild_id, t.channel_id)] = t
    return env.store


async def _join_circle(env, tournament, circle, user):
    interaction = _interaction(user)
    btn = sv.CircleSelectButton(GID, CID, circle, f"Круг {circle}")
    await btn.callback(interaction)
    return interaction


async def _join_pool(env, tournament, user):
    interaction = _interaction(user)
    btn = sv.JoinPoolButton(GID, CID, len(tournament.players_pool), 8)
    await btn.callback(interaction)
    return interaction


# ------------------------------------------------------- circle joins


@pytest.mark.asyncio
async def test_duplicate_display_names_both_join(env):
    """Два пользователя с одинаковым display_name входят в разные
    круги — оба записаны, имена уникальны, ID корректны."""
    t = _tournament()
    _use_store(env, t)

    u1 = _user(1001, "Nick")
    u2 = _user(1002, "Nick")

    await _join_circle(env, t, 1, u1)
    await _join_circle(env, t, 2, u2)

    assert len(t.circle1) == 1 and len(t.circle2) == 1
    n1, n2 = t.circle1[0], t.circle2[0]
    assert n1 != n2
    assert t.player_user_ids[n1] == 1001
    assert t.player_user_ids[n2] == 1002
    # uid — источник истины
    assert t.player_name_for_uid(1001) == n1
    assert t.player_name_for_uid(1002) == n2
    u1.edit.assert_not_awaited()
    u2.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_user_moves_between_circles(env):
    """Повторный вход того же user_id в другой круг — перемещение,
    а не дубликат."""
    t = _tournament()
    _use_store(env, t)
    u = _user(2001, "Mover")

    await _join_circle(env, t, 1, u)
    first_name = t.circle1[0]

    await _join_circle(env, t, 3, u)

    assert t.circle1 == []
    assert t.circle3 == [first_name]
    assert len(t.all_players) == 1
    assert t.player_user_ids[first_name] == 2001


@pytest.mark.asyncio
async def test_same_user_same_circle_rejected(env):
    t = _tournament()
    _use_store(env, t)
    u = _user(2002, "Stay")

    await _join_circle(env, t, 1, u)
    interaction = await _join_circle(env, t, 1, u)

    assert len(t.circle1) == 1
    msg = interaction.response.send_message.call_args
    assert "уже находитесь" in str(msg)


@pytest.mark.asyncio
async def test_nick_change_keeps_identity(env):
    """Смена display_name после входа не создаёт второго игрока —
    идентификация по user_id."""
    t = _tournament()
    _use_store(env, t)
    u = _user(3001, "OldNick")

    await _join_circle(env, t, 1, u)
    assert len(t.all_players) == 1

    u.display_name = "NewNick"
    await _join_circle(env, t, 2, u)

    assert len(t.all_players) == 1, "не должно быть дубликата игрока"
    assert t.circle1 == []
    assert len(t.circle2) == 1
    stored = t.circle2[0]
    assert t.player_user_ids[stored] == 3001


@pytest.mark.asyncio
async def test_unicode_emoji_nick_preserved(env):
    """Ник с Unicode/эмодзи сохраняется как есть, Discord-ник
    не редактируется."""
    t = _tournament()
    _use_store(env, t)
    u = _user(4001, "Player😀🔥")

    await _join_circle(env, t, 1, u)

    assert t.circle1 == ["Player😀🔥"]
    assert t.player_user_ids["Player😀🔥"] == 4001
    u.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_entry_without_uid_adopted(env):
    """Старый турнир: имя записано без user_id — при входе
    пользователя с тем же display_name ID привязывается, дубля нет."""
    t = _tournament()
    t.circle1 = ["LegacyNick"]
    t.player_user_ids = {}  # совсем старая запись
    _use_store(env, t)
    u = _user(5001, "LegacyNick")

    # Пользователь уже «в» турнире по legacy-имени — вход в другой
    # круг должен переместить его, а не добавить дубликат.
    await _join_circle(env, t, 2, u)

    assert len(t.all_players) == 1
    assert t.circle1 == []
    assert t.circle2 == ["LegacyNick"]
    assert t.player_user_ids["LegacyNick"] == 5001


# ------------------------------------------------------------ pool join


@pytest.mark.asyncio
async def test_pool_duplicate_display_names(env):
    t = _tournament(formation_mode=FormationMode.RANDOM)
    _use_store(env, t)
    u1 = _user(6001, "PoolNick")
    u2 = _user(6002, "PoolNick")

    await _join_pool(env, t, u1)
    await _join_pool(env, t, u2)

    assert len(t.players_pool) == 2
    names = set(t.players_pool)
    assert len(names) == 2
    uids = {t.player_user_ids[n] for n in names}
    assert uids == {6001, 6002}


@pytest.mark.asyncio
async def test_pool_same_user_twice_rejected(env):
    t = _tournament(formation_mode=FormationMode.RANDOM)
    _use_store(env, t)
    u = _user(6003, "PoolOnce")

    await _join_pool(env, t, u)
    interaction = await _join_pool(env, t, u)

    assert len(t.players_pool) == 1
    msg = interaction.response.send_message.call_args
    assert "уже участвуете" in str(msg)


# ------------------------------------------------------------ exit


@pytest.mark.asyncio
async def test_exit_button_works_with_suffixed_name(env):
    """Игрок с уникальным (суффиксированным) именем может выйти —
    поиск по user_id, а не по display_name."""
    t = _tournament()
    _use_store(env, t)
    u1 = _user(7001, "Same")
    u2 = _user(7002, "Same")

    await _join_circle(env, t, 1, u1)
    await _join_circle(env, t, 2, u2)
    assert len(t.all_players) == 2

    interaction = _interaction(u2)
    btn = sv.ExitButton(GID, CID)
    await btn.callback(interaction)

    # Ушёл именно второй игрок, первый остался
    assert len(t.all_players) == 1
    remaining = next(iter(t.all_players))
    assert t.player_user_ids[remaining] == 7001


# ------------------------------------------------------------ display


def test_player_display_mention_for_real_user():
    t = _tournament()
    t.circle1 = ["Nick"]
    t.player_user_ids = {"Nick": 12345}
    assert _player_display(t, "Nick") == "<@12345>"


def test_player_display_text_for_test_player():
    t = _tournament()
    t.circle1 = ["Player1"]
    t.player_user_ids = {"Player1": -1}
    assert _player_display(t, "Player1") == "Player1"


def test_player_display_text_without_uid():
    t = _tournament()
    t.circle1 = ["NoId"]
    assert _player_display(t, "NoId") == "NoId"


def test_circle_line_uses_mentions():
    t = _tournament()
    t.circle1 = ["Real", "Bot"]
    t.player_user_ids = {"Real": 777, "Bot": -5}
    line = _circle_line(t.circle1, None, t)
    assert "<@777>" in line
    assert "Bot" in line
    assert "<@-5>" not in line


# ------------------------------------------------------- update result


@pytest.mark.asyncio
async def test_join_warns_when_card_update_fails(env):
    """Если update_tournament_message вернул False — пользователь
    получает предупреждение, а не ложный успех."""
    t = _tournament()
    _use_store(env, t)
    u = _user(8001, "Warn")
    interaction = _interaction(u, update_result=False)

    btn = sv.CircleSelectButton(GID, CID, 1, "Круг 1")
    await btn.callback(interaction)

    # Игрок записан, но отправлено предупреждение об обновлении
    assert len(t.circle1) == 1
    calls = interaction.response.send_message.call_args_list
    texts = [str(c) for c in calls]
    assert any("не удалось обновить" in x for x in texts)


# ------------------------------------------------------- model helpers


def test_remove_player_uid_exact_match():
    t = _tournament()
    t.circle1 = ["Nick ·1001", "nick"]
    t.player_user_ids = {"Nick ·1001": 1001, "nick": 2002}

    assert t.remove_player_uid(1001) is True
    assert t.circle1 == ["nick"]
    assert t.player_user_ids == {"nick": 2002}

    assert t.remove_player_uid(9999) is False
    assert t.circle1 == ["nick"]


def test_serialization_roundtrip_preserves_identity():
    """to_dict/from_dict (restart persistence): player_user_ids и
    уникальные имена сохраняются, uid-lookup работает после reload."""
    t = _tournament()
    t.circle1 = ["Nick", "Nick ·1002"]
    t.player_user_ids = {"Nick": 1001, "Nick ·1002": 1002}

    data = t.to_dict()
    t2 = Tournament.from_dict(data)

    assert t2.circle1 == ["Nick", "Nick ·1002"]
    assert t2.player_user_ids == {"Nick": 1001, "Nick ·1002": 1002}
    assert t2.player_name_for_uid(1002) == "Nick ·1002"
    assert _circle_line(t2.circle1, None, t2) == "<@1001> <@1002>"
