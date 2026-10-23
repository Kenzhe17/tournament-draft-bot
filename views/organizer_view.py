"""Views for organizer management."""

import logging

import discord
from discord.ui import Modal, TextInput, View, Button, button, Select

from config import BOT_OWNER_ID, MANAGER_ROLE_ID, replace_emojis, get_emoji, TOURNAMENT_CHANNELS
from storage.organizer_store import organizer_store
from storage.json_store import store

logger = logging.getLogger(__name__)


class ChannelSelect(Select):
    """Select menu to choose tournament channel."""

    def __init__(self, guild_id: int, guild: discord.Guild):
        super().__init__(
            placeholder="Выберите канал турнира",
            custom_id="org_channel_select"
        )
        self.guild_id = guild_id
        self.guild = guild

        # Add options only for channels with active tournaments
        from storage.json_store import store
        has_tournaments = False
        for channel_id in TOURNAMENT_CHANNELS:
            tournament = store.get(guild_id, channel_id)
            if tournament:
                channel = guild.get_channel(channel_id)
                if channel:
                    has_tournaments = True
                    self.add_option(
                        label=channel.name,
                        value=str(channel_id)
                    )

        # If no tournaments, disable the select
        if not has_tournaments:
            self.disabled = True
            self.placeholder = "Нет активных турниров"

    async def callback(self, interaction: discord.Interaction) -> None:
        channel_id = int(self.values[0])
        view = OrganizerMenuView(self.guild_id, channel_id)
        await interaction.response.edit_message(view=view)


class ChannelSelectView(View):
    """View to select which tournament channel to manage."""

    def __init__(self, guild_id: int, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.guild = guild

        # Add select menu for channel selection
        self.add_item(ChannelSelect(guild_id, guild))


class OrganizerManagementButton(Button):
    """Button to open organizer management menu."""

    def __init__(self, guild_id: int, guild: discord.Guild):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Управление организаторами",
            custom_id=f"org_manage:{guild_id}"
        )
        self.guild_id = guild_id
        self.guild = guild

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check if user has MANAGER_ROLE_ID or is an organizer in any tournament channel
        has_access = False

        # Check MANAGER_ROLE_ID
        role = interaction.guild.get_role(MANAGER_ROLE_ID)
        if role and role in interaction.user.roles:
            has_access = True

        # Check if user is an organizer in any tournament channel
        if not has_access:
            for channel_id in TOURNAMENT_CHANNELS:
                channel_orgs = organizer_store.get(self.guild_id, channel_id)
                if channel_orgs and interaction.user.id in channel_orgs.all_organizers():
                    has_access = True
                    break

        if not has_access:
            await interaction.response.send_message(
                f"❌ У вас нет роли для управления организаторами.",
                ephemeral=True
            )
            return

        # Open channel selection view
        view = ChannelSelectView(self.guild_id, self.guild)
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
        # Bot owner can always manage
        if interaction.user.id == BOT_OWNER_ID:
            return True

        # Main organizer of this channel can manage
        if interaction.user.id == channel_orgs.organizer_id:
            return True

        # Additional organizers of this channel can manage
        if interaction.user.id in channel_orgs.additional_organizers:
            return True

        # Users with MANAGER_ROLE_ID can manage if they are an organizer in any tournament channel
        role = interaction.guild.get_role(MANAGER_ROLE_ID)
        if role and role in interaction.user.roles:
            for channel_id in TOURNAMENT_CHANNELS:
                orgs = organizer_store.get(self.guild_id, channel_id)
                if orgs and interaction.user.id in orgs.all_organizers():
                    return True

        return False


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
        # Find which channel has the message ID
        from config import TOURNAMENT_CHANNELS
        from storage.organizer_store import organizer_store

        message_id = None
        update_channel_id = None

        for channel_id in TOURNAMENT_CHANNELS:
            channel_orgs = organizer_store.get(self.guild_id, channel_id)
            if channel_orgs and channel_orgs.message_id:
                message_id = channel_orgs.message_id
                update_channel_id = channel_id
                break

        if not message_id or not update_channel_id:
            return

        try:
            channel = interaction.guild.get_channel(update_channel_id)
            if not channel:
                return

            message = await channel.fetch_message(message_id)

            # Rebuild embed using multi-channel embed
            from cogs.organizer import OrganizerCog
            cog = interaction.client.get_cog("OrganizerCog")
            if cog:
                embed = cog._build_multi_channel_embed(interaction.guild)

                # Rebuild view
                view = OrganizerManagementView(self.guild_id, interaction.guild)

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
        # Bot owner can always manage
        if interaction.user.id == BOT_OWNER_ID:
            return True

        # Main organizer of this channel can manage
        if interaction.user.id == channel_orgs.organizer_id:
            return True

        # Additional organizers of this channel can manage
        if interaction.user.id in channel_orgs.additional_organizers:
            return True

        # Users with MANAGER_ROLE_ID can manage if they are an organizer in any tournament channel
        role = interaction.guild.get_role(MANAGER_ROLE_ID)
        if role and role in interaction.user.roles:
            for channel_id in TOURNAMENT_CHANNELS:
                orgs = organizer_store.get(self.guild_id, channel_id)
                if orgs and interaction.user.id in orgs.all_organizers():
                    return True

        return False

    async def _update_message(self, interaction: discord.Interaction) -> None:
        """Update the organizer management message."""
        # Find which channel has the message ID
        from config import TOURNAMENT_CHANNELS
        from storage.organizer_store import organizer_store

        message_id = None
        update_channel_id = None

        for channel_id in TOURNAMENT_CHANNELS:
            channel_orgs = organizer_store.get(self.guild_id, channel_id)
            if channel_orgs and channel_orgs.message_id:
                message_id = channel_orgs.message_id
                update_channel_id = channel_id
                break

        if not message_id or not update_channel_id:
            return

        try:
            channel = interaction.guild.get_channel(update_channel_id)
            if not channel:
                return

            message = await channel.fetch_message(message_id)

            # Rebuild embed using multi-channel embed
            from cogs.organizer import OrganizerCog
            cog = interaction.client.get_cog("OrganizerCog")
            if cog:
                embed = cog._build_multi_channel_embed(interaction.guild)

                # Rebuild view
                view = OrganizerManagementView(self.guild_id, interaction.guild)

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

        # Get all users with MANAGER_ROLE_ID
        manager_role = interaction.guild.get_role(MANAGER_ROLE_ID)
        if not manager_role:
            await interaction.response.send_message(
                "❌ MANAGER_ROLE_ID не настроен.",
                ephemeral=True
            )
            return

        # Get all users who are organizers in any tournament channel
        all_tournament_organizers = set()
        for channel_id in TOURNAMENT_CHANNELS:
            orgs = organizer_store.get(self.guild_id, channel_id)
            if orgs:
                all_tournament_organizers.update(orgs.all_organizers())

        # Create options for users with MANAGER_ROLE_ID who are NOT tournament organizers
        options = []
        for member in manager_role.members:
            if member.id not in all_tournament_organizers:
                options.append(
                    discord.SelectOption(
                        label=member.display_name,
                        value=str(member.id)
                    )
                )

        if not options:
            await interaction.response.send_message(
                "❌ Нет свободных организаторов (все с MANAGER_ROLE_ID уже организаторы в турнирах).",
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
        # Bot owner can always manage
        if interaction.user.id == BOT_OWNER_ID:
            return True

        # Main organizer of this channel can manage
        if interaction.user.id == channel_orgs.organizer_id:
            return True

        # Additional organizers of this channel can manage
        if interaction.user.id in channel_orgs.additional_organizers:
            return True

        # Users with MANAGER_ROLE_ID can manage if they are an organizer in any tournament channel
        role = interaction.guild.get_role(MANAGER_ROLE_ID)
        if role and role in interaction.user.roles:
            for channel_id in TOURNAMENT_CHANNELS:
                orgs = organizer_store.get(self.guild_id, channel_id)
                if orgs and interaction.user.id in orgs.all_organizers():
                    return True

        return False


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
        # Find which channel has the message ID
        from config import TOURNAMENT_CHANNELS
        from storage.organizer_store import organizer_store

        message_id = None
        update_channel_id = None

        for channel_id in TOURNAMENT_CHANNELS:
            channel_orgs = organizer_store.get(self.guild_id, channel_id)
            if channel_orgs and channel_orgs.message_id:
                message_id = channel_orgs.message_id
                update_channel_id = channel_id
                break

        if not message_id or not update_channel_id:
            return

        try:
            channel = interaction.guild.get_channel(update_channel_id)
            if not channel:
                return

            message = await channel.fetch_message(message_id)

            # Rebuild embed using multi-channel embed
            from cogs.organizer import OrganizerCog
            cog = interaction.client.get_cog("OrganizerCog")
            if cog:
                embed = cog._build_multi_channel_embed(interaction.guild)

                # Rebuild view
                view = OrganizerManagementView(self.guild_id, interaction.guild)

                await message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error("Error updating organizer message: %s", e)
