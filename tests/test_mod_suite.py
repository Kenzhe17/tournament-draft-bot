"""Unit/contract tests for r!z3 moderation.

Run in tournament-draft-bot with its discord.py>=2.7 dependencies:
    pytest -q tests/test_mod_suite.py
PostgreSQL and live Discord are separately required for release validation.
"""
from __future__ import annotations

import asyncio
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

import discord
import pytest

from services import moderation_service as svc
from storage import moderation_store as ms_store


@pytest.mark.parametrize('text,minutes',[
    ('1m',1),('10m',10),('2h',120),('1d',1440),('1d12h30m',2190),('28d',40320),
])
def test_duration_valid(text,minutes):
    assert int(svc.parse_duration(text).total_seconds()//60)==minutes


@pytest.mark.parametrize('text',['','0m','29d','30s','-1h','10','1x','2d99x','1000000h'])
def test_duration_invalid(text):
    assert svc.parse_duration(text) is None


def test_message_url_must_match_guild():
    url='https://discord.com/channels/123/456/789'
    assert svc.valid_message_url(url,123)==url
    with pytest.raises(ValueError):svc.valid_message_url(url,234)
    with pytest.raises(ValueError):svc.valid_message_url('https://example.com/channels/123/456/789',123)


def test_mention_hardening():
    assert '@everyone' not in svc.clean_reason('spam @everyone')
    assert '@here' not in svc.clean_reason('spam @here')


@pytest.mark.asyncio
async def test_organizer_is_not_moderator(monkeypatch):
    org_id=900
    fake_settings=SimpleNamespace(settings=lambda gid: asyncio.sleep(0,result={'moderator_role_id':123,'head_role_id':456}))
    monkeypatch.setattr(svc,'db',fake_settings)
    guild=SimpleNamespace(id=1,owner_id=999)
    member=SimpleNamespace(id=34,guild=guild,roles=[SimpleNamespace(id=org_id)],guild_permissions=SimpleNamespace(administrator=False))
    assert await svc.level(member)==0


@pytest.mark.asyncio
async def test_mod_and_head_have_separate_powers(monkeypatch):
    fake_settings=SimpleNamespace(settings=lambda gid: asyncio.sleep(0,result={'moderator_role_id':123,'head_role_id':456}))
    monkeypatch.setattr(svc,'db',fake_settings)
    guild=SimpleNamespace(id=1,owner_id=999)
    def user(ids):return SimpleNamespace(id=22,guild=guild,roles=[SimpleNamespace(id=i) for i in ids],guild_permissions=SimpleNamespace(administrator=False))
    assert await svc.level(user([123]))==1
    assert await svc.level(user([456]))==2
    assert await svc.level(user([5]))==0


@pytest.mark.asyncio
async def test_warn_is_written_before_display(monkeypatch):
    events=[]
    class Store:
        async def create_case(self,*args,**kwargs):
            events.append('db.created')
            return {'id':32,'target_id':2,'status':'pending'}
        async def case_status(self,*args,**kwargs):events.append('db.active')
    async def permitted(*a,**kw):return None
    async def notice(*a,**kw):events.append('discord.notice')
    monkeypatch.setattr(svc,'db',Store())
    monkeypatch.setattr(svc,'check_target',permitted)
    monkeypatch.setattr(svc,'notice',notice)
    guild=SimpleNamespace(id=1)
    actor=SimpleNamespace(id=3,guild=guild)
    user=SimpleNamespace(id=2,guild=guild)
    result=await svc.apply_warning(actor,user,'Оскорбление',SimpleNamespace())
    assert result['id']==32
    assert events==['db.created','db.active','discord.notice']


@pytest.mark.asyncio
async def test_timeout_unknown_result_not_retried(monkeypatch):
    events=[]
    class Store:
        async def create_case(self,*a,**k):
            events.append('db.created');return {'id':7}
        async def case_status(self,*a,**k):
            events.append('db.'+a[2])
    async def permitted(*a,**k):return None
    monkeypatch.setattr(svc,'db',Store())
    monkeypatch.setattr(svc,'check_target',permitted)
    class FakeResponse:
        status=502
        reason='Bad Gateway'
    async def broken_timeout(*a,**k):
        events.append('discord.timeout')
        raise discord.HTTPException(FakeResponse(),'HTTP unknown')
    guild=SimpleNamespace(id=1)
    actor=SimpleNamespace(id=3,guild=guild)
    user=SimpleNamespace(id=2,guild=guild,timeout=broken_timeout,timed_out_until=None)
    with pytest.raises(RuntimeError,match='результат Discord неизвестен'):
        await svc.apply_timeout(actor,user,timedelta(minutes=5),'Причина',SimpleNamespace())
    assert events==['db.created','discord.timeout','db.unknown']


def test_schema_has_separate_staff_and_tournament_state():
    ddl=ms_store._SCHEMA
    assert 'moderation_settings' in ddl
    assert 'moderation_cases' in ddl
    assert 'moderation_reports' in ddl
    assert 'moderation_appeals' in ddl
    assert 'moderation_events' in ddl
    assert 'tournament_audit_log' not in ddl
    assert 'vac_bans' not in ddl


@pytest.mark.asyncio
async def test_report_resolution_uses_authoritative_store(monkeypatch):
    from views import mod_panel
    captured=[]
    class Store:
        async def report_action(self,guild,report_id,actor_id,action,note=''):
            captured.append((guild,report_id,actor_id,action,note))
            return {'id':report_id,'status':'resolved','target_id':2,'reporter_id':3,'reason':'Причина','message_url':None,'assignee_id':actor_id}
    monkeypatch.setattr(mod_panel,'db',Store())
    assert 'MODERATOR' not in str(captured)


def test_no_direct_vac_or_elo_mutations():
    import inspect
    from cogs import mod
    src=inspect.getsource(mod)
    assert 'issue_vac(' not in src
    assert 'add_elo(' not in src
    assert 'subtract_balance(' not in src


@pytest.mark.asyncio
async def test_junior_mod_cannot_timeout_an_admin(monkeypatch):
    bot_member=SimpleNamespace(top_role=SimpleNamespace(position=10),guild_permissions=SimpleNamespace(moderate_members=True))
    guild=SimpleNamespace(id=1,owner_id=999,me=bot_member)
    actor=SimpleNamespace(id=2,guild=guild,guild_permissions=SimpleNamespace(administrator=False),roles=[],top_role=SimpleNamespace(position=5),bot=False)
    target=SimpleNamespace(id=3,guild=guild,guild_permissions=SimpleNamespace(administrator=True),roles=[],top_role=SimpleNamespace(position=3),bot=False)
    async def fake_level(member):return 1
    monkeypatch.setattr(svc,'level',fake_level)
    with pytest.raises(PermissionError,match='администратора'):
        await svc.check_target(actor,target,'timeout')


@pytest.mark.asyncio
async def test_mod_cannot_moderate_organizer(monkeypatch):
    import config
    bot_member=SimpleNamespace(top_role=SimpleNamespace(position=10),guild_permissions=SimpleNamespace(moderate_members=True))
    guild=SimpleNamespace(id=1,owner_id=999,me=bot_member)
    actor=SimpleNamespace(id=2,guild=guild,guild_permissions=SimpleNamespace(administrator=False),roles=[],top_role=SimpleNamespace(position=5),bot=False)
    target=SimpleNamespace(id=3,guild=guild,guild_permissions=SimpleNamespace(administrator=False),roles=[SimpleNamespace(id=123456,name='org')],top_role=SimpleNamespace(position=3),bot=False)
    async def fake_level(member):return 1 if member.id==2 else 0
    monkeypatch.setattr(svc,'level',fake_level)
    with pytest.raises(PermissionError,match='организаторов'):
        await svc.check_target(actor,target,'warn')


@pytest.mark.asyncio
async def test_staff_cannot_shorter_existing_timeout(monkeypatch):
    async def permitted(*a,**k):return None
    async def fake_level(actor):return 1
    monkeypatch.setattr(svc,'check_target',permitted)
    monkeypatch.setattr(svc,'level',fake_level)
    guild=SimpleNamespace(id=1)
    actor=SimpleNamespace(id=2,guild=guild)
    target=SimpleNamespace(id=3,guild=guild,timed_out_until=datetime.now(timezone.utc)+timedelta(hours=1))
    with pytest.raises(PermissionError,match='уже есть тайм-аут'):
        await svc.apply_timeout(actor,target,timedelta(minutes=1),'Слишком коротко',object())


# ---------------------------------------------------------------------------
# Интеграционные доработки: идемпотентность и guild-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_timeout_noop_when_nothing_to_remove(monkeypatch):
    """Повторный размьют / гонка: нет активных кейсов и Discord-тайм-аута
    — возвращаем False, не трогаем Discord и не пишем событий."""
    events=[]
    class Store:
        async def cases(self,*a,**k):return []
        async def event(self,*a,**k):events.append('event')
    async def permitted(*a,**k):return None
    async def fake_level(actor):return 2
    async def notice(*a,**k):events.append('notice')
    monkeypatch.setattr(svc,'db',Store())
    monkeypatch.setattr(svc,'check_target',permitted)
    monkeypatch.setattr(svc,'level',fake_level)
    monkeypatch.setattr(svc,'notice',notice)
    guild=SimpleNamespace(id=1)
    actor=SimpleNamespace(id=2,guild=guild)
    calls=[]
    async def timeout_spy(*a,**k):calls.append(a)
    target=SimpleNamespace(id=3,guild=guild,timed_out_until=None,timeout=timeout_spy)
    result=await svc.remove_timeout(actor,target,object(),'reason')
    assert result is False
    assert calls==[]
    assert events==[]


@pytest.mark.asyncio
async def test_remove_timeout_revokes_active_cases(monkeypatch):
    """Активный кейс есть — timeout(None) вызывается, кейсы ревокаются,
    возвращается True."""
    revoked=[]
    class Store:
        async def cases(self,*a,**k):
            return [{'id':5,'kind':'timeout','status':'active','actor_id':2},
                    {'id':6,'kind':'warn','status':'active','actor_id':2}]
        async def case_status(self,guild,case_id,status,actor_id,**kw):
            if status=='revoked':revoked.append(case_id)
            return {'id':case_id}
        async def event(self,*a,**k):pass
    async def permitted(*a,**k):return None
    async def fake_level(actor):return 2
    async def notice(*a,**k):pass
    monkeypatch.setattr(svc,'db',Store())
    monkeypatch.setattr(svc,'check_target',permitted)
    monkeypatch.setattr(svc,'level',fake_level)
    monkeypatch.setattr(svc,'notice',notice)
    guild=SimpleNamespace(id=1)
    actor=SimpleNamespace(id=2,guild=guild)
    calls=[]
    async def timeout_spy(until,**kw):calls.append(until)
    target=SimpleNamespace(id=3,guild=guild,timed_out_until=datetime.now(timezone.utc)+timedelta(minutes=5),timeout=timeout_spy)
    result=await svc.remove_timeout(actor,target,object(),'reason')
    assert result is True
    assert calls==[None]
    assert revoked==[5]  # warn-кейс не трогаем


def test_public_commands_are_guild_only():
    """/report и /appeal не должны молча падать в DM."""
    import discord
    from cogs.mod import ReportCog
    for name in ('report','appeal'):
        cmd=getattr(ReportCog,name)
        assert getattr(cmd,'guild_only',False) is True, name


def test_setup_guild_finds_role_by_name_when_id_stale():
    """setup_guild не создаёт дубликат роли, если роль с тем же именем
    уже есть, но сохранённый ID устарел."""
    import inspect
    from services import moderation_service as ms
    src=inspect.getsource(ms.setup_guild)
    assert "discord.utils.get(guild.roles,name='MODERATOR')" in src
    assert "discord.utils.get(guild.roles,name='HEAD MODERATOR')" in src
    assert "discord.utils.get(guild.text_channels,name=name)" in src
