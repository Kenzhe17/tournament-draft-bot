"""Views for team management."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import ui

from config import replace_emojis
from models.team import Team
from storage.team_store import team_store

if TYPE_CHECKING:
    from bot import TournamentBot


class TeamStatusView(discord.ui.View):
    """View for team status with management buttons."""

    def __init__(self, team: Team, user_id: int, message=None):
        super().__init__(timeout=None)
        self.team = team
        self.user_id = user_id
        self.message = message
        self.refresh_buttons()

    def refresh_buttons(self):
        """Refresh buttons based on user role."""
        # Clear existing buttons
        for item in self.children[:]:
            self.remove_item(item)

        # Always show all buttons - permissions checked in callbacks
        self.add_item(TeamMenuButton(self.team, self))
        self.add_item(TeamLeaveButton(self.team, self.user_id, self))
        if not self.team.is_full():
            self.add_item(TeamJoinButton(self.team, self))

    async def update(self):
        """Update the status message."""
        if not self.message:
            return

        # Reload team from store
        updated_team = await team_store.refresh_from_db(self.team.team_id)
        if updated_team:
            self.team = updated_team

        # Don't refresh buttons - keep them as they were initially shown
        # Buttons will work based on their callbacks checking permissions

        # Rebuild member list
        member_mentions = []
        for member_id in self.team.members:
            member = self.message.guild.get_member(member_id)
            if member:
                mention = member.mention
                if member_id == self.team.captain_id:
                    mention += " 👑"
                member_mentions.append(mention)

        # Determine status
        count = self.team.member_count()
        if count >= 4:
            status_emoji = "🟢"
            status_text = "(Готова)"
        else:
            status_emoji = "🔴"
            missing = 4 - count
            # Fix grammar: "игрок" for 1, "игроков" for 2-4
            if missing == 1:
                status_text = f"(Недостаёт {missing} игрок)"
            else:
                status_text = f"(Недостаёт {missing} игроков)"

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {self.team.team_name}",
            description=(
                f"{replace_emojis('white_dot')} **Состав:** {count}/4\n"
                f"{replace_emojis('white_arrow')} {', '.join(member_mentions) if member_mentions else 'Нет участников'}\n\n"
                f"{replace_emojis('white_dot')} **Статус:** {status_emoji} {status_text}"
            ),
            color=discord.Color.from_rgb(114, 137, 218)
        )

        try:
            await self.message.edit(embed=embed, view=self)
        except (discord.NotFound, discord.Forbidden, AttributeError):
            # If message is not available or None, ignore
            pass


class TeamMenuButton(discord.ui.Button):
    """Button to open captain menu."""

    def __init__(self, team: Team, status_view=None):
        super().__init__(
            label="⚙️ Меню",
            style=discord.ButtonStyle.primary,
            custom_id="team_menu"
        )
        self.team = team
        self.status_view = status_view

    async def callback(self, interaction: discord.Interaction):
        if not self.team.is_captain(interaction.user.id):
            await interaction.response.send_message(
                replace_emojis("❌ Только капитан может открывать меню!"),
                ephemeral=True
            )
            return

        view = TeamMenuView(self.team, self.status_view)
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} Меню капитана",
            description=f"{replace_emojis('white_arrow')} Управление командой **{self.team.team_name}**",
            color=discord.Color.from_rgb(114, 137, 218)
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class TeamLeaveButton(discord.ui.Button):
    """Button to leave team."""

    def __init__(self, team: Team, user_id: int, status_view=None):
        super().__init__(
            label="🚪 Выйти",
            style=discord.ButtonStyle.danger,
            custom_id="team_leave"
        )
        self.team = team
        self.user_id = user_id
        self.status_view = status_view

    async def callback(self, interaction: discord.Interaction):
        if self.team.is_captain(interaction.user.id):
            await interaction.response.send_message(
                replace_emojis("❌ Капитан не может покинуть команду! Используйте `/team delete` для удаления команды."),
                ephemeral=True
            )
            return

        # Check if user is actually in the team
        if not self.team.is_member(interaction.user.id):
            await interaction.response.send_message(
                replace_emojis("❌ Вы не состоите в этой команде!"),
                ephemeral=True
            )
            return

        success = await team_store.remove_member(self.team.team_id, interaction.user.id)
        if not success:
            await interaction.response.send_message(
                replace_emojis("❌ Команда зарегистрирована в BR турнире! Нельзя менять состав."),
                ephemeral=True
            )
            return

        # Update the original status message
        if self.status_view:
            # Refresh team from database and update status message
            await self.status_view.update()
            # Update team reference in status_view
            updated_team = await team_store.refresh_from_db(self.team.team_id)
            if updated_team:
                self.status_view.team = updated_team

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} Вы покинули команду",
            description=f"{replace_emojis('white_arrow')} Вы вышли из команды **{self.team.team_name}**",
            color=discord.Color.from_rgb(235, 100, 100)
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)


class TeamJoinButton(discord.ui.Button):
    """Button to join team via password."""

    def __init__(self, team: Team, status_view=None):
        super().__init__(
            label="🔑 Войти",
            style=discord.ButtonStyle.success,
            custom_id="team_join"
        )
        self.team = team
        self.status_view = status_view

    async def callback(self, interaction: discord.Interaction):
        # Check if user is already in the team
        if self.team.is_member(interaction.user.id):
            await interaction.response.send_message(
                replace_emojis("❌ Вы уже состоите в этой команде!"),
                ephemeral=True
            )
            return

        modal = TeamJoinModal(self.team, self.status_view)
        await interaction.response.send_modal(modal)


class TeamJoinModal(discord.ui.Modal, title="Войти в команду"):
    """Modal for entering team password."""

    def __init__(self, team: Team, status_view=None):
        super().__init__()
        self.team = team
        self.status_view = status_view

    password = discord.ui.TextInput(
        label="Пароль команды",
        placeholder="Введите пароль...",
        style=discord.TextStyle.short,
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        if self.password.value != self.team.password:
            await interaction.response.send_message(
                replace_emojis("❌ Неверный пароль!"),
                ephemeral=True
            )
            return

        # Check if already in team
        if self.team.is_member(interaction.user.id):
            await interaction.response.send_message(
                replace_emojis("❌ Вы уже в этой команде!"),
                ephemeral=True
            )
            return

        # Check if team is full
        if self.team.is_full():
            await interaction.response.send_message(
                replace_emojis("❌ Команда заполнена!"),
                ephemeral=True
            )
            return

        success = await team_store.add_member(self.team.team_id, interaction.user.id)
        if not success:
            await interaction.response.send_message(
                replace_emojis("❌ Команда зарегистрирована в BR турнире! Нельзя менять состав."),
                ephemeral=True
            )
            return

        # Update status message
        if self.status_view:
            await self.status_view.update()
            # Update team reference in status_view
            updated_team = await team_store.refresh_from_db(self.team.team_id)
            if updated_team:
                self.status_view.team = updated_team

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} Вы вступили в команду",
            description=f"{replace_emojis('white_arrow')} Вы вступили в команду **{self.team.team_name}**",
            color=discord.Color.from_rgb(114, 137, 218)
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)


class TeamMenuView(discord.ui.View):
    """Captain menu view."""

    def __init__(self, team: Team, status_view=None):
        super().__init__(timeout=None)
        self.team = team
        self.status_view = status_view

        self.add_item(ChangeNameButton(self.team, self.status_view))
        self.add_item(ChangePasswordButton(self.team, self.status_view))
        self.add_item(KickMemberButton(self.team, self.status_view))


class ChangeNameButton(discord.ui.Button):
    """Button to change team name."""

    def __init__(self, team: Team, status_view=None):
        super().__init__(
            label="Сменить название",
            style=discord.ButtonStyle.primary,
            custom_id="team_change_name"
        )
        self.team = team
        self.status_view = status_view

    async def callback(self, interaction: discord.Interaction):
        modal = ChangeNameModal(self.team, self.status_view)
        await interaction.response.send_modal(modal)


class ChangeNameModal(discord.ui.Modal, title="Сменить название команды"):
    """Modal for changing team name."""

    def __init__(self, team: Team, status_view=None):
        super().__init__()
        self.team = team
        self.status_view = status_view

    new_name = discord.ui.TextInput(
        label="Новое название",
        placeholder="Введите новое название...",
        style=discord.TextStyle.short,
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        new_name = self.new_name.value

        # Check if name already exists (excluding current team)
        existing_team = team_store.get_by_name(self.team.guild_id, new_name)
        if existing_team and existing_team.team_id != self.team.team_id:
            await interaction.response.send_message(
                replace_emojis("❌ Это название уже занято другой командой!"),
                ephemeral=True
            )
            return

        self.team.team_name = new_name
        success = await team_store.update_team(self.team)
        if not success:
            await interaction.response.send_message(
                replace_emojis("❌ Команда зарегистрирована в BR турнире! Нельзя менять название."),
                ephemeral=True
            )
            return

        # Update status message if exists
        if self.status_view and self.status_view.message:
            # Refresh team from database and update status message
            await self.status_view.update()
            # Update team reference in status_view
            updated_team = await team_store.refresh_from_db(self.team.team_id)
            if updated_team:
                self.status_view.team = updated_team

        # Respond with success message (modal response)
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} Название изменено",
            description=f"{replace_emojis('white_arrow')} Новое название: **{self.team.team_name}**",
            color=discord.Color.from_rgb(114, 137, 218)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ChangePasswordButton(discord.ui.Button):
    """Button to change team password."""

    def __init__(self, team: Team, status_view=None):
        super().__init__(
            label="Сменить пароль",
            style=discord.ButtonStyle.primary,
            custom_id="team_change_password"
        )
        self.team = team
        self.status_view = status_view

    async def callback(self, interaction: discord.Interaction):
        modal = ChangePasswordModal(self.team, self.status_view)
        await interaction.response.send_modal(modal)


class ChangePasswordModal(discord.ui.Modal, title="Сменить пароль команды"):
    """Modal for changing team password."""

    def __init__(self, team: Team, status_view=None):
        super().__init__()
        self.team = team
        self.status_view = status_view

    new_password = discord.ui.TextInput(
        label="Новый пароль",
        placeholder="Введите новый пароль...",
        style=discord.TextStyle.short,
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        self.team.password = self.new_password.value
        success = await team_store.update_team(self.team)
        if not success:
            await interaction.response.send_message(
                replace_emojis("❌ Команда зарегистрирована в BR турнире! Нельзя менять пароль."),
                ephemeral=True
            )
            return

        # Update status message if exists
        if self.status_view and self.status_view.message:
            await self.status_view.update()
            # Update team reference in status_view
            updated_team = await team_store.refresh_from_db(self.team.team_id)
            if updated_team:
                self.status_view.team = updated_team

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} Пароль изменен",
            description=f"{replace_emojis('white_arrow')} Новый пароль: ||{self.team.password}||",
            color=discord.Color.from_rgb(114, 137, 218)
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)


class KickMemberButton(discord.ui.Button):
    """Button to kick member."""

    def __init__(self, team: Team, status_view=None):
        super().__init__(
            label="Кикнуть",
            style=discord.ButtonStyle.danger,
            custom_id="team_kick"
        )
        self.team = team
        self.status_view = status_view

    async def callback(self, interaction: discord.Interaction):
        # Build select options
        options = []
        for member_id in self.team.members:
            if member_id == self.team.captain_id:
                continue  # Cannot kick captain
            member = interaction.guild.get_member(member_id)
            if member:
                options.append(
                    discord.SelectOption(
                        label=member.display_name,
                        value=str(member_id)
                    )
                )

        if not options:
            await interaction.response.send_message(
                replace_emojis("❌ Нет участников для кика!"),
                ephemeral=True
            )
            return

        view = KickMemberSelectView(self.team, options, self.status_view)
        await interaction.response.send_message("Выберите участника для кика:", view=view, ephemeral=True)


class KickMemberSelectView(discord.ui.View):
    """View with select menu for kicking members."""

    def __init__(self, team: Team, options: list[discord.SelectOption], status_view=None):
        super().__init__(timeout=None)
        self.team = team
        self.status_view = status_view

        select = discord.ui.Select(
            placeholder="Выберите участника...",
            options=options,
            min_values=1,
            max_values=1
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        user_id = int(interaction.data["values"][0])

        success = await team_store.remove_member(self.team.team_id, user_id)
        if not success:
            await interaction.response.send_message(
                replace_emojis("❌ Команда зарегистрирована в BR турнире! Нельзя менять состав."),
                ephemeral=True
            )
            return

        # Update status message
        if self.status_view:
            await self.status_view.update()
            # Update team reference in status_view
            updated_team = await team_store.refresh_from_db(self.team.team_id)
            if updated_team:
                self.status_view.team = updated_team

        await interaction.response.send_message(
            replace_emojis("✅ Участник исключен из команды!"),
            ephemeral=True
        )
