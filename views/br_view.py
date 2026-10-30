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

        # Кнопки в зависимости от формата
        if tournament.formation.value == "team":
            # Team format: register team button
            self.add_item(RegisterTeamButton(guild_id, channel_id))
            # Leave tournament button for team captains
            self.add_item(LeaveTournamentButton(guild_id, channel_id))
        else:
            # Random format: individual join/leave
            self.add_item(JoinBRButton(guild_id, channel_id))
            self.add_item(LeaveBRButton(guild_id, channel_id))

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
        # Check if bot owner or specific user (582145013358657549)
        if interaction.user.id != BOT_OWNER_ID and interaction.user.id != 582145013358657549:
            await interaction.response.send_message(
                "❌ Только владелец бота или организатор могут управлять BR турниром.",
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

        from storage.br_store import br_store
        from models.br_tournament import BRTournamentPhase

        tournament = br_store.get(guild_id, channel_id)
        if not tournament:
            return

        # Add buttons based on phase
        if tournament.phase == BRTournamentPhase.REGISTRATION:
            if tournament.formation.value == "team":
                self.add_item(RemoveTeamButton(guild_id, channel_id))
            else:
                self.add_item(DeleteBRPlayerButton(guild_id, channel_id))
            self.add_item(StartBRButton(guild_id, channel_id))
        elif tournament.phase in [BRTournamentPhase.BERMUDA, BRTournamentPhase.PURGATORY, BRTournamentPhase.KALAHARI]:
            self.add_item(BRStatsButton(guild_id, channel_id))
            self.add_item(BRRoomButton(guild_id, channel_id))
            self.add_item(BRDataButton(guild_id, channel_id))

            # Add Purgatory button only if all stats are complete for current map
            if tournament.phase == BRTournamentPhase.BERMUDA:
                if tournament.are_all_stats_complete("bermuda"):
                    self.add_item(BRPurgatoryButton(guild_id, channel_id))
                else:
                    # Add disabled button
                    btn = Button(
                        style=discord.ButtonStyle.secondary,
                        label="Чистилище",
                        disabled=True,
                        custom_id=f"br_purgatory_disabled:{guild_id}:{channel_id}"
                    )
                    self.add_item(btn)
            elif tournament.phase == BRTournamentPhase.PURGATORY:
                if tournament.are_all_stats_complete("purgatory"):
                    self.add_item(BRKalahariButton(guild_id, channel_id))
                else:
                    btn = Button(
                        style=discord.ButtonStyle.secondary,
                        label="Калахари",
                        disabled=True,
                        custom_id=f"br_kalahari_disabled:{guild_id}:{channel_id}"
                    )
                    self.add_item(btn)
        elif tournament.phase == BRTournamentPhase.RESULTS:
            # No buttons in results phase - tournament is complete
            pass


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
        tournament.phase = BRTournamentPhase.BERMUDA
        tournament.team_rename_start_time = interaction.created_at.isoformat()

        # Give BR role to real players
        guild = interaction.guild
        role = guild.get_role(tournament.role_id)
        if role:
            if tournament.formation.value == "team":
                # Team format: only give role to captains
                from storage.team_store import team_store
                for team_id in tournament.registered_teams:
                    team = team_store.get(team_id)
                    if team:
                        user_id = team.captain_id
                        if user_id < 0:  # Skip bots
                            continue
                        try:
                            member = guild.get_member(user_id)
                            if member:
                                await member.add_roles(role)
                        except Exception as e:
                            logger.warning("Не удалось выдать роль капитану: %s", e)
            else:
                # Random format: give role to all players
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
        num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                      get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

        for i, team in enumerate(tournament.teams):
            captain = team.get("captain", "Unknown")
            team_name = team.get("name", f"Команда {i+1}")
            players = team.get("players", [])
            players_str = ", ".join(players) if players else ""
            num_emoji = num_emojis[i] if i < len(num_emojis) else "🎮"

            teams_text.append(
                f"{num_emoji} {team_name}\n"
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
            )

        # Update embed in bot style
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | Команды",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: {tournament.formation.value.capitalize()}\n"
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

        from utils.cosmetics import clean_nickname
        user_name = clean_nickname(interaction.user.display_name)
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
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | {tournament.formation.value.capitalize()} | Регистрация",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: {tournament.formation.value.capitalize()}\n"
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
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | {tournament.formation.value.capitalize()} | Регистрация",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: {tournament.formation.value.capitalize()}\n"
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


class LeaveTournamentButton(Button):
    """Кнопка для выхода команды из BR турнира (для капитанов команд)."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Выйти",
            custom_id=f"br_leave_tournament:{guild_id}:{channel_id}"
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

        if tournament.formation.value != "team":
            await interaction.response.send_message(
                "❌ Эта кнопка только для team формата.",
                ephemeral=True
            )
            return

        # Check if user is captain of a registered team
        from storage.team_store import team_store
        user_team = team_store.get_by_captain(interaction.user.id)
        if not user_team:
            await interaction.response.send_message(
                "❌ Вы не капитан зарегистрированной команды.",
                ephemeral=True
            )
            return

        # Check if team is registered in tournament
        if user_team.team_id not in tournament.registered_teams:
            await interaction.response.send_message(
                "❌ Ваша команда не зарегистрирована в этом турнире.",
                ephemeral=True
            )
            return

        # Remove team from tournament
        tournament.registered_teams.remove(user_team.team_id)
        br_store.set(tournament)

        # Обновить embed с текущим списком команд
        team_emoji_names = ["num_1", "num_2", "num_3", "num_4", "num_5", "num_6",
                           "num_7", "num_8", "num_9", "num_ten", "num_eleven", "num_twelve"]
        team_list = []
        for i, team_id in enumerate(tournament.registered_teams):
            team = team_store.get(team_id)
            if team:
                emoji = get_emoji(team_emoji_names[i]) if i < len(team_emoji_names) else f"{i+1}"
                team_list.append(f"{emoji} {team.team_name}")

        # Build description parts
        organizer_line = f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>"
        info_header = f"{replace_emojis('white_dot')} **Информация о турнире:**"
        format_line = f"{replace_emojis('sub_middle')} Формат: Team"
        phase_line = f"{replace_emojis('sub_middle')} Фаза: Регистрация"
        count_line = f"{replace_emojis('sub_directory')} Команд: {len(tournament.registered_teams)}/12"
        teams_header = f"{replace_emojis('white_dot')} **Команды:**"
        teams_content = "\n".join(team_list) if team_list else f"{replace_emojis('white_arrow')} *"
        rules_header = f"{replace_emojis('white_dot')} **Правила:**"
        rules_content = f"{replace_emojis('white_arrow')} Нажмите кнопку '🏆 Зарегистрировать Команду' для регистрации вашей команды."
        footer = f"{replace_emojis('a_dot_smaller')} Используйте кнопки ниже для регистрации на турнир"

        description = (
            f"{organizer_line}\n\n"
            f"{info_header}\n"
            f"{format_line}\n"
            f"{phase_line}\n"
            f"{count_line}\n\n"
            f"{teams_header}\n"
            f"{teams_content}\n\n"
            f"{rules_header}\n"
            f"{rules_content}\n\n"
            f"{footer}"
        )

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Team | Регистрация",
            description=description,
            color=discord.Color.from_rgb(69, 69, 69)
        )

        view = BRRegistrationView(tournament.guild_id, tournament.channel_id, tournament)
        await interaction.response.edit_message(embed=embed, view=view)

        await interaction.followup.send(
            f"✅ Команда '{user_team.team_name}' вышла из турнира.",
            ephemeral=True
        )


class RegisterTeamButton(Button):
    """Кнопка для регистрации команды на BR турнир."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="🏆 Зарегистрировать Команду",
            custom_id=f"br_register_team:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.team_store import team_store

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

        # Get user's team
        team = team_store.get_by_captain(interaction.user.id)
        if not team:
            await interaction.response.send_message(
                "❌ Вы не капитан команды! Создайте команду через `/team create` в канале <#1563826579061215332>.",
                ephemeral=True
            )
            return

        # Check if team is full (4 players)
        if not team.is_full():
            await interaction.response.send_message(
                f"❌ В команде {team.member_count()}/4 игроков! Нужно 4 игрока для регистрации.",
                ephemeral=True
            )
            return

        # Check if team already registered
        if team.team_id in tournament.registered_teams:
            await interaction.response.send_message(
                "❌ Ваша команда уже зарегистрирована!",
                ephemeral=True
            )
            return

        # Register team
        tournament.registered_teams.append(team.team_id)

        # Add team members to players list for role distribution (check for duplicates)
        from utils.cosmetics import clean_nickname
        for member_id in team.members:
            # Check if already in players list
            already_registered = any(p["id"] == member_id for p in tournament.players)
            if not already_registered:
                member = interaction.guild.get_member(member_id)
                if member:
                    clean_name = clean_nickname(member.display_name)
                    tournament.players.append({"name": clean_name, "id": member_id})
                    print(f"Added player to tournament: {clean_name} (ID: {member_id})")
                else:
                    # Member not in guild (bot or offline), add with fallback name
                    tournament.players.append({"name": f"User_{member_id}", "id": member_id})
                    print(f"Added offline/bot player to tournament: User_{member_id} (ID: {member_id})")
            else:
                print(f"Player already in tournament: {member_id}")

        print(f"Total players in tournament after registration: {len(tournament.players)}")
        print(f"Registered teams: {tournament.registered_teams}")

        br_store.set(tournament)

        # Get team number emoji (using same logic as random BR)
        from config import get_emoji
        team_number = len(tournament.registered_teams)
        team_emoji_names = ["num_1", "num_2", "num_3", "num_4", "num_5", "num_6",
                           "num_7", "num_8", "num_9", "num_ten", "num_eleven", "num_twelve"]
        team_emoji = get_emoji(team_emoji_names[team_number - 1]) if team_number <= len(team_emoji_names) else f"{team_number}"

        # Build team list text
        team_list = []
        for i, team_id in enumerate(tournament.registered_teams):
            registered_team = team_store.get(team_id)
            if registered_team:
                emoji = get_emoji(team_emoji_names[i]) if i < len(team_emoji_names) else f"{i+1}"
                team_list.append(f"{emoji} {registered_team.team_name}")

        # Build description parts for debugging
        organizer_line = f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>"
        info_header = f"{replace_emojis('white_dot')} **Информация о турнире:**"
        format_line = f"{replace_emojis('sub_middle')} Формат: Team"
        phase_line = f"{replace_emojis('sub_middle')} Фаза: Регистрация"
        count_line = f"{replace_emojis('sub_directory')} Команд: {len(tournament.registered_teams)}/12"
        teams_header = f"{replace_emojis('white_dot')} **Команды:**"
        teams_content = "\n".join(team_list) if team_list else f"{replace_emojis('white_arrow')} *"
        rules_header = f"{replace_emojis('white_dot')} **Правила:**"
        rules_content = f"{replace_emojis('white_arrow')} Нажмите кнопку '🏆 Зарегистрировать Команду' для регистрации вашей команды."
        footer = f"{replace_emojis('a_dot_smaller')} Используйте кнопки ниже для регистрации на турнир"

        print(f"organizer_line: {organizer_line}")
        print(f"info_header: {info_header}")
        print(f"teams_content: {teams_content}")

        description = (
            f"{organizer_line}\n\n"
            f"{info_header}\n"
            f"{format_line}\n"
            f"{phase_line}\n"
            f"{count_line}\n\n"
            f"{teams_header}\n"
            f"{teams_content}\n\n"
            f"{rules_header}\n"
            f"{rules_content}\n\n"
            f"{footer}"
        )

        print(f"Full description length: {len(description)}")
        print(f"Full description: {description}")

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Team | Регистрация",
            description=description,
            color=discord.Color.from_rgb(69, 69, 69)
        )

        view = BRRegistrationView(tournament.guild_id, tournament.channel_id, tournament)
        await interaction.response.edit_message(embed=embed, view=view)

        await interaction.followup.send(
            f"✅ Команда '{team.team_name}' зарегистрирована как {team_emoji}!",
            ephemeral=True
        )


class RemoveTeamButton(Button):
    """Кнопка для удаления команды из BR турнира (владелец бота)."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Удалить команду",
            custom_id=f"br_remove_team:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check if bot owner
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Только владелец бота может удалять команды.",
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

        if tournament.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message(
                "❌ BR турнир не в фазе регистрации.",
                ephemeral=True
            )
            return

        # Show select menu to choose team to remove
        from storage.team_store import team_store

        options = []
        for team_id in tournament.registered_teams:
            team = team_store.get(team_id)
            if team:
                options.append(
                    discord.SelectOption(
                        label=team.team_name,
                        value=team_id
                    )
                )

        if not options:
            await interaction.response.send_message(
                "❌ Нет зарегистрированных команд для удаления.",
                ephemeral=True
            )
            return

        view = RemoveTeamSelectView(self.guild_id, self.channel_id, options)
        await interaction.response.send_message("Выберите команду для удаления:", view=view, ephemeral=True)


class RemoveTeamSelectView(View):
    """View with select menu for removing teams."""

    def __init__(self, guild_id: int, channel_id: int, options: list[discord.SelectOption]):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id

        select = discord.ui.Select(
            placeholder="Выберите команду...",
            options=options,
            min_values=1,
            max_values=1
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        team_id = interaction.data["values"][0]

        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        if team_id in tournament.registered_teams:
            # Get team before removing to also remove players
            from storage.team_store import team_store
            team = team_store.get(team_id)

            # Remove team from registered teams
            tournament.registered_teams.remove(team_id)

            # Also remove team members from players list
            if team:
                for member_id in team.members:
                    tournament.players = [p for p in tournament.players if p["id"] != member_id]

            # Save tournament
            br_store.set(tournament)

            # Send success message
            await interaction.response.send_message(
                ":white_check_mark: Команда удалена из турнира!",
                ephemeral=True
            )

            # Update main tournament message - delete corrupted and update correct one
            print("Deleting corrupted BATTLE ROYALE messages and updating correct one...")
            try:
                # Find any BATTLE ROYALE message (take first one if no proper one found)
                target_msg = None
                proper_msg = None
                async for msg in interaction.channel.history(limit=50):
                    if msg.author.bot and msg.embeds:
                        embed = msg.embeds[0]
                        if "BATTLE ROYALE CUP" in embed.title:
                            # Check if this is a proper message (title has no team names)
                            if "Team " not in embed.title:
                                proper_msg = msg
                                print(f"Found proper tournament message: {msg.id}")
                            if target_msg is None:
                                target_msg = msg
                            if proper_msg:
                                break
                
                # Use proper message if found, otherwise use first found
                msg_to_update = proper_msg if proper_msg else target_msg
                
                if msg_to_update:
                    # Delete all other BATTLE ROYALE messages
                    async for msg in interaction.channel.history(limit=100):
                        if msg.author.bot and msg.embeds:
                            embed = msg.embeds[0]
                            if "BATTLE ROYALE CUP" in embed.title and msg.id != msg_to_update.id:
                                print(f"Deleting corrupted message: {msg.id}")
                                try:
                                    await msg.delete()
                                except Exception as e:
                                    print(f"Failed to delete message: {e}")
                    
                    # Update the message
                    team_emoji_names = ["num_1", "num_2", "num_3", "num_4", "num_5", "num_6",
                                       "num_7", "num_8", "num_9", "num_ten", "num_eleven", "num_twelve"]
                    team_list = []
                    for i, tid in enumerate(tournament.registered_teams):
                        registered_team = team_store.get(tid)
                        if registered_team:
                            emoji = get_emoji(team_emoji_names[i]) if i < len(team_emoji_names) else f"{i+1}"
                            team_list.append(f"{emoji} {registered_team.team_name}")

                    team_list_str = "\n".join(team_list) if team_list else f"{replace_emojis('white_arrow')} *"
                    print(f"Team list: {team_list_str[:200]}")

                    new_embed = discord.Embed(
                        title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Team | Регистрация",
                        description=(
                            f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                            f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                            f"{replace_emojis('sub_middle')} Формат: Team\n"
                            f"{replace_emojis('sub_middle')} Фаза: Регистрация\n"
                            f"{replace_emojis('sub_directory')} Команд: {len(tournament.registered_teams)}/12\n\n"
                            f"{replace_emojis('white_dot')} **Команды:**\n"
                            f"{team_list_str}\n\n"
                            f"{replace_emojis('white_dot')} **Правила:**\n"
                            f"{replace_emojis('white_arrow')} Нажмите кнопку '🏆 Зарегистрировать Команду' для регистрации вашей команды.\n\n"
                            f"{replace_emojis('a_dot_smaller')} Используйте кнопки ниже для регистрации на турнир"
                        ),
                        color=discord.Color.from_rgb(69, 69, 69)
                    )

                    print(f"New embed title: {new_embed.title}")
                    print(f"New embed description (first 300 chars): {new_embed.description[:300]}")

                    view = BRRegistrationView(tournament.guild_id, tournament.channel_id, tournament)
                    await msg_to_update.edit(embed=new_embed, view=view)
                    tournament.message_id = msg_to_update.id
                    br_store.set(tournament)
                    print(f"Updated tournament message: {msg_to_update.id} (proper: {proper_msg is not None})")
                else:
                    print("No BATTLE ROYALE message found at all")
            except Exception as e:
                print(f"Error updating tournament message: {e}")
                import traceback
                traceback.print_exc()
        else:
            await interaction.response.send_message(
                "❌ Команда не найдена в турнире.",
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

        # Create select menu with all players (paginated, 16 per page)
        if not tournament.players:
            await interaction.response.send_message(
                "❌ Нет зарегистрированных игроков.",
                ephemeral=True
            )
            return

        view = RemoveBRPlayerPaginationView(self.guild_id, self.channel_id, tournament.players, 1)

        await interaction.response.send_message(
            "Выберите игрока для удаления:",
            view=view,
            ephemeral=True
        )


class RemoveBRPlayerSelect(Select):
    """Select menu to choose player to remove with pagination."""

    def __init__(self, guild_id: int, channel_id: int, players: list, page: int = 1):
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.players = players
        self.page = page
        self.per_page = 16

        # Calculate page range
        start = (page - 1) * self.per_page
        end = start + self.per_page
        current_players = players[start:end]

        options = []
        for player in current_players:
            options.append(
                discord.SelectOption(
                    label=player["name"],
                    value=str(player["id"])
                )
            )

        super().__init__(
            placeholder=f"Выберите игрока ({page}/{(len(players) + self.per_page - 1) // self.per_page})",
            options=options,
            custom_id=f"br_remove_player_select:{guild_id}:{channel_id}:{page}"
        )

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

        # Get the main BR message
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel or not tournament.message_id:
            await interaction.response.send_message(
                "❌ Не удалось обновить сообщение турнира.",
                ephemeral=True
            )
            return

        try:
            message = await channel.fetch_message(tournament.message_id)

            # Update embed with current player list
            player_names = [p["name"] for p in tournament.players]
            players_text = ", ".join(player_names) if player_names else "*"

            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | {tournament.formation.value.capitalize()} | Регистрация",
                description=(
                    f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                    f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                    f"{replace_emojis('sub_middle')} Формат: {tournament.formation.value.capitalize()}\n"
                    f"{replace_emojis('sub_middle')} Фаза: Регистрация\n"
                    f"{replace_emojis('sub_directory')} Игроков: {len(tournament.players)}/48\n\n"
                    f"{replace_emojis('white_dot')} **Зарегистрированные игроки:**\n"
                    f"{players_text}\n\n"
                    f"{replace_emojis('a_dot_smaller')} Зарегистрируйтесь для участия!"
                ),
                color=discord.Color.from_rgb(69, 69, 69)
            )

            await message.edit(embed=embed)
            await interaction.response.send_message(
                "✅ Игрок удален.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error updating BR message after player removal: {e}")
            await interaction.response.send_message(
                "❌ Не удалось обновить сообщение турнира.",
                ephemeral=True
            )


class RemoveBRPlayerPaginationView(View):
    """View with pagination for player removal."""

    def __init__(self, guild_id: int, channel_id: int, players: list, page: int = 1):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.players = players
        self.page = page
        self.per_page = 16
        self.total_pages = (len(players) + self.per_page - 1) // self.per_page

        self.add_item(RemoveBRPlayerSelect(guild_id, channel_id, players, page))

        # Add pagination buttons if needed
        if self.total_pages > 1:
            if self.page > 1:
                self.add_item(RemoveBRPlayerPrevButton(guild_id, channel_id, players, page))

            if self.page < self.total_pages:
                self.add_item(RemoveBRPlayerNextButton(guild_id, channel_id, players, page))


class RemoveBRPlayerPrevButton(Button):
    """Previous page button for player removal."""

    def __init__(self, guild_id: int, channel_id: int, players: list, page: int):
        super().__init__(
            label="◀",
            style=discord.ButtonStyle.primary,
            custom_id=f"br_remove_prev:{guild_id}:{channel_id}:{page}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.players = players
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        new_page = self.page - 1
        view = RemoveBRPlayerPaginationView(self.guild_id, self.channel_id, self.players, new_page)
        await interaction.response.edit_message(view=view)


class RemoveBRPlayerNextButton(Button):
    """Next page button for player removal."""

    def __init__(self, guild_id: int, channel_id: int, players: list, page: int):
        super().__init__(
            label="▶",
            style=discord.ButtonStyle.primary,
            custom_id=f"br_remove_next:{guild_id}:{channel_id}:{page}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.players = players
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        new_page = self.page + 1
        view = RemoveBRPlayerPaginationView(self.guild_id, self.channel_id, self.players, new_page)
        await interaction.response.edit_message(view=view)


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
            if tournament.formation.value == "team":
                # Remove role only from captains
                from storage.team_store import team_store
                for team_id in tournament.registered_teams:
                    team = team_store.get(team_id)
                    if team:
                        user_id = team.captain_id
                        if user_id < 0:
                            continue
                        try:
                            member = guild.get_member(user_id)
                            if member:
                                await member.remove_roles(role)
                        except Exception as e:
                            logger.warning("Не удалось снять роль капитана: %s", e)
            else:
                # Remove role from all players (random format)
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


class BRRoomButton(Button):
    """Кнопка для открытия комнаты BR."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Комната",
            custom_id=f"br_room:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        # Проверка на владельца
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Только владелец бота может открыть комнату.",
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

        # Show modal directly
        view = BRRoomModal(self.guild_id, self.channel_id)
        await interaction.response.send_modal(view)


class BRDataButton(Button):
    """Кнопка для отправки данных BR."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Данные",
            custom_id=f"br_data:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        # Проверка на владельца
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Только владелец бота может отправить данные.",
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

        # Check if room data exists
        if not tournament.room_data:
            await interaction.response.send_message(
                "❌ Сначала откройте комнату.",
                ephemeral=True
            )
            return

        # Close the ephemeral menu
        await interaction.response.edit_message(content="✅ Данные отправлены", view=None)

        # Send room data to channel
        try:
            channel = interaction.guild.get_channel(1563195265395982466)
            if not channel:
                await interaction.followup.send(
                    "❌ Канал для данных не найден.",
                    ephemeral=True
                )
                return

            # Get current map
            map_names = {
                "bermuda": "Бермуды",
                "purgatory": "Чистилище",
                "kalahari": "Калахари"
            }
            current_map = tournament.current_map if hasattr(tournament, 'current_map') else "bermuda"
            map_name = map_names.get(current_map, current_map)

            # Get BR role
            role = interaction.guild.get_role(tournament.role_id)
            role_mention = f"<@&{tournament.role_id}>" if role else ""

            # Send message 1: Header
            await channel.send(
                content=f"**КОМНАТА ОТКРЫТА**\nКарта: {map_name}\n\n{role_mention}\n\n**Данные:**"
            )

            # Send message 2: Room ID
            await channel.send(content=tournament.room_data.get("id", ""))

            # Send message 3: Password
            await channel.send(content=tournament.room_data.get("password", ""))

            # Send message 4: GIF
            await channel.send(
                content="https://cdn.discordapp.com/attachments/1560907251412238396/1561697607770832957/382752-purplelines.gif?ex=6ad74bbc&is=6ad5fa3c&hm=e731b1784d3a8945004feccc0b7ae13373bd92341484f8f9e3cddc3c4c8120ff&"
            )

            # Send to general channel
            general_channel = interaction.guild.get_channel(1200125075156910181)
            if general_channel:
                await general_channel.send(
                    content=f"{role_mention}\n\n{replace_emojis('white_dot')} Комната открыта, данные {replace_emojis('white_arrow')} <#1563195265395982466>"
                )
        except Exception as e:
            logger.error(f"Error sending BR data: {e}")
            await interaction.followup.send(
                "❌ Не удалось отправить данные.",
                ephemeral=True
            )


class BRRoomModal(Modal):
    """Modal для ввода ID и пароля комнаты BR."""

    room_id = TextInput(
        label="ID комнаты",
        placeholder="123456",
        required=True,
        max_length=20
    )

    room_password = TextInput(
        label="Пароль",
        placeholder="123",
        required=True,
        max_length=20
    )

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(title="Комната BR")
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tournament = br_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
                ephemeral=True
            )
            return

        # Store room data
        tournament.room_data = {
            "id": self.room_id.value,
            "password": self.room_password.value
        }
        br_store.set(tournament)

        await interaction.response.send_message(
            "✅ Комната сохранена. Нажмите 'Данные' для отправки.",
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
        if tournament.formation.value == "team":
            if len(tournament.registered_teams) < 5:
                await interaction.response.send_message(
                    "❌ Нельзя начать: нужно минимум 5 команд.",
                    ephemeral=True
                )
                return

            # Сформировать команды из зарегистрированных
            if not tournament.form_teams_from_registered():
                await interaction.response.send_message(
                    "❌ Не удалось сформировать команды из зарегистрированных.",
                    ephemeral=True
                )
                return

            # For team format, go directly to Bermuda phase (no rename phase)
            tournament.phase = BRTournamentPhase.BERMUDA
            tournament.current_map = "bermuda"
            br_store.set(tournament)

            # Give role to captains (team format)
            guild = interaction.guild
            role = guild.get_role(tournament.role_id)
            if role:
                from storage.team_store import team_store
                for team_id in tournament.registered_teams:
                    team = team_store.get(team_id)
                    if team:
                        user_id = team.captain_id
                        if user_id < 0:
                            continue
                        try:
                            member = guild.get_member(user_id)
                            if member:
                                await member.add_roles(role)
                        except Exception as e:
                            logger.warning("Не удалось выдать роль капитану: %s", e)

            # Create Bermuda view
            view = BRBermudaView(self.guild_id, self.channel_id)

            map_names = {
                "bermuda": "Бермуды",
                "purgatory": "Чистилище",
                "kalahari": "Калахари"
            }

            # Build teams section with current map points (sorted by points)
            teams_with_points = []
            num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                          get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

            for i, team in enumerate(tournament.teams):
                captain = team.get("captain", "Unknown")
                team_name = team.get("name", f"Команда {i+1}")
                players = team.get("players", [])
                players_str = ", ".join(players) if players else ""

                # Calculate points for current map only
                stats = team.get("stats", {})
                map_points = 0
                if "bermuda" in stats:
                    map_stats = stats["bermuda"]
                    place = map_stats.get("place", 0)
                    kills = map_stats.get("kills", 0)
                    if place > 0:
                        # Points based on place: 1=12, 2=9, 3=8, 4=7, 5=6, 6=5, 7=4, 8=3, 9=2, 10=1, 11=0, 12=0
                        place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                        map_points = place_points.get(place, 0) + kills

                teams_with_points.append({
                    "captain": captain,
                    "team_name": team_name,
                    "players_str": players_str,
                    "points": map_points,
                    "index": i
                })

            # Sort by points (descending)
            teams_with_points.sort(key=lambda x: x["points"], reverse=True)

            teams_text = []
            for team_data in teams_with_points:
                captain = team_data["captain"]
                team_name = team_data["team_name"]
                players_str = team_data["players_str"]
                points = team_data["points"]
                original_index = team_data["index"]
                num_emoji = num_emojis[original_index] if original_index < len(num_emojis) else "🎮"

                teams_text.append(
                    f"{num_emoji} {team_name} ({points} очков)\n"
                    f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
                )

            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | {tournament.formation.value.capitalize()} | {map_names.get('bermuda', 'bermuda')}",
                description=(
                    f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                    f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                    f"{replace_emojis('sub_middle')} Формат: {tournament.formation.value.capitalize()}\n"
                    f"{replace_emojis('sub_directory')} Карта: {map_names.get('bermuda', 'bermuda')}\n\n"
                    f"{replace_emojis('white_dot')} **Участники команд:**\n\n"
                    + "\n".join(teams_text)
                ),
                color=discord.Color.from_rgb(69, 69, 69)
            )

            # Get the main BR message and edit it
            channel = interaction.guild.get_channel(self.channel_id)
            if not channel or not tournament.message_id:
                await interaction.response.send_message(
                    "❌ Не удалось обновить сообщение турнира.",
                    ephemeral=True
                )
                return

            try:
                message = await channel.fetch_message(tournament.message_id)
                await message.edit(embed=embed, view=view)
                await interaction.response.send_message(
                    "✅ Турнир запущен!",
                    ephemeral=True
                )
            except Exception as e:
                logger.error("Ошибка при обновлении сообщения: %s", e)
                await interaction.response.send_message(
                    "❌ Не удалось обновить сообщение турнира.",
                    ephemeral=True
                )
            return
        else:
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
            if tournament.formation.value == "team":
                # Give role only to captains
                from storage.team_store import team_store
                for team_id in tournament.registered_teams:
                    team = team_store.get(team_id)
                    if team:
                        user_id = team.captain_id
                        if user_id < 0:
                            continue
                        try:
                            member = guild.get_member(user_id)
                            if member:
                                await member.add_roles(role)
                        except Exception as e:
                            logger.warning("Не удалось выдать роль капитану: %s", e)
            else:
                # Give role to all players (random format)
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
        num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                      get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

        for i, team in enumerate(tournament.teams):
            captain = team.get("captain", "Unknown")
            team_name = team.get("name", f"Команда {i+1}")
            players = team.get("players", [])
            players_str = ", ".join(players) if players else ""
            num_emoji = num_emojis[i] if i < len(num_emojis) else "🎮"

            teams_text.append(
                f"{num_emoji} {team_name}\n"
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
            )

        # Создать view для фазы команд
        view = BRTeamsView(self.guild_id, self.channel_id, tournament)

        # Создать embed в стиле бота
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | {tournament.formation.value.capitalize()} | Команды",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: {tournament.formation.value.capitalize()}\n"
                f"{replace_emojis('sub_middle')} Фаза: Бермуды\n"
                f"{replace_emojis('sub_directory')} Команд: {len(tournament.teams)}\n\n"
                f"{replace_emojis('white_dot')} **Участники команд:**\n\n"
                + "\n".join(teams_text)
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        # Get the main BR message and edit it
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel or not tournament.message_id:
            await interaction.response.send_message(
                "❌ Не удалось обновить сообщение турнира.",
                ephemeral=True
            )
            return

        try:
            message = await channel.fetch_message(tournament.message_id)
            await message.edit(embed=embed, view=view)
            await interaction.response.send_message(
                "✅ Турнир запущен!",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error updating BR message after start: {e}")
            await interaction.response.send_message(
                "❌ Не удалось обновить сообщение турнира.",
                ephemeral=True
            )


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

        # Create view with only organizer menu button
        view = BRBermudaView(self.guild_id, self.channel_id)

        map_names = {
            "bermuda": "Бермуды",
            "purgatory": "Чистилище",
            "kalahari": "Калахари"
        }

        # Build teams section with current map points (sorted by points)
        teams_with_points = []
        num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                      get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

        for i, team in enumerate(tournament.teams):
            captain = team.get("captain", "Unknown")
            team_name = team.get("name", f"Команда {i+1}")
            players = team.get("players", [])
            players_str = ", ".join(players) if players else ""

            # Calculate points for current map only
            stats = team.get("stats", {})
            map_points = 0
            if "bermuda" in stats:
                map_stats = stats["bermuda"]
                place = map_stats.get("place", 0)
                kills = map_stats.get("kills", 0)
                if place > 0:
                    map_points = (13 - place) + kills

            teams_with_points.append({
                "captain": captain,
                "team_name": team_name,
                "players_str": players_str,
                "points": map_points,
                "index": i
            })

        # Sort by points descending
        teams_with_points.sort(key=lambda x: x["points"], reverse=True)

        teams_text = []
        for team_data in teams_with_points:
            captain = team_data["captain"]
            team_name = team_data["team_name"]
            players_str = team_data["players_str"]
            points = team_data["points"]
            original_index = team_data["index"]
            num_emoji = num_emojis[original_index] if original_index < len(num_emojis) else "🎮"

            teams_text.append(
                f"{num_emoji} {team_name} ({points} очков)\n"
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
            )

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | {tournament.formation.value.capitalize()} | Команды",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: {tournament.formation.value.capitalize()}\n"
                f"{replace_emojis('sub_middle')} Фаза: Бермуды\n"
                f"{replace_emojis('sub_directory')} Команд: {len(tournament.teams)}\n\n"
                f"{replace_emojis('white_dot')} **Участники команд:**\n\n"
                + "\n".join(teams_text)
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        await interaction.response.edit_message(embed=embed, view=view)


class BRBermudaView(View):
    """View for Bermuda phase with only organizer menu button."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id

        self.add_item(BROrganizerMenuButton(guild_id, channel_id))


class BRStatsButton(Button):
    """Button to open stats entry."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Статистика",
            custom_id=f"br_stats:{guild_id}:{channel_id}"
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

        # Close the ephemeral menu
        await interaction.response.edit_message(content="✅ Открыт ввод статистики", view=None)

        # Show team group selection view
        view = BRTeamGroupSelectView(self.guild_id, self.channel_id, tournament)
        await interaction.followup.send("Выберите группу команд:", view=view, ephemeral=True)


class BRTeamGroupSelectView(View):
    """View for selecting team groups for stats entry."""

    def __init__(self, guild_id: int, channel_id: int, tournament):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

        # Calculate number of teams
        num_teams = len(tournament.teams)

        # Add group buttons based on team count
        if num_teams > 0:
            self.add_item(BRTeamGroupButton(guild_id, channel_id, 0, min(4, num_teams), tournament))  # Teams 1-4
        if num_teams > 4:
            self.add_item(BRTeamGroupButton(guild_id, channel_id, 4, min(8, num_teams), tournament))  # Teams 5-8
        if num_teams > 8:
            self.add_item(BRTeamGroupButton(guild_id, channel_id, 8, min(12, num_teams), tournament))  # Teams 9-12


class BRTeamGroupButton(Button):
    """Button for a team group."""

    def __init__(self, guild_id: int, channel_id: int, start_index: int, end_index: int, tournament):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=f"Команды {start_index + 1}-{end_index}",
            custom_id=f"br_team_group:{guild_id}:{channel_id}:{start_index}:{end_index}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.start_index = start_index
        self.end_index = end_index
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check if all teams in this group have stats
        all_have_stats = True
        for i in range(self.start_index, self.end_index):
            if i < len(self.tournament.teams):
                team = self.tournament.teams[i]
                if self.tournament.current_map not in team.get("stats", {}):
                    all_have_stats = False
                    break

        if all_have_stats:
            # Show stats review view
            view = BRStatsReviewView(self.guild_id, self.channel_id, self.tournament, self.start_index, self.end_index)
            await interaction.response.edit_message(view=view)
        else:
            # Show stats input modal for each team
            view = BRStatsInputView(self.guild_id, self.channel_id, self.tournament, self.start_index, self.end_index)
            await interaction.response.edit_message(view=view)


class BRStatsInputView(View):
    """View for inputting stats for a team group."""

    def __init__(self, guild_id: int, channel_id: int, tournament, start_index: int, end_index: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.start_index = start_index
        self.end_index = end_index

        # Add buttons for each team in the group
        for i in range(start_index, end_index):
            if i < len(tournament.teams):
                team = tournament.teams[i]
                captain = team.get("captain", f"Команда {i+1}")
                has_stats = tournament.current_map in team.get("stats", {})
                label = f"{captain} {'✅' if has_stats else '❌'}"
                self.add_item(BRTeamStatsButton(guild_id, channel_id, tournament, i, label))

        # Add back button
        self.add_item(BRBackToGroupSelectButton(guild_id, channel_id, tournament))


class BRTeamStatsButton(Button):
    """Button to input stats for a specific team."""

    def __init__(self, guild_id: int, channel_id: int, tournament, team_index: int, label: str):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            custom_id=f"br_team_stats:{guild_id}:{channel_id}:{team_index}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.team_index = team_index

    async def callback(self, interaction: discord.Interaction) -> None:
        team = self.tournament.teams[self.team_index]
        captain = team.get("captain", f"Команда {self.team_index + 1}")

        # Show modal for stats input
        modal = BRSingleTeamStatsModal(self.guild_id, self.channel_id, self.tournament, self.team_index, captain)
        await interaction.response.send_modal(modal)


class BRSingleTeamStatsModal(Modal):
    """Modal for inputting stats for a single team."""

    place = TextInput(
        label="Место (1-12)",
        placeholder="1",
        required=True,
        max_length=2
    )

    kills = TextInput(
        label="Убийства (0-50)",
        placeholder="0",
        required=True,
        max_length=2
    )

    def __init__(self, guild_id: int, channel_id: int, tournament, team_index: int, team_name: str):
        super().__init__(title=f"Статистика: {team_name}")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.team_index = team_index

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            place = int(self.place.value)
            kills = int(self.kills.value)

            if place < 1 or place > 12:
                await interaction.response.send_message(
                    "❌ Место должно быть от 1 до 12.",
                    ephemeral=True
                )
                return

            if kills < 0 or kills > 50:
                await interaction.response.send_message(
                    "❌ Убийства должны быть от 0 до 50.",
                    ephemeral=True
                )
                return

            # Save stats
            self.tournament.set_team_stats(self.team_index, self.tournament.current_map, place, kills)
            br_store.set(self.tournament)

            await interaction.response.send_message(
                "✅ Статистика сохранена. Нажмите 'Назад' для проверки.",
                ephemeral=True
            )
        except ValueError:
            await interaction.response.send_message(
                "❌ Введите корректные числа.",
                ephemeral=True
            )


class BRBackToGroupSelectButton(Button):
    """Button to go back to group selection."""

    def __init__(self, guild_id: int, channel_id: int, tournament):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Назад",
            custom_id=f"br_back_group:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        view = BRTeamGroupSelectView(self.guild_id, self.channel_id, self.tournament)
        await interaction.response.edit_message(view=view)


class BRStatsReviewView(View):
    """View for reviewing stats of a team group."""

    def __init__(self, guild_id: int, channel_id: int, tournament, start_index: int, end_index: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.start_index = start_index
        self.end_index = end_index

        # Add edit buttons for each team
        for i in range(start_index, end_index):
            if i < len(tournament.teams):
                team = tournament.teams[i]
                captain = team.get("captain", f"Команда {i+1}")
                self.add_item(BREditTeamStatsButton(guild_id, channel_id, tournament, i, captain))

        # Add back button
        self.add_item(BRBackToGroupSelectButton(guild_id, channel_id, tournament))

        # Add confirm button if all stats are complete
        if tournament.are_all_stats_complete(tournament.current_map):
            map_sequence = tournament.get_map_sequence()
            current_phase_index = -1
            for i, map_name in enumerate(map_sequence):
                if map_name == tournament.phase.value:
                    current_phase_index = i
                    break

            # If this is the last map (or only map), show results button
            if current_phase_index == len(map_sequence) - 1:
                self.add_item(BRConfirmResultsButton(guild_id, channel_id, tournament))
            elif current_phase_index >= 0 and current_phase_index < len(map_sequence) - 1:
                # Show next map button
                next_map = map_sequence[current_phase_index + 1]
                if next_map == "purgatory":
                    self.add_item(BRConfirmPurgatoryButton(guild_id, channel_id, tournament))
                elif next_map == "kalahari":
                    self.add_item(BRConfirmKalahariButton(guild_id, channel_id, tournament))
                elif next_map == "bermuda":
                    # Custom button for repeating bermuda
                    self.add_item(BRConfirmNextMapButton(guild_id, channel_id, tournament, next_map))


class BREditTeamStatsButton(Button):
    """Button to edit stats for a team."""

    def __init__(self, guild_id: int, channel_id: int, tournament, team_index: int, team_name: str):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=f"Изменить: {team_name}",
            custom_id=f"br_edit_stats:{guild_id}:{channel_id}:{team_index}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.team_index = team_index

    async def callback(self, interaction: discord.Interaction) -> None:
        team = self.tournament.teams[self.team_index]
        captain = team.get("captain", f"Команда {self.team_index + 1}")

        # Show modal for stats input
        modal = BRSingleTeamStatsModal(self.guild_id, self.channel_id, self.tournament, self.team_index, captain)
        await interaction.response.send_modal(modal)


class BRConfirmPurgatoryButton(Button):
    """Button to confirm and transition to Purgatory."""

    def __init__(self, guild_id: int, channel_id: int, tournament):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Подтвердить (Чистилище)",
            custom_id=f"br_confirm_purgatory:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        # Reload tournament from store to get latest stats BEFORE changing phase
        self.tournament = br_store.get(self.guild_id, self.channel_id)

        # Transition to next map based on map_sequence
        map_sequence = self.tournament.get_map_sequence()
        current_phase_index = -1
        for i, map_name in enumerate(map_sequence):
            if map_name == self.tournament.phase.value:
                current_phase_index = i
                break

        # If this is the last map, go to RESULTS instead of next map
        if current_phase_index == len(map_sequence) - 1:
            self.tournament.phase = BRTournamentPhase.RESULTS
            br_store.set(self.tournament)

            # Close the ephemeral menu
            await interaction.response.edit_message(content="✅ Показать итоги", view=None)

            # Update main message with results embed
            channel = interaction.guild.get_channel(self.channel_id)
            if not channel or not self.tournament.message_id:
                return

            try:
                message = await channel.fetch_message(self.tournament.message_id)

                # Calculate total points for all maps
                teams_with_total_points = []
                num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                              get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

                for i, team in enumerate(self.tournament.teams):
                    captain = team.get("captain", "Unknown")
                    team_name = team.get("name", f"Команда {i+1}")
                    players = team.get("players", [])
                    players_str = ", ".join(players) if players else ""

                    # Calculate total points across all maps
                    stats = team.get("stats", {})
                    total_points = 0
                    for map_name in self.tournament.get_map_sequence():
                        if map_name in stats:
                            map_stats = stats[map_name]
                            place = map_stats.get("place", 0)
                            kills = map_stats.get("kills", 0)
                            if place > 0:
                                place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                                total_points += place_points.get(place, 0) + kills

                    teams_with_total_points.append({
                        "captain": captain,
                        "team_name": team_name,
                        "players_str": players_str,
                        "total_points": total_points,
                        "index": i
                    })

                # Sort by total points descending
                teams_with_total_points.sort(key=lambda x: x["total_points"], reverse=True)

                teams_text = []
                for rank, team_data in enumerate(teams_with_total_points):
                    num_emoji = num_emojis[rank] if rank < len(num_emojis) else "🎮"
                    teams_text.append(
                        f"{num_emoji} {team_data['captain']} ({team_data['total_points']} очков)\n"
                        f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {team_data['players_str']}\n"
                    )

                embed = discord.Embed(
                    title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Итоги",
                    description=(
                        f"{replace_emojis('white_arrow')} **Организатор:** <@{self.tournament.organizer_id}>\n\n"
                        f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                        f"{replace_emojis('sub_middle')} Формат: {self.tournament.formation.value.capitalize()}\n"
                        f"{replace_emojis('sub_directory')} Статус: Завершен\n\n"
                        f"{replace_emojis('white_dot')} **Результаты команд:**\n\n"
                        + "\n".join(teams_text)
                    ),
                    color=discord.Color.gold()
                )

                view = BRBermudaView(self.guild_id, self.channel_id)
                await message.edit(embed=embed, view=view)
            except Exception as e:
                logger.error(f"Error updating BR message for results: {e}")
            return

        # Otherwise, go to next map
        if current_phase_index >= 0 and current_phase_index < len(map_sequence) - 1:
            next_map = map_sequence[current_phase_index + 1]
            self.tournament.phase = BRTournamentPhase(next_map)
            self.tournament.current_map = next_map
            map_display = {
                "bermuda": "Бермуды",
                "purgatory": "Чистилище",
                "kalahari": "Калахари"
            }.get(next_map, next_map)
            br_store.set(self.tournament)

            # Close the ephemeral menu
            await interaction.response.edit_message(content=f"✅ Переход к {map_display}", view=None)

        # Update main message with Purgatory embed
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel or not self.tournament.message_id:
            return

        try:
            message = await channel.fetch_message(self.tournament.message_id)

            map_names = {
                "bermuda": "Бермуды",
                "purgatory": "Чистилище",
                "kalahari": "Калахари"
            }

            # Build teams section with current map points (sorted by points)
            teams_with_points = []
            num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                          get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

            for i, team in enumerate(self.tournament.teams):
                captain = team.get("captain", "Unknown")
                team_name = team.get("name", f"Команда {i+1}")
                players = team.get("players", [])
                players_str = ", ".join(players) if players else ""

                # Calculate cumulative points for all completed maps
                stats = team.get("stats", {})
                cumulative_points = 0
                # For Purgatory, only count Bermuda points (since Purgatory not started yet)
                if "bermuda" in stats:
                    map_stats = stats["bermuda"]
                    place = map_stats.get("place", 0)
                    kills = map_stats.get("kills", 0)
                    if place > 0:
                        # Points based on place: 1=12, 2=9, 3=8, 4=7, 5=6, 6=5, 7=4, 8=3, 9=2, 10=1, 11=0, 12=0
                        place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                        cumulative_points += place_points.get(place, 0) + kills

                teams_with_points.append({
                    "captain": captain,
                    "team_name": team_name,
                    "players_str": players_str,
                    "points": cumulative_points,
                    "index": i
                })

            # Sort by points descending
            teams_with_points.sort(key=lambda x: x["points"], reverse=True)

            teams_text = []
            for team_data in teams_with_points:
                captain = team_data["captain"]
                team_name = team_data["team_name"]
                players_str = team_data["players_str"]
                points = team_data["points"]
                original_index = team_data["index"]
                num_emoji = num_emojis[original_index] if original_index < len(num_emojis) else "🎮"

                teams_text.append(
                    f"{num_emoji} {team_name} ({points} очков)\n"
                    f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
                )

            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | {self.tournament.formation.value.capitalize()} | {map_names.get('purgatory', 'purgatory')}",
                description=(
                    f"{replace_emojis('white_arrow')} **Организатор:** <@{self.tournament.organizer_id}>\n\n"
                    f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                    f"{replace_emojis('sub_middle')} Формат: {self.tournament.formation.value.capitalize()}\n"
                    f"{replace_emojis('sub_directory')} Карта: {map_names.get('purgatory', 'purgatory')}\n\n"
                    f"{replace_emojis('white_dot')} **Участники команд:**\n\n"
                    + "\n".join(teams_text)
                ),
                color=discord.Color.from_rgb(69, 69, 69)
            )

            view = BRBermudaView(self.guild_id, self.channel_id)
            await message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error updating BR message for Purgatory: {e}")


class BRConfirmKalahariButton(Button):
    """Button to confirm and transition to Kalahari."""

    def __init__(self, guild_id: int, channel_id: int, tournament):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Подтвердить (Калахари)",
            custom_id=f"br_confirm_kalahari:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        # Reload tournament from store to get latest stats BEFORE changing phase
        self.tournament = br_store.get(self.guild_id, self.channel_id)

        # Transition to next map based on map_sequence
        map_sequence = self.tournament.get_map_sequence()
        current_phase_index = -1
        for i, map_name in enumerate(map_sequence):
            if map_name == self.tournament.phase.value:
                current_phase_index = i
                break

        # If this is the last map, go to RESULTS instead of next map
        if current_phase_index == len(map_sequence) - 1:
            self.tournament.phase = BRTournamentPhase.RESULTS
            br_store.set(self.tournament)

            # Close the ephemeral menu
            await interaction.response.edit_message(content="✅ Показать итоги", view=None)

            # Update main message with results embed
            channel = interaction.guild.get_channel(self.channel_id)
            if not channel or not self.tournament.message_id:
                return

            try:
                message = await channel.fetch_message(self.tournament.message_id)

                # Calculate total points for all maps
                teams_with_total_points = []
                num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                              get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

                for i, team in enumerate(self.tournament.teams):
                    captain = team.get("captain", "Unknown")
                    team_name = team.get("name", f"Команда {i+1}")
                    players = team.get("players", [])
                    players_str = ", ".join(players) if players else ""

                    # Calculate total points across all maps
                    stats = team.get("stats", {})
                    total_points = 0
                    for map_name in self.tournament.get_map_sequence():
                        if map_name in stats:
                            map_stats = stats[map_name]
                            place = map_stats.get("place", 0)
                            kills = map_stats.get("kills", 0)
                            if place > 0:
                                place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                                total_points += place_points.get(place, 0) + kills

                    teams_with_total_points.append({
                        "captain": captain,
                        "team_name": team_name,
                        "players_str": players_str,
                        "total_points": total_points,
                        "index": i
                    })

                # Sort by total points descending
                teams_with_total_points.sort(key=lambda x: x["total_points"], reverse=True)

                teams_text = []
                for rank, team_data in enumerate(teams_with_total_points):
                    num_emoji = num_emojis[rank] if rank < len(num_emojis) else "🎮"
                    teams_text.append(
                        f"{num_emoji} {team_data['captain']} ({team_data['total_points']} очков)\n"
                        f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {team_data['players_str']}\n"
                    )

                embed = discord.Embed(
                    title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Итоги",
                    description=(
                        f"{replace_emojis('white_arrow')} **Организатор:** <@{self.tournament.organizer_id}>\n\n"
                        f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                        f"{replace_emojis('sub_middle')} Формат: {self.tournament.formation.value.capitalize()}\n"
                        f"{replace_emojis('sub_directory')} Статус: Завершен\n\n"
                        f"{replace_emojis('white_dot')} **Результаты команд:**\n\n"
                        + "\n".join(teams_text)
                    ),
                    color=discord.Color.gold()
                )

                view = BRBermudaView(self.guild_id, self.channel_id)
                await message.edit(embed=embed, view=view)
            except Exception as e:
                logger.error(f"Error updating BR message for results: {e}")
            return

        # Otherwise, go to next map
        if current_phase_index >= 0 and current_phase_index < len(map_sequence) - 1:
            next_map = map_sequence[current_phase_index + 1]
            self.tournament.phase = BRTournamentPhase(next_map)
            self.tournament.current_map = next_map
            map_display = {
                "bermuda": "Бермуды",
                "purgatory": "Чистилище",
                "kalahari": "Калахари"
            }.get(next_map, next_map)
            br_store.set(self.tournament)

            # Close the ephemeral menu
            await interaction.response.edit_message(content=f"✅ Переход к {map_display}", view=None)

        # Update main message with Kalahari embed
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel or not self.tournament.message_id:
            return

        try:
            message = await channel.fetch_message(self.tournament.message_id)

            map_names = {
                "bermuda": "Бермуды",
                "purgatory": "Чистилище",
                "kalahari": "Калахари"
            }

            # Build teams section with current map points (sorted by points)
            teams_with_points = []
            num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                          get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

            for i, team in enumerate(self.tournament.teams):
                captain = team.get("captain", "Unknown")
                team_name = team.get("name", f"Команда {i+1}")
                players = team.get("players", [])
                players_str = ", ".join(players) if players else ""

                # Calculate cumulative points for all completed maps
                stats = team.get("stats", {})
                cumulative_points = 0
                # For Kalahari, count both Bermuda and Purgatory points
                for map_name in ["bermuda", "purgatory"]:
                    if map_name in stats:
                        map_stats = stats[map_name]
                        place = map_stats.get("place", 0)
                        kills = map_stats.get("kills", 0)
                        if place > 0:
                            # Points based on place: 1=12, 2=9, 3=8, 4=7, 5=6, 6=5, 7=4, 8=3, 9=2, 10=1, 11=0, 12=0
                            place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                            cumulative_points += place_points.get(place, 0) + kills

                teams_with_points.append({
                    "captain": captain,
                    "team_name": team_name,
                    "players_str": players_str,
                    "points": cumulative_points,
                    "index": i
                })

            # Sort by points descending
            teams_with_points.sort(key=lambda x: x["points"], reverse=True)

            teams_text = []
            for team_data in teams_with_points:
                captain = team_data["captain"]
                team_name = team_data["team_name"]
                players_str = team_data["players_str"]
                points = team_data["points"]
                original_index = team_data["index"]
                num_emoji = num_emojis[original_index] if original_index < len(num_emojis) else "🎮"

                teams_text.append(
                    f"{num_emoji} {team_name} ({points} очков)\n"
                    f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
                )

            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | {self.tournament.formation.value.capitalize()} | {map_names.get('kalahari', 'kalahari')}",
                description=(
                    f"{replace_emojis('white_arrow')} **Организатор:** <@{self.tournament.organizer_id}>\n\n"
                    f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                    f"{replace_emojis('sub_middle')} Формат: {self.tournament.formation.value.capitalize()}\n"
                    f"{replace_emojis('sub_directory')} Карта: {map_names.get('kalahari', 'kalahari')}\n\n"
                    f"{replace_emojis('white_dot')} **Участники команд:**\n\n"
                    + "\n".join(teams_text)
                ),
                color=discord.Color.from_rgb(69, 69, 69)
            )

            view = BRBermudaView(self.guild_id, self.channel_id)
            await message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error updating BR message for Kalahari: {e}")


class BRConfirmNextMapButton(Button):
    """Button to confirm and transition to next map (for dynamic map sequences)."""

    def __init__(self, guild_id: int, channel_id: int, tournament, next_map: str):
        super().__init__(
            style=discord.ButtonStyle.success,
            label=f"Подтвердить ({next_map.capitalize()})",
            custom_id=f"br_confirm_next:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.next_map = next_map

    async def callback(self, interaction: discord.Interaction) -> None:
        # Reload tournament from store to get latest stats BEFORE changing phase
        self.tournament = br_store.get(self.guild_id, self.channel_id)

        # Transition to next map
        self.tournament.phase = BRTournamentPhase(self.next_map)
        self.tournament.current_map = self.next_map
        map_display = {
            "bermuda": "Бермуды",
            "purgatory": "Чистилище",
            "kalahari": "Калахари"
        }.get(self.next_map, self.next_map)
        br_store.set(self.tournament)

        # Close the ephemeral menu
        await interaction.response.edit_message(content=f"✅ Переход к {map_display}", view=None)

        # Update main message with next map embed
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel or not self.tournament.message_id:
            return

        try:
            message = await channel.fetch_message(self.tournament.message_id)

            map_names = {
                "bermuda": "Бермуды",
                "purgatory": "Чистилище",
                "kalahari": "Калахари"
            }

            # Build teams section with current map points (sorted by points)
            teams_with_points = []
            num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                          get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

            for i, team in enumerate(self.tournament.teams):
                captain = team.get("captain", "Unknown")
                team_name = team.get("name", f"Команда {i+1}")
                players = team.get("players", [])
                players_str = ", ".join(players) if players else ""

                # Calculate cumulative points for completed maps
                stats = team.get("stats", {})
                cumulative_points = 0
                for map_name in self.tournament.get_map_sequence():
                    if map_name == self.next_map:
                        break  # Don't count the next map we're transitioning to
                    if map_name in stats:
                        map_stats = stats[map_name]
                        place = map_stats.get("place", 0)
                        kills = map_stats.get("kills", 0)
                        if place > 0:
                            place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                            cumulative_points += place_points.get(place, 0) + kills

                teams_with_points.append((cumulative_points, i, captain, players_str))

            # Sort by points (descending)
            teams_with_points.sort(key=lambda x: x[0], reverse=True)

            # Build display with sorted teams
            teams_text = []
            for rank, (points, original_index, captain, players_str) in enumerate(teams_with_points):
                num_emoji = num_emojis[rank] if rank < len(num_emojis) else "🎮"
                teams_text.append(
                    f"{num_emoji} {captain} ({points} очков)\n"
                    f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
                )

            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | {map_display}",
                description=(
                    f"{replace_emojis('white_arrow')} **Организатор:** <@{self.tournament.organizer_id}>\n\n"
                    f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                    f"{replace_emojis('sub_middle')} Формат: Random\n"
                    f"{replace_emojis('sub_middle')} Фаза: {map_display}\n"
                    f"{replace_emojis('sub_directory')} Карта: {map_display}\n\n"
                    f"{replace_emojis('white_dot')} **Участники команд:**\n\n"
                    + "\n".join(teams_text) + "\n\n"
                    f"{replace_emojis('white_dot')} **Статус:**\n"
                    f"{replace_emojis('white_arrow')} Все команды заполнили статистику. Переход к следующей карте."
                ),
                color=discord.Color.from_rgb(69, 69, 69)
            )

            view = BRBermudaView(self.guild_id, self.channel_id)
            await message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error updating BR message for next map: {e}")


class BRConfirmResultsButton(Button):
    """Button to confirm and show final results."""

    def __init__(self, guild_id: int, channel_id: int, tournament):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Показать итоги",
            custom_id=f"br_confirm_results:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        # Reload tournament from store to get latest stats BEFORE changing phase
        self.tournament = br_store.get(self.guild_id, self.channel_id)

        # Transition to RESULTS phase
        self.tournament.phase = BRTournamentPhase.RESULTS
        br_store.set(self.tournament)

        # Close the ephemeral menu
        await interaction.response.edit_message(content="✅ Показать итоги", view=None)

        # Update main message with final results embed
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel or not self.tournament.message_id:
            return

        try:
            message = await channel.fetch_message(self.tournament.message_id)

            # Calculate total points for all maps
            teams_with_total_points = []
            num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                          get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

            for i, team in enumerate(self.tournament.teams):
                captain = team.get("captain", "Unknown")
                team_name = team.get("name", f"Команда {i+1}")
                players = team.get("players", [])
                players_str = ", ".join(players) if players else ""

                # Calculate total points across all maps
                stats = team.get("stats", {})
                total_points = 0
                for map_name in self.tournament.get_map_sequence():
                    if map_name in stats:
                        map_stats = stats[map_name]
                        place = map_stats.get("place", 0)
                        kills = map_stats.get("kills", 0)
                        if place > 0:
                            # Points based on place: 1=12, 2=9, 3=8, 4=7, 5=6, 6=5, 7=4, 8=3, 9=2, 10=1, 11=0, 12=0
                            place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                            total_points += place_points.get(place, 0) + kills

                teams_with_total_points.append({
                    "captain": captain,
                    "team_name": team_name,
                    "players_str": players_str,
                    "total_points": total_points,
                    "index": i
                })

            # Sort by total points descending
            teams_with_total_points.sort(key=lambda x: x["total_points"], reverse=True)

            teams_text = []
            for i, team_data in enumerate(teams_with_total_points):
                captain = team_data["captain"]
                team_name = team_data["team_name"]
                players_str = team_data["players_str"]
                total_points = team_data["total_points"]
                original_index = team_data["index"]
                num_emoji = num_emojis[i] if i < len(num_emojis) else "🎮"

                # Add medals for top 3
                if i == 0:
                    teams_text.append(
                        f"{get_emoji('winner')} **{team_name}** ({total_points} очков) - ПОБЕДИТЕЛЬ!\n"
                        f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
                    )
                elif i == 1:
                    teams_text.append(
                        f"{get_emoji('silver_medal')} **{team_name}** ({total_points} очков)\n"
                        f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
                    )
                elif i == 2:
                    teams_text.append(
                        f"{get_emoji('bronze_medal')} **{team_name}** ({total_points} очков)\n"
                        f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
                    )
                else:
                    teams_text.append(
                        f"{num_emoji} {team_name} ({total_points} очков)\n"
                        f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
                    )

            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | ИТОГИ",
                description=(
                    f"{replace_emojis('white_arrow')} **Организатор:** <@{self.tournament.organizer_id}>\n\n"
                    f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                    f"{replace_emojis('sub_middle')} Формат: {self.tournament.formation.value.capitalize()}\n"
                    f"{replace_emojis('sub_directory')} Финальные результаты\n\n"
                    f"{replace_emojis('white_dot')} **Команды по очкам:**\n\n"
                    + "\n".join(teams_text)
                ),
                color=discord.Color.gold()
            )

            view = None
            await message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error updating BR message for results: {e}")


class BRPurgatoryButton(Button):
    """Button to transition to Purgatory phase."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Чистилище",
            custom_id=f"br_purgatory:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Только владелец бота может перейти к Чистилище.",
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

        # Transition to next map based on map_sequence
        map_sequence = tournament.get_map_sequence()
        current_phase_index = -1
        for i, map_name in enumerate(map_sequence):
            if map_name == tournament.phase.value:
                current_phase_index = i
                break

        if current_phase_index >= 0 and current_phase_index < len(map_sequence) - 1:
            next_map = map_sequence[current_phase_index + 1]
            tournament.phase = BRTournamentPhase(next_map)
            tournament.current_map = next_map
            br_store.set(tournament)

        # Reload to get latest
        tournament = br_store.get(self.guild_id, self.channel_id)

        # Create view with only organizer menu button
        view = BRBermudaView(self.guild_id, self.channel_id)

        map_names = {
            "bermuda": "Бермуды",
            "purgatory": "Чистилище",
            "kalahari": "Калахари"
        }

        # Build teams section with current map points (sorted by points)
        teams_with_points = []
        num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                      get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

        for i, team in enumerate(tournament.teams):
            captain = team.get("captain", "Unknown")
            team_name = team.get("name", f"Команда {i+1}")
            players = team.get("players", [])
            players_str = ", ".join(players) if players else ""

            # Calculate cumulative points (Bermuda only since moving to Purgatory)
            stats = team.get("stats", {})
            cumulative_points = 0
            if "bermuda" in stats:
                map_stats = stats["bermuda"]
                place = map_stats.get("place", 0)
                kills = map_stats.get("kills", 0)
                if place > 0:
                    # Points based on place: 1=12, 2=9, 3=8, 4=7, 5=6, 6=5, 7=4, 8=3, 9=2, 10=1, 11=0, 12=0
                    place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                    cumulative_points += place_points.get(place, 0) + kills

            teams_with_points.append({
                "captain": captain,
                "team_name": team_name,
                "players_str": players_str,
                "points": cumulative_points,
                "index": i
            })

        # Sort by points descending
        teams_with_points.sort(key=lambda x: x["points"], reverse=True)

        teams_text = []
        for team_data in teams_with_points:
            captain = team_data["captain"]
            team_name = team_data["team_name"]
            players_str = team_data["players_str"]
            points = team_data["points"]
            original_index = team_data["index"]
            num_emoji = num_emojis[original_index] if original_index < len(num_emojis) else "🎮"

            teams_text.append(
                f"{num_emoji} {team_name} ({points} очков)\n"
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
            )

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | {map_names.get('purgatory', 'purgatory')}",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: {tournament.formation.value.capitalize()}\n"
                f"{replace_emojis('sub_directory')} Карта: {map_names.get('purgatory', 'purgatory')}\n\n"
                f"{replace_emojis('white_dot')} **Участники команд:**\n\n"
                + "\n".join(teams_text)
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        # Get the main BR message and edit it
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel or not tournament.message_id:
            await interaction.response.send_message(
                "❌ Не удалось обновить сообщение турнира.",
                ephemeral=True
            )
            return

        try:
            message = await channel.fetch_message(tournament.message_id)
            await message.edit(embed=embed, view=view)
            await interaction.response.send_message(
                "✅ Переход к Чистилище.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error updating BR message for Purgatory: {e}")
            await interaction.response.send_message(
                "❌ Не удалось обновить сообщение турнира.",
                ephemeral=True
            )


class BRKalahariButton(Button):
    """Button to transition to Kalahari phase."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Калахари",
            custom_id=f"br_kalahari:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Только владелец бота может перейти к Калахари.",
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

        # Transition to next map based on map_sequence
        map_sequence = tournament.get_map_sequence()
        current_phase_index = -1
        for i, map_name in enumerate(map_sequence):
            if map_name == tournament.phase.value:
                current_phase_index = i
                break

        if current_phase_index >= 0 and current_phase_index < len(map_sequence) - 1:
            next_map = map_sequence[current_phase_index + 1]
            tournament.phase = BRTournamentPhase(next_map)
            tournament.current_map = next_map
            br_store.set(tournament)

        # Reload to get latest
        tournament = br_store.get(self.guild_id, self.channel_id)

        # Create view with only organizer menu button
        view = BRBermudaView(self.guild_id, self.channel_id)

        map_names = {
            "bermuda": "Бермуды",
            "purgatory": "Чистилище",
            "kalahari": "Калахари"
        }

        # Build teams section with current map points (sorted by points)
        teams_with_points = []
        num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                      get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

        for i, team in enumerate(tournament.teams):
            captain = team.get("captain", "Unknown")
            team_name = team.get("name", f"Команда {i+1}")
            players = team.get("players", [])
            players_str = ", ".join(players) if players else ""

            # Calculate cumulative points (Bermuda + Purgatory since moving to Kalahari)
            stats = team.get("stats", {})
            cumulative_points = 0
            for map_name in ["bermuda", "purgatory"]:
                if map_name in stats:
                    map_stats = stats[map_name]
                    place = map_stats.get("place", 0)
                    kills = map_stats.get("kills", 0)
                    if place > 0:
                        # Points based on place: 1=12, 2=9, 3=8, 4=7, 5=6, 6=5, 7=4, 8=3, 9=2, 10=1, 11=0, 12=0
                        place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                        cumulative_points += place_points.get(place, 0) + kills

            teams_with_points.append({
                "captain": captain,
                "team_name": team_name,
                "players_str": players_str,
                "points": cumulative_points,
                "index": i
            })

        # Sort by points descending
        teams_with_points.sort(key=lambda x: x["points"], reverse=True)

        teams_text = []
        for team_data in teams_with_points:
            captain = team_data["captain"]
            team_name = team_data["team_name"]
            players_str = team_data["players_str"]
            points = team_data["points"]
            original_index = team_data["index"]
            num_emoji = num_emojis[original_index] if original_index < len(num_emojis) else "🎮"

            teams_text.append(
                f"{num_emoji} {team_name} ({points} очков)\n"
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
            )

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | {tournament.formation.value.capitalize()} | {map_names.get('kalahari', 'kalahari')}",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: {tournament.formation.value.capitalize()}\n"
                f"{replace_emojis('sub_directory')} Карта: {map_names.get('kalahari', 'kalahari')}\n\n"
                f"{replace_emojis('white_dot')} **Участники команд:**\n\n"
                + "\n".join(teams_text)
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        # Get the main BR message and edit it
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel or not tournament.message_id:
            await interaction.response.send_message(
                "❌ Не удалось обновить сообщение турнира.",
                ephemeral=True
            )
            return

        try:
            message = await channel.fetch_message(tournament.message_id)
            await message.edit(embed=embed, view=view)
            await interaction.response.send_message(
                "✅ Переход к Калахари.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error updating BR message for Kalahari: {e}")
            await interaction.response.send_message(
                "❌ Не удалось обновить сообщение турнира.",
                ephemeral=True
            )


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
        if not tournament:
            await interaction.response.send_message(
                "❌ BR турнир не найден.",
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
        num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                      get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

        for i, team in enumerate(tournament.teams):
            captain = team.get("captain", "Unknown")
            team_name = team.get("name", f"Команда {i+1}")
            players = team.get("players", [])
            players_str = ", ".join(players) if players else ""
            num_emoji = num_emojis[i] if i < len(num_emojis) else "🎮"

            teams_text.append(
                f"{num_emoji} {team_name}\n"
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
            )

        # Обновить view (убрать кнопку если больше нельзя переименовывать)
        view = BRTeamsView(self.guild_id, self.channel_id, tournament)

        # Обновить embed в стиле бота
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | {tournament.formation.value.capitalize()} | Команды",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: {tournament.formation.value.capitalize()}\n"
                f"{replace_emojis('sub_middle')} Фаза: Бермуды\n"
                f"{replace_emojis('sub_directory')} Команд: {len(tournament.teams)}\n\n"
                f"{replace_emojis('white_dot')} **Участники команд:**\n\n"
                + "\n".join(teams_text)
            ),
            color=discord.Color.from_rgb(69, 69, 69)
        )

        await interaction.response.edit_message(embed=embed, view=view)


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
        if tournament.phase == BRTournamentPhase.BERMUDA:
            options.append(discord.SelectOption(label="Чистилище", value="purgatory"))
        elif tournament.phase == BRTournamentPhase.PURGATORY:
            options.append(discord.SelectOption(label="Калахари", value="kalahari"))
        elif tournament.phase == BRTournamentPhase.REGISTRATION:
            options.append(discord.SelectOption(label="Бермуды", value="bermuda"))

        options.append(discord.SelectOption(label="Данные", value="data"))

        super().__init__(
            placeholder="Выберите действие",
            options=options,
            custom_id=f"br_map_select:{guild_id}:{channel_id}"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]

        if value == "data":
            # Отправить данные комнаты (пинг роли)
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

            # Generate data message with current map stats
            map_names = {
                "bermuda": "Бермуды",
                "purgatory": "Чистилище",
                "kalahari": "Калахари"
            }
            current_map_name = map_names.get(tournament.current_map, tournament.current_map)

            data_text = f"📊 **Данные BR турнира - {current_map_name}**\n\n"
            for i, team in enumerate(tournament.teams):
                team_name = team.get("name", f"Команда {i+1}")
                stats = team.get("stats", {})
                if tournament.current_map in stats:
                    map_stats = stats[tournament.current_map]
                    data_text += f"**{i+1}. {team_name}** - Место: {map_stats.get('place', '-')}, Убийства: {map_stats.get('kills', '-')}\n"
                else:
                    data_text += f"**{i+1}. {team_name}** - Нет данных\n"

            if role:
                await channel.send(f"{role.mention}\n{data_text}")
            else:
                await channel.send(data_text)

            await interaction.response.send_message(
                "✅ Данные отправлены.",
                ephemeral=True
            )
        else:
            # Показать кнопки групп команд для ввода статистики
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

            # Build teams section with current map points (sorted by points)
            teams_with_points = []
            num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                      get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

            for i, team in enumerate(tournament.teams):
                captain = team.get("captain", "Unknown")
                team_name = team.get("name", f"Команда {i+1}")
                players = team.get("players", [])
                all_players = [captain] + players
                players_str = ", ".join(all_players)

                # Calculate points for current map only
                stats = team.get("stats", {})
                map_points = 0
                if value in stats:
                    map_stats = stats[value]
                    place = map_stats.get("place", 0)
                    kills = map_stats.get("kills", 0)
                    # Points calculation based on place: 1=12, 2=9, 3=8, 4=7, 5=6, 6=5, 7=4, 8=3, 9=2, 10=1, 11=0, 12=0
                    if place > 0:
                        place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                        map_points = place_points.get(place, 0) + kills

                teams_with_points.append((map_points, i, captain, players_str))

            # Sort by points (descending)
            teams_with_points.sort(key=lambda x: x[0], reverse=True)

            # Build display with sorted teams
            teams_text = []
            for rank, (points, original_index, captain, players_str) in enumerate(teams_with_points):
                num_emoji = num_emojis[rank] if rank < len(num_emojis) else "🎮"
                teams_text.append(
                    f"{num_emoji} {captain} ({points} очков)\n"
                    f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
                )

            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | {map_names.get(value, value)}",
                description=(
                    f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                    f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                    f"{replace_emojis('sub_middle')} Формат: Random\n"
                    f"{replace_emojis('sub_middle')} Фаза: {map_names.get(value, value)}\n"
                    f"{replace_emojis('sub_directory')} Карта: {map_names.get(value, value)}\n\n"
                    f"{replace_emojis('white_dot')} **Участники команд:**\n\n"
                    + "\n".join(teams_text) + "\n\n"
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

        # Вернуться к select menu с обновленным embed
        view = BRMapSelectView(self.guild_id, self.channel_id, tournament)

        map_names = {
            "bermuda": "Бермуды",
            "purgatory": "Чистилище",
            "kalahari": "Калахари"
        }

        current_map_name = map_names.get(tournament.current_map, tournament.current_map)

        # Build teams section with current points
        teams_text = []
        team_emojis = {1: "num_1", 2: "num_2", 3: "num_3", 4: "num_4", 5: "num_5", 6: "num_6",
                      7: "num_7", 8: "num_8", 9: "num_9", 10: "num_ten", 11: "num_eleven", 12: "num_twelve"}

        for i, team in enumerate(tournament.teams):
            captain = team.get("captain", "Unknown")
            team_name = team.get("name", f"Команда {i+1}")
            players = team.get("players", [])
            players_str = ", ".join(players) if players else ""
            num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                      get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]
            emoji = num_emojis[i] if i < len(num_emojis) else "🎮"

            # Calculate total points so far
            total_points = 0
            stats = team.get("stats", {})
            for map_name in tournament.get_map_sequence():
                if map_name in stats:
                    map_stats = stats[map_name]
                    place = map_stats.get("place", 0)
                    kills = map_stats.get("kills", 0)
                    # Points calculation based on place: 1=12, 2=9, 3=8, 4=7, 5=6, 6=5, 7=4, 8=3, 9=2, 10=1, 11=0, 12=0
                    if place > 0:
                        place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                        total_points += place_points.get(place, 0) + kills

            teams_text.append(
                f"{emoji} **{team_name}** ({total_points} очков)\n"
                f"┣ **Капитан:** {captain}\n"
                f"┗ **Состав:** {players_str}\n"
            )

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | {current_map_name}",
            description=(
                f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                f"{replace_emojis('sub_middle')} Формат: {tournament.formation.value.capitalize()}\n"
                f"{replace_emojis('sub_middle')} Фаза: {current_map_name}\n"
                f"{replace_emojis('sub_directory')} Карта: {current_map_name}\n\n"
                f"{replace_emojis('white_dot')} **Команды:**\n"
                + "\n".join(teams_text) + "\n\n"
                f"{replace_emojis('white_dot')} **Правила:**\n"
                f"{replace_emojis('white_arrow')} Выберите действие для ввода статистики или отправки данных.\n\n"
                f"{replace_emojis('a_dot_smaller')} Данные пингуют роль BR игроков."
            ),
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

        # Build teams section with current map points (sorted by points)
        teams_with_points = []
        num_emojis = [replace_emojis("num_1"), replace_emojis("num_2"), replace_emojis("num_3"), replace_emojis("num_4"), replace_emojis("num_5"), replace_emojis("num_6"),
                      replace_emojis("num_7"), replace_emojis("num_8"), replace_emojis("num_9"), replace_emojis("num_ten"), replace_emojis("num_eleven"), replace_emojis("num_twelve")]

        for i, team in enumerate(tournament.teams):
            captain = team.get("captain", "Unknown")
            team_name = team.get("name", f"Команда {i+1}")
            players = team.get("players", [])
            players_str = ", ".join(players) if players else ""

            # Calculate points for current map only
            stats = team.get("stats", {})
            map_points = 0
            if self.map_name in stats:
                map_stats = stats[self.map_name]
                place = map_stats.get("place", 0)
                kills = map_stats.get("kills", 0)
                # Points calculation based on place: 1=12, 2=9, 3=8, 4=7, 5=6, 6=5, 7=4, 8=3, 9=2, 10=1, 11=0, 12=0
                if place > 0:
                    place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                    map_points = place_points.get(place, 0) + kills

            teams_with_points.append((map_points, i, captain, players_str))

        # Sort by points (descending)
        teams_with_points.sort(key=lambda x: x[0], reverse=True)

        # Build display with sorted teams
        teams_text = []
        for rank, (points, original_index, captain, players_str) in enumerate(teams_with_points):
            num_emoji = num_emojis[rank] if rank < len(num_emojis) else "🎮"
            teams_text.append(
                f"{num_emoji} {captain} ({points} очков)\n"
                f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
            )

        # Проверить все ли команды заполнили статистику для текущей карты
        if tournament.are_all_stats_complete(self.map_name):
            # Переход к следующей фазе на основе map_count
            map_sequence = tournament.get_map_sequence()
            current_phase_index = -1
            for i, map_name in enumerate(map_sequence):
                if map_name == tournament.phase.value:
                    current_phase_index = i
                    break

            # If this is the last map (or only map), go to RESULTS
            if current_phase_index == len(map_sequence) - 1:
                tournament.phase = BRTournamentPhase.RESULTS
            elif current_phase_index >= 0 and current_phase_index < len(map_sequence) - 1:
                # Go to next map
                next_map = map_sequence[current_phase_index + 1]
                tournament.phase = BRTournamentPhase(next_map)
                tournament.current_map = next_map

            br_store.set(tournament)

            if tournament.phase == BRTournamentPhase.RESULTS:
                # Снять роль с реальных игроков
                guild = interaction.guild
                role = guild.get_role(tournament.role_id)
                if role:
                    if tournament.formation.value == "team":
                        # Remove role only from captains
                        from storage.team_store import team_store
                        for team_id in tournament.registered_teams:
                            team = team_store.get(team_id)
                            if team:
                                user_id = team.captain_id
                                if user_id < 0:
                                    continue
                                try:
                                    member = guild.get_member(user_id)
                                    if member:
                                        await member.remove_roles(role)
                                except Exception as e:
                                    logger.warning("Не удалось снять роль капитана: %s", e)
                    else:
                        # Remove role from all players (random format)
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
                        f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
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

                # Build teams section with next map points (sorted)
                teams_with_points = []
                num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                      get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

                for i, team in enumerate(tournament.teams):
                    captain = team.get("captain", "Unknown")
                    players = team.get("players", [])
                    all_players = [captain] + players
                    players_str = ", ".join(all_players)

                    # Calculate points for next map only
                    stats = team.get("stats", {})
                    map_points = 0
                    next_map_key = "purgatory" if tournament.phase == BRTournamentPhase.PURGATORY else "kalahari"
                    if next_map_key in stats:
                        map_stats = stats[next_map_key]
                        place = map_stats.get("place", 0)
                        kills = map_stats.get("kills", 0)
                        if place > 0:
                            # Points based on place: 1=12, 2=9, 3=8, 4=7, 5=6, 6=5, 7=4, 8=3, 9=2, 10=1, 11=0, 12=0
                            place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                            map_points = place_points.get(place, 0) + kills

                    teams_with_points.append((map_points, i, captain, players_str))

                # Sort by points (descending)
                teams_with_points.sort(key=lambda x: x[0], reverse=True)

                # Build display with sorted teams
                teams_text = []
                for rank, (points, original_index, captain, players_str) in enumerate(teams_with_points):
                    num_emoji = num_emojis[rank] if rank < len(num_emojis) else "🎮"
                    teams_text.append(
                        f"{num_emoji} {captain} ({points} очков)\n"
                        f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
                    )

                embed = discord.Embed(
                    title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | {next_map}",
                    description=(
                        f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                        f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                        f"{replace_emojis('sub_middle')} Формат: Random\n"
                        f"{replace_emojis('sub_middle')} Фаза: {next_map}\n"
                        f"{replace_emojis('sub_directory')} Карта: {next_map}\n\n"
                        f"{replace_emojis('white_dot')} **Участники команд:**\n\n"
                        + "\n".join(teams_text) + "\n\n"
                        f"{replace_emojis('white_dot')} **Статус:**\n"
                        f"{replace_emojis('white_arrow')} Все команды заполнили статистику. Переход к следующей карте."
                    ),
                    color=discord.Color.from_rgb(69, 69, 69)
                )

                await interaction.response.edit_message(embed=embed, view=view)
        else:
            # Not all teams done yet - return to team group view with updated embed
            view = BRTeamGroupView(self.guild_id, self.channel_id, tournament)

            map_names = {
                "bermuda": "Бермуды",
                "purgatory": "Чистилище",
                "kalahari": "Калахари"
            }

            current_map_name = map_names.get(tournament.current_map, tournament.current_map)

            # Build teams section with current map points (sorted)
            teams_with_points = []
            num_emojis = [get_emoji("num_1"), get_emoji("num_2"), get_emoji("num_3"), get_emoji("num_4"), get_emoji("num_5"), get_emoji("num_6"),
                      get_emoji("num_7"), get_emoji("num_8"), get_emoji("num_9"), get_emoji("num_ten"), get_emoji("num_eleven"), get_emoji("num_twelve")]

            for i, team in enumerate(tournament.teams):
                captain = team.get("captain", "Unknown")
                players = team.get("players", [])
                all_players = [captain] + players
                players_str = ", ".join(all_players)

                # Calculate points for current map only
                stats = team.get("stats", {})
                map_points = 0
                if tournament.current_map in stats:
                    map_stats = stats[tournament.current_map]
                    place = map_stats.get("place", 0)
                    kills = map_stats.get("kills", 0)
                    if place > 0:
                        # Points based on place: 1=12, 2=9, 3=8, 4=7, 5=6, 6=5, 7=4, 8=3, 9=2, 10=1, 11=0, 12=0
                        place_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0}
                        map_points = place_points.get(place, 0) + kills

                teams_with_points.append((map_points, i, captain, players_str))

            # Sort by points (descending)
            teams_with_points.sort(key=lambda x: x[0], reverse=True)

            # Build display with sorted teams
            teams_text = []
            for rank, (points, original_index, captain, players_str) in enumerate(teams_with_points):
                num_emoji = num_emojis[rank] if rank < len(num_emojis) else "🎮"
                teams_text.append(
                    f"{num_emoji} {captain} ({points} очков)\n"
                    f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {players_str}\n"
                )

            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} {replace_emojis('winner')} BATTLE ROYALE CUP | Random | {current_map_name}",
                description=(
                    f"{replace_emojis('white_arrow')} **Организатор:** <@{tournament.organizer_id}>\n\n"
                    f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
                    f"{replace_emojis('sub_middle')} Формат: Random\n"
                    f"{replace_emojis('sub_middle')} Фаза: {current_map_name}\n"
                    f"{replace_emojis('sub_directory')} Карта: {current_map_name}\n\n"
                    f"{replace_emojis('white_dot')} **Участники команд:**\n\n"
                    + "\n".join(teams_text) + "\n\n"
                    f"{replace_emojis('white_dot')} **Правила:**\n"
                    f"{replace_emojis('white_arrow')} Выберите группу команд для ввода статистики.\n\n"
                    f"{replace_emojis('a_dot_smaller')} Введите место (1-12) и убийства (0-50) для каждой команды."
                ),
                color=discord.Color.from_rgb(69, 69, 69)
            )

            await interaction.response.edit_message(embed=embed, view=view)


class BRResultsView(View):
    """View для финальных результатов."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id

        # Кнопка "Удалить"
        self.add_item(DeleteBRButton(guild_id, channel_id))
