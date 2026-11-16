"""Private Components V2 review UI for r!z3 moderators.

No persistent button state is needed; all authoritative state is in PostgreSQL.
Each callback checks staff role again, so a revoked moderator cannot use stale views.
"""
from __future__ import annotations

import discord
from storage.moderation_store import moderation_store as db
from services import moderation_service as ms

PAGE=5


def _safe(s):
    return str(s).replace('@everyone','＠everyone').replace('@here','＠here')[:300]


def _view(title,body):
    # timeout=None: ephemeral-панель не должна «умирать» через 3 минуты —
    # разбор жалоб/апелляций занимает больше времени. Права проверяются
    # при каждом callback, поэтому «вечный» view безопасен.
    v=discord.ui.LayoutView(timeout=None)
    c=discord.ui.Container()
    c.add_item(discord.ui.TextDisplay(content=f'> ## {title}'))
    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay(content=body[:3500] or 'Нет записей.'))
    v.add_item(c)
    return v,c


async def _must(interaction,required=1):
    try:
        await ms.require(interaction,required)
        return True
    except PermissionError:
        await interaction.response.send_message('Недостаточно прав модерации.',ephemeral=True)
        return False
    except Exception:
        # PG/Discord недоступны — не показываем «нет прав»,
        # чтобы не вводить сотрудника в заблуждение.
        import logging
        logging.getLogger(__name__).exception('mod panel permission check failed')
        try:
            await interaction.response.send_message('Ошибка модерации, попробуйте позже.',ephemeral=True)
        except Exception:
            pass
        return False


async def main(guild_id,owner_id):
    s=await db.stats(guild_id)
    body=(f'**Жалобы на рассмотрении:** {s["open_reports"]}\n'
          f'**Апелляции:** {s["open_appeals"]}\n'
          f'**Активные тайм-ауты:** {s["active_timeouts"]}\n'
          f'**События за 7 дней:** {s["events_week"]}\n\n'
          'Выберите раздел. Все решения сохраняются в PostgreSQL.')
    v,c=_view('r!z3 · ЦЕНТР МОДЕРАЦИИ',body)
    row=discord.ui.ActionRow()
    for name,view_fn in [('Жалобы',reports),('Апелляции',appeals),('История',history)]:
        b=discord.ui.Button(label=name,style=discord.ButtonStyle.secondary)
        async def on_click(i,fun=view_fn):
            if not await _must(i): return
            await i.response.edit_message(view=await fun(i.guild_id,i.user.id),allowed_mentions=discord.AllowedMentions.none())
        b.callback=on_click
        row.add_item(b)
    c.add_item(row)
    return v


async def reports(guild_id,owner_id,page=0):
    rows=await db.reports(guild_id,limit=100)
    pages=max(1,(len(rows)+PAGE-1)//PAGE)
    page=min(max(0,page),pages-1)
    visible=rows[page*PAGE:(page+1)*PAGE]
    body='\n'.join(f'**RPT-{r["id"]:06d}** · <@{r["target_id"]}> · {_safe(r["status"])}\n'
                   f'Заявитель: <@{r["reporter_id"]}> · {_safe(r["reason"][:100])}' for r in visible)
    v,c=_view('ЖАЛОБЫ',body or 'Открытых жалоб нет.')
    if visible:
        opts=[discord.SelectOption(label=f'RPT-{r["id"]:06d}',description=r['reason'][:90],value=str(r['id'])) for r in visible]
        row=discord.ui.ActionRow();sel=discord.ui.Select(placeholder='Открыть жалобу',options=opts)
        async def select(i):
            if not await _must(i): return
            item=await db.report(i.guild_id,int(sel.values[0]))
            await i.response.edit_message(view=await report_detail(i.guild_id,i.user.id,item),allowed_mentions=discord.AllowedMentions.none())
        sel.callback=select;row.add_item(sel);c.add_item(row)
    nav=discord.ui.ActionRow()
    for label,nxt in [('Назад',page-1),('Далее',page+1)]:
        b=discord.ui.Button(label=label,style=discord.ButtonStyle.secondary,disabled=nxt<0 or nxt>=pages)
        async def cb(i,n=nxt):
            if not await _must(i):return
            await i.response.edit_message(view=await reports(i.guild_id,i.user.id,n))
        b.callback=cb;nav.add_item(b)
    c.add_item(nav)
    _back(c)
    return v


async def report_detail(guild_id,owner_id,report):
    if not report:
        return await reports(guild_id,owner_id)
    body=(f'**RPT-{report["id"]:06d}** · {_safe(report["status"])}\n'
          f'Участник: <@{report["target_id"]}>\n'
          f'Заявитель: <@{report["reporter_id"]}>\n'
          f'Причина: {_safe(report["reason"])}\n'
          f'Сообщение: {_safe(report["message_url"] or "не указано")}\n'
          f'Назначено: <@{report["assignee_id"]}>' if report.get('assignee_id') else
          f'**RPT-{report["id"]:06d}** · {_safe(report["status"])}\nУчастник: <@{report["target_id"]}>\n'
          f'Заявитель: <@{report["reporter_id"]}>\nПричина: {_safe(report["reason"])}\n'
          f'Сообщение: {_safe(report["message_url"] or "не указано")}'
          )
    v,c=_view('КАРТОЧКА ЖАЛОБЫ',body)
    if report['status'] in ('open','claimed','escalated'):
        row=discord.ui.ActionRow()
        for label,action,style in [('Взять','claim',discord.ButtonStyle.primary),('Решено','resolve',discord.ButtonStyle.success),
                                   ('Отклонить','dismiss',discord.ButtonStyle.secondary),('Админу','escalate',discord.ButtonStyle.danger)]:
            b=discord.ui.Button(label=label,style=style)
            async def cb(i,a=action):
                if not await _must(i):return
                if a=='claim':
                    updated=await db.report_action(i.guild_id,report['id'],i.user.id,'claim')
                    if updated:
                        await ms.notice(i.client,i.guild_id,'Жалоба взята в работу',f'RPT-{report["id"]:06d} · <@{i.user.id}>')
                    await i.response.edit_message(view=await report_detail(i.guild_id,i.user.id,updated or report))
                else:
                    await i.response.send_modal(ReportDecisionModal(report['id'],a))
            b.callback=cb;row.add_item(b)
        c.add_item(row)
    _back(c,where='reports')
    return v


class ReportDecisionModal(discord.ui.Modal,title='Решение по жалобе'):
    note=discord.ui.TextInput(label='Обоснование решения',style=discord.TextStyle.paragraph,min_length=5,max_length=500)
    def __init__(self,report_id,action):
        super().__init__();self.report_id=report_id;self.action=action
    async def on_submit(self,interaction):
        if not await _must(interaction):return
        row=await db.report_action(interaction.guild_id,self.report_id,interaction.user.id,self.action,note=str(self.note))
        if not row:
            await interaction.response.send_message('Дело уже обработано другим модератором или назначено другому сотруднику.',ephemeral=True)
            return
        await ms.notice(interaction.client,interaction.guild_id,'Решение по жалобе',f'RPT-{self.report_id:06d} · {self.action}\nМодератор: <@{interaction.user.id}>\nОбоснование: {_safe(self.note)}')
        await interaction.response.edit_message(view=await report_detail(interaction.guild_id,interaction.user.id,row),allowed_mentions=discord.AllowedMentions.none())


async def appeals(guild_id,owner_id):
    rows=await db.appeals(guild_id)
    body='\n'.join(f'**APL-{r["id"]:06d}** · MOD-{r["case_id"]:06d} · <@{r["appellant_id"]}>\n{_safe(r["reason"][:120])}' for r in rows[:10])
    v,c=_view('АПЕЛЛЯЦИИ',body or 'Нет апелляций.')
    if rows:
        opts=[discord.SelectOption(label=f'APL-{r["id"]:06d}',value=str(r['id']),description=r['reason'][:90]) for r in rows[:25]]
        row=discord.ui.ActionRow();sel=discord.ui.Select(placeholder='Открыть апелляцию',options=opts)
        async def cb(i):
            if not await _must(i,2):return
            row=next((x for x in rows if x['id']==int(sel.values[0])),None)
            await i.response.edit_message(view=await appeal_detail(i.guild_id,i.user.id,row))
        sel.callback=cb;row.add_item(sel);c.add_item(row)
    _back(c);return v


async def appeal_detail(guild_id,owner_id,appeal):
    if not appeal:return await appeals(guild_id,owner_id)
    v,c=_view('АПЕЛЛЯЦИЯ',f'APL-{appeal["id"]:06d} · MOD-{appeal["case_id"]:06d}\n'
               f'Игрок: <@{appeal["appellant_id"]}>\nПричина: {_safe(appeal["reason"])}')
    row=discord.ui.ActionRow()
    for label,accepted in [('Принять',True),('Отклонить',False)]:
        b=discord.ui.Button(label=label,style=discord.ButtonStyle.success if accepted else discord.ButtonStyle.danger)
        async def cb(i,decision=accepted):
            if not await _must(i,2):return
            await i.response.send_modal(AppealDecisionModal(appeal['id'],decision))
        b.callback=cb;row.add_item(b)
    c.add_item(row)
    _back(c,where='appeals')
    return v


class AppealDecisionModal(discord.ui.Modal,title='Рассмотрение апелляции'):
    note=discord.ui.TextInput(label='Обоснование',style=discord.TextStyle.paragraph,min_length=5,max_length=500)
    def __init__(self,appeal_id,accepted):
        super().__init__();self.appeal_id=appeal_id;self.accepted=accepted
    async def on_submit(self,interaction):
        if not await _must(interaction,2):return
        row=await db.appeal_action(interaction.guild_id,self.appeal_id,interaction.user.id,self.accepted,str(self.note))
        if not row:
            await interaction.response.send_message('Апелляция уже рассмотрена.',ephemeral=True);return
        # Appeal acceptance is a review outcome, not a blind re-execution of Discord actions.
        # An authorised moderator must explicitly revoke a sanction if applicable.
        await ms.notice(interaction.client,interaction.guild_id,'Решение по апелляции',f'APL-{self.appeal_id:06d} · {"принята" if self.accepted else "отклонена"}\n'
                        f'Решение: {_safe(self.note)}\nСотрудник: <@{interaction.user.id}>')
        await interaction.response.edit_message(view=await appeals(interaction.guild_id,interaction.user.id))
        await interaction.followup.send('Если апелляция принята, снимите наказание отдельно через /mod revoke или /mod untimeout.',ephemeral=True)


async def history(guild_id,owner_id):
    cases=await db.cases(guild_id,limit=10)
    lines=[f'**MOD-{r["id"]:06d}** · {r["kind"]} · {r["status"]} · <@{r["target_id"]}>\n{_safe(r["reason"][:100])}' for r in cases]
    v,c=_view('ПОСЛЕДНИЕ ДЕЛА','\n'.join(lines) or 'Дел пока нет.')
    _back(c);return v


def _back(container,where='main'):
    row=discord.ui.ActionRow()
    b=discord.ui.Button(label='Назад',style=discord.ButtonStyle.secondary)
    async def cb(i):
        if not await _must(i):return
        fun={'main':main,'reports':reports,'appeals':appeals}[where]
        await i.response.edit_message(view=await fun(i.guild_id,i.user.id),allowed_mentions=discord.AllowedMentions.none())
    b.callback=cb;row.add_item(b);container.add_item(row)
