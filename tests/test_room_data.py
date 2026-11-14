"""Данные комнат турнира (ID/пароль Free Fire) — регрессии.

Покрывает: перечисление матчей по фазам, участие по user_id,
независимость комнат разных матчей, идемпотентность upsert,
сохранение после «перезапуска» (reload из JSON), статусы в
публичной карточке и кнопки в фазовых View.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from models.tournament import Tournament, TournamentPhase
from storage.room_store import RoomStore, match_key
from views.room_view import (
    _match_title,
    _user_matches,
    iter_tournament_matches,
    build_room_card,
    room_buttons,
)


def _tournament(phase: TournamentPhase) -> Tournament:
    t = Tournament(guild_id=1, channel_id=2)
    t.id = "t1"
    t.phase = phase
    t.teams = [
        {"captain": f"Cap{i}", "players": [f"P{i}a", f"P{i}b", f"P{i}c", f"P{i}d"]}
        for i in range(4)
    ]
    t.team_names = {i: f"Team{i}" for i in range(4)}
    # user_id: Cap0->100, P0a->101 ... — маппинг имя->uid
    t.player_user_ids = {f"P{i}{s}": 100 + i * 10 + j for i in range(4)
                         for j, s in enumerate("abcd")}
    for i in range(4):
        t.player_user_ids[f"Cap{i}"] = 200 + i
    return t


def _semifinal_tournament() -> Tournament:
    t = _tournament(TournamentPhase.SEMIFINALS)
    t.semifinal_matches = [(0, 1), (2, 3)]
    t.semifinal_winners = [None, None]
    t.final_teams = [0, 2]
    return t


def test_iter_matches_qualifiers():
    t = _tournament(TournamentPhase.QUALIFIERS)
    t.qualifier_matches = [(0, 1), (2, 3)]
    t.qualifier_winners = [0, None]
    ms = iter_tournament_matches(t)
    assert len(ms) == 2
    assert ms[0]["active"] is False   # победитель подтверждён
    assert ms[1]["active"] is True    # матч ещё идёт
    assert ms[0]["key"].endswith("qualifier:0")
    assert ms[1]["key"].endswith("qualifier:1")


def test_iter_matches_final_and_complete():
    t = _tournament(TournamentPhase.FINAL)
    t.final_teams = [0, 2]
    ms = iter_tournament_matches(t)
    assert len(ms) == 1 and ms[0]["phase"] == "final" and ms[0]["active"]
    t.phase = TournamentPhase.COMPLETE
    t.winner_team_index = 0
    ms = iter_tournament_matches(t)
    assert ms[0]["active"] is False


def test_user_matches_by_user_id():
    t = _semifinal_tournament()
    # uid 101 = P0a → команда 0 → матч semifinal:0 активен
    ms = _user_matches(t, 101)
    assert len(ms) == 1 and ms[0]["index"] == 0
    # uid 203 = Cap2 → команда 2 → матч semifinal:1
    ms = _user_matches(t, 203)
    assert len(ms) == 1 and ms[0]["index"] == 1
    # посторонний
    assert _user_matches(t, 999999) == []


def test_user_cannot_touch_finished_match():
    t = _semifinal_tournament()
    t.semifinal_winners[0] = 0  # матч 0 завершён
    # игрок команды 0 больше не видит активных матчей
    assert _user_matches(t, 101) == []
    # но видит его в неактивном режиме (для просмотра данных)
    all_ms = _user_matches(t, 101, active_only=False)
    assert len(all_ms) == 1


def test_rooms_isolated_per_match(tmp_path):
    store = RoomStore(tmp_path / "rooms.json")
    k1 = match_key("t1", "semifinal", 0)
    k2 = match_key("t1", "semifinal", 1)
    store.upsert_room(1, 2, k1, "111", "pw1", 101)
    store.upsert_room(1, 2, k2, "222", "pw2", 203)
    assert store.get_room(1, 2, k1)["room_id"] == "111"
    assert store.get_room(1, 2, k2)["room_id"] == "222"
    assert store.get_room(1, 2, k1)["password"] == "pw1"


def test_upsert_updates_in_place(tmp_path):
    store = RoomStore(tmp_path / "rooms.json")
    k = match_key("t1", "final", 0)
    r1 = store.upsert_room(1, 2, k, "111", "pw1", 101)
    assert r1["created"] is True
    r2 = store.upsert_room(1, 2, k, "999", "pw9", 105)
    assert r2["created"] is False
    rooms = store.get_rooms(1, 2, k)
    assert len(rooms) == 1                      # без дублей
    assert rooms[0]["room_id"] == "999"
    assert rooms[0]["submitted_by"] == 101      # автор первой отправки
    assert rooms[0]["updated_by"] == 105        # кто изменил
    assert rooms[0]["updated_at"] >= rooms[0]["submitted_at"]


def test_room_no_multiple_rooms(tmp_path):
    store = RoomStore(tmp_path / "rooms.json")
    k = match_key("t1", "final", 0)
    store.upsert_room(1, 2, k, "111", "pw1", 101, room_no=1)
    store.upsert_room(1, 2, k, "222", "pw2", 101, room_no=2)
    rooms = store.get_rooms(1, 2, k)
    assert len(rooms) == 2
    assert store.get_room(1, 2, k, 1)["room_id"] == "111"
    assert store.get_room(1, 2, k, 2)["room_id"] == "222"


def test_persistence_reload(tmp_path):
    path = tmp_path / "rooms.json"
    s1 = RoomStore(path)
    s1.upsert_room(1, 2, match_key("t1", "qualifier", 0), "555", "pw", 101)
    s2 = RoomStore(path)  # «перезапуск»
    assert s2.get_room(1, 2, match_key("t1", "qualifier", 0))["room_id"] == "555"


def test_scope_isolation_between_channels(tmp_path):
    store = RoomStore(tmp_path / "rooms.json")
    k = match_key("t1", "final", 0)
    store.upsert_room(1, 2, k, "111", "pw", 101)
    assert store.get_rooms(1, 3, k) == []


def test_room_card_hides_password(tmp_path, monkeypatch):
    import views.room_view as rv

    monkeypatch.setattr(
        rv, "room_store", RoomStore(tmp_path / "rooms.json"))
    store = rv.room_store
    t = _semifinal_tournament()
    m = iter_tournament_matches(t)[0]
    store.upsert_room(1, 2, m["key"], "123456789", "secretpw", 101)

    card_pub = build_room_card(t, m, 1, 2, can_see_password=False)
    pub_text = "\n".join(
        getattr(i, "content", "")
        for i in _walk(card_pub)
    )
    assert "secretpw" not in pub_text
    assert "123456789" in pub_text
    assert "Комната готова" in pub_text

    card_priv = build_room_card(t, m, 1, 2, can_see_password=True)
    priv_text = "\n".join(
        getattr(i, "content", "")
        for i in _walk(card_priv)
    )
    assert "secretpw" in priv_text


def _walk(view):
    for item in getattr(view, "children", []):
        yield item
        yield from _walk(item)


def test_phase_views_have_room_buttons():
    from views.matches_view import QualifiersView, SemifinalsView
    from views.final_view import FinalView

    t = _tournament(TournamentPhase.QUALIFIERS)
    t.qualifier_matches = [(0, 1)]
    t.qualifier_winners = [None]
    v = QualifiersView(1, 2, t.qualifier_matches, t.qualifier_winners, t)
    labels = [getattr(i, "label", "") for i in v.children]
    assert "Отправить комнату" in labels
    assert "Данные комнаты" in labels
    assert "Управление" in labels

    t2 = _semifinal_tournament()
    v2 = SemifinalsView(1, 2, t2.semifinal_matches,
                        t2.semifinal_winners, t2)
    labels2 = [getattr(i, "label", "") for i in v2.children]
    assert "Отправить комнату" in labels2

    t3 = _tournament(TournamentPhase.FINAL)
    t3.final_teams = [0, 1]
    v3 = FinalView(1, 2, t3.final_teams, t3)
    labels3 = [getattr(i, "label", "") for i in v3.children]
    assert "Отправить комнату" in labels3


def test_room_status_suffix(tmp_path, monkeypatch):
    import utils.embeds as embeds
    from storage import room_store as rs

    monkeypatch.setattr(rs, "room_store", RoomStore(tmp_path / "r.json"))
    # embeds импортирует room_store лениво из storage.room_store —
    # патчим модульный синглтон
    import storage.room_store as room_store_mod
    monkeypatch.setattr(room_store_mod, "room_store",
                        RoomStore(tmp_path / "r2.json"))

    t = _semifinal_tournament()
    key = match_key("t1", "semifinal", 0)
    room_store_mod.room_store.upsert_room(1, 2, key, "1", "p", 101)
    s = embeds._room_status_suffix(t, "semifinal", 0)
    assert "Комната готова" in s
    s2 = embeds._room_status_suffix(t, "semifinal", 1)
    assert "Ожидаются данные" in s2
