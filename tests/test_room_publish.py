"""Публикация карточек «КОМНАТА ОТКРЫТА» — services/room_publisher.

Покрывает: CV2-карточка в канале данных (команды, 8 mentions,
пароль, кнопка ID), уведомление в общий чат (без ID/пароля,
ссылки), однократность уведомления, редактирование карточки
при обновлении, pending-recovery после «рестарта», изоляция
параллельных матчей.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from models.tournament import Tournament, TournamentPhase
from storage.room_store import RoomStore, match_key, room_store as _rs
from services import room_publisher as rp
from views.room_view import iter_tournament_matches, RoomModal


# ---------------------------------------------------------------------------
# Фейки Discord
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, mid: int, view=None):
        self.id = mid
        self.view = view
        self.edits = []
        self.allowed_mentions = None

    async def edit(self, view=None, allowed_mentions=None, **kw):
        self.edits.append(view)
        self.view = view
        self.allowed_mentions = allowed_mentions
        return self


class _FakeChannel:
    def __init__(self, cid: int):
        self.id = cid
        self.sent = []          # [(view, allowed_mentions)]
        self._next = 1000
        self.deleted = set()

    async def send(self, view=None, allowed_mentions=None, **kw):
        self._next += 1
        msg = _FakeMessage(self._next, view)
        msg.allowed_mentions = allowed_mentions
        self.sent.append(msg)
        return msg

    async def fetch_message(self, mid: int):
        for m in self.sent:
            if m.id == mid and mid not in self.deleted:
                return m
        raise RuntimeError("not found")


class _FakeBot:
    def __init__(self, channels: dict[int, _FakeChannel]):
        self._channels = channels

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def fetch_channel(self, cid):
        ch = self._channels.get(cid)
        if ch is None:
            raise RuntimeError("no channel")
        return ch


def _view_text(view) -> str:
    parts = []
    stack = list(getattr(view, "children", []))
    while stack:
        item = stack.pop()
        c = getattr(item, "content", None)
        if isinstance(c, str):
            parts.append(c)
        stack.extend(getattr(item, "children", []) or [])
    return "\n".join(parts)


def _custom_ids(view) -> list[str]:
    out = []
    stack = list(getattr(view, "children", []))
    while stack:
        item = stack.pop()
        cid = getattr(item, "custom_id", None)
        if cid:
            out.append(cid)
        stack.extend(getattr(item, "children", []) or [])
    return out


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


def _tournament() -> Tournament:
    t = Tournament(guild_id=1, channel_id=2)
    t.id = "t1"
    t.phase = TournamentPhase.SEMIFINALS
    t.teams = [
        {"captain": f"Cap{i}",
         "players": [f"P{i}a", f"P{i}b", f"P{i}c", f"P{i}d"]}
        for i in range(4)
    ]
    t.team_names = {i: f"Team{i}" for i in range(4)}
    t.player_user_ids = {
        f"P{i}{s}": 100 + i * 10 + j
        for i in range(4) for j, s in enumerate("abcd")
    }
    for i in range(4):
        t.player_user_ids[f"Cap{i}"] = 200 + i
    t.semifinal_matches = [(0, 1), (2, 3)]
    t.semifinal_winners = [None, None]
    t.final_teams = [0, 2]
    return t


@pytest.fixture
def room_store_tmp(tmp_path, monkeypatch):
    rs = RoomStore(tmp_path / "rooms.json")
    monkeypatch.setattr(rp, "room_store", rs)
    monkeypatch.setattr("views.room_view.room_store", rs)
    monkeypatch.setattr("storage.room_store.room_store", rs)
    return rs


@pytest.fixture
def channels(monkeypatch):
    data_ch = _FakeChannel(1561683314480255057)
    notify_ch = _FakeChannel(1200125075156910181)
    monkeypatch.setattr(rp, "ROOM_DATA_CHANNEL_ID", data_ch.id)
    monkeypatch.setattr(rp, "ROOM_OPEN_NOTIFY_CHANNEL_ID", notify_ch.id)
    return {data_ch.id: data_ch, notify_ch.id: notify_ch}


def _bot(channels):
    return _FakeBot(channels)


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_creates_card_and_notification(
        room_store_tmp, channels):
    t = _tournament()
    m = iter_tournament_matches(t)[0]
    res = room_store_tmp.upsert_room(
        1, 2, m["key"], "555", "pw1", 101, tournament_id="t1")
    room = res["room"]

    out = await rp.publish_room(_bot(channels), t, m, room)

    assert out["card"] and out["notified"]
    data_ch = channels[1561683314480255057]
    notify_ch = channels[1200125075156910181]
    assert len(data_ch.sent) == 1
    assert len(notify_ch.sent) == 1

    card_text = _view_text(data_ch.sent[0].view)
    assert "КОМНАТА ОТКРЫТА" in card_text
    assert "Team0" in card_text and "Team1" in card_text
    for uid in (100, 101, 102, 103, 110, 111, 112, 113):
        assert f"<@{uid}>" in card_text
    assert "pw1" in card_text                     # пароль в карточке
    assert any(c.startswith("rcard:id:")
               for c in _custom_ids(data_ch.sent[0].view))

    note_text = _view_text(notify_ch.sent[0].view)
    assert "КОМНАТА ОТКРЫТА" in note_text
    assert "Team0 vs Team1" in note_text
    assert "555" not in note_text and "pw1" not in note_text
    assert f"<#{data_ch.id}>" in note_text
    assert f"/{data_ch.id}/{data_ch.sent[0].id}" in note_text  # jump-ссылка
    for uid in (100, 101, 102, 103, 110, 111, 112, 113):
        assert f"<@{uid}>" in note_text

    active = room_store_tmp.get_active_room(1, 2, m["key"])
    assert active["data_message_id"] == data_ch.sent[0].id
    assert active["notification_message_id"] == notify_ch.sent[0].id
    assert not active.get("card_pending")
    assert not active.get("notify_required")


@pytest.mark.asyncio
async def test_update_edits_card_no_second_notification(
        room_store_tmp, channels):
    t = _tournament()
    m = iter_tournament_matches(t)[0]
    room_store_tmp.upsert_room(
        1, 2, m["key"], "555", "pw1", 101, tournament_id="t1")
    bot = _bot(channels)
    await rp.publish_room(
        bot, t, m,
        room_store_tmp.get_active_room(1, 2, m["key"]))

    # Изменение пароля → подтверждение → повторная публикация.
    room_store_tmp.upsert_room(
        1, 2, m["key"], "555", "pw2", 105, tournament_id="t1")
    out = await rp.publish_room(
        bot, t, m,
        room_store_tmp.get_active_room(1, 2, m["key"]))

    data_ch = channels[1561683314480255057]
    assert out["card"] and not out["notified"]
    assert len(data_ch.sent) == 1               # карточка не дублируется
    card_msg = data_ch.sent[0]
    assert len(card_msg.edits) == 1             # отредактирована
    assert "pw2" in _view_text(card_msg.view)
    assert len(channels[1200125075156910181].sent) == 1  # 1 уведомление


@pytest.mark.asyncio
async def test_failed_publish_sets_pending_and_retries(
        room_store_tmp, channels, monkeypatch):
    t = _tournament()
    m = iter_tournament_matches(t)[0]
    room_store_tmp.upsert_room(
        1, 2, m["key"], "555", "pw1", 101, tournament_id="t1")

    # Канал данных недоступен → pending.
    monkeypatch.setattr(rp, "ROOM_DATA_CHANNEL_ID", 999)
    out = await rp.publish_room(
        _bot(channels), t, m,
        room_store_tmp.get_active_room(1, 2, m["key"]))
    assert not out["card"]
    active = room_store_tmp.get_active_room(1, 2, m["key"])
    assert active.get("card_pending")

    # Retry после «перезапуска»: канал вернулся.
    monkeypatch.setattr(rp, "ROOM_DATA_CHANNEL_ID",
                        1561683314480255057)
    # publish_room напрямую (retry_pending_rooms требует store).
    out2 = await rp.publish_room(
        _bot(channels), t, m,
        room_store_tmp.get_active_room(1, 2, m["key"]))
    assert out2["card"] and out2["notified"]
    assert not room_store_tmp.get_active_room(
        1, 2, m["key"]).get("card_pending")


@pytest.mark.asyncio
async def test_parallel_matches_isolated(room_store_tmp, channels):
    t = _tournament()
    m0, m1 = iter_tournament_matches(t)
    bot = _bot(channels)
    for m, rid, pw in ((m0, "111", "p1"), (m1, "222", "p2")):
        room_store_tmp.upsert_room(
            1, 2, m["key"], rid, pw, 101, tournament_id="t1")
        await rp.publish_room(
            bot, t, m,
            room_store_tmp.get_active_room(1, 2, m["key"]))

    data_ch = channels[1561683314480255057]
    assert len(data_ch.sent) == 2
    texts = [_view_text(msg.view) for msg in data_ch.sent]
    assert any("Team0" in x and "Team1" in x and "p1" in x
               for x in texts)
    assert any("Team2" in x and "Team3" in x and "p2" in x
               for x in texts)
    # У каждого матча свой data_message_id.
    a0 = room_store_tmp.get_active_room(1, 2, m0["key"])
    a1 = room_store_tmp.get_active_room(1, 2, m1["key"])
    assert a0["data_message_id"] != a1["data_message_id"]


def test_modal_has_no_room_number_field():
    t = _tournament()
    m = iter_tournament_matches(t)[0]
    modal = RoomModal(1, 2, t, m)
    labels = [c.label for c in modal.children
              if getattr(c, "label", None)]
    assert labels == ["ID комнаты", "Пароль комнаты"]


def test_id_button_returns_room_id():
    """RoomIdBtn отдаёт только ID ephemeral."""
    import asyncio
    from services.room_publisher import RoomIdBtn, T_ROOM_ID

    assert T_ROOM_ID.match(
        "rcard:id:1:2:t1:semifinal:0")

    sent = {}

    class _Resp:
        async def send_message(self, content=None, **kw):
            sent["content"] = content
            sent["ephemeral"] = kw.get("ephemeral")

    class _IC:
        data = {"custom_id": "rcard:id:1:2:t1:semifinal:0"}
        response = _Resp()

    import asyncio
    btn = RoomIdBtn("rcard:id:1:2:t1:semifinal:0")
    # Без записи в store — «не найдены».
    asyncio.run(btn.callback(_IC()))
    assert "не найдены" in sent["content"]
