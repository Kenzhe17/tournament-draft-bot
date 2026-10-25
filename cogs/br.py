"""Battle Royale tournament commands."""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID, BR_ROLE_ID, BR_ROOM_CHANNEL_ID
from models.br_tournament import BRTournamentFormation, BRTournamentPhase
from models.team import Team
from storage.br_store import br_store
from utils.embeds import replace_emojis
from views.br_view import BRRegistrationView

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

        # Создать турнир
        from models.br_tournament import BRTournament
        formation_mode = BRTournamentFormation.RANDOM if formation == "random" else BRTournamentFormation.TEAM
        tournament = BRTournament(
            id="",
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            formation=formation_mode,
            role_id=BR_ROLE_ID,
            room_channel_id=BR_ROOM_CHANNEL_ID,
            organizer_id=interaction.user.id,
        )

        # Сохранить
        br_store.set(tournament)

        # Создать view
        from views.br_view import BRRegistrationView
        view = BRRegistrationView(tournament.guild_id, tournament.channel_id, tournament)

        # Build player/team list for embed
        if tournament.formation.value == "team":
            from storage.team_store import team_store
            from config import get_emoji
            from utils.cosmetics import clean_nickname
            team_emoji_names = ["num_1", "num_2", "num_3", "num_4", "num_5", "num_6",
                               "num_7", "num_8", "num_9", "num_ten", "num_eleven", "num_twelve"]
            team_list = []
            for i, team_id in enumerate(tournament.registered_teams):
                team = team_store.get(team_id)
                if team:
                    emoji = get_emoji(team_emoji_names[i]) if i < len(team_emoji_names) else f"{i+1}"
                    # Get member names (cleaned - no tags/icons)
                    member_names = []
                    for member_id in team.members:
                        member = interaction.guild.get_member(member_id)
                        if member:
                            member_names.append(clean_nickname(member.display_name))
                    members_text = f" ({', '.join(member_names)})" if member_names else ""
                    team_list.append(f"{emoji} {team.team_name}{members_text}")
            players_text = "\n".join(team_list) if team_list else f"{replace_emojis('white_arrow')} *"
        else:
            player_names = [p["name"] for p in tournament.players]
            players_text = ", ".join(player_names) if player_names else "*"

        # Создать embed в стиле tournament create
        if tournament.formation.value == "team":
            players_label = "Команд"
            players_count = f"{len(tournament.registered_teams)}/12"
            rules_text = "Нажмите кнопку '🏆 Зарегистрировать Команду' для регистрации вашей команды."
            team_display_label = "Команды"
        else:
            players_label = "Игроков"
            players_count = f"{len(tournament.players)}/48"
            rules_text = "Нажмите кнопку 'Войти' для регистрации."
            team_display_label = "Игроки"

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | {tournament.formation.value.capitalize()} | Регистрация",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{interaction.user.id}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: {tournament.formation.value.capitalize()}\n"
                f"{replace_emojis('sub_middle')} Фаза: Регистрация\n"
                f"{replace_emojis('sub_directory')} {players_label}: {players_count}\n\n"
                f"{replace_emojis('white_dot')} **{team_display_label}:**\n"
                f"{players_text}\n\n"
                f"{replace_emojis('white_dot')} **Правила:**\n"
                f"{replace_emojis('white_arrow')} {rules_text}\n\n"
                f"{replace_emojis('a_dot_smaller')} Используйте кнопки ниже для регистрации на турнир"
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        # Save tournament first
        br_store.set(tournament)

        # Send ephemeral confirmation first
        await interaction.response.send_message(
            replace_emojis("✅ BR турнир создан"),
            ephemeral=True
        )

        # Send main tournament message through channel.send to unbind it
        message = await interaction.channel.send(embed=embed, view=view)
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

    @app_commands.command(name="brtest", description="Заполнить BR турнир ботами для тестирования")
    async def br_test(self, interaction: discord.Interaction) -> None:
        """Заполнить BR турнир ботами для тестирования."""
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

        # brtest only works for random format
        if tournament.formation.value == "team":
            await interaction.response.send_message(
                "❌ brtest только для random формата. Для team формата регистрируйте команды через кнопку.",
                ephemeral=True
            )
            return

        # Random format: add 48 bots
        tournament.clear_players()
        tournament.registered_teams = []

        # Generate unique negative user_ids within int64 range
        base_user_id = -((interaction.guild_id % 10000) * 10000 + (interaction.channel_id % 100) * 10)

        # Добавить 48 ботов
        for i in range(1, 49):
            tournament.add_player(f"Bot {i}", base_user_id - i)  # Unique negative IDs for bots

            # Сохранить
            br_store.set(tournament)

            await interaction.response.send_message(
                replace_emojis("✅ Добавлено 48 ботов!"),
                ephemeral=True
            )

        # Обновить сообщение с новым стилем
        try:
            channel = interaction.guild.get_channel(tournament.channel_id)
            if channel and tournament.message_id:
                message = await channel.fetch_message(tournament.message_id)

                # Build embed based on format
                if tournament.formation.value == "team":
                    from storage.team_store import team_store
                    from config import get_emoji
                    team_emoji_names = ["num_1", "num_2", "num_3", "num_4", "num_5", "num_6",
                                       "num_7", "num_8", "num_9", "num_ten", "num_eleven", "num_twelve"]
                    team_list = []
                    for i, team_id in enumerate(tournament.registered_teams):
                        team = team_store.get(team_id)
                        if team:
                            emoji = get_emoji(team_emoji_names[i]) if i < len(team_emoji_names) else f"{i+1}"
                            # Get member names (cleaned)
                            member_names = []
                            for member_id in team.members:
                                member = interaction.guild.get_member(member_id)
                                if member:
                                    member_names.append(clean_nickname(member.display_name))
                            members_text = f" ({', '.join(member_names)})" if member_names else ""
                            team_list.append(f"{emoji} {team.team_name}{members_text}")
                    players_text = "\n".join(team_list) if team_list else f"{replace_emojis('white_arrow')} *"
                    players_label = "Команд"
                    players_count = f"{len(tournament.registered_teams)}/12"
                    rules_text = "Нажмите кнопку '🏆 Зарегистрировать Команду' для регистрации вашей команды."
                    team_display_label = "Команды"
                else:
                    player_names = [p["name"] for p in tournament.players]
                    players_text = ", ".join(player_names) if player_names else "*"
                    players_label = "Игроков"
                    players_count = f"{len(tournament.players)}/48"
                    rules_text = "Нажмите кнопку 'Войти' для регистрации."
                    team_display_label = "Игроки"

                embed = discord.Embed(
                    title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | {tournament.formation.value.capitalize()} | Регистрация",
                    description=(
                        f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                        f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                        f"{replace_emojis('sub_middle')} Формат: {tournament.formation.value.capitalize()}\n"
                        f"{replace_emojis('sub_middle')} Фаза: Регистрация\n"
                        f"{replace_emojis('sub_directory')} {players_label}: {players_count}\n\n"
                        f"{replace_emojis('white_dot')} **{team_display_label}:**\n"
                        f"{players_text}\n\n"
                        f"{replace_emojis('white_dot')} **Правила:**\n"
                        f"{replace_emojis('white_arrow')} {rules_text}\n\n"
                        f"{replace_emojis('a_dot_smaller')} Используйте кнопки ниже для регистрации на турнир"
                    ),
                    color=discord.Color.from_rgb(69, 69, 69)
                )

                view = BRRegistrationView(tournament.guild_id, tournament.channel_id, tournament)
                await message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error("Ошибка при обновлении сообщения: %s", e)

        logger.info("BR турнир заполнен 48 ботами в канале %s сервера %s", interaction.channel_id, interaction.guild_id)


async def setup(bot: "TournamentBot"):
    """Setup the cog."""
    await bot.add_cog(BRCog(bot))
