"""Views for Battle Royale tournament."""

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord.ui import Modal, TextInput, View, Button, button, Select

from config import BOT_OWNER_ID, replace_emojis, get_emoji
from models.br_tournament import BRTournamentPhase
from storage.br_store import br_store

if TYPE_CHECKING:
    from models.br_tournament import BRTournament

logger = logging.getLogger(__name__)


def get_user_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    """Get or create a lock for user operations."""
    if not hasattr(get_user_lock, "_locks"):
        get_user_lock._locks = {}
    key = (guild_id, user_id)
    if key not in get_user_lock._locks:
        get_user_lock._locks[key] = asyncio.Lock()
    return get_user_lock._locks[key]


class BRRegistrationView(View):
    """View для фазы регистрации BR турнира."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "BRTournament"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

        # Кнопка "Войти"
        self.add_item(JoinBRButton(guild_id, channel_id))

        # Кнопка "Выйти"
        self.add_item(LeaveBRButton(guild_id, channel_id))

        # Кнопка "Удалить игрока" (только для владельца)
        self.add_item(DeleteBRPlayerButton(guild_id, channel_id))

        # Кнопка меню организатора (только для владельца)
        self.add_item(BROrganizerMenuButton(guild_id, channel_id))


class BROrganizerMenuButton(Button):
    """Кнопка для открытия меню организатора BR (владелец бота)."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Орг Меню",
            custom_id=f"br_org_menu:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check if bot owner
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Только владелец бота может управлять BR турниром.",
                ephemeral=True
            )
            return

        view = BRManagementMenuView(self.guild_id, self.channel_id)
        await interaction.response.send_message(view=view, ephemeral=True)


class BRManagementMenuView(View):
    """Menu for BR tournament management."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id

        self.add_item(DeleteBRButton(guild_id, channel_id))
        self.add_item(StartBRButton(guild_id, channel_id))


class DeleteBRButton(Button):
    """Button to delete BR tournament."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Удалить BR",
            custom_id=f"br_delete_menu:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.br_store import br_store

        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        # Снять роль с реальных игроков
        guild = interaction.guild
        role = guild.get_role(tournament.role_id)
        if role:
            for player in tournament.players:
                user_id = player["id"]
                if user_id < 0:  # Пропускаем ботов
                    continue
                try:
                    member = guild.get_member(user_id)
                    if member:
                        await member.remove_roles(role)
                except Exception as e:
                    logger.warning("Не удалось снять роль: %s", e)

        # Удалить сообщение
        if tournament.message_id:
            try:
                channel = guild.get_channel(tournament.channel_id)
                if channel:
                    message = await channel.fetch_message(tournament.message_id)
                    await message.delete()
            except Exception as e:
                logger.warning("Не удалось удалить сообщение: %s", e)

        # Удалить из хранилища
        br_store.delete(self.guild_id, self.channel_id)

        await interaction.response.send_message(
            "✅ BR турнир удален.",
            ephemeral=True
        )
        logger.info("BR турнир удален в канале %s сервера %s", self.channel_id, self.guild_id)


class StartBRButton(Button):
    """Button to start BR tournament."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Старт BR",
            custom_id=f"br_start_menu:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.br_store import br_store
        from models.br_tournament import BRTournamentPhase

        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        if tournament.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message(
                "❌ BR турнир не в фазе регистрации.",
                ephemeral=True
            )
            return

        # Check player count requirements
        player_count = len(tournament.players)
        if player_count < 20:
            await interaction.response.send_message(
                f"❌ Нужно минимум 20 игроков (текущих: {player_count}).",
                ephemeral=True
            )
            return

        if player_count % 4 != 0:
            await interaction.response.send_message(
                f"❌ Количество игроков должно делиться на 4 (текущих: {player_count}).",
                ephemeral=True
            )
            return

        # Generate random teams of 4
        import random
        random.shuffle(tournament.players)

        team_count = player_count // 4
        tournament.teams = []
        for i in range(team_count):
            team_players = tournament.players[i*4:(i+1)*4]
            captain = team_players[0]["name"]
            tournament.teams.append({
                "captain": captain,
                "players": [p["name"] for p in team_players[1:]],
                "name": f"Команда {i+1}",
                "stats": {}
            })
            tournament.team_rename_remaining[i] = True

        # Update phase
        tournament.phase = BRTournamentPhase.TEAMS
        tournament.team_rename_start_time = interaction.created_at.isoformat()

        # Give BR role to real players
        guild = interaction.guild
        role = guild.get_role(tournament.role_id)
        if role:
            for player in tournament.players:
                user_id = player["id"]
                if user_id < 0:  # Skip bots
                    continue
                try:
                    member = guild.get_member(user_id)
                    if member:
                        await member.add_roles(role)
                except Exception as e:
                    logger.warning("Не удалось выдать роль: %s", e)

        # Save
        br_store.set(tournament)

        # Build teams section
        teams_text = []
        team_emojis = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣", 10: "🔟", 11: "1️⃣1️⃣", 12: "1️⃣2️⃣"}

        for i, team in enumerate(tournament.teams):
            captain = team.get("captain", "Unknown")
            team_name = team.get("name", f"Команда {i+1}")
            players = team.get("players", [])
            all_players = [captain] + players
            players_str = ", ".join(all_players)
            emoji = team_emojis.get(i + 1, "🎮")

            teams_text.append(
                f"{emoji} **{team_name}**\n"
                f"┣ **Капитан:** {captain}\n"
                f"┗ **Состав:** {players_str}\n"
            )

        # Update embed in bot style
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | Команды",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{BOT_OWNER_ID}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: Random\n"
                f"{replace_emojis('sub_middle')} Фаза: Команды\n"
                f"{replace_emojis('sub_directory')} Команд: {team_count}\n\n"
                f"{replace_emojis('white_dot')} **Команды:**\n"
                + "\n".join(teams_text) + "\n\n"
                f"{replace_emojis('a_dot_smaller')} Капитаны могут переименовать свои команды в течение 1 минуты."
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        view = BRTeamsView(tournament.guild_id, tournament.channel_id, tournament)

        await interaction.response.edit_message(embed=embed, view=view)
        logger.info("BR турнир запущен в канале %s сервера %s", self.channel_id, self.guild_id)


class JoinBRButton(Button):
    """Кнопка для входа в BR турнир."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Войти",
            custom_id=f"br_join:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament or tournament.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message(
                "❌ BR турнир не в фазе регистрации.",
                ephemeral=True
            )
            return

        user_name = interaction.user.display_name
        user_id = interaction.user.id

        # Добавить игрока
        if not tournament.add_player(user_name, user_id):
            await interaction.response.send_message(
                "❌ Не удалось присоединиться (лимит 48 или уже зарегистрирован, или вы в другом турнире).",
                ephemeral=True
            )
            return

        # Сохранить
        br_store.set(tournament)

        # Обновить embed с текущим списком игроков
        player_names = [p["name"] for p in tournament.players]
        players_text = ", ".join(player_names) if player_names else "*"

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | Регистрация",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.players[0]['id'] if tournament.players else 0}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: Random\n"
                f"{replace_emojis('sub_middle')} Фаза: Регистрация\n"
                f"{replace_emojis('sub_directory')} Игроков: {len(tournament.players)}/48\n\n"
                f"{replace_emojis('white_dot')} **Игроки:**\n"
                f"{replace_emojis('white_arrow')} {players_text}\n\n"
                f"{replace_emojis('white_dot')} **Правила:**\n"
                f"{replace_emojis('white_arrow')} Нажмите кнопку 'Войти' для регистрации.\n\n"
                f"{replace_emojis('a_dot_smaller')} Используйте кнопки ниже для регистрации на турнир"
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        view = BRRegistrationView(tournament.guild_id, tournament.channel_id, tournament)
        await interaction.response.edit_message(embed=embed, view=view)

        await interaction.followup.send(
            f"✅ Вы зарегистрированы как {user_name}!",
            ephemeral=True
        )


class LeaveBRButton(Button):
    """Кнопка для выхода из BR турнира."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Выйти",
            custom_id=f"br_leave:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament or tournament.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message(
                "❌ BR турнир не в фазе регистрации.",
                ephemeral=True
            )
            return

        user_id = interaction.user.id

        # Удалить игрока
        if not tournament.remove_player(user_id):
            await interaction.response.send_message(
                "❌ Вы не зарегистрированы.",
                ephemeral=True
            )
            return

        # Сохранить
        br_store.set(tournament)

        # Обновить embed с текущим списком игроков
        player_names = [p["name"] for p in tournament.players]
        players_text = ", ".join(player_names) if player_names else "*"

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | Регистрация",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.players[0]['id'] if tournament.players else 0}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: Random\n"
                f"{replace_emojis('sub_middle')} Фаза: Регистрация\n"
                f"{replace_emojis('sub_directory')} Игроков: {len(tournament.players)}/48\n\n"
                f"{replace_emojis('white_dot')} **Игроки:**\n"
                f"{replace_emojis('white_arrow')} {players_text}\n\n"
                f"{replace_emojis('white_dot')} **Правила:**\n"
                f"{replace_emojis('white_arrow')} Нажмите кнопку 'Войти' для регистрации.\n\n"
                f"{replace_emojis('a_dot_smaller')} Используйте кнопки ниже для регистрации на турнир"
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        view = BRRegistrationView(tournament.guild_id, tournament.channel_id, tournament)
        await interaction.response.edit_message(embed=embed, view=view)

        await interaction.followup.send(
            "✅ Вы вышли из турнира.",
            ephemeral=True
        )


class DeleteBRPlayerButton(Button):
    """Кнопка для удаления игрока из BR турнира (владелец бота)."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Удалить игрока",
            custom_id=f"br_delete_player:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check if bot owner
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Только владелец бота может удалять игроков.",
                ephemeral=True
            )
            return

        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        # Create select menu with all players
        if not tournament.players:
            await interaction.response.send_message(
                "❌ Нет зарегистрированных игроков.",
                ephemeral=True
            )
            return

        options = []
        for player in tournament.players:
            options.append(
                discord.SelectOption(
                    label=player["name"],
                    value=str(player["id"])
                )
            )

        select = RemoveBRPlayerSelect(self.guild_id, self.channel_id, options)
        view = View()
        view.add_item(select)

        await interaction.response.send_message(
            "Выберите игрока для удаления:",
            view=view,
            ephemeral=True
        )


class RemoveBRPlayerSelect(Select):
    """Select menu to choose player to remove."""

    def __init__(self, guild_id: int, channel_id: int, options: list):
        super().__init__(
            placeholder="Выберите игрока",
            options=options,
            custom_id=f"br_remove_player_select:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        user_id = int(self.values[0])

        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        # Remove player
        if not tournament.remove_player(user_id):
            await interaction.response.send_message(
                "❌ Игрок не найден.",
                ephemeral=True
            )
            return

        # Save
        br_store.set(tournament)

        # Update embed with current player list
        player_names = [p["name"] for p in tournament.players]
        players_text = ", ".join(player_names) if player_names else "*"

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | Регистрация",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.players[0]['id'] if tournament.players else 0}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: Random\n"
                f"{replace_emojis('sub_middle')} Фаза: Регистрация\n"
                f"{replace_emojis('sub_directory')} Игроков: {len(tournament.players)}/48\n\n"
                f"{replace_emojis('white_dot')} **Игроки:**\n"
                f"{replace_emojis('white_arrow')} {players_text}\n\n"
                f"{replace_emojis('white_dot')} **Правила:**\n"
                f"{replace_emojis('white_arrow')} Нажмите кнопку 'Войти' для регистрации.\n\n"
                f"{replace_emojis('a_dot_smaller')} Используйте кнопки ниже для регистрации на турнир"
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        view = BRRegistrationView(tournament.guild_id, tournament.channel_id, tournament)
        await interaction.message.edit(embed=embed, view=view)

        await interaction.response.edit_message(
            content="✅ Игрок удален.",
            view=None
        )


class DeleteBRButton(Button):
    """Кнопка для удаления BR турнира."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Удалить",
            custom_id=f"br_delete:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        # Проверка на владельца
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Только владелец бота может удалить турнир.",
                ephemeral=True
            )
            return

        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        # Снять роль с реальных игроков
        guild = interaction.guild
        role = guild.get_role(tournament.role_id)
        if role:
            for player in tournament.players:
                user_id = player["id"]
                if user_id < 0:  # Пропускаем ботов
                    continue
                try:
                    member = guild.get_member(user_id)
                    if member:
                        await member.remove_roles(role)
                except Exception as e:
                    logger.warning("Не удалось снять роль: %s", e)

        # Удалить сообщение
        if tournament.message_id:
            try:
                channel = guild.get_channel(tournament.channel_id)
                if channel:
                    message = await channel.fetch_message(tournament.message_id)
                    await message.delete()
            except Exception as e:
                logger.warning("Не удалось удалить сообщение: %s", e)

        # Удалить из хранилища
        br_store.delete(self.guild_id, self.channel_id)

        await interaction.response.send_message(
            "✅ BR турнир удален.",
            ephemeral=True
        )


class StartBRButton(Button):
    """Кнопка для старта BR турнира."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Старт",
            custom_id=f"br_start:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        # Проверка на владельца
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Только владелец бота может начать турнир.",
                ephemeral=True
            )
            return

        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        # Проверка условий старта
        if len(tournament.players) < 20 or len(tournament.players) % 4 != 0:
            await interaction.response.send_message(
                "❌ Нельзя начать: нужно 20+ игроков и количество должно делиться на 4.",
                ephemeral=True
            )
            return

        # Сформировать команды
        if not tournament.form_teams_random():
            await interaction.response.send_message(
                "❌ Не удалось сформировать команды.",
                ephemeral=True
            )
            return

        # Выдать роль реальным игрокам
        guild = interaction.guild
        role = guild.get_role(tournament.role_id)
        if role:
            for player in tournament.players:
                user_id = player["id"]
                if user_id < 0:  # Пропускаем ботов
                    continue
                try:
                    member = guild.get_member(user_id)
                    if member:
                        await member.add_roles(role)
                except Exception as e:
                    logger.warning("Не удалось выдать роль: %s", e)

        # Сохранить
        br_store.set(tournament)

        # Build teams section
        teams_text = []
        team_emojis = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣", 10: "🔟", 11: "1️⃣1️⃣", 12: "1️⃣2️⃣"}

        for i, team in enumerate(tournament.teams):
            captain = team.get("captain", "Unknown")
            team_name = team.get("name", f"Команда {i+1}")
            players = team.get("players", [])
            all_players = [captain] + players
            players_str = ", ".join(all_players)
            emoji = team_emojis.get(i + 1, "🎮")

            teams_text.append(
                f"{emoji} **{team_name}**\n"
                f"┣ **Капитан:** {captain}\n"
                f"┗ **Состав:** {players_str}\n"
            )

        # Создать view для фазы команд
        view = BRTeamsView(self.guild_id, self.channel_id, tournament)

        # Создать embed в стиле бота
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | Команды",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{BOT_OWNER_ID}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: Random\n"
                f"{replace_emojis('sub_middle')} Фаза: Команды\n"
                f"{replace_emojis('sub_directory')} Команд: {len(tournament.teams)}\n\n"
                f"{replace_emojis('white_dot')} **Команды:**\n"
                + "\n".join(teams_text) + "\n\n"
                f"{replace_emojis('a_dot_smaller')} Капитаны могут переименовать свои команды в течение 1 минуты."
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        await interaction.response.edit_message(embed=embed, view=view)


class BRTeamsView(View):
    """View для фазы команд (переименование)."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "BRTournament"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

        # Add single dynamic Team Name button (only for captains who haven't renamed yet)
        self.add_item(TeamNameButton(guild_id, channel_id, tournament))

        # Add Start Bermuda button (for bot owner)
        self.add_item(StartBermudaButton(guild_id, channel_id))


class StartBermudaButton(Button):
    """Button to start Bermuda phase."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Start Bermuda",
            custom_id=f"br_start_bermuda:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Только владелец бота может начать Bermuda.",
                ephemeral=True
            )
            return

        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        # Transition to Bermuda phase
        tournament.phase = BRTournamentPhase.BERMUDA
        tournament.current_map = "bermuda"
        br_store.set(tournament)

        # Create view for map selection
        view = BRMapSelectView(self.guild_id, self.channel_id, tournament)

        map_names = {
            "bermuda": "Бермуды",
            "purgatory": "Чистилище",
            "kalahari": "Калахари"
        }

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | {map_names.get('bermuda', 'bermuda')}",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{BOT_OWNER_ID}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: Random\n"
                f"{replace_emojis('sub_middle')} Фаза: {map_names.get('bermuda', 'bermuda')}\n"
                f"{replace_emojis('sub_directory')} Карта: {map_names.get('bermuda', 'bermuda')}\n\n"
                f"{replace_emojis('white_dot')} **Правила:**\n"
                f"{replace_emojis('white_arrow')} Выберите группу команд для ввода статистики.\n\n"
                f"{replace_emojis('a_dot_smaller')} Введите место (1-12) и убийства (0-50) для каждой команды."
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        await interaction.response.edit_message(embed=embed, view=view)


class TeamNameButton(Button):
    """Dynamic button for team name (only shows for captains who haven't renamed)."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "BRTournament"):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Team Name",
            custom_id=f"br_team_name:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament or tournament.phase != BRTournamentPhase.TEAMS:
            await interaction.response.send_message(
                "❌ BR турнир не в фазе команд.",
                ephemeral=True
            )
            return

        # Find which team the user is captain of
        user_name = interaction.user.display_name
        team_index = None
        for i, team in enumerate(tournament.teams):
            if team.get("captain") == user_name:
                team_index = i
                break

        if team_index is None:
            await interaction.response.send_message(
                "❌ Вы не капитан ни одной команды.",
                ephemeral=True
            )
            return

        # Check if this captain can still rename
        if not tournament.team_rename_remaining.get(team_index, False):
            await interaction.response.send_message(
                "❌ Вы уже переименовали свою команду или время истекло.",
                ephemeral=True
            )
            return

        # Show modal
        modal = TeamNameModal(self.guild_id, self.channel_id, team_index, user_name)
        await interaction.response.send_modal(modal)


class TeamNameModal(Modal):
    """Modal для ввода названия команды."""

    def __init__(self, guild_id: int, channel_id: int, team_index: int, captain_name: str):
        super().__init__(title="Название команды")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.team_index = team_index
        self.captain_name = captain_name

        self.name_input = TextInput(
            label="Название команды",
            placeholder="Введите название",
            required=True,
            max_length=50
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_name = self.name_input.value.strip()

        if not new_name:
            await interaction.response.send_message(
                "❌ Название не может быть пустым.",
                ephemeral=True
            )
            return

        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        # Изменить название
        if not tournament.rename_team(self.team_index, new_name, self.captain_name):
            await interaction.response.send_message(
                "❌ Не удалось изменить название (возможно уже переименовано).",
                ephemeral=True
            )
            return

        # Сохранить
        br_store.set(tournament)

        # Build teams section
        teams_text = []
        team_emojis = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣", 10: "🔟", 11: "1️⃣1️⃣", 12: "1️⃣2️⃣"}

        for i, team in enumerate(tournament.teams):
            captain = team.get("captain", "Unknown")
            team_name = team.get("name", f"Команда {i+1}")
            players = team.get("players", [])
            all_players = [captain] + players
            players_str = ", ".join(all_players)
            emoji = team_emojis.get(i + 1, "🎮")

            teams_text.append(
                f"{emoji} **{team_name}**\n"
                f"┣ **Капитан:** {captain}\n"
                f"┗ **Состав:** {players_str}\n"
            )

        # Обновить view (убрать кнопку если больше нельзя переименовывать)
        view = BRTeamsView(self.guild_id, self.channel_id, tournament)

        # Обновить embed в стиле бота
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | Команды",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{BOT_OWNER_ID}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: Random\n"
                f"{replace_emojis('sub_middle')} Фаза: Команды\n"
                f"{replace_emojis('sub_directory')} Команд: {len(tournament.teams)}\n\n"
                f"{replace_emojis('white_dot')} **Команды:**\n"
                + "\n".join(teams_text) + "\n\n"
                f"{replace_emojis('a_dot_smaller')} Капитаны могут переименовать свои команды в течение 1 минуты."
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        await interaction.response.edit_message(embed=embed, view=view)

        await interaction.followup.send(
            f"✅ Название изменено на '{new_name}'.",
            ephemeral=True
        )


class BRMapSelectView(View):
    """View для выбора карты и отправки данных."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "BRTournament"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

        # Select menu для выбора карты/данных
        select = MapSelectMenu(guild_id, channel_id, tournament)
        self.add_item(select)


class MapSelectMenu(Select):
    """Select menu для выбора карты."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "BRTournament"):
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

        # Опции в зависимости от текущей фазы
        options = []
        if tournament.phase == BRTournamentPhase.TEAMS:
            options.append(discord.SelectOption(label="Бермуды", value="bermuda"))
        elif tournament.phase == BRTournamentPhase.BERMUDA:
            options.append(discord.SelectOption(label="Чистилище", value="purgatory"))
        elif tournament.phase == BRTournamentPhase.PURGATORY:
            options.append(discord.SelectOption(label="Калахари", value="kalahari"))

        options.append(discord.SelectOption(label="Данные", value="data"))

        super().__init__(
            placeholder="Выберите действие",
            options=options,
            custom_id=f"br_map_select:{guild_id}:{channel_id}"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]

        if value == "data":
            # Отправить данные комнаты
            tournament = br_store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    "❌ BR турнир не найден.",
                    ephemeral=True
                )
                return

            guild = interaction.guild
            role = guild.get_role(tournament.role_id)
            channel = guild.get_channel(tournament.room_channel_id)

            if not channel:
                await interaction.response.send_message(
                    "❌ Канал для данных не найден.",
                    ephemeral=True
                )
                return

            if role:
                await channel.send(f"{role.mention} Данные комнаты для BR турнира!")
            else:
                await channel.send("Данные комнаты для BR турнира!")

            await interaction.response.send_message(
                "✅ Данные отправлены.",
                ephemeral=True
            )
        else:
            # Показать кнопки групп команд
            tournament = br_store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    "❌ BR турнир не найден.",
                    ephemeral=True
                )
                return

            # Обновить фазу
            if value == "bermuda":
                tournament.phase = BRTournamentPhase.BERMUDA
                tournament.current_map = "bermuda"
            elif value == "purgatory":
                tournament.phase = BRTournamentPhase.PURGATORY
                tournament.current_map = "purgatory"
            elif value == "kalahari":
                tournament.phase = BRTournamentPhase.KALAHARI
                tournament.current_map = "kalahari"

            br_store.set(tournament)

            # Создать view с кнопками групп
            view = BRTeamGroupView(self.guild_id, self.channel_id, tournament)

            map_names = {
                "bermuda": "Бермуды",
                "purgatory": "Чистилище",
                "kalahari": "Калахари"
            }

            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | {map_names.get(value, value)}",
                description=(
                    f"{replace_emojis('white_arrow')} **Организатор:** <@{BOT_OWNER_ID}>\n\n"
                    f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                    f"{replace_emojis('sub_middle')} Формат: Random\n"
                    f"{replace_emojis('sub_middle')} Фаза: {map_names.get(value, value)}\n"
                    f"{replace_emojis('sub_directory')} Карта: {map_names.get(value, value)}\n\n"
                    f"{replace_emojis('white_dot')} **Правила:**\n"
                    f"{replace_emojis('white_arrow')} Выберите группу команд для ввода статистики.\n\n"
                    f"{replace_emojis('a_dot_smaller')} Введите место (1-12) и убийства (0-50) для каждой команды."
                ),
                color=discord.Color.from_rgb(69, 69, 69)
            )

            await interaction.response.edit_message(embed=embed, view=view)


class BRTeamGroupView(View):
    """View с кнопками групп команд."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "BRTournament"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

        team_count = len(tournament.teams)

        # Кнопка "Команды 1-6"
        if team_count >= 1:
            self.add_item(TeamGroupButton(guild_id, channel_id, 0, 6, "Команды 1-6"))

        # Кнопка "Команды 7-12"
        if team_count >= 7:
            self.add_item(TeamGroupButton(guild_id, channel_id, 6, 12, "Команды 7-12"))


class TeamGroupButton(Button):
    """Кнопка группы команд."""

    def __init__(self, guild_id: int, channel_id: int, start_idx: int, end_idx: int, label: str):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"br_team_group:{guild_id}:{channel_id}:{start_idx}:{end_idx}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.start_idx = start_idx
        self.end_idx = end_idx

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        # Создать view с кнопками команд
        view = BRTeamSelectView(self.guild_id, self.channel_id, tournament, self.start_idx, self.end_idx)

        await interaction.response.edit_message(view=view)


class BRTeamSelectView(View):
    """View с кнопками названий команд."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "BRTournament", start_idx: int, end_idx: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

        # Добавить кнопки для команд в диапазоне
        for i in range(start_idx, min(end_idx, len(tournament.teams))):
            team = tournament.teams[i]
            self.add_item(TeamButton(guild_id, channel_id, i, team["name"]))

        # Кнопка "Назад"
        self.add_item(BackToMapButton(guild_id, channel_id))


class TeamButton(Button):
    """Кнопка команды."""

    def __init__(self, guild_id: int, channel_id: int, team_index: int, team_name: str):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=team_name,
            custom_id=f"br_team:{guild_id}:{channel_id}:{team_index}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.team_index = team_index

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        # Показать modal для ввода статистики
        modal = BRStatsModal(self.guild_id, self.channel_id, self.team_index, tournament.current_map)
        await interaction.response.send_modal(modal)


class BackToMapButton(Button):
    """Кнопка возврата к выбору карты."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Назад",
            custom_id=f"br_back_map:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        # Вернуться к select menu
        view = BRMapSelectView(self.guild_id, self.channel_id, tournament)

        map_names = {
            "bermuda": "Бермуды",
            "purgatory": "Чистилище",
            "kalahari": "Калахари"
        }

        embed = discord.Embed(
            title=f"{replace_emojis('a_sparkle')} **BATTLE ROYALE - {map_names.get(tournament.current_map, tournament.current_map)}**",
            description="Выберите действие.",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        await interaction.response.edit_message(embed=embed, view=view)


class BRStatsModal(Modal):
    """Modal для ввода статистики команды."""

    def __init__(self, guild_id: int, channel_id: int, team_index: int, map_name: str):
        super().__init__(title="Статистика команды")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.team_index = team_index
        self.map_name = map_name

        self.stats_input = TextInput(
            label="Статистика (место/киллы)",
            placeholder="Пример: 1/23",
            required=True,
            max_length=10
        )
        self.add_item(self.stats_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        stats_str = self.stats_input.value.strip()

        # Парсинг формата "место/киллы"
        try:
            parts = stats_str.split("/")
            if len(parts) != 2:
                raise ValueError("Неверный формат")

            place = int(parts[0])
            kills = int(parts[1])
        except ValueError:
            await interaction.response.send_message(
                "❌ Неверный формат. Используйте: место/киллы (например: 1/23)",
                ephemeral=True
            )
            return

        # Валидация
        if place < 1 or place > 12:
            await interaction.response.send_message(
                "❌ Место должно быть от 1 до 12.",
                ephemeral=True
            )
            return

        if kills < 0 or kills > 50:
            await interaction.response.send_message(
                "❌ Киллы должны быть от 0 до 50.",
                ephemeral=True
            )
            return

        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        # Сохранить статистику
        if not tournament.set_team_stats(self.team_index, self.map_name, place, kills):
            await interaction.response.send_message(
                "❌ Не удалось сохранить статистику.",
                ephemeral=True
            )
            return

        # Сохранить
        br_store.set(tournament)

        # Проверить все ли команды заполнили статистику
        if tournament.are_all_stats_complete(self.map_name):
            # Переход к следующей фазе
            if tournament.phase == BRTournamentPhase.BERMUDA:
                tournament.phase = BRTournamentPhase.PURGATORY
            elif tournament.phase == BRTournamentPhase.PURGATORY:
                tournament.phase = BRTournamentPhase.KALAHARI
            elif tournament.phase == BRTournamentPhase.KALAHARI:
                tournament.phase = BRTournamentPhase.RESULTS

            br_store.set(tournament)

            if tournament.phase == BRTournamentPhase.RESULTS:
                # Снять роль с реальных игроков
                guild = interaction.guild
                role = guild.get_role(tournament.role_id)
                if role:
                    for player in tournament.players:
                        user_id = player["id"]
                        if user_id < 0:  # Пропускаем ботов
                            continue
                        try:
                            member = guild.get_member(user_id)
                            if member:
                                await member.remove_roles(role)
                        except Exception as e:
                            logger.warning("Не удалось снять роль с пользователя %s: %s", user_id, e)

                # Показать результаты
                results = tournament.calculate_final_results()

                # Build results section
                results_text = []
                for i, result in enumerate(results):
                    medal = ""
                    if i == 0:
                        medal = "🥇 "
                    elif i == 1:
                        medal = "🥈 "
                    elif i == 2:
                        medal = "🥉 "

                    results_text.append(
                        f"{medal} **{i + 1}. {result['name']}** - Очки: **{result['total_points']}**"
                    )

                embed = discord.Embed(
                    title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | Результаты",
                    description=(
                        f"{replace_emojis('white_arrow')} **Организатор:** <@{BOT_OWNER_ID}>\n\n"
                        f"{replace_emojis('white_dot')} **Результаты турнира:**\n"
                        + "\n".join(results_text)
                    ),
                    color=discord.Color.gold()
                )

                view = BRResultsView(self.guild_id, self.channel_id)
                await interaction.response.edit_message(embed=embed, view=view)
            else:
                # Вернуться к select menu следующей карты
                view = BRMapSelectView(self.guild_id, self.channel_id, tournament)

                map_names = {
                    "bermuda": "Бермуды",
                    "purgatory": "Чистилище",
                    "kalahari": "Калахари"
                }

                next_map = ""
                if tournament.phase == BRTournamentPhase.PURGATORY:
                    next_map = "Чистилище"
                elif tournament.phase == BRTournamentPhase.KALAHARI:
                    next_map = "Калахари"

                embed = discord.Embed(
                    title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | {next_map}",
                    description=(
                        f"{replace_emojis('white_arrow')} **Организатор:** <@{BOT_OWNER_ID}>\n\n"
                        f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                        f"{replace_emojis('sub_middle')} Формат: Random\n"
                        f"{replace_emojis('sub_middle')} Фаза: {next_map}\n"
                        f"{replace_emojis('sub_directory')} Следующая карта: {next_map}\n\n"
                        f"{replace_emojis('white_dot')} **Статус:**\n"
                        f"{replace_emojis('white_arrow')} Все команды заполнили статистику. Переход к следующей карте."
                    ),
                    color=discord.Color.from_rgb(69, 69, 69)
                )

                await interaction.response.edit_message(embed=embed, view=view)
        else:
            # Вернуться к select menu
            view = BRMapSelectView(self.guild_id, self.channel_id, tournament)

            await interaction.response.edit_message(view=view)

        await interaction.followup.send(
            f"✅ Статистика сохранена: место {place}, киллы {kills}.",
            ephemeral=True
        )


class BRResultsView(View):
    """View для финальных результатов."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id

        # Кнопка "Удалить"
        self.add_item(DeleteBRButton(guild_id, channel_id))
