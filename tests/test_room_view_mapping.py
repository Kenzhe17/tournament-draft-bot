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


# ---------- Per-room mapping (комната финализирована ≠ весь комплект) ----------

def _partial_session(env, n_rooms=3):
    """rooms_expected=3: комната 1 завершена+panels, 2–3 собираются."""
    t = _tournament(pair=(0, 1), n_teams=8)
    r1 = _room(0, 1)
    r1["room_index"] = 1
    rooms = [r1]
    for i in range(2, n_rooms + 1):
        rooms.append({
            "room_index": i, "status": "collecting_files",
            "pending": [{"sha": f"s{i}", "kind": "classic",
                         "pipe": {}, "shot_message_id": None}],
        })
    t.ocr_sessions["qualifier_0"] = {
        "mode": "rooms", "status": "collecting", "gen": 1,
        "rooms_expected": n_rooms, "rooms_received": 1,
        "rooms": rooms,
    }
    env.tournament = t
    return t


class TestPerRoomMapping:
    @pytest.mark.asyncio
    async def test_room1_of_3_mapping_allowed(self, env):
        """Комната 1/3 завершена → кнопка команды открывает
        mapping, глобального rooms_expected-гейта нет."""
        t = _partial_session(env)
        view = omv.RoomView(1, 1, t, "qualifier", 0, 1)
        btns = _team_buttons(view)
        assert len(btns) == 2
        ix = _Ix(env, user_id=1000)  # участник команды 0
        await btns[0].callback(ix)
        # Открыт mapping (view) или panel-pick — НЕ «дождитесь
        # полного комплекта».
        assert not any("полного комплекта" in m for m in ix.messages)
        assert ix.last_view is not None or ix.messages

    @pytest.mark.asyncio
    async def test_unfinished_room_rejected(self, env):
        """Комната без panels → mapping отклонён."""
        t = _partial_session(env)
        view = omv.RoomView(1, 1, t, "qualifier", 0, 2)
        btns = _team_buttons(view)
        ix = _Ix(env, user_id=1000)
        await btns[0].callback(ix)
        assert any("не завершена" in m for m in ix.messages)

    def test_rooms_all_mapped_partial_false(self, env):
        """1/3 комнат → _rooms_all_mapped False даже если комната
        сопоставлена; все 3 → True."""
        t = _partial_session(env)
        sess = t.ocr_sessions["qualifier_0"]
        assert omv._rooms_all_mapped(sess, 0, 1) is False
        # Финализируем «виртуально» комнаты 2,3 и привязываем всех
        for i in (2, 3):
            r = sess["rooms"][i - 1]
            r["panels"] = {
                "left": {"name": "L", "rows": [_prow(f"L{i}", j) for j in range(4)]},
                "right": {"name": "R", "rows": [_prow(f"R{i}", j) for j in range(4)]},
            }
            for ti, panel in (("0", "left"), ("1", "right")):
                r["maps"] = r.get("maps") or {}
                r["maps"][ti] = {
                    "panel": panel, "confirmed": True,
                    "rows": [
                        {**_map_rows("X")[j],
                         "discord_name": f"T{ti}p{j}",
                         "discord_id": str(1000 + int(ti) * 10 + j),
                         "source": "captain"}
                        for j in range(4)
                    ],
                }
        # Комната 1 тоже должна быть полностью привязана
        r1 = sess["rooms"][0]
        for ti, panel in (("0", "left"), ("1", "right")):
            r1["maps"][ti]["rows"] = [
                {**row,
                 "discord_name": f"T{ti}p{j}",
                 "discord_id": str(1000 + int(ti) * 10 + j),
                 "source": "captain"}
                for j, row in enumerate(r1["maps"][ti]["rows"])
            ]
        assert omv._rooms_all_mapped(sess, 0, 1) is True
        ready, reason, _ = omv.check_ocr_session_ready(
            t, sess, 0, 1, require_winner=False,
        )
        assert reason != "unmapped" or ready

    @pytest.mark.asyncio
    async def test_unbind_via_none(self, env):
        """«— не назначен —» (__none__) реально снимает привязку:
        строка очищается, confirmed=False, _rooms_all_mapped →
        False до повторного полного сопоставления."""
        t = _attach_session(env, pair=(0, 1))
        sess = t.ocr_sessions["qualifier_0"]
        room = sess["rooms"][0]
        # Полностью привязать команду 0
        view = omv.RoomTeamMapView(1, 1, t, "qualifier", 0, 1, 0, "left")
        for i, sel in view.selects.items():
            sel._values = [f"T0p{i}"]
        ix = _Ix(env, user_id=1000)
        await view._on_save(ix)
        assert room["maps"]["0"]["confirmed"] is True

        # Снять привязку строки 0 явным __none__
        view2 = omv.RoomTeamMapView(1, 1, t, "qualifier", 0, 1, 0, "left")
        for i, sel in view2.selects.items():
            sel._values = [f"T0p{i}"] if i != 0 else ["__none__"]
        await view2._on_save(_Ix(env, user_id=1000))
        rows = room["maps"]["0"]["rows"]
        assert rows[0].get("discord_id") in (None, "")
        assert rows[0].get("discord_name") is None
        assert room["maps"]["0"]["confirmed"] is False
        # Строки 1..3 остались привязанными
        assert rows[1].get("discord_id")
        # Готовность комнаты сломана
        assert omv._classic_mapping_complete(
            omv._room_session(sess, 1), 0, 1
        ) is False

    @pytest.mark.asyncio
    async def test_untouched_select_does_not_unbind(self, env):
        """Пустой values (select не трогали) ≠ намеренное снятие:
        привязка сохраняется."""
        t = _attach_session(env, pair=(0, 1))
        sess = t.ocr_sessions["qualifier_0"]
        room = sess["rooms"][0]
        view = omv.RoomTeamMapView(1, 1, t, "qualifier", 0, 1, 0, "left")
        for i, sel in view.selects.items():
            sel._values = [f"T0p{i}"]
        await view._on_save(_Ix(env, user_id=1000))
        did = room["maps"]["0"]["rows"][0]["discord_id"]

        # Новый save: все selects без values (пользователь ничего
        # не менял) — привязки НЕ должны сброситься.
        view2 = omv.RoomTeamMapView(1, 1, t, "qualifier", 0, 1, 0, "left")
        await view2._on_save(_Ix(env, user_id=1000))
        assert room["maps"]["0"]["rows"][0]["discord_id"] == did


# ---------- 📣 Вызвать админа — только капитаны матча ----------

class TestAdminCallCaptainOnly:
    """room_admin / rooms_confirm admin: нажать могут только
    капитаны команд ЭТОГО матча (серверная проверка)."""

    def _captain_ix(self, env, user_id=1000):
        ix = _Ix(env, user_id=user_id)
        ix.user.display_name = "T0p0"  # капитан команды 0 (pair 0,1)
        return ix

    def _stranger_ix(self, env):
        ix = _Ix(env, user_id=5555)
        ix.user.display_name = "random_player"
        return ix

    @pytest.mark.asyncio
    async def test_room_admin_rejects_non_captain(self, env):
        _attach_session(env)
        view = omv.RoomView(1, 1, env.tournament, "qualifier", 0, 1)
        ix = self._stranger_ix(env)
        await view._on_admin(ix)
        assert any("только капитаны" in m for m in ix.messages)
        assert not any("Администратор уведомлён" in m for m in ix.messages)

    @pytest.mark.asyncio
    async def test_room_admin_allows_captain(self, env):
        _attach_session(env)
        view = omv.RoomView(1, 1, env.tournament, "qualifier", 0, 1)
        ix = self._captain_ix(env)
        await view._on_admin(ix)
        assert any("Администратор уведомлён" in m for m in ix.messages)

    @pytest.mark.asyncio
    async def test_confirm_admin_rejects_non_captain(self, env):
        _attach_session(env)
        view = omv.RoomsConfirmView(1, 1, env.tournament, "qualifier", 0)
        ix = self._stranger_ix(env)
        await view._on_admin(ix)
        assert any("только капитаны" in m for m in ix.messages)

    @pytest.mark.asyncio
    async def test_confirm_admin_allows_captain(self, env):
        _attach_session(env)
        view = omv.RoomsConfirmView(1, 1, env.tournament, "qualifier", 0)
        ix = self._captain_ix(env)
        await view._on_admin(ix)
        assert any("Администратор уведомлён" in m for m in ix.messages)

    @pytest.mark.asyncio
    async def test_captain_of_other_match_rejected(self, env):
        """Капитан команды 2 (другой матч) не может вызвать
        админа для qualifier_0 (команды 0 и 1)."""
        t = _attach_session(env)
        # qualifier_1: команды (2,3), капитан T2p0
        t.qualifier_matches = [(0, 1), (2, 3)]
        t.ocr_sessions["qualifier_1"] = _session(_room(2, 3))
        view = omv.RoomView(1, 1, t, "qualifier", 0, 1)
        ix = _Ix(env, user_id=1020)
        ix.user.display_name = "T2p0"
        await view._on_admin(ix)
        assert any("только капитаны" in m for m in ix.messages)
