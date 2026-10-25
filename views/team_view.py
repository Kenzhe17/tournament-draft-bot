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

        # Menu button (captain only)
        if self.team.is_captain(self.user_id):
            self.add_item(TeamMenuButton(self.team, self))

        # Leave button (for members)
        if self.team.is_member(self.user_id):
            self.add_item(TeamLeaveButton(self.team, self.user_id, self))

        # Join button (for non-members, if team not full)
        if not self.team.is_member(self.user_id) and not self.team.is_full():
            self.add_item(TeamJoinButton(self.team, self))

    async def update(self):
        """Update the status message."""
        if not self.message:
            return

        # Reload team from store
        updated_team = team_store.get(self.team.team_id)
        if updated_team:
            self.team = updated_team

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
            status_text = "Зелёный (Готова)"
        else:
            status_emoji = "🔴"
            missing = 4 - count
            status_text = f"Красный (Недостаёт {missing} игроков)"

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {self.team.team_name}",
            description=(
                f"{replace_emojis('white_dot')} **Состав:** {count}/4\n"
                f"{replace_emojis('white_arrow')} {', '.join(member_mentions) if member_mentions else 'Нет участников'}\n\n"
                f"{replace_emojis('white_dot')} **Статус:** {status_emoji} {status_text}"
            ),
            color=discord.Color.from_rgb(114, 137, 218)
        )

        # Refresh view
        self.refresh_buttons()

        try:
            await self.message.edit(embed=embed, view=self)
        except Exception:
            # If message is not available, ignore
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

        await team_store.remove_member(self.team.team_id, interaction.user.id)

        # Update the original status message
        if self.status_view:
            await self.status_view.update()

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
                replace_emojis("❌ Не удалось добавить в команду!"),
                ephemeral=True
            )
            return

        # Update status message
        if self.status_view:
            await self.status_view.update()

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} Вы вступили в команду",
            description=f"{replace_emojis('white_arrow')} Вы вступили в команду **{self.team.team_name}**",
            color=discord.Color.from_rgb(114, 137, 218)
        )

        await interaction.response.edit_message(embed=embed)


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
        self.team.team_name = self.new_name.value
        await team_store.update_team(self.team)

        # Update status message
        if self.status_view:
            await self.status_view.update()

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} Название изменено",
            description=f"{replace_emojis('white_arrow')} Новое название: **{self.team.team_name}**",
            color=discord.Color.from_rgb(114, 137, 218)
        )

        await interaction.response.edit_message(embed=embed)


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
        await team_store.update_team(self.team)

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} Пароль изменен",
            description=f"{replace_emojis('white_arrow')} Новый пароль: ||{self.team.password}||",
            color=discord.Color.from_rgb(114, 137, 218)
        )

        await interaction.response.edit_message(embed=embed)


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
                replace_emojis("❌ Не удалось исключить участника!"),
                ephemeral=True
            )
            return

        # Update status message
        if self.status_view:
            await self.status_view.update()

        await interaction.response.send_message(
            replace_emojis("✅ Участник исключен из команды!"),
            ephemeral=True
        )
