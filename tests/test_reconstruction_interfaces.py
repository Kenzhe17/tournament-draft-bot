from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.info import InfoCog
from services import welcome_card as wc
from services.info_content import section_by_key
from views.club_hub import HubView


def texts(view):
    return '\n'.join(getattr(c,'content','') for c in view.walk_children())


def test_minimal_hub_and_welcome():
    assert 'Discord Server r!z3' in texts(HubView())
    assert 'ИГРОВОЙ КЛУБ' not in texts(HubView())
    assert 'Турниры организаторов' not in texts(HubView())
    member = NS(mention='<@42>',guild=NS(id=1))
    view = wc.build_welcome_view(100,member,info_channel_id=2,start_channel_id=3)
    assert 'Добро пожаловать в Discord Server r!z3' in texts(view)
    assert not any(isinstance(c,discord.ui.MediaGallery) for c in view.walk_children())
    buttons=[c for c in view.walk_children() if isinstance(c,discord.ui.Button)]
    assert [b.label for b in buttons] == ['Открыть старт','Правила']
    assert buttons[0].url == 'https://discord.com/channels/1/3'
    assert '/hub' in texts(wc.build_welcome_view(100,member))


@pytest.mark.asyncio
async def test_welcome_never_falls_back_to_another_guild(monkeypatch):
    monkeypatch.setattr(wc,'WELCOME_CHANNEL_ID',11)
    from storage.server_layout_store import server_layout_store
    monkeypatch.setattr(server_layout_store,'binding',lambda *a:0)
    guild = NS(id=1,get_channel=lambda _:NS(guild=NS(id=9)))
    assert await wc._find_channel(NS(guilds=[guild]),NS(guild=guild)) == (None,None)


@pytest.fixture
def info(monkeypatch):
    from cogs import info as mod
    records = {}
    async def get(_): return records
    async def pending(gid,key):records[key]={**records.get(key,{}),'pending':True}
    async def save(gid,key,mid,version):records[key]={'message_id':mid,'version':version}
    monkeypatch.setattr(mod.info_store,'get_messages',get)
    monkeypatch.setattr(mod.info_store,'set_pending',pending)
    monkeypatch.setattr(mod.info_store,'set_message',save)
    return InfoCog(NS()),records


@pytest.mark.asyncio
async def test_info_timeout_does_not_duplicate_existing_message(info):
    cog,records=info
    records['info']={'message_id':7,'version':'old'}
    message=NS(edit=AsyncMock(side_effect=RuntimeError('timeout')))
    channel=NS(fetch_message=AsyncMock(return_value=message),send=AsyncMock())
    result=await cog._sync_section(NS(id=1),channel,section_by_key('info'))
    assert 'новое сообщение не создано' in result
    channel.send.assert_not_called()
    assert records['info']['message_id'] == 7


@pytest.mark.asyncio
async def test_info_uncertain_send_blocks_retry_after_restart(info):
    cog,records=info
    channel=NS(send=AsyncMock(side_effect=RuntimeError('send uncertain')))
    with pytest.raises(RuntimeError):
        await cog._sync_section(NS(id=1),channel,section_by_key('info'))
    assert records['info']['pending']
    restarted=InfoCog(NS())
    result=await restarted._sync_section(NS(id=1),channel,section_by_key('info'))
    assert 'не подтверждена' in result
    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_info_update_reuses_message_and_removes_stale_banner(info):
    cog,records=info
    records['info']={'message_id':7,'version':'old'}
    message=NS(edit=AsyncMock())
    channel=NS(fetch_message=AsyncMock(return_value=message),send=AsyncMock())
    await cog._sync_section(NS(id=1),channel,section_by_key('info'))
    channel.send.assert_not_called()
    assert message.edit.call_args.kwargs['attachments'] == []
    assert records['info']['version'] != 'old'


@pytest.mark.asyncio
async def test_info_storage_failures_are_not_silent(tmp_path,monkeypatch):
    from storage import info_store as mod
    monkeypatch.setattr(mod,'DATABASE_URL','')
    monkeypatch.setattr(mod,'FALLBACK_PATH',tmp_path/'info.json')
    store=mod.InfoStore()
    monkeypatch.setattr(mod,'atomic_write_text',lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        await store.set_pending(1,'info')
    mod.FALLBACK_PATH.write_text('{corrupt')
    with pytest.raises(ValueError):
        await mod.InfoStore().get_messages(1)


@pytest.mark.asyncio
async def test_reconstruction_commands_register_without_discord_mutations():
    from discord.ext import commands

    from cogs.groups import server_group
    from cogs.server_reconstruction import ServerReconstructionCog
    bot=commands.Bot(command_prefix='!',intents=discord.Intents.none())
    async with bot:
        cog=ServerReconstructionCog(bot)
        await bot.add_cog(cog)
        bot.tree.add_command(server_group)
        group=bot.tree.get_command('server')
        assert group.get_command('reconstruction').binding is cog
        assert group.get_command('notification_roles').binding is cog
        assert group.get_command('reconstruction').checks


def test_configuration_groups_hidden_by_default_for_regular_members():
    from cogs.groups import admin_group, banner_group, info_group, server_group
    for group in (admin_group, banner_group, info_group, server_group):
        assert group.default_permissions.administrator
