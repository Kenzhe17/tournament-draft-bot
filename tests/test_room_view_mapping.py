"""V6: RoomView — кнопки команд (team_index=0 валиден),
компактный mapping UI, защита interaction timeout.

Проверяем:
- кнопки для пар (0,1), (1,2), (0,7) — обе видны и несут
  собственный team_index в custom_id/коллбэке;
- RoomTeamMapView: 4 select'а, sentinel "__none__" вместо
  пустого value, сохранение привязок по Discord ID;
- _on_team/_on_save отвечают через defer+followup и не падают
  молча при ошибке.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import views.ocr_match_view as omv


# ---------- fakes ----------

class _Resp:
    def __init__(self, ix):
        self._ix = ix
        self._done = False

    async def defer(self, **kw):
        self._done = True

    async def send_message(self, content=None, view=None, **kw):
        self._done = True
        self._ix.messages.append(str(content))
        self._ix.last_view = view

    async def send_modal(self, modal=None, **kw):
        self._done = True
        self._ix.modal = modal

    def is_done(self):
        return self._done


class _Follow:
    def __init__(self, ix):
        self._ix = ix

    async def send(self, content=None, view=None, **kw):
        self._ix.messages.append(str(content))
        self._ix.last_view = view


class _Ix:
    def __init__(self, env, user_id=1):
        self.user = SimpleNamespace(
            id=user_id, display_name="u", name="u",
            guild_permissions=SimpleNamespace(administrator=False),
            roles=[], guild=env.guild,
        )
        self.guild = env.guild
        self.channel = env.stats
        self.response = _Resp(self)
        self.followup = _Follow(self)
        self.modal = None
        self.messages: list[str] = []
        self.last_view = None

    async def edit_original_response(self, content=None, **kw):
        self.messages.append(str(content))


def _member_ix(env, user_id):
    return _Ix(env, user_id)


# ---------- tournament/session fixtures ----------

def _prow(name, i):
    return {
        "ocr_name": name, "kills": i + 1, "deaths": 1,
        "assists": 1, "dmg": 100 + i,
    }


def _map_rows(prefix):
    return [
        {
            "ocr_name": f"{prefix}{i}", "kills": 1, "deaths": 1,
            "assists": 1, "dmg": 10, "discord_name": None,
            "discord_id": None, "source": None,
        }
        for i in range(4)
    ]


def _tournament(pair=(0, 1), n_teams=8):
    teams = [
        {"players": [f"T{i}p{j}" for j in range(4)],
         "captain": f"T{i}p0"}
        for i in range(n_teams)
    ]
    return SimpleNamespace(
        id="t1", channel_id=1, guild_id=1,
        teams=teams,
        team_names={},
        player_user_ids={
            f"T{i}p{j}": 1000 + i * 10 + j
            for i in range(n_teams) for j in range(4)
        },
        qualifier_matches=[pair],
        semifinal_matches=[(2, 3)],
        final_teams=[4, 5],
        ocr_sessions={},
        processed_elo_matches=set(),
    )


def _room(t1=0, t2=1):
    return {
        "room_index": 1, "kind": "classic",
        "status": "awaiting_mapping",
        "panels": {
            "left": {"name": "L", "rows": [_prow(f"L{i}", i) for i in range(4)]},
            "right": {"name": "R", "rows": [_prow(f"R{i}", i) for i in range(4)]},
        },
        "maps": {
            str(t1): {"panel": "left", "rows": _map_rows("L"),
                      "confirmed": False},
            str(t2): {"panel": "right", "rows": _map_rows("R"),
                      "confirmed": False},
        },
        "panel_picks": {},
        "shas": ["s1"], "sha": "s1", "message_id": 999,
    }


def _session(room=None, status="awaiting_mapping"):
    return {
        "mode": "rooms", "status": status, "gen": 1,
        "rooms_expected": 1, "rooms_received": 1,
        "rooms": [room if room is not None else _room()],
    }


@pytest.fixture
def env(monkeypatch):
    guild = SimpleNamespace(
        id=1, roles=[],
        get_role=lambda rid: None,
        get_channel=lambda cid: None,
        fetch_channel=lambda cid: _noop(),
    )
    stats = SimpleNamespace(id=1)
    env = SimpleNamespace(guild=guild, stats=stats)
    monkeypatch.setattr(
        omv.store, "get",
        lambda gid, cid: getattr(env, "tournament", None),
    )
    monkeypatch.setattr(omv.store, "set", lambda t: None)
    return env


async def _noop():
    return None


def _attach_session(env, pair=(0, 1), n_teams=8, room=None):
    t = _tournament(pair=pair, n_teams=n_teams)
    t.ocr_sessions["qualifier_0"] = _session(room)
    env.tournament = t
    return t


def _team_buttons(view):
    return [
        c for c in view.children
        if getattr(c, "custom_id", "").startswith("room_map:")
    ]


# ---------- P0-1: кнопки команд ----------

class TestRoomViewTeamButtons:
    @pytest.mark.parametrize("pair", [(0, 1), (1, 2), (0, 7)])
    def test_both_team_buttons_visible(self, env, pair):
        t = _attach_session(env, pair=pair)
        view = omv.RoomView(1, 1, t, "qualifier", 0, 1)
        btns = _team_buttons(view)
        assert len(btns) == 2
        ids = {b.custom_id.rsplit(":", 1)[-1] for b in btns}
        assert ids == {str(pair[0]), str(pair[1])}
        # Понятные подписи с именем команды
        for b in btns:
            assert "Сопоставить" in b.label

    def test_match_tuple_failure_no_fake_buttons(self, env, caplog):
        """Битый match tuple → лог + без кнопок команд
        (не подменяем обе индексом 0)."""
        t = _attach_session(env)
        t.qualifier_matches = []  # IndexError внутри _match_tuple
        view = omv.RoomView(1, 1, t, "qualifier", 0, 1)
        assert _team_buttons(view) == []
        # self/wrong/admin кнопки остаются
        assert len(view.children) == 3

    @pytest.mark.asyncio
    async def test_button_callbacks_carry_team_index(self, env):
        """Каждая кнопка вызывает _on_team со СВОИМ team_index —
        в т.ч. team_index=0."""
        t = _attach_session(env, pair=(0, 7))
        view = omv.RoomView(1, 1, t, "qualifier", 0, 1)
        btns = _team_buttons(view)
        assert len(btns) == 2
        seen: list[int] = []

        async def fake_on_team(ix, ti):
            seen.append(ti)

        view._on_team = fake_on_team
        ix = _Ix(env)
        for b in btns:
            await b.callback(ix)
        assert sorted(seen) == [0, 7]


# ---------- P1-5: compact mapping view ----------

class TestRoomTeamMapView:
    def test_four_selects_valid_options(self, env):
        t = _attach_session(env)
        view = omv.RoomTeamMapView(
            1, 1, t, "qualifier", 0, 1, 0, "left",
        )
        sels = [c for c in view.children if isinstance(c, omv.Select)]
        assert len(sels) == 4
        for sel in sels:
            values = [o.value for o in sel.options]
            # Discord API требует value 1..100 chars — sentinel
            # вместо пустой строки.
            assert all(0 < len(v) <= 100 for v in values)
            assert "__none__" in values
            # Roster команды 0 доступен для выбора
            assert any(v.startswith("T0p") for v in values)

    @pytest.mark.asyncio
    async def test_save_binds_discord_ids(self, env):
        """Выбор 4 строк → save → discord_id проставлены по
        player_user_ids; повторное открытие видит привязки."""
        t = _attach_session(env, pair=(0, 1))
        view = omv.RoomTeamMapView(
            1, 1, t, "qualifier", 0, 1, 0, "left",
        )
        # Каждой строке — свой Discord-игрок команды 0.
        for i, sel in view.selects.items():
            sel._values = [f"T0p{i}"]
        # user — участник команды 0 (uid T0p0)
        ix = _Ix(env, user_id=1000)
        await view._on_save(ix)
        room = t.ocr_sessions["qualifier_0"]["rooms"][0]
        rows = room["maps"]["0"]["rows"]
        assert all(r.get("discord_id") for r in rows)
        assert str(rows[0]["discord_id"]) == "1000"
        assert str(rows[1]["discord_id"]) == "1001"
        assert room["maps"]["0"]["confirmed"] is True
        assert any("сохранено" in m for m in ix.messages)

    @pytest.mark.asyncio
    async def test_same_discord_id_two_rows_rejected(self, env):
        """Один Discord ID нельзя назначить двум строкам комнаты."""
        t = _attach_session(env, pair=(0, 1))
        view = omv.RoomTeamMapView(
            1, 1, t, "qualifier", 0, 1, 0, "left",
        )
        # Строка 1 команды 1 уже занята игроком T0p0 (uid 1000)
        # в ДРУГОЙ команде той же комнаты — forbidden в select'ах
        # не сработает (другой roster), но used-check в save —
        # здесь эмулируем: обе строки команды 0 → один uid.
        # Проще: две строки maps["0"] с одинаковым discord_id.
        room = t.ocr_sessions["qualifier_0"]["rooms"][0]
        room["maps"]["1"]["rows"][0]["discord_id"] = "1000"
        room["maps"]["1"]["rows"][0]["discord_name"] = "T0p0"
        for i, sel in view.selects.items():
            sel._values = ["T0p0" if i == 0 else f"T0p{i}"]
        ix = _Ix(env, user_id=1000)
        await view._on_save(ix)
        # Строка 0 отклонена (uid 1000 уже в maps["1"]), остальные ок
        rows = room["maps"]["0"]["rows"]
        assert rows[0].get("discord_id") in (None, "")
        assert any("уже привязан" in m for m in ix.messages)

    @pytest.mark.asyncio
    async def test_on_team_error_responds_not_hangs(self, env):
        """Исключение внутри _on_team → пользователю уходит
        понятное сообщение, interaction не остаётся без ответа."""
        t = _attach_session(env)
        view = omv.RoomView(1, 1, t, "qualifier", 0, 1)

        async def boom(ix, ti):
            raise RuntimeError("discord exploded")

        # Ошибка в _on_team_inner — оборачиваем через патч store
        orig_get = omv.store.get
        def bad_get(gid, cid):
            raise RuntimeError("store dead")
        omv.store.get = bad_get
        try:
            btn = _team_buttons(view)[0]
            ix = _Ix(env)
            await btn.callback(ix)
        finally:
            omv.store.get = orig_get
        assert ix.messages  # ответ отправлен, не молчание
