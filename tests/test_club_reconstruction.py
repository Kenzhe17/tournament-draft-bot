import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import config
from models.tournament import RegistrationState, Tournament, TournamentPhase
from services import club_hub, room_publisher
from utils import permissions
from views.club_hub import HubButton, HubView, build_page


def member(uid=42, roles=(), admin=False):
    return NS(id=uid, roles=list(roles), guild_permissions=NS(administrator=admin), display_name='Player')


def guild(gid=1, visible=(2,)):
    return NS(id=gid, get_channel=lambda cid: NS(permissions_for=lambda _: NS(view_channel=cid in visible)),
              get_role=lambda rid: None, roles=[])


def interaction(uid=42, gid=1):
    return NS(guild=guild(gid), guild_id=gid, user=member(uid),
              response=NS(defer=AsyncMock(), send_message=AsyncMock()),
              edit_original_response=AsyncMock())


def text(view):
    return '\n'.join(getattr(x, 'content', '') for x in view.walk_children())


def test_organizer_is_not_admin(monkeypatch):
    role = NS(id=7, name='org')
    monkeypatch.setattr(permissions, 'ORG_ROLE_ID', 7)
    assert not permissions.is_admin_check(member(roles=[role]), guild())
    assert permissions.is_admin_check(member(admin=True), guild())
    assert not permissions.is_admin_check(member(), None)
    assert not permissions.is_admin_check(member(0), guild())


def test_channel_role_does_not_manage_other_tournament(monkeypatch):
    monkeypatch.setattr(config, 'ORG_ROLE_MAP', {2: 7})
    monkeypatch.setattr(config, 'MANAGER_ROLE_ID', 0)
    monkeypatch.setattr(permissions.organizer_store, 'get', lambda *a: None)
    role = NS(id=7)
    g = guild(); g.get_role = lambda rid: role if rid == 7 else None
    u = member(roles=[role])
    assert permissions.can_manage_tournament(u, g, Tournament(guild_id=1, channel_id=2))
    assert not permissions.can_manage_tournament(u, g, Tournament(guild_id=1, channel_id=3))
    assert not permissions.can_manage_tournament(member(admin=True), g, Tournament(guild_id=9, channel_id=2))


@pytest.mark.asyncio
@pytest.mark.parametrize('uid,gid,tid,allowed', [(101,1,'t1',True),(103,1,'t1',False),(101,2,'t1',False),(101,1,'old',False),(900,1,'t1',True)])
async def test_room_credentials_authorized_each_click(monkeypatch, uid, gid, tid, allowed):
    from storage.json_store import store
    t = Tournament(guild_id=1, channel_id=2, organizer_id=900)
    t.id='t1'; t.phase=TournamentPhase.SEMIFINALS
    t.teams=[{'captain':'A','players':['A']},{'captain':'B','players':['B']},
             {'captain':'C','players':['C']},{'captain':'D','players':['D']}]
    t.player_user_ids={'A':101,'B':102,'C':103,'D':104}
    t.semifinal_matches=[(0,1),(2,3)]; t.semifinal_winners=[None,None]
    monkeypatch.setattr(store, 'get', lambda *a: t)
    monkeypatch.setattr(room_publisher.room_store, 'get_active_room', lambda *a: {'room_id':'4321','password':'private-secret'})
    ix=interaction(uid,gid); ix.data={'custom_id':f'rcard:id:1:2:{tid}:semifinal:0'}
    await room_publisher.RoomIdBtn(ix.data['custom_id']).callback(ix)
    call=ix.response.send_message.call_args
    assert call.kwargs['ephemeral']
    assert ('private-secret' in call.args[0]) is allowed
    assert ('4321' in call.args[0]) is allowed


@pytest.mark.asyncio
async def test_tournament_discovery_filters_hidden_test_mm_complete(monkeypatch):
    def t(cid, **kw): return Tournament(guild_id=1, channel_id=cid, **kw)
    a=t(2); a.registration=RegistrationState.OPEN; a.players_pool=['Player']
    rows=[a,t(3),t(2,is_test=True),t(2,mm_search_channel_id=99),t(2,phase=TournamentPhase.COMPLETE)]
    monkeypatch.setattr(club_hub.store,'get_by_guild',lambda _: rows)
    monkeypatch.setattr(club_hub.br_store,'get_by_guild',lambda _: [])
    monkeypatch.setattr(club_hub,'DATABASE_URL','')
    events, unavailable=await club_hub.active_events(guild(),member())
    assert len(events)==1 and events[0].players==1
    assert events[0].state=='Регистрация открыта'
    assert unavailable


@pytest.mark.asyncio
async def test_hub_public_click_never_edits_public_panel(monkeypatch):
    import views.club_hub as hv
    monkeypatch.setattr(hv,'build_page',AsyncMock(return_value=HubView()))
    ix=interaction()
    await HubButton('play','Играть',public=True).callback(ix)
    assert ix.response.defer.call_args.kwargs == {'ephemeral':True,'thinking':True}
    ix.edit_original_response.assert_awaited_once()
    assert HubView().is_persistent()


@pytest.mark.asyncio
async def test_hub_private_page_cannot_be_used_by_other_member():
    ix=interaction(uid=99)
    await HubButton('profile','Профиль',owner_id=42).callback(ix)
    ix.response.defer.assert_not_called()
    ix.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_hub_play_uses_actual_models(monkeypatch):
    monkeypatch.setattr(club_hub.store,'get_by_guild',lambda _: [Tournament(guild_id=1,channel_id=2)])
    monkeypatch.setattr(club_hub.br_store,'get_by_guild',lambda _: [])
    monkeypatch.setattr(club_hub,'DATABASE_URL','')
    page=await build_page(interaction(),'play')
    assert 'CS 4×4' in text(page) and 'Регистрация закрыта' in text(page)
    assert page.total_children_count <= 40


@pytest.mark.asyncio
async def test_retired_matchmaking_never_starts_or_joins(monkeypatch):
    from cogs.mm import MMCog
    monkeypatch.setattr(config,'MATCHMAKING_ENABLED',False)
    cog=MMCog(NS())
    ix=interaction()
    result=await cog._begin_match_impl(None,ix)
    assert result[0] is None
    await cog.handle_queue_join(ix,1,2)
    ix.response.send_message.assert_awaited_once()
    from views.mm_search_view import build_queue_panel
    assert 'ТУРНИРЫ' in text(build_queue_panel(None))


@pytest.mark.asyncio
async def test_retired_load_keeps_recovery_without_pool_mutation(monkeypatch):
    from cogs.mm import MMCog, mm_search_store
    from services import match_channel_pool
    monkeypatch.setattr(config,'MATCHMAKING_ENABLED',False)
    monkeypatch.setattr(mm_search_store,'load',AsyncMock())
    ensure=AsyncMock(); monkeypatch.setattr(match_channel_pool,'ensure_slots',ensure)
    bot=NS(wait_until_ready=AsyncMock(side_effect=lambda: asyncio.sleep(60)))
    cog=MMCog(bot)
    # Hold recovery before readiness; startup must not touch Discord ACLs.
    blocker=asyncio.Event()
    async def wait(): await blocker.wait()
    bot.wait_until_ready=wait
    await cog.cog_load()
    assert cog._queue_recovery_task is None
    assert cog._match_sweeper_task is not None
    ensure.assert_not_called()
    cog.cog_unload()
    await asyncio.gather(cog._match_sweeper_task,return_exceptions=True)


@pytest.mark.asyncio
async def test_hub_publish_is_idempotent_and_pending_blocks_duplicates(tmp_path,monkeypatch):
    import cogs.club_hub as ch
    monkeypatch.setattr(ch,'HUB_STATE',tmp_path/'hub.json')
    message=NS(id=5,jump_url='https://discord.com/channels/1/2/5',edit=AsyncMock())
    channel=NS(id=2,send=AsyncMock(return_value=message),fetch_message=AsyncMock(return_value=message))
    bot=NS(get_channel=lambda _:channel)
    cog=ch.ClubHubCog(bot)
    ix=interaction();ix.channel=channel;ix.channel_id=2;ix.followup=NS(send=AsyncMock())
    await ch.ClubHubCog.publish.callback(cog,ix)
    await ch.ClubHubCog.publish.callback(cog,ix)
    assert channel.send.await_count==1
    assert message.edit.await_count==1
    ch.HUB_STATE.write_text(json.dumps({'1':{'channel_id':2,'pending':True}}))
    await ch.ClubHubCog.publish.callback(cog,ix)
    assert channel.send.await_count==1


@pytest.mark.asyncio
async def test_secure_cards_edits_known_message_without_new_publication(monkeypatch):
    from storage.json_store import store
    message = NS(author=NS(id=5), edit=AsyncMock())
    channel = NS(guild=NS(id=1), fetch_message=AsyncMock(return_value=message), send=AsyncMock())
    monkeypatch.setattr(room_publisher, '_get_channel', AsyncMock(return_value=channel))
    monkeypatch.setattr(room_publisher.room_store, 'published_cards', lambda _: [
        {'channel_id':2,'key':'old:final:0','message_id':8}])
    monkeypatch.setattr(store, 'get', lambda *a: None)
    result = await room_publisher.secure_published_cards(NS(user=NS(id=5)),1)
    assert result == {'updated':1,'missing':0,'failed':0}
    channel.send.assert_not_called()
    message.edit.assert_awaited_once()
    assert message.edit.call_args.kwargs['embeds'] == []
    assert 'АРХИВ' in text(message.edit.call_args.kwargs['view'])


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['admin', 'org', 'managed', 'private_channel', 'channel_moderation', 'everyone', 'safe'])
async def test_shop_role_cannot_grant_staff_access(monkeypatch, reason):
    from services.shop_role_policy import can_sell_role
    monkeypatch.setattr(config, 'DATABASE_URL', '')
    monkeypatch.setattr(config, 'ORG_ROLE_ID', 7 if reason == 'org' else 0)
    monkeypatch.setattr(config, 'ORG_ROLE_MAP', {})
    role = NS(id=1 if reason == 'everyone' else 7,
              managed=reason == 'managed', permissions=NS(administrator=reason == 'admin'))
    channel = NS(overwrites_for=lambda _: NS(view_channel=True if reason == 'private_channel' else None,
                                             manage_messages=True if reason == 'channel_moderation' else None),
                 permissions_for=lambda _: NS(view_channel=False))
    g = NS(id=1, channels=[channel], default_role=NS(id=1))
    assert await can_sell_role(g, role) is (reason == 'safe')


@pytest.mark.asyncio
async def test_shop_role_rejects_dynamic_moderator_role_and_unknown_db(monkeypatch):
    from services.shop_role_policy import can_sell_role
    from storage.moderation_store import moderation_store
    monkeypatch.setattr(config, 'DATABASE_URL', 'configured')
    monkeypatch.setattr(config, 'ORG_ROLE_ID', 0)
    monkeypatch.setattr(config, 'ORG_ROLE_MAP', {})
    role = NS(id=7, managed=False, permissions=NS())
    g = NS(id=1, channels=[], default_role=NS(id=1))
    monkeypatch.setattr(moderation_store, 'settings', AsyncMock(return_value={'moderator_role_id':7}))
    assert not await can_sell_role(g, role)
    monkeypatch.setattr(moderation_store, 'settings', AsyncMock(side_effect=RuntimeError('DB down')))
    assert not await can_sell_role(g, role)


@pytest.mark.asyncio
async def test_support_modal_rechecks_owner_and_uses_existing_handler():
    from views.club_support import SupportModal
    ix = interaction()
    callback = AsyncMock()
    command = NS(_check_can_run=AsyncMock(return_value=True), callback=callback, binding=object())
    ix.client = NS(tree=NS(get_command=lambda _:command))
    modal = SupportModal('appeal',42,1)
    modal.target._value='MOD-000123'
    modal.reason._value='Пересмотрите это наказание'
    await modal.on_submit(ix)
    assert callback.call_args.args[2:] == (123,'Пересмотрите это наказание')
    ix.user.id=99
    await modal.on_submit(ix)
    assert callback.await_count==1
    ix.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_hub_balance_displays_rep_and_survives_rep_outage(monkeypatch):
    from storage.rep_balance_store import rep_balance_store
    from storage.user_balance_store import user_balance_store
    monkeypatch.setattr(user_balance_store,'get_balance',AsyncMock(return_value=100))
    monkeypatch.setattr(rep_balance_store,'get_balance',AsyncMock(return_value=7))
    assert 'Доступный REP: **7**' in text(await build_page(interaction(),'economy'))
    monkeypatch.setattr(rep_balance_store,'get_balance',AsyncMock(side_effect=RuntimeError('down')))
    page=await build_page(interaction(),'economy')
    assert 'временно недоступен' in text(page) and '100' in text(page)


@pytest.mark.asyncio
async def test_recovery_does_not_restore_newly_privileged_role(monkeypatch):
    from services import shop_purchase_service as purchase
    from services import shop_role_policy
    role=NS(id=7)
    target=NS(roles=[],add_roles=AsyncMock())
    g=NS(get_role=lambda _:role,fetch_member=AsyncMock(return_value=target))
    monkeypatch.setattr(shop_role_policy,'can_sell_role',AsyncMock(return_value=False))
    assert not await purchase._restore_sell_role(g,42,7)
    target.add_roles.assert_not_called()
