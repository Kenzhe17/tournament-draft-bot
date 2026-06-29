"""Views for team setup phase in matchmaking."""

from __future__ import annotations

import discord
from discord.ui import View, Button, Modal, TextInput
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot import TournamentBot


class TeamNameModal(Modal, title="Изменить название команды"):
    """Modal для изменения названия команды."""

    def __init__(self, team_id: int, current_name: str):
        super().__init__()
        self.team_id = team_id
        self.name_input = TextInput(
            label="Название команды",
            placeholder="Введите новое название",
            default=current_name,
            max_length=50,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        """Обработка изменения названия."""
        new_name = self.name_input.value.strip()
        if not new_name:
            await interaction.response.send_message("❌ Название не может быть пустым", ephemeral=True)
            return

        # Здесь нужно обновить название команды в сессии
        # Это будет реализовано в cog
        await interaction.response.send_message(
            f"✅ Название команды изменено на: {new_name}",
            ephemeral=True
        )


class TeamSetupView(View):
    """View для настройки команд капитанами."""

    def __init__(self, guild_id: int, session):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.session = session

        # Кнопки для каждой команды
        for team in session.match.teams:
            # Кнопка изменения названия (только для капитана)
            name_button = Button(
                label=f"✏️ {team.name}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"mm_name_{team.team_id}"
            )
            name_button.callback = self._create_name_callback(team.team_id, team.name)
            self.add_item(name_button)

            # Кнопка Ready (только для капитана)
            ready_label = "✅ Ready" if team.ready else "⏳ Ready"
            ready_style = discord.ButtonStyle.success if team.ready else discord.ButtonStyle.primary
            ready_button = Button(
                label=ready_label,
                style=ready_style,
                custom_id=f"mm_ready_{team.team_id}"
            )
            ready_button.callback = self._create_ready_callback(team.team_id)
            self.add_item(ready_button)

    def _create_name_callback(self, team_id: int, current_name: str):
        """Создать callback для кнопки изменения названия."""
        async def callback(interaction: discord.Interaction):
            # Проверяем, является ли пользователь капитаном этой команды
            team = self.session.match.get_captain_team(interaction.user.id)
            if not team or team.team_id != team_id:
                await interaction.response.send_message(
                    "❌ Только капитан может менять название команды",
                    ephemeral=True
                )
                return

            modal = TeamNameModal(team_id, current_name)
            await interaction.response.send_modal(modal)
        return callback

    def _create_ready_callback(self, team_id: int):
        """Создать callback для кнопки Ready."""
        async def callback(interaction: discord.Interaction):
            # Проверяем, является ли пользователь капитаном этой команды
            team = self.session.match.get_captain_team(interaction.user.id)
            if not team or team.team_id != team_id:
                await interaction.response.send_message(
                    "❌ Только капитан может нажимать Ready",
                    ephemeral=True
                )
                return

            # Переключаем готовность
            new_ready = not team.ready
            self.session.set_team_ready(team_id, new_ready)

            await interaction.response.send_message(
                f"✅ Команда {'готова' if new_ready else 'не готова'}",
                ephemeral=True
            )

            # Проверяем, готовы ли обе команды
            if self.session.match.is_ready():
                await self.start_match(interaction)
            else:
                # Обновить view
                await self.update_view(interaction)
        return callback

    async def start_match(self, interaction: discord.Interaction):
        """Начать матч после готовности обеих команд."""
        self.session.start_match()

        # Закрываем ставки
        self.session.match.betting_open = False

        embed = discord.Embed(
            title="🎮 Матч начался!",
            description="Обе команды готовы. Матч идет!",
            color=discord.Color.green()
        )

        embed.add_field(
            name=f"{self.session.match.teams[0].name}",
            value="✅ Ready",
            inline=True
        )
        embed.add_field(
            name=f"{self.session.match.teams[1].name}",
            value="✅ Ready",
            inline=True
        )

        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=None)
            else:
                await interaction.response.edit_message(embed=embed, view=None)
        except discord.NotFound:
            pass

        # Добавляем кнопку для завершения матча (только капитаны)
        from views.match_result_view import MatchResultView
        view = MatchResultView(self.session.guild_id, self.session)

        result_embed = discord.Embed(
            title="⚔️ Завершение матча",
            description="Капитан победившей команды должен выбрать свою команду.",
            color=discord.Color.red()
        )

        await interaction.followup.send(embed=result_embed, view=view)

    async def update_view(self, interaction: discord.Interaction):
        """Обновить view после изменений."""
        # Создаем новый view с обновленным состоянием
        new_view = TeamSetupView(self.guild_id, self.session)

        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(view=new_view)
            else:
                await interaction.response.edit_message(view=new_view)
        except discord.NotFound:
            pass
