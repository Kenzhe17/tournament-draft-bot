"""Organizer management commands."""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID, MANAGER_ROLE_ID, replace_emojis, get_emoji
from storage.organizer_store import organizer_store
from storage.json_store import store

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class OrganizerCog(commands.Cog):
    """Organizer management commands."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="organizer_setup", description="Создать сообщение управления организаторами канала")
    async def organizer_setup(self, interaction: discord.Interaction) -> None:
        """Создать сообщение управления организаторами канала."""
        # Проверка на владельца бота
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Эта команда доступна только владельцу бота.",
                ephemeral=True
            )
            return

        # Проверить MANAGER_ROLE_ID
        if MANAGER_ROLE_ID == 0:
            await interaction.response.send_message(
                "❌ MANAGER_ROLE_ID не настроен в конфигурации.",
                ephemeral=True
            )
            return

        # Получить турнир в канале
        tournament = store.get(interaction.guild_id, interaction.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ В этом канале нет турнира.",
                ephemeral=True
            )
            return

        # Создать или получить конфигурацию организаторов
        channel_orgs = organizer_store.get(interaction.guild_id, interaction.channel_id)
        if not channel_orgs:
            from storage.organizer_store import ChannelOrganizers
            channel_orgs = ChannelOrganizers()
            channel_orgs.organizer_id = interaction.user.id  # Создатель становится организатором
            channel_orgs.tournament_id = tournament.id
            organizer_store.set(interaction.guild_id, interaction.channel_id, channel_orgs)
        else:
            # Обновить tournament_id если изменился
            channel_orgs.tournament_id = tournament.id
            organizer_store.set(interaction.guild_id, interaction.channel_id, channel_orgs)

        # Создать view
        from views.organizer_view import OrganizerManagementView
        view = OrganizerManagementView(interaction.guild_id, interaction.channel_id)

        # Создать embed
        embed = self._build_embed(interaction.guild, interaction.channel, channel_orgs)

        message = await interaction.response.send_message(embed=embed, view=view)
        channel_orgs.message_id = message.id
        organizer_store.set(interaction.guild_id, interaction.channel_id, channel_orgs)

        logger.info("Создано сообщение управления организаторами в канале %s сервера %s", interaction.channel_id, interaction.guild_id)

    def _build_embed(self, guild: discord.Guild, channel: discord.TextChannel, channel_orgs) -> discord.Embed:
        """Построить embed для управления организаторами."""
        # Получить турнир
        tournament = store.get(guild.id, channel.id)

        # Статус турнира
        if tournament:
            tournament_info = (
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **Канал:** {channel.mention}\n"
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **Турнир ID:** `{tournament.id}`\n"
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **Фаза:** {tournament.phase.value}"
            )
        else:
            tournament_info = (
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **Канал:** {channel.mention}\n"
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **Турнир ID:** *Нет турнира*"
            )

        # Список организаторов
        organizers_list = []
        all_orgs = channel_orgs.all_organizers()

        if channel_orgs.organizer_id:
            main_member = guild.get_member(channel_orgs.organizer_id)
            main_name = main_member.display_name if main_member else f"<@{channel_orgs.organizer_id}>"
            organizers_list.append(f"{replace_emojis('a_star')} {main_name} (основной)")

        for org_id in channel_orgs.additional_organizers:
            member = guild.get_member(org_id)
            name = member.display_name if member else f"<@{org_id}>"
            organizers_list.append(f"{replace_emojis('white_dot')} {name}")

        if not organizers_list:
            organizers_text = "*Нет организаторов*"
        else:
            organizers_text = "\n".join(organizers_list)

        description = (
            f"{replace_emojis('a_sparkle')} **УПРАВЛЕНИЕ ОРГАНИЗАТОРАМИ КАНАЛА**\n\n"
            f"{replace_emojis('a_star')} **ИНФОРМАЦИЯ О ТУРНИРЕ:**\n"
            f"{tournament_info}\n\n"
            f"{replace_emojis('a_star')} **ОРГАНИЗАТОРЫ:**\n"
            f"{organizers_text}\n\n"
            f"{replace_emojis('a_star')} *Максимум 3 организатора на канал*"
        )

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} r1z3 | УПРАВЛЕНИЕ ОРГАНИЗАТОРАМИ",
            description=description,
            color=discord.Color.from_rgb(69, 69, 69)
        )

        return embed


async def setup(bot: "TournamentBot"):
    """Setup the cog."""
    await bot.add_cog(OrganizerCog(bot))
