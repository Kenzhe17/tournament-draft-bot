"""Explicit administrator entry point for plan/apply/rollback; no startup mutations."""
from __future__ import annotations

import io
import json
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from cogs.groups import bind_commands, server_group
from config import BOT_OWNER_ID, guild_allowed
from services import server_layout
from storage.server_layout_store import server_layout_store
from utils.permissions import is_admin


def _is_owner(interaction: discord.Interaction) -> bool:
    """OWNER = владелец сервера или BOT_OWNER_ID. Apply/rollback — только он."""
    uid = interaction.user.id
    if BOT_OWNER_ID and uid == BOT_OWNER_ID:
        return True
    guild = interaction.guild
    return bool(guild and getattr(guild, 'owner_id', 0) == uid)


class ServerReconstructionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        bind_commands(server_group, self, ['reconstruction', 'notification_roles'])

    @server_group.command(name='reconstruction', description='План, применение и откат структуры сервера без удаления каналов')
    @app_commands.guild_only()
    @is_admin()
    @app_commands.describe(action='Сначала plan, затем apply с ID проверенного плана',
                           plan_id='ID ранее сформированного плана',
                           archive_ids='ID лишних каналов через запятую; только для plan',
                           moves='Переносы "id:категория[=новое имя]" через запятую; только для plan',
                           categories='Переиспользование категорий "ЦЕЛЕВАЯ=СУЩЕСТВУЮЩАЯ" через запятую')
    async def reconstruction(self, interaction: discord.Interaction,
                             action: Literal['plan', 'status', 'apply', 'rollback'],
                             plan_id: str = '', archive_ids: str = '',
                             moves: str = '', categories: str = ''):
        if not guild_allowed(interaction.guild_id):
            await interaction.response.send_message('Этот сервер не обслуживается.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if action in ('apply', 'rollback') and not _is_owner(interaction):
                raise ValueError('Применение и откат реконструкции — только OWNER.')
            if action in ('apply', 'rollback') and not interaction.guild.me.guild_permissions.manage_channels:
                raise ValueError('Боту необходимо право управления каналами.')
            if archive_ids and action != 'plan':
                raise ValueError('archive_ids задаётся только при планировании.')
            if moves and action != 'plan':
                raise ValueError('moves задаётся только при планировании.')
            if categories and action != 'plan':
                raise ValueError('categories задаётся только при планировании.')
            if action == 'plan':
                tokens = [v.strip() for v in archive_ids.split(',') if v.strip()]
                if any(not v.isdecimal() or int(v) <= 0 for v in tokens):
                    raise ValueError('Нужны положительные числовые ID каналов через запятую.')
                move_map: dict[int, str] = {}
                move_names: dict[int, str] = {}
                for pair in moves.split(','):
                    pair = pair.strip()
                    if not pair:
                        continue
                    if ':' not in pair:
                        raise ValueError('moves: формат "id:категория[=имя]" через запятую.')
                    cid_s, rest = pair.split(':', 1)
                    if not cid_s.strip().isdecimal() or int(cid_s) <= 0:
                        raise ValueError('moves: нужен положительный числовой ID канала.')
                    cat, _, new_name = rest.partition('=')
                    cat = cat.strip()
                    if cat not in server_layout.CATEGORIES or cat == 'лишние-каналы':
                        raise ValueError(f'moves: недопустимая категория {cat!r}.')
                    move_map[int(cid_s)] = cat
                    if new_name.strip():
                        if len(new_name.strip()) > 100:
                            raise ValueError('moves: имя канала длиннее 100 символов.')
                        move_names[int(cid_s)] = new_name.strip()
                cat_map: dict[str, str] = {}
                for pair in categories.split(','):
                    pair = pair.strip()
                    if not pair:
                        continue
                    if '=' not in pair:
                        raise ValueError('categories: формат "ЦЕЛЕВАЯ=СУЩЕСТВУЮЩАЯ" через запятую.')
                    target, existing = (v.strip() for v in pair.split('=', 1))
                    if target not in server_layout.CATEGORIES:
                        raise ValueError(f'categories: недопустимая целевая категория {target!r}.')
                    if not existing:
                        raise ValueError('categories: укажите имя существующей категории.')
                    cat_map[target] = existing
                result = await server_layout.prepare(
                    interaction.guild, {int(v) for v in tokens},
                    move_map, move_names, cat_map)
            elif action == 'status':
                result = server_layout_store.read(interaction.guild_id)
            elif action == 'apply':
                result = await server_layout.apply(interaction.guild, plan_id)
            else:
                result = await server_layout.rollback(interaction.guild, plan_id)
            payload = json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8')
            current = result.get('plan', result)
            summary = f"План: {current.get('id', 'нет')}. Статус: {current.get('status', 'не создан')}."
            if action == 'plan':
                summary += (f" Операций: {len(result['operations'])}. "
                            f"Архив: {len(result['archive_selected'])}. "
                            "Сверьте приложенные исходные права/каналы и операции перед apply. "
                            "Неуказанные лишние каналы автоматически не определяются.")
            await interaction.followup.send(summary, ephemeral=True,
                file=discord.File(io.BytesIO(payload), filename=f"r!z3-layout-{interaction.guild_id}.json"),
                allowed_mentions=discord.AllowedMentions.none())
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(f'Перестройка остановлена: {exc}', ephemeral=True,
                                             allowed_mentions=discord.AllowedMentions.none())

    @server_group.command(name='notification_roles', description='Создать безопасные роли добровольных уведомлений')
    @app_commands.guild_only()
    @is_admin()
    async def notification_roles(self, interaction: discord.Interaction):
        if not guild_allowed(interaction.guild_id):
            await interaction.response.send_message('Этот сервер не обслуживается.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            from services.notification_preferences import provision
            if not interaction.guild.me.guild_permissions.manage_roles:
                raise ValueError('Боту необходимо право управления ролями.')
            await provision(interaction.guild)
            await interaction.followup.send('Подписки настроены: HUB → Помощь → Уведомления.', ephemeral=True)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(str(exc), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(ServerReconstructionCog(bot))
