"""r!z3 moderation: RBAC, sanction execution and audit notifications.

Moderator != tournament organizer. Never import utils.permissions.is_admin_check here.
Discord actions are never automatically retried after ambiguous network failures.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import discord

from config import (BOT_OWNER_ID, ADMIN_USER_IDS, MOD_LOG_CHANNEL_ID, MOD_LOG_WEBHOOK_URL,
                    ORG_ROLE_ID, ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID, MANAGER_ROLE_ID)
from storage.moderation_store import moderation_store as db

log=logging.getLogger(__name__)
_DURATION=re.compile(r'(?i)^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$')
_JUMP=re.compile(r'^https://(?:discord\.com|canary\.discord\.com|ptb\.discord\.com)/channels/(\d+)/(\d+)/(\d+)$')


def parse_duration(raw:str)->timedelta | None:
    text=raw.strip().lower()
    m=_DURATION.fullmatch(text)
    if not m or not any(m.groups()):
        return None
    d,h,minutes=(int(n or 0) for n in m.groups())
    seconds=d*86400+h*3600+minutes*60
    if seconds < 60 or seconds>28*86400:
        return None
    return timedelta(seconds=seconds)


def valid_message_url(url:str|None,guild_id:int)->str|None:
    if not url: return None
    m=_JUMP.fullmatch(url.strip())
    if not m or int(m.group(1))!=guild_id:
        raise ValueError('Нужна ссылка на сообщение этого сервера Discord.')
    return url.strip()


def clean_reason(s:str)->str:
    s=s.strip().replace('@everyone','＠everyone').replace('@here','＠here')
    return s[:500]


def is_admin(member:discord.Member)->bool:
    return bool(member.guild_permissions.administrator or member.id == member.guild.owner_id
                or member.id==BOT_OWNER_ID or member.id in ADMIN_USER_IDS)


async def level(member:discord.Member)->int:
    """0 player; 1 moderator; 2 head moderator; 3 admin. DB required for staff roles."""
    if is_admin(member): return 3
    conf=await db.settings(member.guild.id)
    roles={r.id for r in member.roles}
    if conf.get('head_role_id') and conf['head_role_id'] in roles:return 2
    if conf.get('moderator_role_id') and conf['moderator_role_id'] in roles:return 1
    return 0


async def require(interaction:discord.Interaction,threshold:int=1)->int:
    if not isinstance(interaction.user,discord.Member) or not interaction.guild:
        raise PermissionError('Только на сервере.')
    staff=await level(interaction.user)
    if staff < threshold:
        raise PermissionError('Недостаточно прав модерации.')
    return staff


def _bot_member(guild:discord.Guild)->discord.Member|None:
    return guild.me


async def check_target(actor:discord.Member,target:discord.Member,action:str,*,trusted_gif=False):
    """Check actor and bot hierarchy before creating a sanction case."""
    if actor.guild.id != target.guild.id:
        raise PermissionError('Участник из другого сервера.')
    if target.id in (actor.id,actor.guild.owner_id) or target.bot:
        raise PermissionError('Нельзя наказать себя, владельца или бота.')
    actor_level=await level(actor)
    if actor_level < (2 if action=='kick' else 3 if action=='ban' else 1) and not trusted_gif:
        raise PermissionError('Недостаточно прав для этого действия.')
    if target.guild_permissions.administrator:
        raise PermissionError('Нельзя наказать администратора сервера.')
    target_level=await level(target)
    if actor_level<3 and ((trusted_gif and target_level>0) or
                          (not trusted_gif and target_level>=actor_level)):
        raise PermissionError('Нельзя применять наказание к сотруднику равного или более высокого уровня.')
    protected={ORG_ROLE_ID,ORG_1_ROLE_ID,ORG_2_ROLE_ID,ORG_3_ROLE_ID,MANAGER_ROLE_ID}
    if actor_level<3 and any(r.id in protected or r.name.lower()=='org' for r in target.roles):
        raise PermissionError('Наказания организаторов и менеджеров рассматривает администратор.')
    if trusted_gif and not is_admin(actor) and actor.top_role<=target.top_role:
        raise PermissionError('Для GIF-модерации цель должна быть ниже вашей роли.')
    bot=_bot_member(actor.guild)
    if bot is None or bot.top_role<=target.top_role:
        raise PermissionError('Роль бота должна быть выше роли участника.')
    perms=bot.guild_permissions
    if action=='timeout' and not perms.moderate_members:
        raise PermissionError('Боту требуется Moderate Members.')
    if action=='kick' and not perms.kick_members:
        raise PermissionError('Боту требуется Kick Members.')
    if action=='ban' and not perms.ban_members:
        raise PermissionError('Боту требуется Ban Members.')


async def _log(bot,guild_id:int,title:str,body:str,*,reports=False):
    """Discord is a view, DB is authoritative. Log failures must not undo actions."""
    try:
        conf=await db.settings(guild_id)
        channel_id=(conf.get('reports_channel_id') if reports else conf.get('log_channel_id')) or (0 if reports else MOD_LOG_CHANNEL_ID)
        if not channel_id and not (MOD_LOG_WEBHOOK_URL and not reports): return
        container=discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(content='## '+title[:100]))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(content=body[:3000]))
        v=discord.ui.LayoutView(timeout=None);v.add_item(container)
        if channel_id:
            channel=bot.get_channel(int(channel_id))
            if channel is None:channel=await bot.fetch_channel(int(channel_id))
            await channel.send(view=v,allowed_mentions=discord.AllowedMentions.none())
        else:
            webhook=discord.Webhook.from_url(MOD_LOG_WEBHOOK_URL,client=bot)
            await webhook.send(view=v,username='r!z3 Moderation',allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        log.exception('moderation Discord publication failed')


async def notice(bot,guild_id:int,title:str,body:str,*,reports=False):
    await _log(bot,guild_id,title,body,reports=reports)


def ref(case:dict)->str:
    return f"MOD-{case['id']:06d}"


async def apply_warning(actor:discord.Member,target:discord.Member,reason:str,bot,*,url=None):
    await check_target(actor,target,'warn')
    reason=clean_reason(reason)
    if not reason: raise ValueError('Укажи причину предупреждения.')
    url=valid_message_url(url,actor.guild.id)
    rec=await db.create_case(actor.guild.id,target.id,actor.id,'warn',reason,uuid.uuid4().hex,
                             expires_at=datetime.now(timezone.utc)+timedelta(days=30),message_url=url)
    await db.case_status(actor.guild.id,rec['id'],'active',actor.id,expected=('pending',))
    await notice(bot,actor.guild.id,'Предупреждение '+ref(rec),f'Участник: <@{target.id}>\nМодератор: <@{actor.id}>\nПричина: {reason}')
    return rec


async def apply_timeout(actor:discord.Member,target:discord.Member,duration:timedelta,reason:str,bot,*,trusted_gif=False,url=None):
    await check_target(actor,target,'timeout',trusted_gif=trusted_gif)
    reason=clean_reason(reason)
    if not reason: raise ValueError('Укажи причину.')
    now=datetime.now(timezone.utc)
    existing=target.timed_out_until
    if existing and existing>now and await level(actor)<3 and not trusted_gif:
        raise PermissionError('У участника уже есть тайм-аут. Изменение срока доступно администратору.')
    expires=now+duration
    rec=await db.create_case(actor.guild.id,target.id,actor.id,'gif_timeout' if trusted_gif else 'timeout',reason,
                             uuid.uuid4().hex,expires_at=expires,message_url=valid_message_url(url,actor.guild.id))
    try:
        await target.timeout(expires,reason=f'{ref(rec)} · {reason[:380]}')
    except discord.Forbidden:
        await db.case_status(actor.guild.id,rec['id'],'revoked',actor.id,details={'error':'discord_forbidden'},expected=('pending',))
        raise PermissionError('Discord запретил тайм-аут.')
    except discord.HTTPException:
        await db.case_status(actor.guild.id,rec['id'],'unknown',actor.id,details={'error':'discord_http_unknown'},expected=('pending',))
        raise RuntimeError(f'{ref(rec)}: результат Discord неизвестен, проверь тайм-аут участника. Не повторяй команду вслепую.')
    await db.case_status(actor.guild.id,rec['id'],'active',actor.id,expected=('pending',))
    # A new timeout supersedes the previous native Discord timeout.
    for old in await db.cases(actor.guild.id,target_id=target.id,limit=50):
        if old['id']!=rec['id'] and old['kind'] in ('timeout','gif_timeout') and old['status']=='active':
            await db.case_status(actor.guild.id,old['id'],'revoked',actor.id,
                                 details={'superseded_by':rec['id']},expected=('active',))
    await notice(bot,actor.guild.id,'Тайм-аут '+ref(rec),f'Участник: <@{target.id}>\nМодератор: <@{actor.id}>\nДо: <t:{int(expires.timestamp())}:F>\nПричина: {reason}')
    return rec


async def remove_timeout(actor:discord.Member,target:discord.Member,bot,reason:str='Снят модератором',*,trusted_gif=False):
    await check_target(actor,target,'timeout',trusted_gif=trusted_gif)
    cases=await db.cases(actor.guild.id,target_id=target.id,limit=50)
    current=[r for r in cases if r['kind'] in ('timeout','gif_timeout') and r['status']=='active']
    # Идемпотентность: снимать нечего — ни зарегистрированного
    # тайм-аута, ни Discord-native. Иначе повторный GIF-размьют
    # или гонка двух модераторов писали бы ложные события.
    if not current and not getattr(target,'timed_out_until',None):
        return False
    staff=await level(actor)
    if staff<3 and not current:
        raise PermissionError('Тайм-аут не зарегистрирован в боте; снять может только администратор.')
    if staff<2 and not trusted_gif and not any(r['actor_id']==actor.id for r in current):
        raise PermissionError('Модератор может снять только свой тайм-аут.')
    await target.timeout(None,reason=clean_reason(reason))
    changed=[]
    for item in cases:
        if item['kind'] in ('timeout','gif_timeout') and item['status']=='active':
            if await db.case_status(actor.guild.id,item['id'],'revoked',actor.id,expected=('active',)):
                changed.append(item['id'])
    await db.event(actor.guild.id,actor.id,'timeout.removed',detail={'target_id':target.id,'case_ids':changed})
    await notice(bot,actor.guild.id,'Тайм-аут снят',f'Участник: <@{target.id}>\nМодератор: <@{actor.id}>')
    return True


async def apply_remove(actor:discord.Member,target:discord.Member,kind:str,reason:str,bot):
    if kind not in ('kick','ban'):
        raise ValueError('Unsupported moderation action')
    await check_target(actor,target,kind)
    reason=clean_reason(reason)
    if not reason: raise ValueError('Укажи причину.')
    rec=await db.create_case(actor.guild.id,target.id,actor.id,kind,reason,uuid.uuid4().hex)
    try:
        if kind=='kick':await target.kick(reason=f'{ref(rec)} · {reason[:380]}')
        else:await target.ban(reason=f'{ref(rec)} · {reason[:380]}',delete_message_seconds=0)
    except discord.Forbidden:
        await db.case_status(actor.guild.id,rec['id'],'revoked',actor.id,expected=('pending',))
        raise PermissionError('Недостаточно прав Discord.')
    except discord.HTTPException:
        await db.case_status(actor.guild.id,rec['id'],'unknown',actor.id,expected=('pending',))
        raise RuntimeError(f'{ref(rec)}: Discord не подтвердил результат. Проверь аудит сервера.')
    await db.case_status(actor.guild.id,rec['id'],'completed',actor.id,expected=('pending',))
    await notice(bot,actor.guild.id,kind.upper()+' '+ref(rec),f'Участник: <@{target.id}>\nМодератор: <@{actor.id}>\nПричина: {reason}')
    return rec


async def revoke_warning(actor:discord.Member,case_id:int,bot):
    if await level(actor)<2: raise PermissionError('Снятие предупреждений доступно старшему модератору.')
    rec=await db.case(actor.guild.id,case_id)
    if not rec or rec['kind']!='warn' or rec['status']!='active':
        raise ValueError('Активное предупреждение не найдено.')
    await db.case_status(actor.guild.id,case_id,'revoked',actor.id,expected=('active',))
    await notice(bot,actor.guild.id,'Предупреждение снято',f'{ref(rec)} · <@{rec["target_id"]}> · снял <@{actor.id}>')
    return rec


async def send_user_notice(user:discord.abc.User,summary:str):
    try:
        await user.send(summary,allowed_mentions=discord.AllowedMentions.none())
        return True
    except (discord.Forbidden,discord.HTTPException):
        return False


async def setup_guild(interaction:discord.Interaction):
    """Explicit administrator action: create roles/private channels and persist IDs.

    Does not attempt to move hierarchy or grant Administrator to any role.
    """
    guild=interaction.guild
    if not guild or not isinstance(interaction.user,discord.Member) or not is_admin(interaction.user):
        raise PermissionError('Настройка доступна только администраторам.')
    me=guild.me
    if not me or not me.guild_permissions.manage_roles or not me.guild_permissions.manage_channels:
        raise PermissionError('Для установки боту нужны Manage Roles и Manage Channels.')
    conf=await db.settings(guild.id)
    # Ищем по сохранённому ID, затем по имени — удалённые и
    # пересозданные вручную роли/каналы не должны дублироваться.
    mod_role=(guild.get_role(conf.get('moderator_role_id') or 0)
              or discord.utils.get(guild.roles,name='MODERATOR'))
    head_role=(guild.get_role(conf.get('head_role_id') or 0)
               or discord.utils.get(guild.roles,name='HEAD MODERATOR'))
    if mod_role is None:
        # Least privilege: moderators act through logged /mod commands.
        perms=discord.Permissions.none()
        mod_role=await guild.create_role(name='MODERATOR',permissions=perms,colour=discord.Colour(0x295E56),reason='r!z3 MODERATION setup')
    if head_role is None:
        # Head moderators can kick via bot but receive no native Administrator power.
        perms=discord.Permissions.none()
        head_role=await guild.create_role(name='HEAD MODERATOR',permissions=perms,colour=discord.Colour(0x1C4A42),reason='r!z3 MODERATION setup')
    await db.put_settings(guild.id,moderator_role_id=mod_role.id,head_role_id=head_role.id)
    everyone=guild.default_role
    overwrites={
        everyone:discord.PermissionOverwrite(view_channel=False),
        mod_role:discord.PermissionOverwrite(view_channel=True,read_message_history=True,send_messages=False),
        head_role:discord.PermissionOverwrite(view_channel=True,read_message_history=True,send_messages=True),
        me:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True),
    }
    for key,name in (('log_channel_id','mod-logs'),('reports_channel_id','mod-reports')):
        existing=guild.get_channel(conf.get(key) or 0)
        if not isinstance(existing,discord.TextChannel):
            # Нет канала или по ID лежит не текстовый канал —
            # ищем по имени, чтобы не плодить дубликаты.
            existing=discord.utils.get(guild.text_channels,name=name)
        local=dict(overwrites)
        if key=='reports_channel_id':
            local[mod_role]=discord.PermissionOverwrite(view_channel=True,read_message_history=True,send_messages=True)
        if isinstance(existing,discord.TextChannel):
            # Канал уже есть — точечно синхронизируем только свои
            # overwrites, чужие разрешения не трогаем.
            for target,ow in local.items():
                try:
                    await existing.set_permissions(target,overwrite=ow,reason='r!z3 MODERATION setup sync')
                except discord.HTTPException as exc:
                    log.warning('mod channel overwrite sync failed %s/%s: %s',guild.id,existing.id,exc)
            await db.put_settings(guild.id,**{key:existing.id})
        else:
            channel=await guild.create_text_channel(name,overwrites=local,reason='r!z3 MODERATION setup')
            await db.put_settings(guild.id,**{key:channel.id})
    return await db.settings(guild.id)


async def reconcile_guild(guild:discord.Guild):
    """Reconcile Discord-native timeouts without replaying irreversible sanctions.

    Pending kick/ban remain UNKNOWN for administrator review; no blind retry.
    """
    try:
        candidates=await db.reconcile_candidates(guild.id)
    except Exception:
        log.exception('moderation recovery read failed for guild %s',guild.id)
        return
    now=datetime.now(timezone.utc)
    for rec in candidates:
        try:
            if rec['status']=='active' and rec.get('expires_at') and rec['expires_at']<=now:
                await db.case_status(guild.id,rec['id'],'expired',guild.me.id,
                                     expected=('active',))
                continue
            if rec['kind'] in ('warn','note') and rec['status']=='pending':
                await db.case_status(guild.id,rec['id'],'active' if rec['kind']=='warn' else 'completed',
                                     guild.me.id,expected=('pending',))
                continue
            if rec['kind'] not in ('timeout','gif_timeout'):
                if rec['status']=='pending':
                    await db.case_status(guild.id,rec['id'],'unknown',guild.me.id,expected=('pending',))
                continue
            member=guild.get_member(rec['target_id'])
            if member is None:
                try:member=await guild.fetch_member(rec['target_id'])
                except discord.HTTPException:continue
            actual=member.timed_out_until
            expected=rec.get('expires_at')
            if actual and expected and abs((actual-expected).total_seconds())<=120 and actual>now:
                await db.case_status(guild.id,rec['id'],'active',guild.me.id,
                                     expected=('pending','unknown'))
            elif rec['status']=='pending':
                await db.case_status(guild.id,rec['id'],'unknown',guild.me.id,expected=('pending',))
        except Exception:
            log.exception('moderation reconciliation error for case %s',rec['id'])
