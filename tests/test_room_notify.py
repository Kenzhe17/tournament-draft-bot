"""Регрессии уведомлений «Комната открыта».

- MM (/mm squad): карточка + ID/пароль отдельными сообщениями —
  всё в канал матча, общий канал уведомлений не используется.
- Обычный турнир: карточка с данными в канал результатов,
  анонс с ролью в content (пинг работает) в канал уведомлений.
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
    """MM: 1 карточка + 2 отдельных сообщения в канал матча."""
    t = _tournament(mm=True)
    match_ch = _Chan(900)
    general = _Chan(1200125075156910181)
    data_ch = _Chan(1561683314480255057)
    bot = _Bot({900: match_ch, 1200125075156910181: general,
                1561683314480255057: data_ch})

    await rm.send_room_dm_notifications(
        bot, t, "qualifier", 0, 0, 1, "38765135", "11"
    )

    # В канал матча: ОДНО embed-сообщение с кнопками данных
    assert len(match_ch.sent) == 1
    kw = match_ch.sent[0][1]
    emb = kw.get("embed")
    assert emb is not None and "Комната открыта" in emb.title
    view = kw.get("view")
    assert view is not None
    ids = [getattr(c, "custom_id", "") for c in view.children]
    assert any(i.endswith(":id") for i in ids)
    assert any(i.endswith(":pass") for i in ids)
    # ID/пароль НЕ отправляются сырым текстом
    assert len(match_ch.sent) == 1
    # Общий канал и канал данных НЕ используются для MM
    assert general.sent == []
    assert data_ch.sent == []


@pytest.mark.asyncio
async def test_tournament_room_notify_two_places():
    """Турнир: карточка+данные в канал результатов, анонс с ролью —
    в канал уведомлений (mention в content, не в embed)."""
    t = _tournament(mm=False)
    general = _Chan(1200125075156910181)
    data_ch = _Chan(1561683314480255057)
    bot = _Bot({1200125075156910181: general,
                1561683314480255057: data_ch})

    await rm.send_room_dm_notifications(
        bot, t, "qualifier", 0, 0, 1, "37736674", "11"
    )

    # Канал данных: ОДНО embed-сообщение с кнопками данных
    assert len(data_ch.sent) == 1
    kw = data_ch.sent[0][1]
    assert kw.get("embed") is not None
    assert kw.get("view") is not None
    # Канал уведомлений: 1 сообщение, role mention в content
    assert len(general.sent) == 1
    content = general.sent[0][1].get("content") or (
        general.sent[0][0][0] if general.sent[0][0] else None
    )
    assert content and "<@&" in str(content)


def test_room_card_embed_format():
    t = _tournament(mm=True)
    t1 = t.teams[0]
    t2 = t.teams[1]
    emb = rm._room_card_embed(t, t1, t2, "CapA", "CapB")
    assert emb.title == "🔓 Комната открыта"
    values = "\n".join(f.value for f in emb.fields)
    assert "CapA" in values and "CapB" in values
    assert "<@111>" in values  # упоминания игроков внутри embed
    assert "<@222>" in values
    assert "кнопке ниже" in values


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
    assert "38765135" in content and ix.response.messages[0][1].get("ephemeral")


@pytest.mark.asyncio
async def test_room_data_captain_b_gets_password(_store_tournament):
    ix = _FakeInteraction(_room_custom_id("pass"), _user(222), _guild())
    await rm._room_data_press(ix)
    content = ix.response.messages[0][0]
    assert "Пароль" in content and "`11`" in content


@pytest.mark.asyncio
async def test_room_data_admin_allowed(_store_tournament):
    ix = _FakeInteraction(_room_custom_id("id"), _user(9999, admin=True), _guild())
    await rm._room_data_press(ix)
    assert "38765135" in ix.response.messages[0][0]


@pytest.mark.asyncio
async def test_room_data_regular_player_denied(_store_tournament):
    ix = _FakeInteraction(_room_custom_id("id"), _user(113), _guild())
    await rm._room_data_press(ix)
    content = ix.response.messages[0][0]
    assert "капитаны, организаторы или администраторы" in content
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
