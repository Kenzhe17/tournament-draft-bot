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

    # В канал матча: карточка + 2 сообщения с данными
    assert len(match_ch.sent) == 3
    card_view = match_ch.sent[0][1].get("view")
    assert card_view is not None
    # ID и пароль — отдельные обычные сообщения (без код-блоков)
    assert match_ch.sent[1][0][0] == "38765135"
    assert match_ch.sent[2][0][0] == "11"
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

    # Канал данных: карточка + 2 сообщения
    assert len(data_ch.sent) == 3
    # Канал уведомлений: 1 сообщение, role mention в content
    assert len(general.sent) == 1
    content = general.sent[0][1].get("content") or (
        general.sent[0][0][0] if general.sent[0][0] else None
    )
    assert content and "<@&" in str(content)


def test_room_card_text_format():
    t = _tournament(mm=True)
    t1 = t.teams[0]
    t2 = t.teams[1]
    text = rm._room_card_text(t, t1, t2, "CapA", "CapB")
    assert "КОМНАТА ОТКРЫТА" in text
    assert "Матч:" in text
    assert "<@111>" in text  # упоминания игроков внутри карточки
    assert "<@222>" in text
    assert text.rstrip().endswith("**Данные:**")
