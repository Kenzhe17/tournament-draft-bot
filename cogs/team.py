"""Team commands — Squad Hub 2.0.

Slash-команды для обычных пользователей:
- /team create — мастер создания команды (кнопки + модалки)
- /team status — приватная панель команды / Squad Hub
- /team delete — подтверждение расформирования
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import replace_emojis
from storage.team_store import team_store

logger = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()


class TeamCog(commands.Cog):
    """Команды для управления командами."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    team = app_commands.Group(name="team", description="Управление командами")

    @team.command(name="create", description="Создать команду")
    async def create(self, interaction: discord.Interaction):
        """Открыть мастер создания команды."""
        from views.team_create_view import open_create_flow
        await open_create_flow(interaction)

    @team.command(name="delete", description="Расформировать команду")
    async def delete(self, interaction: discord.Interaction):
        """Расформировать команду (только капитан, с подтверждением)."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                replace_emojis("Команды работают только на сервере."),
                ephemeral=True)
            return
        team = team_store.get_by_member_in_guild(guild.id,
                                                 interaction.user.id)
        if team is None:
            await interaction.response.send_message(
                replace_emojis("Вы не состоите в команде!"),
                ephemeral=True)
            return
        if not team.is_captain(interaction.user.id):
            await interaction.response.send_message(
                replace_emojis(
                    "Расформировать команду может только капитан."),
                ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        from views.team_panel_view import open_disband_command
        await open_disband_command(interaction, team.team_id)

    @team.command(name="status", description="Панель команды / Squad Hub")
    @app_commands.describe(team_name="Название команды (опционально)")
    async def status(self, interaction: discord.Interaction,
                     team_name: str = None):
        """Открыть приватную панель команды."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                replace_emojis("Команды работают только на сервере."),
                ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        if team_name:
            team = team_store.get_by_name(guild.id, team_name)
            if team is None:
                await interaction.followup.send(
                    replace_emojis("Команда с таким названием не найдена!"),
                    ephemeral=True)
                return
            from views.team_panel_view import open_team_panel
            await open_team_panel(interaction, team.team_id)
            return

        team = team_store.get_by_member_in_guild(guild.id,
                                                 interaction.user.id)
        if team is not None:
            from views.team_panel_view import open_team_panel
            await open_team_panel(interaction, team.team_id)
            return

        # Нет команды — открыть Squad Hub меню
        from views.squad_hub_view import hub_menu_view
        await interaction.followup.send(
            view=hub_menu_view(), ephemeral=True,
            allowed_mentions=_NO_MENTIONS)

    @team.command(name="admin", description="Администрирование команд")
    async def admin(self, interaction: discord.Interaction):
        """Приватный админ-интерфейс команд (r!z3 admin-права)."""
        from views.team_admin_view import open_admin_panel
        await open_admin_panel(interaction)

    @status.autocomplete("team_name")
    async def status_autocomplete(
            self, interaction: discord.Interaction,
            current: str) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []
        cur = (current or "").lower()
        names = sorted(
            {t.team_name for t in team_store.get_by_guild(guild.id)
             if cur in t.team_name.lower()},
            key=str.lower)[:25]
        return [app_commands.Choice(name=n[:100], value=n)
                for n in names]


async def setup(bot: commands.Bot):
    """Setup the team cog + persistent views Squad Hub."""
    # Persistent dispatch для публичной карточки и рекрутинговых
    # карточек (custom_id-шаблоны squad:* / squad:rec:*).
    try:
        from views.squad_hub_view import (
            RecruitmentCardView, SquadHubView)
        bot.add_view(SquadHubView())
        bot.add_view(RecruitmentCardView())
    except Exception as exc:
        logger.warning("squad persistent views registration failed: %s",
                       exc)
    await bot.add_cog(TeamCog(bot))
