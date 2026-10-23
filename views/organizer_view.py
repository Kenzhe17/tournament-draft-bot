"""Views for organizer management."""

import logging
from typing import TYPE_CHECKING

import discord
from discord.ui import Modal, TextInput, View, Button, button, Select

from config import BOT_OWNER_ID, MANAGER_ROLE_ID, replace_emojis, get_emoji
from storage.organizer_store import organizer_store
from storage.json_store import store

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class OrganizerManagementButton(Button):
    """Button to open organizer management menu."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Управление организаторами",
            custom_id=f"org_manage:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check if user has MANAGER_ROLE_ID
        role = interaction.guild.get_role(MANAGER_ROLE_ID)
        if not role or role not in interaction.user.roles:
            await interaction.response.send_message(
                f"❌ У вас нет роли для управления организаторами.",
                ephemeral=True
            )
            return

        # Open management view
        view = OrganizerMenuView(self.guild_id, self.channel_id)
        await interaction.response.send_message(view=view, ephemeral=True)


class OrganizerMenuView(View):
    """Main menu for organizer management."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id

        # Add button to add organizer
        self.add_item(AddOrganizerButton(guild_id, channel_id))

        # Add button to transfer organizer
        self.add_item(TransferOrganizerButton(guild_id, channel_id))

        # Add buttons to remove each organizer
        channel_orgs = organizer_store.get(guild_id, channel_id)
        if channel_orgs:
            all_orgs = channel_orgs.all_organizers()
            for org_id in all_orgs:
                self.add_item(RemoveOrganizerButton(guild_id, channel_id, org_id))


class AddOrganizerButton(Button):
    """Button to add an organizer."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Добавить организатора",
            custom_id=f"org_add:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        channel_orgs = organizer_store.get(self.guild_id, self.channel_id)
        if not channel_orgs:
            await interaction.response.send_message(
                "❌ Конфигурация организаторов не найдена.",
                ephemeral=True
            )
            return

        # Check permissions
        if not self._can_manage(interaction, channel_orgs):
            await interaction.response.send_message(
                "❌ Только организатор канала или владелец бота может добавлять организаторов.",
                ephemeral=True
            )
            return

        # Check limit
        if len(channel_orgs.all_organizers()) >= 3:
            await interaction.response.send_message(
                "❌ Достигнут лимит организаторов (максимум 3).",
                ephemeral=True
            )
            return

        # Create select menu with MANAGER_ROLE_ID members
        role = interaction.guild.get_role(MANAGER_ROLE_ID)
        if not role:
            await interaction.response.send_message(
                "❌ Роль менеджера не найдена.",
                ephemeral=True
            )
            return

        options = []
        for member in role.members:
            if member.id not in channel_orgs.all_organizers():
                options.append(
                    discord.SelectOption(
                        label=member.display_name,
                        value=str(member.id)
                    )
                )

        if not options:
            await interaction.response.send_message(
                "❌ Нет доступных пользователей с ролью менеджера.",
                ephemeral=True
            )
            return

        select = AddOrganizerSelect(self.guild_id, self.channel_id, options)
        view = View()
        view.add_item(select)

        await interaction.response.send_message(
            "Выберите пользователя для добавления:",
            view=view,
            ephemeral=True
        )

    def _can_manage(self, interaction: discord.Interaction, channel_orgs) -> bool:
        """Check if user can manage organizers."""
        return (
            interaction.user.id == BOT_OWNER_ID or
            interaction.user.id == channel_orgs.organizer_id
        )


class AddOrganizerSelect(Select):
    """Select menu to choose organizer to add."""

    def __init__(self, guild_id: int, channel_id: int, options: list):
        super().__init__(
            placeholder="Выберите организатора",
            options=options,
            custom_id=f"org_add_select:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        user_id = int(self.values[0])

        channel_orgs = organizer_store.get(self.guild_id, self.channel_id)
        if not channel_orgs:
            await interaction.response.send_message(
                "❌ Конфигурация организаторов не найдена.",
                ephemeral=True
            )
            return

        if not channel_orgs.add_organizer(user_id):
            await interaction.response.send_message(
                "❌ Не удалось добавить организатора (лимит или уже существует).",
                ephemeral=True
            )
            return

        organizer_store.set(self.guild_id, self.channel_id, channel_orgs)

        # Update the original message
        await self._update_message(interaction)

        await interaction.response.send_message(
            f"✅ Организатор добавлен.",
            ephemeral=True
        )

    async def _update_message(self, interaction: discord.Interaction) -> None:
        """Update the organizer management message."""
        channel_orgs = organizer_store.get(self.guild_id, self.channel_id)
        if not channel_orgs or not channel_orgs.message_id:
            return

        try:
            channel = interaction.guild.get_channel(self.channel_id)
            if not channel:
                return

            message = await channel.fetch_message(channel_orgs.message_id)

            # Rebuild embed
            from cogs.organizer import OrganizerCog
            cog = interaction.client.get_cog("OrganizerCog")
            if cog:
                embed = cog._build_embed(interaction.guild, channel, channel_orgs)

                # Rebuild view
                view = OrganizerManagementView(self.guild_id, self.channel_id)

                await message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error("Error updating organizer message: %s", e)


class RemoveOrganizerButton(Button):
    """Button to remove an organizer."""

    def __init__(self, guild_id: int, channel_id: int, organizer_id: int):
        member = None
        # Try to get member from guild if available
        # We'll get the name in the callback
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=f"Удалить @{organizer_id}",
            custom_id=f"org_remove:{guild_id}:{channel_id}:{organizer_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.organizer_id = organizer_id

    async def callback(self, interaction: discord.Interaction) -> None:
        channel_orgs = organizer_store.get(self.guild_id, self.channel_id)
        if not channel_orgs:
            await interaction.response.send_message(
                "❌ Конфигурация организаторов не найдена.",
                ephemeral=True
            )
            return

        # Check permissions
        if not self._can_manage(interaction, channel_orgs):
            await interaction.response.send_message(
                "❌ Только организатор канала или владелец бота может удалять организаторов.",
                ephemeral=True
            )
            return

        if not channel_orgs.remove_organizer(self.organizer_id):
            await interaction.response.send_message(
                "❌ Организатор не найден.",
                ephemeral=True
            )
            return

        organizer_store.set(self.guild_id, self.channel_id, channel_orgs)

        # Update the original message
        await self._update_message(interaction)

        await interaction.response.send_message(
            "✅ Организатор удален.",
            ephemeral=True
        )

    def _can_manage(self, interaction: discord.Interaction, channel_orgs) -> bool:
        """Check if user can manage organizers."""
        return (
            interaction.user.id == BOT_OWNER_ID or
            interaction.user.id == channel_orgs.organizer_id
        )

    async def _update_message(self, interaction: discord.Interaction) -> None:
        """Update the organizer management message."""
        channel_orgs = organizer_store.get(self.guild_id, self.channel_id)
        if not channel_orgs or not channel_orgs.message_id:
            return

        try:
            channel = interaction.guild.get_channel(self.channel_id)
            if not channel:
                return

            message = await channel.fetch_message(channel_orgs.message_id)

            # Rebuild embed
            from cogs.organizer import OrganizerCog
            cog = interaction.client.get_cog("OrganizerCog")
            if cog:
                embed = cog._build_embed(interaction.guild, channel, channel_orgs)

                # Rebuild view
                view = OrganizerManagementView(self.guild_id, self.channel_id)

                await message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error("Error updating organizer message: %s", e)


class TransferOrganizerButton(Button):
    """Button to transfer main organizer role."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Передать права основного организатора",
            custom_id=f"org_transfer:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        channel_orgs = organizer_store.get(self.guild_id, self.channel_id)
        if not channel_orgs:
            await interaction.response.send_message(
                "❌ Конфигурация организаторов не найдена.",
                ephemeral=True
            )
            return

        # Check permissions
        if not self._can_manage(interaction, channel_orgs):
            await interaction.response.send_message(
                "❌ Только организатор канала или владелец бота может передавать права.",
                ephemeral=True
            )
            return

        # Create select menu with other organizers
        all_orgs = channel_orgs.all_organizers()
        options = []

        for org_id in all_orgs:
            if org_id != channel_orgs.organizer_id:
                member = interaction.guild.get_member(org_id)
                if member:
                    options.append(
                        discord.SelectOption(
                            label=member.display_name,
                            value=str(org_id)
                        )
                    )

        if not options:
            await interaction.response.send_message(
                "❌ Нет других организаторов для передачи прав.",
                ephemeral=True
            )
            return

        select = TransferOrganizerSelect(self.guild_id, self.channel_id, options)
        view = View()
        view.add_item(select)

        await interaction.response.send_message(
            "Выберите нового основного организатора:",
            view=view,
            ephemeral=True
        )

    def _can_manage(self, interaction: discord.Interaction, channel_orgs) -> bool:
        """Check if user can manage organizers."""
        return (
            interaction.user.id == BOT_OWNER_ID or
            interaction.user.id == channel_orgs.organizer_id
        )


class TransferOrganizerSelect(Select):
    """Select menu to choose new main organizer."""

    def __init__(self, guild_id: int, channel_id: int, options: list):
        super().__init__(
            placeholder="Выберите нового организатора",
            options=options,
            custom_id=f"org_transfer_select:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        new_org_id = int(self.values[0])

        channel_orgs = organizer_store.get(self.guild_id, self.channel_id)
        if not channel_orgs:
            await interaction.response.send_message(
                "❌ Конфигурация организаторов не найдена.",
                ephemeral=True
            )
            return

        if not channel_orgs.transfer_organizer(new_org_id):
            await interaction.response.send_message(
                "❌ Не удалось передать права.",
                ephemeral=True
            )
            return

        organizer_store.set(self.guild_id, self.channel_id, channel_orgs)

        # Update the original message
        await self._update_message(interaction)

        await interaction.response.send_message(
            "✅ Права основного организатора переданы.",
            ephemeral=True
        )

    async def _update_message(self, interaction: discord.Interaction) -> None:
        """Update the organizer management message."""
        channel_orgs = organizer_store.get(self.guild_id, self.channel_id)
        if not channel_orgs or not channel_orgs.message_id:
            return

        try:
            channel = interaction.guild.get_channel(self.channel_id)
            if not channel:
                return

            message = await channel.fetch_message(channel_orgs.message_id)

            # Rebuild embed
            from cogs.organizer import OrganizerCog
            cog = interaction.client.get_cog("OrganizerCog")
            if cog:
                embed = cog._build_embed(interaction.guild, channel, channel_orgs)

                # Rebuild view
                view = OrganizerManagementView(self.guild_id, self.channel_id)

                await message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error("Error updating organizer message: %s", e)
