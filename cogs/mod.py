"""/mod staff group + public /report, /appeal and message context menu.

Install via bot.load_extension('cogs.mod') after cogs.moderation.
All staff permissions are independent of organizer/VAC permissions.
"""
from __future__ import annotations

import logging
import uuid

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services import moderation_service as ms
from storage.moderation_store import moderation_store as db
from views import mod_panel
from config import GUILD_ID

logger=logging.getLogger(__name__)


def _short(s):
    return str(s).replace('@everyone','＠everyone').replace('@here','＠here')[:300]


async def _fail(interaction:discord.Interaction,exception:Exception):
    text='Ошибка выполнения операции. Попробуйте позже.'
    if isinstance(exception,(ValueError,PermissionError,RuntimeError)):
        text=str(exception)[:1000]
    else:
        logger.exception('Moderation unexpected error',exc_info=exception)
    if interaction.response.is_done():
        await interaction.followup.send(text,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(text,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())


def _me(guild:discord.Guild)->discord.Member:
    """guild.me может быть None до завершения chunking — не падать."""
    me=guild.me
    if me is None:
        raise PermissionError('Бот не найден на сервере.')
    return me


async def _require(interaction,minimum=1):
    try:
        return await ms.require(interaction,minimum)
    except Exception as exc:
        await _fail(interaction,exc)
        return 0


class ModeratorCog(commands.GroupCog,group_name='mod',group_description='Центр модерации r!z3'):
    def __init__(self,bot):
        self.bot=bot

    async def cog_load(self):
        self.reconcile_periodic.start()

    async def cog_unload(self):
        self.reconcile_periodic.cancel()

    @tasks.loop(minutes=30)
    async def reconcile_periodic(self):
        for guild in self.bot.guilds:
            await ms.reconcile_guild(guild)

    @reconcile_periodic.before_loop
    async def before_reconcile(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        # Native timeouts survive restarts; reconcile unacknowledged API results.
        for guild in self.bot.guilds:
            await ms.reconcile_guild(guild)

    @app_commands.command(name='setup',description='Создать роли и закрытые каналы модерации')
    async def setup_server(self,interaction:discord.Interaction):
        if not await _require(interaction,3):return
        await interaction.response.defer(ephemeral=True,thinking=True)
        try:
            conf=await ms.setup_guild(interaction)
            await db.event(interaction.guild_id,interaction.user.id,'moderation.setup',
                detail={'moderator_role_id':conf['moderator_role_id'],
                        'head_role_id':conf['head_role_id'],
                        'log_channel_id':conf['log_channel_id'],
                        'reports_channel_id':conf['reports_channel_id']})
            await interaction.followup.send('Модерация настроена.\n'
                f'MODERATOR: <@&{conf["moderator_role_id"]}>\n'
                f'HEAD MODERATOR: <@&{conf["head_role_id"]}>\n'
                f'Журнал: <#{conf["log_channel_id"]}>\n'
                f'Жалобы: <#{conf["reports_channel_id"]}>\n'
                '**Проверьте иерархию ролей вручную: роли должны быть ниже роли бота и выше модерируемых участников.**',
                ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='configure',description='Привязать существующие роли и каналы к модерации')
    async def configure(self,interaction:discord.Interaction,moderator:discord.Role|None=None,
                        head_moderator:discord.Role|None=None,log_channel:discord.TextChannel|None=None,
                        reports_channel:discord.TextChannel|None=None):
        if not await _require(interaction,3):return
        try:
            vals={}
            if moderator:vals['moderator_role_id']=moderator.id
            if head_moderator:vals['head_role_id']=head_moderator.id
            if log_channel:vals['log_channel_id']=log_channel.id
            if reports_channel:vals['reports_channel_id']=reports_channel.id
            for role in (moderator,head_moderator):
                if role and (role.is_default() or role.managed or role.permissions.administrator):
                    raise ValueError('Нельзя использовать @everyone, интеграционную или административную роль.')
            if moderator and head_moderator and moderator.id==head_moderator.id:
                raise ValueError('MODERATOR и HEAD MODERATOR должны быть разными ролями.')
            for channel in (log_channel,reports_channel):
                if channel and channel.permissions_for(interaction.guild.default_role).view_channel:
                    raise ValueError('Канал должен быть закрыт для @everyone.')
            if not vals:raise ValueError('Выбери хотя бы одну роль или канал.')
            old=await db.settings(interaction.guild_id)
            mod_id=vals.get('moderator_role_id') or old.get('moderator_role_id')
            head_id=vals.get('head_role_id') or old.get('head_role_id')
            if mod_id and head_id and mod_id==head_id:
                raise ValueError('Нельзя назначить одну роль одновременно MODERATOR и HEAD MODERATOR.')
            await db.put_settings(interaction.guild_id,**vals)
            await db.event(interaction.guild_id,interaction.user.id,'moderation.configured',detail=vals)
            await interaction.response.send_message('Настройки сохранены.',ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='panel',description='Открыть панель модератора')
    async def panel(self,interaction:discord.Interaction):
        if not await _require(interaction):return
        try:
            v=await mod_panel.main(interaction.guild_id,interaction.user.id)
            await interaction.response.send_message(view=v,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='warn',description='Выдать предупреждение участнику')
    async def warn(self,interaction:discord.Interaction,user:discord.Member,reason:str,message_url:str|None=None):
        if not await _require(interaction):return
        await interaction.response.defer(thinking=True,ephemeral=True)
        try:
            r=await ms.apply_warning(interaction.user,user,reason,self.bot,url=message_url)
            await interaction.followup.send(f'Предупреждение {ms.ref(r)} выдано <@{user.id}>.',ephemeral=True)
            await ms.send_user_notice(user,f'r!z3 · Вы получили предупреждение {ms.ref(r)}. Причина: {_short(reason)}. Обжаловать: /appeal {r["id"]}.')
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='timeout',description='Временно ограничить участника (до 28 дней)')
    async def timeout(self,interaction:discord.Interaction,user:discord.Member,duration:str,reason:str):
        if not await _require(interaction):return
        td=ms.parse_duration(duration)
        if not td:
            await interaction.response.send_message('Длительность: например 10m, 2h, 1d12h; от 1 минуты до 28 дней.',ephemeral=True);return
        await interaction.response.defer(thinking=True,ephemeral=True)
        try:
            r=await ms.apply_timeout(interaction.user,user,td,reason,self.bot)
            await interaction.followup.send(f'Тайм-аут {ms.ref(r)} установлен для <@{user.id}>.',ephemeral=True)
            await ms.send_user_notice(user,f'r!z3 · Тайм-аут {ms.ref(r)} до <t:{int(r["expires_at"].timestamp())}:F>. Причина: {_short(reason)}.')
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='untimeout',description='Снять тайм-аут участника')
    async def untimeout(self,interaction:discord.Interaction,user:discord.Member,reason:str='Снят модератором'):
        if not await _require(interaction):return
        await interaction.response.defer(thinking=True,ephemeral=True)
        try:
            removed=await ms.remove_timeout(interaction.user,user,self.bot,reason)
            await interaction.followup.send(
                'Тайм-аут снят.' if removed else 'Активного тайм-аута нет.',
                ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='kick',description='Исключить участника (старшие модераторы)')
    async def kick(self,interaction:discord.Interaction,user:discord.Member,reason:str):
        if not await _require(interaction,2):return
        await interaction.response.defer(thinking=True,ephemeral=True)
        try:
            r=await ms.apply_remove(interaction.user,user,'kick',reason,self.bot)
            await interaction.followup.send(f'Участник исключён, дело {ms.ref(r)}.',ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='ban',description='Забанить участника (только администраторы)')
    async def ban(self,interaction:discord.Interaction,user:discord.Member,reason:str):
        if not await _require(interaction,3):return
        await interaction.response.defer(thinking=True,ephemeral=True)
        try:
            r=await ms.apply_remove(interaction.user,user,'ban',reason,self.bot)
            await interaction.followup.send(f'Участник забанен, дело {ms.ref(r)}.',ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='unban',description='Снять бан пользователя по ID (администратор)')
    async def unban(self,interaction:discord.Interaction,user_id:str,reason:str='Апелляция удовлетворена'):
        if not await _require(interaction,3):return
        await interaction.response.defer(thinking=True,ephemeral=True)
        try:
            uid=int(user_id)
            if uid<=0:raise ValueError('Некорректный ID.')
            await interaction.guild.unban(discord.Object(id=uid),reason=ms.clean_reason(reason))
            await db.event(interaction.guild_id,interaction.user.id,'ban.removed',detail={'target_id':uid,'reason':ms.clean_reason(reason)})
            await ms.notice(self.bot,interaction.guild_id,'Бан снят',f'Участник: <@{uid}>\nАдминистратор: <@{interaction.user.id}>')
            await interaction.followup.send(f'Бан <@{uid}> снят.',ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='revoke',description='Снять предупреждение по номеру дела')
    async def revoke(self,interaction:discord.Interaction,case_id:int):
        if not await _require(interaction,2):return
        try:
            rec=await ms.revoke_warning(interaction.user,case_id,self.bot)
            await interaction.response.send_message(f'{ms.ref(rec)} отменено.',ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='note',description='Служебная заметка без наказания')
    async def note(self,interaction:discord.Interaction,user:discord.Member,text:str):
        if not await _require(interaction):return
        try:
            await ms.check_target(interaction.user,user,'warn')
            rec=await db.create_case(interaction.guild_id,user.id,interaction.user.id,'note',ms.clean_reason(text),uuid.uuid4().hex)
            await db.case_status(interaction.guild_id,rec['id'],'completed',interaction.user.id,expected=('pending',))
            await interaction.response.send_message(f'Заметка {ms.ref(rec)} сохранена.',ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='history',description='История наказаний конкретного игрока')
    async def history(self,interaction:discord.Interaction,user:discord.User):
        if not await _require(interaction):return
        try:
            rows=await db.cases(interaction.guild_id,target_id=user.id,limit=15)
            text='\n'.join(f'`{ms.ref(r)}` {r["kind"]} · {r["status"]} · {_short(r["reason"][:100])}' for r in rows) or 'Нет дел.'
            v,_=mod_panel._view('ИСТОРИЯ УЧАСТНИКА',f'<@{user.id}>\n\n'+text)
            await interaction.response.send_message(view=v,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='case',description='Посмотреть конкретное дело и его историю')
    async def case(self,interaction:discord.Interaction,case_id:int):
        if not await _require(interaction):return
        try:
            rec=await db.case(interaction.guild_id,case_id)
            if not rec:raise ValueError('Дело не найдено.')
            ev=await db.events(interaction.guild_id,case_id=case_id)
            text=(f'**{ms.ref(rec)}** · {rec["kind"]} · {rec["status"]}\nУчастник: <@{rec["target_id"]}>\n'
                  f'Сотрудник: <@{rec["actor_id"]}>\nПричина: {_short(rec["reason"])}\n\n'
                  + '\n'.join(f'{x["action"]} · <t:{int(x["created_at"].timestamp())}:f>' for x in ev[:12]))
            v,_=mod_panel._view('ДЕЛО МОДЕРАЦИИ',text)
            await interaction.response.send_message(view=v,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='reports',description='Просмотреть очередь жалоб')
    async def reports(self,interaction:discord.Interaction):
        if not await _require(interaction):return
        try:await interaction.response.send_message(view=await mod_panel.reports(interaction.guild_id,interaction.user.id),ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='appeals',description='Рассмотреть апелляции')
    async def appeals(self,interaction:discord.Interaction):
        if not await _require(interaction,2):return
        try:await interaction.response.send_message(view=await mod_panel.appeals(interaction.guild_id,interaction.user.id),ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='delete',description='Удалить сообщение по ссылке с записью действия')
    async def delete_message(self,interaction:discord.Interaction,message_url:str,reason:str):
        if not await _require(interaction):return
        await interaction.response.defer(ephemeral=True,thinking=True)
        try:
            link=ms.valid_message_url(message_url,interaction.guild_id)
            parts=link.rstrip('/').split('/')
            channel_id,message_id=int(parts[-2]),int(parts[-1])
            channel=interaction.guild.get_channel_or_thread(channel_id)
            if channel is None:
                channel=await self.bot.fetch_channel(channel_id)
            if channel.guild.id != interaction.guild_id:
                raise PermissionError('Сообщение другого сервера.')
            if not channel.permissions_for(_me(interaction.guild)).manage_messages:
                raise PermissionError('У бота нет Manage Messages в этом канале.')
            msg=await channel.fetch_message(message_id)
            sender=msg.author
            if isinstance(sender,discord.Member):
                await ms.check_target(interaction.user,sender,'warn')
            elif await ms.level(interaction.user)<3:
                raise PermissionError('Удаление сообщений ботов доступно администраторам.')
            await msg.delete(reason=ms.clean_reason(reason))
            await db.event(interaction.guild_id,interaction.user.id,'message.deleted',
                detail={'target_id':sender.id,'message_url':link,'reason':ms.clean_reason(reason)})
            await ms.notice(self.bot,interaction.guild_id,'Сообщение удалено',
                f'Модератор: <@{interaction.user.id}>\nАвтор: <@{sender.id}>\nСсылка: {link}\nПричина: {_short(reason)}')
            await interaction.followup.send('Сообщение удалено, действие записано.',ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='purge',description='Очистить до 50 сообщений в текущем канале (старший модератор)')
    async def purge(self,interaction:discord.Interaction,count:app_commands.Range[int,1,50],reason:str):
        if not await _require(interaction,2):return
        await interaction.response.defer(ephemeral=True,thinking=True)
        try:
            channel=interaction.channel
            if not isinstance(channel,discord.TextChannel):
                raise ValueError('Только текстовый канал сервера.')
            if not channel.permissions_for(_me(interaction.guild)).manage_messages:
                raise PermissionError('Боту требуется Manage Messages.')
            deleted=await channel.purge(limit=count,reason=ms.clean_reason(reason),bulk=True)
            await db.event(interaction.guild_id,interaction.user.id,'messages.purged',
                detail={'channel_id':channel.id,'requested':count,'deleted':len(deleted),'reason':ms.clean_reason(reason)})
            await ms.notice(self.bot,interaction.guild_id,'Очистка сообщений',
                f'Канал: <#{channel.id}>\nУдалено: {len(deleted)}\nСотрудник: <@{interaction.user.id}>')
            await interaction.followup.send(f'Удалено сообщений: {len(deleted)}.',ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='evidence',description='Добавить ссылку на доказательство в дело')
    async def evidence(self,interaction:discord.Interaction,case_id:int,message_url:str,note:str=''):
        if not await _require(interaction):return
        try:
            rec=await db.case(interaction.guild_id,case_id)
            if not rec:raise ValueError('Дело не найдено.')
            link=ms.valid_message_url(message_url,interaction.guild_id)
            await db.event(interaction.guild_id,interaction.user.id,'case.evidence',case_id=case_id,
                detail={'url':link,'note':ms.clean_reason(note)})
            await interaction.response.send_message('Доказательство добавлено.',ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    async def _voice_action(self,interaction:discord.Interaction,user:discord.Member,field:str,value:bool,reason:str):
        if not await _require(interaction):return
        await interaction.response.defer(thinking=True,ephemeral=True)
        try:
            await ms.check_target(interaction.user,user,'warn')
            if not user.voice or not user.voice.channel:
                raise ValueError('Участник сейчас не находится в голосовом канале.')
            needed='mute_members' if field=='mute' else 'deafen_members'
            if not getattr(_me(interaction.guild).guild_permissions,needed,False):
                raise PermissionError('У бота нет разрешения '+needed)
            await db.event(interaction.guild_id,interaction.user.id,f'voice.{field}.requested',
                detail={'target_id':user.id,'value':value,'reason':ms.clean_reason(reason)})
            await user.edit(**{field:value},reason=f'r!z3 · {_short(reason)}')
            await db.event(interaction.guild_id,interaction.user.id,f'voice.{field}.applied',
                detail={'target_id':user.id,'value':value,'reason':ms.clean_reason(reason)})
            await ms.notice(self.bot,interaction.guild_id,'Голосовая модерация',
                f'Участник: <@{user.id}>\nДействие: {field}={value}\nМодератор: <@{interaction.user.id}>')
            await interaction.followup.send('Действие выполнено и записано.',ephemeral=True)
        except Exception as exc:
            try:
                await db.event(interaction.guild_id,interaction.user.id,f'voice.{field}.unknown',
                    detail={'target_id':user.id,'value':value})
            except Exception:logger.exception('Could not record voice action failure')
            await _fail(interaction,exc)

    @app_commands.command(name='voicemute',description='Отключить микрофон в голосовом канале')
    async def voicemute(self,interaction:discord.Interaction,user:discord.Member,reason:str):
        await self._voice_action(interaction,user,'mute',True,reason)

    @app_commands.command(name='voiceunmute',description='Включить микрофон в голосовом канале')
    async def voiceunmute(self,interaction:discord.Interaction,user:discord.Member,reason:str):
        await self._voice_action(interaction,user,'mute',False,reason)

    @app_commands.command(name='voicedeafen',description='Отключить звук в голосовом канале')
    async def voicedeafen(self,interaction:discord.Interaction,user:discord.Member,reason:str):
        await self._voice_action(interaction,user,'deafen',True,reason)

    @app_commands.command(name='voiceundeafen',description='Включить звук в голосовом канале')
    async def voiceundeafen(self,interaction:discord.Interaction,user:discord.Member,reason:str):
        await self._voice_action(interaction,user,'deafen',False,reason)

    @app_commands.command(name='voicemove',description='Переместить участника в другой голосовой канал')
    async def voicemove(self,interaction:discord.Interaction,user:discord.Member,
                        channel:discord.VoiceChannel,reason:str):
        if not await _require(interaction):return
        await interaction.response.defer(thinking=True,ephemeral=True)
        try:
            await ms.check_target(interaction.user,user,'warn')
            if not user.voice or not user.voice.channel:
                raise ValueError('Участник сейчас не в голосовом канале.')
            if not interaction.guild.me.guild_permissions.move_members:
                raise PermissionError('У бота нет Move Members.')
            await db.event(interaction.guild_id,interaction.user.id,'voice.move.requested',
                detail={'target_id':user.id,'channel_id':channel.id,'reason':ms.clean_reason(reason)})
            await user.move_to(channel,reason=ms.clean_reason(reason))
            await db.event(interaction.guild_id,interaction.user.id,'voice.move.applied',
                detail={'target_id':user.id,'channel_id':channel.id})
            await interaction.followup.send(f'Участник перемещён в {channel.name}.',ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='slowmode',description='Установить медленный режим в текстовом канале (старший модератор)')
    async def slowmode(self,interaction:discord.Interaction,seconds:app_commands.Range[int,0,21600],
                       reason:str):
        if not await _require(interaction,2):return
        await interaction.response.defer(thinking=True,ephemeral=True)
        try:
            channel=interaction.channel
            if not isinstance(channel,discord.TextChannel):raise ValueError('Только обычный текстовый канал.')
            if not channel.permissions_for(_me(interaction.guild)).manage_channels:
                raise PermissionError('Боту требуется Manage Channels.')
            previous=channel.slowmode_delay
            await channel.edit(slowmode_delay=seconds,reason=ms.clean_reason(reason))
            await db.event(interaction.guild_id,interaction.user.id,'channel.slowmode.changed',
                detail={'channel_id':channel.id,'before':previous,'after':seconds,'reason':ms.clean_reason(reason)})
            await ms.notice(self.bot,interaction.guild_id,'Медленный режим',
                f'Канал: <#{channel.id}>\n{previous} → {seconds} секунд\nСотрудник: <@{interaction.user.id}>')
            await interaction.followup.send(f'Режим установлен: {seconds} сек.',ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.command(name='stats',description='Статистика команды модерации')
    async def stats(self,interaction:discord.Interaction):
        if not await _require(interaction):return
        try:await interaction.response.send_message(view=await mod_panel.main(interaction.guild_id,interaction.user.id),ephemeral=True)
        except Exception as exc:await _fail(interaction,exc)


class ReportCog(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        self._appeal_cd={}

    @commands.Cog.listener()
    async def on_message(self,message:discord.Message):
        """Внешняя апелляция для забаненных: ЛС боту `appeal <MOD-id> <причина>`.
        create_appeal проверяет target_id дела — чужие дела недоступны,
        дедупликация по (case_id, appellant) встроена в store."""
        if message.author.bot or message.guild is not None:
            return
        text=message.content.strip()
        if not text.lower().startswith('appeal '):
            return
        parts=text.split(maxsplit=2)
        if len(parts)<3 or not parts[1].isdigit():
            await message.channel.send('Формат: appeal <номер дела, напр. 123> <причина>')
            return
        now=__import__('time').monotonic()
        if now-self._appeal_cd.get(message.author.id,0)<60:
            return
        self._appeal_cd[message.author.id]=now
        case_id=int(parts[1]);reason=ms.clean_reason(parts[2])
        if len(reason)<10:
            await message.channel.send('Опиши причину обжалования подробнее (от 10 символов).');return
        gid=GUILD_ID or (self.bot.guilds[0].id if self.bot.guilds else None)
        if not gid:
            await message.channel.send('Сервис временно недоступен.');return
        try:
            r=await db.create_appeal(gid,case_id,message.author.id,reason)
        except Exception:
            logger.exception('DM appeal failed')
            await message.channel.send('Не удалось принять апелляцию, попробуйте позже.');return
        if not r:
            await message.channel.send('Дело не найдено, не принадлежит тебе или апелляция уже создана.')
            return
        await message.channel.send(f'Апелляция APL-{r["id"]:06d} принята на рассмотрение.')
        await ms.notice(self.bot,gid,'Апелляция из ЛС',
            f'APL-{r["id"]:06d} на дело MOD-{case_id:06d}\nИгрок: <@{message.author.id}>\nИсточник: ЛС (вне сервера)')

    @commands.Cog.listener()
    async def on_automod_action(self,action:discord.AutoModAction):
        """Record native Discord AutoMod alerts; do not automatically ban/VAC."""
        try:
            gid=action.guild_id
            await db.event(gid,int(self.bot.user.id), 'automod.action', detail={
                'rule_id':action.rule_id,
                'user_id':action.user_id,
                'channel_id':action.channel_id,
                'message_id':getattr(action,'message_id',None),
            })
            await ms.notice(self.bot,gid,'Discord AutoMod',
                f'Правило: `{action.rule_id}`\nУчастник: <@{action.user_id}>\n'
                f'Канал: <#{action.channel_id}>')
        except Exception:
            logger.exception('AutoMod log failed')

    @app_commands.guild_only()
    @app_commands.command(name='report',description='Отправить конфиденциальную жалобу модераторам')
    async def report(self,interaction:discord.Interaction,user:discord.Member,reason:str,message_url:str|None=None):
        if not interaction.guild or not isinstance(interaction.user,discord.Member):return
        if user.id==interaction.user.id or user.bot:
            await interaction.response.send_message('Нельзя пожаловаться на себя или бота.',ephemeral=True);return
        try:
            url=ms.valid_message_url(message_url,interaction.guild_id)
            reason=ms.clean_reason(reason)
            if len(reason)<10:raise ValueError('Опиши нарушение подробнее (от 10 символов).')
            r=await db.create_report(interaction.guild_id,interaction.user.id,user.id,reason,url)
            if not r:
                await interaction.response.send_message('Не чаще одной жалобы в минуту.',ephemeral=True);return
            await interaction.response.send_message(f'Жалоба RPT-{r["id"]:06d} получена. Модераторы рассмотрят её.',ephemeral=True)
            await ms.notice(self.bot,interaction.guild_id,'Новая жалоба',f'RPT-{r["id"]:06d}\nУчастник: <@{user.id}>\nЗаявитель: <@{interaction.user.id}>\nПричина: {_short(reason)}',reports=True)
        except Exception as exc:await _fail(interaction,exc)

    @app_commands.guild_only()
    @app_commands.command(name='appeal',description='Обжаловать наказание по номеру дела')
    async def appeal(self,interaction:discord.Interaction,case_id:int,reason:str):
        if not interaction.guild:return
        try:
            reason=ms.clean_reason(reason)
            if len(reason)<10:raise ValueError('Опиши причину обжалования подробнее.')
            r=await db.create_appeal(interaction.guild_id,case_id,interaction.user.id,reason)
            if not r:
                await interaction.response.send_message('Дело не принадлежит тебе или апелляция уже создана.',ephemeral=True);return
            await interaction.response.send_message(f'Апелляция APL-{r["id"]:06d} принята.',ephemeral=True)
            await ms.notice(self.bot,interaction.guild_id,'Новая апелляция',f'APL-{r["id"]:06d} на дело MOD-{case_id:06d}\nИгрок: <@{interaction.user.id}>',reports=True)
        except Exception as exc:await _fail(interaction,exc)


class MessageReportModal(discord.ui.Modal,title='Жалоба на сообщение'):
    reason=discord.ui.TextInput(label='Причина жалобы',style=discord.TextStyle.paragraph,min_length=10,max_length=500)
    def __init__(self,msg:discord.Message):
        super().__init__();self._target=msg.author.id;self._jump=msg.jump_url
    async def on_submit(self,interaction:discord.Interaction):
        if not interaction.guild:return
        try:
            r=await db.create_report(interaction.guild_id,interaction.user.id,self._target,ms.clean_reason(str(self.reason)),self._jump)
            if not r:
                await interaction.response.send_message('Не чаще одной жалобы в минуту.',ephemeral=True);return
            await interaction.response.send_message(f'Жалоба RPT-{r["id"]:06d} получена.',ephemeral=True)
            await ms.notice(interaction.client,interaction.guild_id,'Жалоба на сообщение',f'RPT-{r["id"]:06d}\nСообщение: {self._jump}\nУчастник: <@{self._target}>',reports=True)
        except Exception as exc:await _fail(interaction,exc)


async def report_message(interaction:discord.Interaction,message:discord.Message):
    if not interaction.guild or message.guild.id != interaction.guild.id:
        await interaction.response.send_message('Сообщение другого сервера.',ephemeral=True);return
    if message.author.id==interaction.user.id or message.author.bot:
        await interaction.response.send_message('Нельзя жаловаться на себя или бота.',ephemeral=True);return
    await interaction.response.send_modal(MessageReportModal(message))


async def setup(bot):
    await bot.add_cog(ModeratorCog(bot))
    await bot.add_cog(ReportCog(bot))
    bot.tree.add_command(app_commands.ContextMenu(name='Пожаловаться в r!z3',callback=report_message))


async def teardown(bot):
    try:bot.tree.remove_command('Пожаловаться в r!z3',type=discord.AppCommandType.message)
    except Exception:pass
