"""Регрессии уведомлений «Комната открыта».

- MM (/mm squad): карточка + ID/пароль отдельными сообщениями —
  всё в канал матча, общий канал уведомлений не используется.
- Обычный турнир: карточка с данными в канал результатов,
  анонс с ролью в TextDisplay V2-карточки (пинг работает).
- Карточка содержит «КОМНАТА ОТКРЫТА», матч, упоминания, «Данные:».
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from views import room_modal as rm


def _tournament(mm: bool = True):
    return SimpleNamespace(
        teams=[
            {"captain": "CapA", "players": ["CapA", "P1", "P2", "P3"]},
            {"captain": "CapB", "players": ["CapB", "Q1", "Q2", "Q3"]},
        ],
        player_user_ids={
            "CapA": 111, "P1": 113, "P2": 114, "P3": 115,
            "CapB": 222, "Q1": 223, "Q2": 224, "Q3": 225,
        },
        team_names={},
        mm_search_channel_id=555 if mm else 0,
        mm_preset_channel_id=0,
        guild_id=1,
        channel_id=2,
        qualifier_rooms={},
        semifinal_rooms={},
        final_room=None,
        qualifier_matches=[(0, 1)],
        semifinal_matches=[],
        final_teams=[],
        match_channels={
            "900": {
                "match_type": "qualifier",
                "match_index": 0,
                "matches": [["qualifier", 0]],
            }
        } if mm else {},
        id="t1",
    )


class _Chan:
    def __init__(self, cid):
        self.id = cid
        self.sent = []
        self.guild = SimpleNamespace(
            get_role=lambda rid: SimpleNamespace(mention=f"<@&{rid}>")
        )

    async def send(self, *a, **kw):
        self.sent.append((a, kw))


class _Bot:
    def __init__(self, channels):
        self._channels = channels

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def fetch_channel(self, cid):
        return self._channels.get(cid)


@pytest.fixture(autouse=True)
def _channel_ids(monkeypatch):
    """ID каналов теперь из env — подменяем для тестов."""
    monkeypatch.setattr(rm, "ROOM_DATA_CHANNEL_ID", 1561683314480255057)
    monkeypatch.setattr(rm, "NOTIFICATION_CHANNEL_ID", 1200125075156910181)
    monkeypatch.setattr(
        "config.NOTIFICATION_ROLE_ID", 999, raising=False
    )
    # room_modal импортирует NOTIFICATION_ROLE_ID локально в функции —
    # config подмена покрывает его.


@pytest.mark.asyncio
async def test_mm_room_notify_goes_to_match_channel():
    """MM: 1 карточка в канал матча; ID — по кнопке, пароль — в тексте."""
    t = _tournament(mm=True)
    t.qualifier_rooms[0] = {"id": "38765135", "password": "11"}
    match_ch = _Chan(900)
    general = _Chan(1200125075156910181)
    data_ch = _Chan(1561683314480255057)
    bot = _Bot({900: match_ch, 1200125075156910181: general,
                1561683314480255057: data_ch})

    await rm.send_room_dm_notifications(
        bot, t, "qualifier", 0, 0, 1, "38765135", "11"
    )

    # В канал матча: ОДНО V2-сообщение (LayoutView) с кнопкой ID
    assert len(match_ch.sent) == 1
    kw = match_ch.sent[0][1]
    view = kw.get("view")
    assert view is not None
    assert kw.get("embed") is None
    text = _view_text(view)
    assert "🔓 Комната открыта" in text
    assert "<@111>" in text and "<@222>" in text  # реальные ping ID
    assert "Пароль: `11`" in text  # пароль прямо в карточке
    assert "38765135" not in text  # ID — только по кнопке
    ids = _view_custom_ids(view)
    assert any(i.endswith(":id") for i in ids)
    assert not any(i.endswith(":pass") for i in ids)
    # ID/пароль НЕ отправляются отдельными сообщениями
    assert len(match_ch.sent) == 1
    # Общий канал и канал данных НЕ используются для MM
    assert general.sent == []
    assert data_ch.sent == []


@pytest.mark.asyncio
async def test_tournament_room_notify_two_places():
    """Турнир: карточка в канал результатов, анонс с ролью —
    в канал уведомлений (mention в content, не в embed)."""
    t = _tournament(mm=False)
    t.qualifier_rooms[0] = {"id": "37736674", "password": "11"}
    general = _Chan(1200125075156910181)
    data_ch = _Chan(1561683314480255057)
    bot = _Bot({1200125075156910181: general,
                1561683314480255057: data_ch})

    await rm.send_room_dm_notifications(
        bot, t, "qualifier", 0, 0, 1, "37736674", "11"
    )

    # Канал данных: ОДНО V2-сообщение (LayoutView) с кнопками данных
    assert len(data_ch.sent) == 1
    kw = data_ch.sent[0][1]
    assert kw.get("view") is not None
    assert kw.get("embed") is None
    assert "🔓 Комната открыта" in _view_text(kw["view"])
    # Обычный турнир: <@id>-упоминания игроков пингуют участников
    # матча; roles/everyone выключены.
    text = _view_text(kw["view"])
    assert "<@111>" in text and "<@222>" in text
    am = kw.get("allowed_mentions")
    assert am is not None
    assert getattr(am, "users", False) is True
    assert getattr(am, "roles", True) is False
    assert getattr(am, "everyone", True) is False
    # Канал уведомлений: 1 V2-сообщение; role mention — внутри
    # TextDisplay (content= с IS_COMPONENTS_V2 запрещён, 50035).
    assert len(general.sent) == 1
    kw = general.sent[0][1]
    assert kw.get("content") is None
    am = kw.get("allowed_mentions")
    assert am is not None
    assert getattr(am, "roles", False) is True
    text = _view_text(kw["view"])
    assert "<@&" in text and "Комната открыта" in text


def _view_text(view):
    """Собрать текст всех TextDisplay внутри LayoutView."""
    import discord.ui as dui
    parts = []
    for item in view.walk_children():
        if isinstance(item, dui.TextDisplay):
            parts.append(item.content)
    return "\n".join(parts)


def _view_custom_ids(view):
    ids = []
    for item in view.walk_children():
        cid = getattr(item, "custom_id", None)
        if cid:
            ids.append(cid)
    return ids


def test_room_card_view_format():
    t = _tournament(mm=True)
    t.qualifier_rooms[0] = {"id": "38765135", "password": "11"}
    t1 = t.teams[0]
    t2 = t.teams[1]
    view = rm._room_card_view(
        t, t1, t2, "CapA", "CapB",
        "qualifier", 0, 0, 1,
    )
    text = _view_text(view)
    assert "🔓 Комната открыта" in text
    # Строгая структура: без заголовков «Матч»/«Данные комнаты»
    assert "⚔️ Матч" not in text
    assert "Данные комнаты" not in text
    assert "CapA" in text and "CapB" in text
    assert "<@111>" in text and "<@222>" in text
    # Пароль — в тексте карточки; ID — только по кнопке
    assert "Пароль: `11`" in text
    assert "38765135" not in text
    ids = _view_custom_ids(view)
    assert any(i.endswith(":id") for i in ids)
    assert not any(i.endswith(":pass") for i in ids)


# ---------------------------------------------------------------------------
# Кнопки «Получить ID / Получить пароль» — права и ephemeral-ответы
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kw):
        self.messages.append((content, kw))


class _FakeInteraction:
    def __init__(self, custom_id, user, guild=None):
        self.data = {"custom_id": custom_id}
        self.user = user
        self.guild = guild
        self.response = _FakeResponse()


def _user(uid, admin=False, name="user"):
    return SimpleNamespace(
        id=uid, display_name=name,
        guild_permissions=SimpleNamespace(administrator=admin),
    )


def _guild():
    return SimpleNamespace(
        roles=[],
        get_role=lambda rid: None,
    )


def _room_custom_id(what="id", gid=1, cid=2, mtype="qualifier",
                    midx=0, t1=0, t2=1):
    return f"rd:{gid}:{cid}:{mtype}:{midx}:{t1}:{t2}:{what}"


@pytest.fixture
def _store_tournament():
    from storage.json_store import store
    t = _tournament(mm=False)
    t.qualifier_rooms[0] = {"id": "38765135", "password": "11"}
    key = (t.guild_id, t.channel_id)
    store._tournaments[key] = t
    yield t
    store._tournaments.pop(key, None)


@pytest.mark.asyncio
async def test_room_data_captain_a_gets_id(_store_tournament):
    ix = _FakeInteraction(_room_custom_id("id"), _user(111), _guild())
    await rm._room_data_press(ix)
    assert ix.response.messages
    content = ix.response.messages[0][0]
    # ответ — ТОЛЬКО значение ID, без заголовков/markdown
    assert content == "38765135"
    assert ix.response.messages[0][1].get("ephemeral")


@pytest.mark.asyncio
async def test_room_data_captain_b_gets_password(_store_tournament):
    ix = _FakeInteraction(_room_custom_id("pass"), _user(222), _guild())
    await rm._room_data_press(ix)
    content = ix.response.messages[0][0]
    # ответ — ТОЛЬКО пароль
    assert content == "11"


@pytest.mark.asyncio
async def test_room_data_admin_allowed(_store_tournament):
    ix = _FakeInteraction(_room_custom_id("id"), _user(9999, admin=True), _guild())
    await rm._room_data_press(ix)
    assert ix.response.messages[0][0] == "38765135"


@pytest.mark.asyncio
async def test_room_data_regular_player_allowed(_store_tournament):
    """Обычный участник матча (не капитан) — данные доступны."""
    ix = _FakeInteraction(_room_custom_id("id"), _user(113), _guild())
    await rm._room_data_press(ix)
    assert ix.response.messages[0][0] == "38765135"


@pytest.mark.asyncio
async def test_room_data_outsider_denied(_store_tournament):
    """Посторонний пользователь — отказ."""
    ix = _FakeInteraction(_room_custom_id("id"), _user(7777), _guild())
    await rm._room_data_press(ix)
    content = ix.response.messages[0][0]
    assert "участники матча" in content
    assert "38765135" not in content


@pytest.mark.asyncio
async def test_room_data_org_without_admin_denied(_store_tournament):
    ix = _FakeInteraction(
        _room_custom_id("pass"), _user(555, admin=False, name="org"), _guild()
    )
    await rm._room_data_press(ix)
    assert "администраторы" in ix.response.messages[0][0]


@pytest.mark.asyncio
async def test_room_data_missing_room(_store_tournament):
    ix = _FakeInteraction(_room_custom_id("id", midx=3), _user(111), _guild())
    await rm._room_data_press(ix)
    assert "не найдены" in ix.response.messages[0][0]


# ---------------------------------------------------------------------------
# card_pending: безопасный повтор публикации без дублей
# ---------------------------------------------------------------------------

class _FailChan(_Chan):
    def __init__(self, cid, fail=False):
        super().__init__(cid)
        self.fail = fail
        self.next_id = 1000

    async def send(self, *a, **kw):
        if self.fail:
            raise RuntimeError("discord down")
        self.sent.append((a, kw))
        self.next_id += 1
        return SimpleNamespace(id=self.next_id)

    async def fetch_message(self, mid):
        for _a, _kw in self.sent:
            pass
        # «живое» сообщение — всегда успешно
        return SimpleNamespace(id=mid)


@pytest.mark.asyncio
async def test_room_card_failure_marks_pending(monkeypatch):
    monkeypatch.setattr(rm, "ROOM_DATA_CHANNEL_ID", 1561683314480255057)
    t = _tournament(mm=False)
    t.qualifier_rooms[0] = {"id": "1", "password": "2"}
    data_ch = _FailChan(1561683314480255057, fail=True)
    bot = _Bot({1561683314480255057: data_ch,
                1200125075156910181: _Chan(1200125075156910181)})
    ok = await rm.send_room_dm_notifications(
        bot, t, "qualifier", 0, 0, 1, "1", "2"
    )
    assert ok is False
    assert t.qualifier_rooms[0].get("card_pending") is True


@pytest.mark.asyncio
async def test_pending_retry_with_live_message_no_dup(monkeypatch):
    """card_message_id жив → retry только снимает флаг, без send."""
    monkeypatch.setattr(rm, "ROOM_DATA_CHANNEL_ID", 1561683314480255057)
    t = _tournament(mm=False)
    t.qualifier_rooms[0] = {
        "id": "1", "password": "2",
        "card_pending": True, "card_message_id": 555,
    }
    data_ch = _FailChan(1561683314480255057)  # send не вызывается
    bot = _Bot({1561683314480255057: data_ch})
    ok = await rm.retry_pending_room_card(bot, t, "qualifier", 0)
    assert ok is True
    assert "card_pending" not in t.qualifier_rooms[0]
    assert data_ch.sent == []  # НИКАКИХ повторных отправок


@pytest.mark.asyncio
async def test_pending_retry_resends_when_message_gone(monkeypatch):
    monkeypatch.setattr(rm, "ROOM_DATA_CHANNEL_ID", 1561683314480255057)
    t = _tournament(mm=False)
    t.qualifier_rooms[0] = {
        "id": "1", "password": "2",
        "card_pending": True, "card_message_id": 999,
    }
    data_ch = _FailChan(1561683314480255057)
    # fetch_message падает → сообщения нет → переотправка
    async def _missing(mid):
        raise Exception("Unknown Message")
    data_ch.fetch_message = _missing
    bot = _Bot({1561683314480255057: data_ch,
                1200125075156910181: _Chan(1200125075156910181)})
    ok = await rm.retry_pending_room_card(bot, t, "qualifier", 0)
    assert ok is True
    assert len(data_ch.sent) == 1  # ровно одна переотправка
    assert "card_pending" not in t.qualifier_rooms[0]
    assert t.qualifier_rooms[0].get("card_message_id")
