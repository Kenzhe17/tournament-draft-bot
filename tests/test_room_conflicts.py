"""Тесты блокировки финализации неразрешёнными конфликтами
classic/squad и безопасного aux-заполнения (mixed room).

Проверяем:
- check_ocr_session_ready → "conflicts_unresolved";
- resolve_room_conflict: classic/squad, метаданные решения,
  повторный вызов идемпотентен, блокировка снимается после
  разрешения ВСЕХ конфликтов;
- автозаполнение None только при уверенном совпадении
  (уникальное имя + одна команда); спорные случаи → aux_review;
- откат/отмена сессии убирает конфликты из гейта.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cogs.ocr_upload as up
import views.ocr_match_view as omv


# ---------------------------------------------------------------------------
# Хелперы сессии (формат rooms)
# ---------------------------------------------------------------------------


def _bound_row(name, ocr, k, d, a, dmg, uid):
    return {
        "ocr_name": ocr, "kills": k, "deaths": d, "assists": a,
        "dmg": dmg, "discord_name": name, "discord_id": uid,
        "discord_display": name, "source": "self",
    }


def _team_rows(base_uid, ocr_prefix, uid_offset=0):
    return [
        _bound_row(f"P{base_uid + i}", f"{ocr_prefix}{i}",
                   1, 1, 1, 10, base_uid + i)
        for i in range(4)
    ]


def _panel_rows(names_stats):
    return [
        {"name": n, "kills": k, "deaths": d, "assists": a, "dmg": dmg}
        for n, k, d, a, dmg in names_stats
    ]


def _room(i, kind="mixed", panel_rows=None, conflicts=None,
          aux_squads=None, aux_review=None):
    panels = {
        "left": {"name": "TA", "rows": (panel_rows or {}).get("left", [])},
        "right": {"name": "TB", "rows": (panel_rows or {}).get("right", [])},
    }
    room = {
        "room_index": i,
        "sha": f"sha{i}",
        "shas": [],
        "kind": kind,
        "status": "awaiting_mapping",
        "panels": panels,
        "banners": {},
        "maps": {
            "0": {"panel": "left", "rows": _team_rows(1, "NA"),
                  "confirmed": True},
            "1": {"panel": "right", "rows": _team_rows(5, "NB"),
                  "confirmed": True},
        },
        "panel_picks": {},
        "winner_side": None,
        "banner": None,
        "score": None,
        "top5": False,
        "message_id": 100 + i,
        "message_ids": [100 + i],
        "uploaded_at": "2026-01-01T00:00:00",
    }
    if conflicts:
        room["stat_conflicts"] = conflicts
    if aux_squads:
        room["aux_squads"] = aux_squads
    if aux_review:
        room["aux_review"] = aux_review
    return room


def _session(rooms):
    return {
        "mode": "rooms",
        "match_type": "qualifier",
        "match_index": 0,
        "status": "awaiting_mapping",
        "rooms_expected": len(rooms),
        "rooms_received": len(rooms),
        "rooms": rooms,
        "uploaded_by": 999,
        "uploaded_by_name": "cap",
        "winner_pending": False,
        "winning_team_index": 0,
        "teams": [0, 1],
        "maps": {},
        "panel_picks": {},
        "gen": 1,
    }


def _tournament(session):
    names = [f"P{i}" for i in range(1, 9)]
    return SimpleNamespace(
        channel_id=1,
        guild_id=1,
        teams=[
            {"players": names[0:4], "captain": "P1"},
            {"players": names[4:8], "captain": "P5"},
        ],
        player_user_ids={n: i + 1 for i, n in enumerate(names)},
        team_names={0: "TeamA", 1: "TeamB"},
        mm_search_channel_id=None,
        ocr_sessions={"qualifier_0": session},
    )


@pytest.fixture
def store_env(monkeypatch):
    """Подменённый store для resolve_room_conflict/check_ready."""
    holder = {}

    def _set(t):
        holder["t"] = t

    monkeypatch.setattr(omv.store, "get",
                        lambda gid, cid: holder.get("t"))
    monkeypatch.setattr(omv.store, "set", _set)
    monkeypatch.setattr(up.store, "get",
                        lambda gid, cid: holder.get("t"))
    monkeypatch.setattr(up.store, "set", _set)
    return holder


def _conflict(field="dmg", classic=100, squad=200, side="left",
              row=0, name="A1"):
    return {
        "side": side, "row": row, "name": name, "field": field,
        "classic": classic, "squad": squad, "resolved": False,
    }


# ---------------------------------------------------------------------------
# P0 — блокировка финализации
# ---------------------------------------------------------------------------


class TestConflictBlocksFinalize:
    def test_unresolved_conflict_blocks_ready(self):
        room = _room(1, conflicts=[_conflict()])
        session = _session([room])
        t = _tournament(session)
        ready, reason, _ = omv.check_ocr_session_ready(t, session, 0, 1)
        assert not ready
        assert reason == "conflicts_unresolved"

    @pytest.mark.asyncio
    async def test_resolved_conflict_unblocks(self, store_env):
        room = _room(
            1,
            panel_rows={
                "left": _panel_rows(
                    [("NA0", 1, 1, 1, 10), ("NA1", 1, 1, 1, 10),
                     ("NA2", 1, 1, 1, 10), ("NA3", 1, 1, 1, 10)]
                ),
                "right": _panel_rows(
                    [("NB0", 1, 1, 1, 10), ("NB1", 1, 1, 1, 10),
                     ("NB2", 1, 1, 1, 10), ("NB3", 1, 1, 1, 10)]
                ),
            },
            conflicts=[_conflict(field="dmg", classic=10, squad=99,
                                 name="NA0")],
        )
        # Конфликт на строке 0 левой панели — maps должны
        # ссылаться на те же bound-строки? maps независимы:
        # stats_complete смотрит maps rows, aggregate — тоже.
        session = _session([room])
        t = _tournament(session)
        store_env["t"] = t

        # До разрешения — блок
        ready, reason, _ = omv.check_ocr_session_ready(t, session, 0, 1)
        assert reason == "conflicts_unresolved"

        user = SimpleNamespace(id=555, display_name="Admin",
                               name="admin")
        res = await omv.resolve_room_conflict(
            1, 1, "qualifier", 0, 1, 0, "classic", user
        )
        assert res == "ok"

        c = room["stat_conflicts"][0]
        assert c["resolved"] is True
        assert c["source"] == "classic"
        assert c["value"] == 10
        assert c["by"] == 555
        assert c["by_name"] == "Admin"
        assert c["at"]

        ready, reason, _ = omv.check_ocr_session_ready(t, session, 0, 1)
        assert ready, reason

    @pytest.mark.asyncio
    async def test_multiple_conflicts_block_until_all_resolved(
        self, store_env
    ):
        rows = {
            "left": _panel_rows(
                [("A1", 5, 2, 1, 100), ("A2", 1, 1, 1, 50),
                 ("A3", 1, 1, 1, 10), ("A4", 1, 1, 1, 10)]
            ),
            "right": _panel_rows(
                [("B1", 1, 1, 1, 10), ("B2", 1, 1, 1, 10),
                 ("B3", 1, 1, 1, 10), ("B4", 1, 1, 1, 10)]
            ),
        }
        room = _room(1, panel_rows=rows, conflicts=[
            _conflict(field="dmg", row=0, classic=100, squad=200),
            _conflict(field="kills", row=1, classic=5, squad=9),
        ])
        session = _session([room])
        t = _tournament(session)
        store_env["t"] = t

        ready, reason, _ = omv.check_ocr_session_ready(t, session, 0, 1)
        assert reason == "conflicts_unresolved"

        user = SimpleNamespace(id=1, display_name="A", name="a")
        await omv.resolve_room_conflict(
            1, 1, "qualifier", 0, 1, 0, "squad", user)
        ready, reason, _ = omv.check_ocr_session_ready(t, session, 0, 1)
        assert reason == "conflicts_unresolved"  # второй ещё открыт

        await omv.resolve_room_conflict(
            1, 1, "qualifier", 0, 1, 1, "classic", user)
        ready, reason, _ = omv.check_ocr_session_ready(t, session, 0, 1)
        assert ready, reason

    @pytest.mark.asyncio
    async def test_squad_source_writes_squad_value(self, store_env):
        rows = {"left": _panel_rows([("A1", 5, 2, 1, 100)]),
                "right": _panel_rows([("B1", 1, 1, 1, 10)])}
        room = _room(1, panel_rows=rows, conflicts=[
            _conflict(field="dmg", classic=100, squad=3200, name="A1")])
        session = _session([room])
        t = _tournament(session)
        store_env["t"] = t

        user = SimpleNamespace(id=9, display_name="Mod",
                               name="mod")
        await omv.resolve_room_conflict(
            1, 1, "qualifier", 0, 1, 0, "squad", user)
        row = room["panels"]["left"]["rows"][0]
        assert row["dmg"] == 3200
        # Mapped-строка (источник агрегации) обновлена тоже
        assert room["maps"]["0"]["rows"][0]["dmg"] == 3200

    @pytest.mark.asyncio
    async def test_repeat_resolution_idempotent(self, store_env):
        rows = {"left": _panel_rows([("A1", 5, 2, 1, 100)]),
                "right": _panel_rows([("B1", 1, 1, 1, 10)])}
        room = _room(1, panel_rows=rows, conflicts=[
            _conflict(field="dmg", classic=100, squad=3200, name="A1")])
        session = _session([room])
        t = _tournament(session)
        store_env["t"] = t

        user = SimpleNamespace(id=9, display_name="Mod", name="mod")
        await omv.resolve_room_conflict(
            1, 1, "qualifier", 0, 1, 0, "squad", user)
        # Повторный callback (двойной клик/ретрай) — без эффекта
        res = await omv.resolve_room_conflict(
            1, 1, "qualifier", 0, 1, 0, "classic", user)
        assert res == "resolved"
        assert room["panels"]["left"]["rows"][0]["dmg"] == 3200
        c = room["stat_conflicts"][0]
        assert c["source"] == "squad"  # первое решение сохранено

    @pytest.mark.asyncio
    async def test_closed_session_rejects_resolution(self, store_env):
        room = _room(1, conflicts=[_conflict()])
        session = _session([room])
        session["status"] = "completed"
        t = _tournament(session)
        store_env["t"] = t
        user = SimpleNamespace(id=1, display_name="A", name="a")
        res = await omv.resolve_room_conflict(
            1, 1, "qualifier", 0, 1, 0, "classic", user)
        assert res == "closed"
        assert room["stat_conflicts"][0]["resolved"] is False

    def test_cancelled_session_conflicts_not_consulted(self):
        """Откат комплекта: cancelled-сессия → 'closed', конфликты
        не блокируют и не учитываются."""
        room = _room(1, conflicts=[_conflict()])
        session = _session([room])
        session["status"] = "cancelled"
        t = _tournament(session)
        ready, reason, _ = omv.check_ocr_session_ready(t, session, 0, 1)
        assert reason == "closed"


# ---------------------------------------------------------------------------
# P0 — безопасное автозаполнение
# ---------------------------------------------------------------------------


class TestSafeAuxFill:
    def _env(self):
        rows_a = [
            {"name": "Hero", "kills": 5, "deaths": 2, "assists": 1,
             "dmg": None},
            {"name": "Mate", "kills": 1, "deaths": 3, "assists": 0,
             "dmg": 50},
        ]
        rows_b = [
            {"name": "Enemy1", "kills": 2, "deaths": 4, "assists": 1,
             "dmg": 60},
            {"name": "Enemy2", "kills": 0, "deaths": 5, "assists": 2,
             "dmg": 70},
        ]
        return {
            "panels": {
                "left": {"name": "TA", "rows": rows_a},
                "right": {"name": "TB", "rows": rows_b},
            }
        }

    def _aux(self, players):
        return {"sha": "s", "players": players, "banner": None}

    def _player(self, name, k=1, d=1, a=1, dmg=100):
        return {"name": name, "kills": k, "deaths": d,
                "assists": a, "dmg": dmg}

    def test_fill_single_team_confident(self):
        """Уникальное имя + все совпадения отряда в одной панели →
        None заполняется."""
        room = {"panels": self._env()["panels"], "aux_squads": [
            self._aux([self._player("Hero", dmg=999),
                       self._player("Mate", dmg=50)])
        ]}
        up._reconcile_mixed_squad(room)
        row = room["panels"]["left"]["rows"][0]
        assert row["dmg"] == 999
        assert room.get("aux_filled")
        assert not room.get("aux_review")

    def test_same_name_both_panels_no_fill(self):
        """Имя встречается на обеих панелях → неоднозначно:
        автозаполнения нет, запись уходит в aux_review."""
        env = self._env()
        env["panels"]["right"]["rows"].append(
            {"name": "Hero", "kills": 9, "deaths": 9, "assists": 9,
             "dmg": 9}
        )
        room = {"panels": env["panels"], "aux_squads": [
            self._aux([self._player("Hero", dmg=999)])
        ]}
        up._reconcile_mixed_squad(room)
        assert env["panels"]["left"]["rows"][0]["dmg"] is None
        assert room.get("aux_review")
        assert room["aux_review"][0]["reason"] == "no_unique_match"

    def test_squad_spanning_teams_no_fill(self):
        """Отряд «размазался» по обеим панелям — принадлежность
        к команде не подтверждена: НИЧЕГО не заполняем."""
        room = {"panels": self._env()["panels"], "aux_squads": [
            self._aux([self._player("Hero", dmg=999),
                       self._player("Enemy1", dmg=888)])
        ]}
        up._reconcile_mixed_squad(room)
        assert room["panels"]["left"]["rows"][0]["dmg"] is None
        assert not room.get("aux_filled")
        assert len(room.get("aux_review") or []) == 2
        assert all(r["reason"] == "squad_spans_both_teams"
                   for r in room["aux_review"])

    def test_no_conflicts_normal_path(self):
        """Совпадающие значения — ни конфликтов, ни review."""
        room = {"panels": self._env()["panels"], "aux_squads": [
            self._aux([self._player("Hero", k=5, d=2, a=1, dmg=100),
                       self._player("Mate", k=1, d=3, a=0, dmg=50)])
        ]}
        # Hero.dmg=None в classic vs squad 100 → заполнение (conflict
        # нет, т.к. classic None); остальные равны.
        up._reconcile_mixed_squad(room)
        assert not room.get("stat_conflicts")
        assert room["panels"]["left"]["rows"][0]["dmg"] == 100
