from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest

from services import notification_preferences as prefs
from services import tournament_notifications as nt
from storage.server_layout_store import ServerLayoutStore


@pytest.fixture
def notifications(tmp_path,monkeypatch):
    store=ServerLayoutStore(tmp_path/'layout')
    monkeypatch.setattr(prefs,'server_layout_store',store)
    monkeypatch.setattr(nt,'server_layout_store',store)
    monkeypatch.setattr(nt,'STATE_PATH',tmp_path/'notifications.json')
    monkeypatch.setattr(nt,'channel_id',lambda *a:0)
    store.write(1,{'guild_id':1,'bindings':{'announcements':22,'notify_cs':33}})
    from storage import admin_config_store
    monkeypatch.setattr(admin_config_store,'get_template',AsyncMock(return_value=None))
    return store


def make_guild():
    role=NS(id=33,mention='<@&33>')
    guild=NS(id=1,default_role=NS(id=1),get_role=lambda _:role)
    destination=NS(id=22,guild=guild,send=AsyncMock(return_value=NS(id=99)))
    source=NS(permissions_for=lambda _:NS(view_channel=True))
    guild.get_channel=lambda cid: destination if cid==22 else source
    return guild,destination


@pytest.mark.asyncio
async def test_announcement_once_and_restricts_mentions(notifications):
    guild,channel=make_guild()
    assert await nt.announce(guild,'cs','t1',10,77) == 'sent'
    assert await nt.announce(guild,'cs','t1',10,77) == 'sent'
    assert channel.send.await_count == 1
    mentions=channel.send.call_args.kwargs['allowed_mentions'].to_dict()
    assert mentions['roles'] == [33]
    assert 'everyone' not in mentions['parse'] and 'users' not in mentions['parse']
    assert json.loads(nt.STATE_PATH.read_text())['1:cs:t1:created']['message_id'] == 99


@pytest.mark.asyncio
async def test_ambiguous_send_keeps_pending_no_second_ping(notifications):
    guild,channel=make_guild()
    channel.send.side_effect=RuntimeError('send response lost')
    with pytest.raises(RuntimeError):
        await nt.announce(guild,'cs','t1',10,77)
    assert await nt.announce(guild,'cs','t1',10,77) == 'pending'
    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_private_event_and_test_event_are_not_announced(notifications):
    guild,channel=make_guild()
    private=NS(permissions_for=lambda _:NS(view_channel=False))
    original=guild.get_channel
    guild.get_channel=lambda cid: original(cid) if cid==22 else private
    assert await nt.announce(guild,'cs','t1',10,77) == 'private'
    assert await nt.announce(guild,'cs','t2',10,77,is_test=True) == 'skipped'
    channel.send.assert_not_called()


class Role:
    id=33
    permissions=NS(value=0)
    def __ge__(self,other): return False


@pytest.mark.asyncio
async def test_preferences_toggle_current_member_role(notifications,monkeypatch):
    role=Role()
    user=NS(roles=[],add_roles=AsyncMock(),remove_roles=AsyncMock())
    guild=NS(id=1,fetch_roles=AsyncMock(return_value=[role]),fetch_member=AsyncMock(return_value=user),
             me=NS(guild_permissions=NS(manage_roles=True),top_role=NS()))
    monkeypatch.setattr(prefs,'can_sell_role',AsyncMock(return_value=True))
    assert await prefs.toggle(guild,42,'cs') is True
    user.add_roles.assert_awaited_once()
    user.roles=[role]
    assert await prefs.toggle(guild,42,'cs') is False
    user.remove_roles.assert_awaited_once()


@pytest.mark.asyncio
async def test_preferences_reject_role_that_gained_permissions(notifications,monkeypatch):
    role=Role();role.permissions=NS(value=8)
    guild=NS(id=1,fetch_roles=AsyncMock(return_value=[role]),fetch_member=AsyncMock())
    monkeypatch.setattr(prefs,'can_sell_role',AsyncMock(return_value=True))
    with pytest.raises(ValueError):
        await prefs.toggle(guild,42,'cs')
    guild.fetch_member.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_filters_hidden_events(monkeypatch):
    from views.club_hub import build_page
    events=[NS(id=i,name=f'event {i}',channel_id=i,start_time=datetime(2026,11,17,tzinfo=UTC),
               status=discord.EventStatus.scheduled) for i in (1,2)]
    guild=NS(id=7,fetch_scheduled_events=AsyncMock(return_value=events),
             get_channel=lambda cid:NS(permissions_for=lambda _:NS(view_channel=cid==1)))
    view=await build_page(NS(guild=guild,user=NS(id=42)),'schedule')
    text='\n'.join(getattr(c,'content','') for c in view.walk_children())
    assert 'event 1' in text and 'event 2' not in text


@pytest.mark.asyncio
async def test_personal_tournament_uses_discord_id_not_name(monkeypatch):
    from models.tournament import Tournament
    from services import club_hub as ch
    t=Tournament(guild_id=1,channel_id=2)
    t.circle1=['DifferentNickname'];t.player_user_ids={'DifferentNickname':42}
    monkeypatch.setattr(ch,'active_events',AsyncMock(return_value=([ch.Event('CS 4×4',2,7,'Регистрация',99,tournament_id=t.id)],False)))
    monkeypatch.setattr(ch.store,'get',lambda *a:t)
    mine,_=await ch.my_events(NS(id=1),NS(id=42,display_name='DiscordNickname'))
    other,_=await ch.my_events(NS(id=1),NS(id=43,display_name='DifferentNickname'))
    assert len(mine)==1 and not other
