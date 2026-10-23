"""Battle Royale tournament commands."""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID, BR_ROLE_ID, BR_ROOM_CHANNEL_ID
from models.br_tournament import BRTournamentFormation, BRTournamentPhase
from storage.br_store import br_store
from utils.embeds import replace_emojis

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class BRCog(commands.Cog):
    """Battle Royale tournament commands."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="br", description="Создать Battle Royale турнир")
    @app_commands.describe(formation="Формат турнира")
    @app_commands.choices(formation=[
        app_commands.Choice(name="random", value="random"),
        app_commands.Choice(name="team", value="team"),
    ])
    async def br_create(
        self,
        interaction: discord.Interaction,
        formation: str
    ) -> None:
        """Создать BR турнир."""
        # Проверка на владельца бота
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Эта команда доступна только владельцу бота.",
                ephemeral=True
            )
            return

        # Проверка конфигурации
        if BR_ROLE_ID == 0:
            await interaction.response.send_message(
                "❌ BR_ROLE_ID не настроен в конфигурации.",
                ephemeral=True
            )
            return

        if BR_ROOM_CHANNEL_ID == 0:
            await interaction.response.send_message(
                "❌ BR_ROOM_CHANNEL_ID не настроен в конфигурации.",
                ephemeral=True
            )
            return

        # Проверка на существующий турнир в канале
        existing = br_store.get(interaction.guild_id, interaction.channel_id)
        if existing and existing.phase != BRTournamentPhase.COMPLETE:
            await interaction.response.send_message(
                "❌ В этом канале уже есть активный BR турнир.",
                ephemeral=True
            )
            return

        # Проверка формата
        if formation == "team":
            await interaction.response.send_message(
                "❌ Формат 'team' в разработке. Используйте 'random'.",
                ephemeral=True
            )
            return

        # Создать турнир
        from models.br_tournament import BRTournament
        tournament = BRTournament(
            id="",
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            formation=BRTournamentFormation.RANDOM,
            role_id=BR_ROLE_ID,
            room_channel_id=BR_ROOM_CHANNEL_ID,
        )

        # Сохранить
        br_store.set(tournament)

        # Создать view
        from views.br_view import BRRegistrationView
        view = BRRegistrationView(tournament.guild_id, tournament.channel_id, tournament)

        # Создать embed в стиле tournament create
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | Регистрация",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{interaction.user.id}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: Random\n"
                f"{replace_emojis('sub_middle')} Фаза: Регистрация\n"
                f"{replace_emojis('sub_directory')} Игроков: {len(tournament.players)}/48\n\n"
                f"{replace_emojis('white_dot')} **Правила:**\n"
                f"{replace_emojis('white_arrow')} Нажмите кнопку 'Войти' для регистрации.\n\n"
                f"{replace_emojis('a_dot_smaller')} Используйте кнопки ниже для регистрации на турнир"
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        message = await interaction.response.send_message(embed=embed, view=view)
        tournament.message_id = message.id
        br_store.set(tournament)

        logger.info("BR турнир создан в канале %s сервера %s", interaction.channel_id, interaction.guild_id)

    @app_commands.command(name="br_delete", description="Удалить BR турнир")
    async def br_delete(self, interaction: discord.Interaction) -> None:
        """Удалить BR турнир."""
        # Проверка на владельца бота
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Эта команда доступна только владельцу бота.",
                ephemeral=True
            )
            return

        # Получить турнир
        tournament = br_store.get(interaction.guild_id, interaction.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ В этом канале нет BR турнира.",
                ephemeral=True
            )
            return

        # Снять роль с реальных игроков (не ботов с отрицательными ID)
        guild = interaction.guild
        role = guild.get_role(tournament.role_id)
        if role:
            for player in tournament.players:
                user_id = player["id"]
                # Пропускаем ботов с отрицательными ID
                if user_id < 0:
                    continue
                try:
                    member = guild.get_member(user_id)
                    if member:
                        await member.remove_roles(role)
                except discord.Forbidden:
                    logger.warning("Не удалось снять роль с пользователя %s", user_id)
                except Exception as e:
                    logger.error("Ошибка при снятии роли: %s", e)

        # Удалить сообщение
        if tournament.message_id:
            try:
                channel = guild.get_channel(tournament.channel_id)
                if channel:
                    message = await channel.fetch_message(tournament.message_id)
                    await message.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                logger.warning("Не удалось удалить сообщение BR турнира: %s", e)

        # Удалить из хранилища
        br_store.delete(interaction.guild_id, interaction.channel_id)

        await interaction.response.send_message(
            "✅ BR турнир удален.",
            ephemeral=True
        )

        logger.info("BR турнир удален в канале %s сервера %s", interaction.channel_id, interaction.guild_id)

    @app_commands.command(name="brtest", description="Заполнить BR турнир 48 ботами")
    async def br_test(self, interaction: discord.Interaction) -> None:
        """Заполнить BR турнир 48 ботами для тестирования."""
        # Проверка на владельца бота
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Эта команда доступна только владельцу бота.",
                ephemeral=True
            )
            return

        # Получить турнир
        tournament = br_store.get(interaction.guild_id, interaction.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ В этом канале нет BR турнира.",
                ephemeral=True
            )
            return

        # Проверка фазы
        if tournament.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message(
                "❌ BR турнир не в фазе регистрации.",
                ephemeral=True
            )
            return

        # Очистить существующих игроков
        tournament.clear_players()

        # Добавить 48 ботов
        for i in range(1, 49):
            tournament.add_player(f"Bot {i}", -i)  # Отрицательные ID для ботов

        # Сохранить
        br_store.set(tournament)

        # Обновить сообщение
        try:
            channel = interaction.guild.get_channel(tournament.channel_id)
            if channel and tournament.message_id:
                message = await channel.fetch_message(tournament.message_id)
                embed = message.embeds[0]
                embed.description = f"Формат: **Random**\n" \
                                   f"Фаза: **Регистрация**\n" \
                                   f"Игроков: **{len(tournament.players)}/48**"
                await message.edit(embed=embed)
        except Exception as e:
            logger.error("Ошибка при обновлении сообщения: %s", e)

        await interaction.response.send_message(
            f"✅ Добавлено 48 ботов в BR турнир.",
            ephemeral=True
        )

        logger.info("BR турнир заполнен 48 ботами в канале %s сервера %s", interaction.channel_id, interaction.guild_id)


async def setup(bot: "TournamentBot"):
    """Setup the cog."""
    await bot.add_cog(BRCog(bot))
