"""Club Hub entry point and explicit, administrator-only publication."""
from __future__ import annotations

import asyncio
import json

import discord
from discord import app_commands
from discord.ext import commands

from cogs.groups import bind_commands, server_group
from config import DATA_DIR, guild_allowed
from storage.json_atomic import atomic_write_json
from utils.permissions import is_admin
from views.club_hub import HubView

HUB_STATE = DATA_DIR / 'club_hub.json'


class ClubHubCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._publish_lock = asyncio.Lock()

    async def cog_load(self):
        bind_commands(server_group, self, ['hub_publish', 'room_cards_secure'])
        self.bot.add_view(HubView())

    @app_commands.command(name='hub', description='Главный центр r!z3')
    @app_commands.guild_only()
    async def hub(self, interaction: discord.Interaction):
        if not guild_allowed(interaction.guild_id):
            await interaction.response.send_message('Этот сервер не обслуживается.', ephemeral=True)
            return
        await interaction.response.send_message(view=HubView(), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @server_group.command(name='hub_publish', description='Опубликовать или обновить главную панель r!z3')
    @app_commands.guild_only()
    @is_admin()
    async def publish(self, interaction: discord.Interaction):
        if not guild_allowed(interaction.guild_id):
            await interaction.response.send_message('Этот сервер не обслуживается.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self._publish_lock:
            # A corrupt state must not be treated as an empty one (duplicate panels).
            state = json.loads(HUB_STATE.read_text()) if HUB_STATE.exists() else {}
            key = str(interaction.guild_id)
            saved = state.get(key)
            if saved and saved.get('pending'):
                await interaction.followup.send(
                    'Предыдущая публикация не подтверждена. Проверьте канал перед восстановлением состояния панели.',
                    ephemeral=True)
                return
            if saved:
                channel = self.bot.get_channel(saved['channel_id'])
                if channel is None:
                    channel = await self.bot.fetch_channel(saved['channel_id'])
                # Do not recreate after Forbidden/timeouts; only definite NotFound.
                try:
                    message = await channel.fetch_message(saved['message_id'])
                except discord.NotFound:
                    message = None
                if message:
                    await message.edit(view=HubView(), allowed_mentions=discord.AllowedMentions.none())
                    await interaction.followup.send(f'Панель обновлена: {message.jump_url}', ephemeral=True)
                    return
            state[key] = {'channel_id': interaction.channel_id, 'pending': True}
            atomic_write_json(HUB_STATE, state)
            message = await interaction.channel.send(view=HubView(), allowed_mentions=discord.AllowedMentions.none())
            state[key] = {'channel_id': interaction.channel_id, 'message_id': message.id}
            atomic_write_json(HUB_STATE, state)
            await interaction.followup.send(f'Панель опубликована: {message.jump_url}', ephemeral=True)

    @server_group.command(name='room_cards_secure', description='Обновить опубликованные карточки комнат до текущего оформления')
    @app_commands.guild_only()
    @is_admin()
    async def secure_rooms(self, interaction: discord.Interaction):
        if not guild_allowed(interaction.guild_id):
            await interaction.response.send_message('Этот сервер не обслуживается.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        from services.room_publisher import secure_published_cards
        result = await secure_published_cards(self.bot, interaction.guild_id)
        await interaction.followup.send(
            f"Обновлено: {result['updated']}. Удалённых карточек: {result['missing']}. Ошибок: {result['failed']}.",
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(ClubHubCog(bot))
