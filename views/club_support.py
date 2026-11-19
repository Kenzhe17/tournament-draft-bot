"""Private Hub entry points reusing the public moderation command handlers."""
from __future__ import annotations

import discord

from config import guild_allowed


class SupportModal(discord.ui.Modal):
    def __init__(self, kind: str, owner_id: int, guild_id: int):
        super().__init__(title='Жалоба' if kind == 'report' else 'Апелляция')
        self.kind, self.owner_id, self.guild_id = kind, owner_id, guild_id
        self.target = discord.ui.TextInput(
            label='ID участника' if kind == 'report' else 'Номер дела MOD', max_length=22)
        self.reason = discord.ui.TextInput(label='Причина', style=discord.TextStyle.paragraph,
                                          min_length=10, max_length=500)
        self.add_item(self.target)
        self.add_item(self.reason)
        if kind == 'report':
            self.message_url = discord.ui.TextInput(label='Ссылка на сообщение', required=False, max_length=200)
            self.add_item(self.message_url)

    async def on_submit(self, interaction: discord.Interaction):
        if (interaction.guild_id != self.guild_id or interaction.user.id != self.owner_id
                or not guild_allowed(interaction.guild_id)):
            await interaction.response.send_message('Этот запрос недоступен.', ephemeral=True)
            return
        raw = str(self.target).strip()
        if self.kind == 'appeal':
            raw = raw.removeprefix('MOD-').removeprefix('mod-')
        if not raw.isdecimal() or int(raw) <= 0:
            await interaction.response.send_message('Укажите корректный числовой ID или номер дела.', ephemeral=True)
            return
        command = interaction.client.tree.get_command(self.kind)
        if command is None or not await command._check_can_run(interaction):
            await interaction.response.send_message('Центр обращений временно недоступен.', ephemeral=True)
            return
        if self.kind == 'appeal':
            await command.callback(command.binding, interaction, int(raw), str(self.reason))
            return
        target = interaction.guild.get_member(int(raw))
        if target is None:
            try:
                target = await interaction.guild.fetch_member(int(raw))
            except discord.HTTPException:
                await interaction.response.send_message('Участник этого сервера не найден.', ephemeral=True)
                return
        await command.callback(command.binding, interaction, target, str(self.reason),
                               str(self.message_url).strip() or None)
