"""Organizer management commands."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID, MANAGER_ROLE_ID, replace_emojis, get_emoji, TOURNAMENT_CHANNELS
from storage.organizer_store import organizer_store
from storage.json_store import store

logger = logging.getLogger(__name__)


class OrganizerCog(commands.Cog):
    """Organizer management commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="organizer_setup", description="Создать сообщение управления организаторами для всех каналов турниров")
    async def organizer_setup(self, interaction: discord.Interaction) -> None:
        """Создать сообщение управления организаторами для всех каналов турниров."""
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

        # Создать embed с информацией о всех каналах турниров
        embed = self._build_multi_channel_embed(interaction.guild)

        # Создать view для управления
        from views.organizer_view import OrganizerManagementButton
        view = View()
        view.add_item(OrganizerManagementButton(interaction.guild_id, interaction.guild))

        message = await interaction.response.send_message(embed=embed, view=view)

        logger.info("Создано сообщение управления организаторами для всех каналов сервера %s", interaction.guild_id)

    def _build_multi_channel_embed(self, guild: discord.Guild) -> discord.Embed:
        """Построить embed с информацией о всех каналах турниров."""
        description = f"{replace_emojis('a_sparkle')} **УПРАВЛЕНИЕ ОРГАНИЗАТОРАМИ ТУРНИРОВ**\n\n"

        for i, channel_id in enumerate(TOURNAMENT_CHANNELS, 1):
            channel = guild.get_channel(channel_id)
            if not channel:
                description += f"{replace_emojis('a_star')} **Канал {i}:** *Канал не найден*\n\n"
                continue

            # Получить турнир в канале
            tournament = store.get(guild.id, channel_id)

            # Получить организаторов канала
            channel_orgs = organizer_store.get(guild.id, channel_id)

            # Информация о турнире
            if tournament:
                tournament_info = (
                    f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **Канал:** {channel.mention}\n"
                    f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **Турнир ID:** `{tournament.id}`\n"
                    f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **Фаза:** {tournament.phase.value}\n"
                )
            else:
                tournament_info = (
                    f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **Канал:** {channel.mention}\n"
                    f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **Турнир:** *Нет активного турнира*\n"
                )

            # Организаторы
            organizers_list = []
            if channel_orgs:
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

            description += (
                f"{replace_emojis('a_star')} **КАНАЛ {i}:**\n"
                f"{tournament_info}\n"
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **Организаторы:**\n"
                f"{organizers_text}\n\n"
            )

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} r1z3 | УПРАВЛЕНИЕ ОРГАНИЗАТОРАМИ",
            description=description,
            color=discord.Color.from_rgb(69, 69, 69)
        )

        return embed


async def setup(bot: commands.Bot):
    """Setup the cog."""
    await bot.add_cog(OrganizerCog(bot))
