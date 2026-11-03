"""Views for Matchmaking tournament."""

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord.ui import Modal, TextInput, View, Button, button, Select

from config import BOT_OWNER_ID, replace_emojis, get_emoji
from models.mm_tournament import MMFormat, MMPhase
from storage.mm_store import mm_store

if TYPE_CHECKING:
    from models.mm_tournament import MMTournament

logger = logging.getLogger(__name__)


def get_user_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    """Get or create a lock for user operations."""
    if not hasattr(get_user_lock, "_locks"):
        get_user_lock._locks = {}
    key = (guild_id, user_id)
    if key not in get_user_lock._locks:
        get_user_lock._locks[key] = asyncio.Lock()
    return get_user_lock._locks[key]


class MMRegistrationView(View):
    """View для фазы поиска MM турнира."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "MMTournament"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

        self.add_item(JoinMMButton(guild_id, channel_id))
        self.add_item(LeaveMMButton(guild_id, channel_id))


class JoinMMButton(Button):
    """Кнопка для входа в поиск."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Войти в поиск",
            emoji="🔍",
            custom_id=f"mm_join:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.mm_store import mm_store
        from storage.player_stats_store import player_stats_store

        tournament = mm_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message("❌ Турнир не найден.", ephemeral=True)
            return

        # Check if tournament is complete
        if tournament.phase == MMPhase.COMPLETE:
            await interaction.response.send_message("❌ Турнир завершен. Запустите новый поиск.", ephemeral=True)
            return

        # Check if already in any active MM tournament (including this one)
        user_id = interaction.user.id
        user_name = interaction.user.display_name

        all_mm = mm_store.get_by_guild(self.guild_id)
        for mm in all_mm:
            if mm.phase.value not in ["complete", "searching"]:
                for player in mm.players:
                    if player["id"] == user_id:
                        await interaction.response.send_message(
                            f"❌ Вы уже участвуете в активном матче. Завершите его чтобы войти в новый поиск.",
                            ephemeral=True
                        )
                        return

        # Get player skill rating from profile
        player_stats = await player_stats_store.get(self.guild_id, user_id)
        skill_rating = player_stats.skill_rating if player_stats else 1.0

        # Add player
        if not tournament.add_player(user_name, user_id, skill_rating):
            await interaction.response.send_message("❌ Поиск уже заполнен или вы уже в поиске.", ephemeral=True)
            return

        mm_store.set(tournament)

        # Update embed
        embed = create_searching_embed(tournament)
        await interaction.response.edit_message(embed=embed, view=MMRegistrationView(self.guild_id, self.channel_id, tournament))

        # Notify player
        await interaction.followup.send(f"✅ Вы добавлены в поиск! ({len(tournament.players)}/{tournament.required_players})", ephemeral=True)

        # Check if full
        if tournament.is_full():
            # Form teams and start
            tournament.form_teams_balanced()
            mm_store.set(tournament)

            # Send match embed to match channel
            try:
                match_channel = interaction.guild.get_channel(1564629925494001744)
                if match_channel:
                    match_embed = create_playing_embed(tournament)
                    match_view = MMPlayingView(self.guild_id, self.channel_id, tournament)
                    match_message = await match_channel.send(embed=match_embed, view=match_view)
                    # Store match message ID in tournament
                    tournament.match_message_id = match_message.id
                    mm_store.set(tournament)
            except Exception as e:
                logger.error(f"Error sending match embed: {e}")

            # Update main embed to show new search is available
            new_tournament = MMTournament(
                id="",
                guild_id=self.guild_id,
                channel_id=self.channel_id,
                format=tournament.format,
            )
            mm_store.set(new_tournament)

            new_embed = create_searching_embed(new_tournament)
            new_view = MMRegistrationView(self.guild_id, self.channel_id, new_tournament)
            await interaction.edit_original_response(embed=new_embed, view=new_view)


class LeaveMMButton(Button):
    """Кнопка для выхода из поиска."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Выйти",
            emoji="❌",
            custom_id=f"mm_leave:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.mm_store import mm_store

        tournament = mm_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message("❌ Турнир не найден.", ephemeral=True)
            return

        user_id = interaction.user.id

        if tournament.remove_player(user_id):
            mm_store.set(tournament)

            # Update embed
            embed = create_searching_embed(tournament)
            await interaction.response.edit_message(embed=embed, view=MMRegistrationView(self.guild_id, self.channel_id, tournament))
            await interaction.followup.send("✅ Вы вышли из поиска.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Вы не в поиске.", ephemeral=True)


class MMPlayingView(View):
    """View для фазы игры."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "MMTournament"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

        self.add_item(EnterRoomDataButton(guild_id, channel_id))
        self.add_item(GoToReportingButton(guild_id, channel_id))


class EnterRoomDataButton(Button):
    """Кнопка для ввода данных комнаты."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Ввести данные комнаты",
            emoji="🏠",
            custom_id=f"mm_room:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.mm_store import mm_store

        tournament = mm_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message("❌ Турнир не найден.", ephemeral=True)
            return

        modal = MMRoomModal(self.guild_id, self.channel_id, tournament)
        await interaction.response.send_modal(modal)


class MMRoomModal(Modal):
    """Modal для ввода ID и пароля комнаты."""

    room_id = TextInput(
        label="ID комнаты",
        placeholder="123456",
        required=True,
        max_length=20
    )

    password = TextInput(
        label="Пароль (если есть)",
        placeholder="Пароль",
        required=False,
        max_length=20
    )

    def __init__(self, guild_id: int, channel_id: int, tournament: "MMTournament"):
        super().__init__(title="Данные комнаты")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from storage.mm_store import mm_store

        self.tournament.room_data = {
            "id": self.room_id.value,
            "password": self.password.value if self.password.value else ""
        }
        mm_store.set(self.tournament)

        # Update match message
        try:
            if self.tournament.match_message_id:
                match_channel = interaction.guild.get_channel(1564629925494001744)
                if match_channel:
                    match_message = await match_channel.fetch_message(self.tournament.match_message_id)
                    embed = create_playing_embed(self.tournament)
                    view = MMPlayingView(self.guild_id, self.channel_id, self.tournament)
                    await match_message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error updating match message: {e}")

        await interaction.response.send_message("✅ Данные комнаты сохранены!", ephemeral=True)


class GoToReportingButton(Button):
    """Кнопка для перехода к вводу статистики."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Ввести статистику",
            emoji="📊",
            custom_id=f"mm_report:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.mm_store import mm_store

        tournament = mm_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message("❌ Турнир не найден.", ephemeral=True)
            return

        # Check if room data is set
        if not tournament.room_data.get("id"):
            await interaction.response.send_message("❌ Сначала введите данные комнаты.", ephemeral=True)
            return

        tournament.phase = MMPhase.REPORTING
        mm_store.set(tournament)

        embed = create_reporting_embed(tournament)
        view = MMReportingView(self.guild_id, self.channel_id, tournament)
        await interaction.response.edit_message(embed=embed, view=view)


class MMReportingView(View):
    """View для фазы ввода статистики."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "MMTournament"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

        self.add_item(EnterTeamAKillsButton(guild_id, channel_id, tournament))
        self.add_item(EnterTeamBKillsButton(guild_id, channel_id, tournament))
        self.add_item(FinishReportingButton(guild_id, channel_id))


class EnterTeamAKillsButton(Button):
    """Кнопка для ввода киллов команды A."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "MMTournament"):
        team_name = tournament.teams[0]["name"] if tournament.teams else "Команда A"
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=f"Киллы {team_name}",
            custom_id=f"mm_kills_a:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        modal = MMKillsModal(self.guild_id, self.channel_id, self.tournament, 0)
        await interaction.response.send_modal(modal)


class EnterTeamBKillsButton(Button):
    """Кнопка для ввода киллов команды B."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "MMTournament"):
        team_name = tournament.teams[1]["name"] if len(self.tournament.teams) > 1 else "Команда B"
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=f"Киллы {team_name}",
            custom_id=f"mm_kills_b:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        modal = MMKillsModal(self.guild_id, self.channel_id, self.tournament, 1)
        await interaction.response.send_modal(modal)


class MMKillsModal(Modal):
    """Modal для ввода киллов команды."""

    kills = TextInput(
        label="Количество киллов",
        placeholder="10",
        required=True,
        max_length=3
    )

    def __init__(self, guild_id: int, channel_id: int, tournament: "MMTournament", team_index: int):
        team_name = tournament.teams[team_index]["name"] if team_index < len(tournament.teams) else "Команда"
        super().__init__(title=f"Киллы {team_name}")
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.team_index = team_index

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from storage.mm_store import mm_store

        try:
            kills = int(self.kills.value)
        except ValueError:
            await interaction.response.send_message("❌ Введите число.", ephemeral=True)
            return

        if kills < 0 or kills > 100:
            await interaction.response.send_message("❌ Киллы должны быть от 0 до 100.", ephemeral=True)
            return

        self.tournament.set_team_kills(self.team_index, kills)
        mm_store.set(self.tournament)

        # Update match message
        try:
            if self.tournament.match_message_id:
                match_channel = interaction.guild.get_channel(1564629925494001744)
                if match_channel:
                    match_message = await match_channel.fetch_message(self.tournament.match_message_id)
                    embed = create_reporting_embed(self.tournament)
                    view = MMReportingView(self.guild_id, self.channel_id, self.tournament)
                    await match_message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error updating match message: {e}")

        await interaction.response.send_message(f"✅ Киллы команды {self.team_index + 1} сохранены: {kills}", ephemeral=True)


class FinishReportingButton(Button):
    """Кнопка для завершения отчета."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Завершить",
            emoji="✅",
            custom_id=f"mm_finish:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.mm_store import mm_store

        tournament = mm_store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message("❌ Турнир не найден.", ephemeral=True)
            return

        # Validate stats
        is_valid, message = tournament.validate_stats()
        if not is_valid:
            # Show manual winner selection
            embed = create_manual_winner_embed(tournament, message)
            view = MMManualWinnerView(self.guild_id, self.channel_id, tournament)
            # Update match message
            try:
                if tournament.match_message_id:
                    match_channel = interaction.guild.get_channel(1564629925494001744)
                    if match_channel:
                        match_message = await match_channel.fetch_message(tournament.match_message_id)
                        await match_message.edit(embed=embed, view=view)
            except Exception as e:
                logger.error(f"Error updating match message: {e}")
            await interaction.response.edit_message(content="❌ Статистика не совпадает. Выберите победителя вручную в канале матчей.", embed=None, view=None)
            return

        # Auto-determine winner
        winner_index = tournament.determine_winner()
        if winner_index is None:
            await interaction.response.send_message("❌ Не удалось определить победителя.", ephemeral=True)
            return

        tournament.winner_team_index = winner_index
        tournament.manual_winner = False
        tournament.phase = MMPhase.COMPLETE
        mm_store.set(tournament)

        embed = create_results_embed(tournament)
        view = MMCompleteView(self.guild_id, self.channel_id)
        # Update match message
        try:
            if tournament.match_message_id:
                match_channel = interaction.guild.get_channel(1564629925494001744)
                if match_channel:
                    match_message = await match_channel.fetch_message(tournament.match_message_id)
                    await match_message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error updating match message: {e}")
        await interaction.response.send_message("✅ Турнир завершен!", ephemeral=True)


class MMManualWinnerView(View):
    """View для ручного выбора победителя."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "MMTournament"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

        self.add_item(SelectTeamAButton(guild_id, channel_id, tournament))
        self.add_item(SelectTeamBButton(guild_id, channel_id, tournament))


class SelectTeamAButton(Button):
    """Выбрать команду A победителем."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "MMTournament"):
        team_name = tournament.teams[0]["name"] if tournament.teams else "Команда A"
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=f"{team_name} победила",
            custom_id=f"mm_winner_a:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.mm_store import mm_store

        self.tournament.winner_team_index = 0
        self.tournament.manual_winner = True
        self.tournament.phase = MMPhase.COMPLETE
        mm_store.set(self.tournament)

        embed = create_results_embed(self.tournament)
        view = MMCompleteView(self.guild_id, self.channel_id)
        # Update match message
        try:
            if self.tournament.match_message_id:
                match_channel = interaction.guild.get_channel(1564629925494001744)
                if match_channel:
                    match_message = await match_channel.fetch_message(self.tournament.match_message_id)
                    await match_message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error updating match message: {e}")

        await interaction.response.edit_message(embed=embed, view=view)



class SelectTeamBButton(Button):
    """Выбрать команду B победителем."""

    def __init__(self, guild_id: int, channel_id: int, tournament: "MMTournament"):
        team_name = tournament.teams[1]["name"] if len(self.tournament.teams) > 1 else "Команда B"
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=f"{team_name} победила",
            custom_id=f"mm_winner_b:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.mm_store import mm_store

        self.tournament.winner_team_index = 1
        self.tournament.manual_winner = True
        self.tournament.phase = MMPhase.COMPLETE
        mm_store.set(self.tournament)

        embed = create_results_embed(self.tournament)
        view = MMCompleteView(self.guild_id, self.channel_id)
        # Update match message
        try:
            if self.tournament.match_message_id:
                match_channel = interaction.guild.get_channel(1564629925494001744)
                if match_channel:
                    match_message = await match_channel.fetch_message(self.tournament.match_message_id)
                    await match_message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error updating match message: {e}")

        await interaction.response.edit_message(embed=embed, view=view)



class MMCompleteView(View):
    """View для завершенного турнира."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        # No buttons - tournament is complete


# Embed builders

def create_searching_embed(tournament: "MMTournament") -> discord.Embed:
    """Create embed for searching phase."""
    format_name = "Duo (2x2)" if tournament.format == MMFormat.DUO else "Squad (4x4)"
    current = len(tournament.players)
    required = tournament.required_players

    # Players list with SR
    players_text = ""
    if tournament.players:
        for i, p in enumerate(tournament.players):
            sr_display = f"{p['skill_rating']:.1f}" if isinstance(p['skill_rating'], float) else str(p['skill_rating'])
            players_text += f"{replace_emojis('white_arrow')} {p['name']} (SR: {sr_display})\n"
    else:
        players_text = replace_emojis('white_arrow') + " *Ожидание игроков...*"

    # Timer info
    timer_text = ""
    if tournament.first_player_time:
        first_time = datetime.fromisoformat(tournament.first_player_time)
        elapsed = (datetime.now() - first_time).total_seconds()
        remaining = max(0, 600 - elapsed)  # 10 minutes
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        timer_text = f"{replace_emojis('sub_middle')} Таймер: {minutes}:{seconds:02d}\n"

    description = (
        f"{replace_emojis('white_dot')} **Информация о поиске:**\n"
        f"{replace_emojis('sub_middle')} Формат: {format_name}\n"
        f"{replace_emojis('sub_middle')} Игроки: {current}/{required}\n"
        f"{timer_text}\n"
        f"{replace_emojis('white_dot')} **Игроки в поиске:**\n"
        f"{players_text}\n"
        f"{replace_emojis('a_dot_smaller')} Нажмите кнопку ниже чтобы войти в поиск"
    )

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} 🔍 MATCHMAKING ПОИСК",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69)
    )

    return embed


def create_playing_embed(tournament: "MMTournament") -> discord.Embed:
    """Create embed for playing phase."""
    format_name = "Duo (2x2)" if tournament.format == MMFormat.DUO else "Squad (4x4)"

    # Room data
    room_text = ""
    if tournament.room_data.get("id"):
        room_text = f"{replace_emojis('sub_middle')} ID: {tournament.room_data['id']}\n"
        if tournament.room_data.get("password"):
            room_text += f"{replace_emojis('sub_middle')} Пароль: {tournament.room_data['password']}\n"
    else:
        room_text = replace_emojis('white_arrow') + " *Данные комнаты не введены*"

    # Teams
    teams_text = ""
    for i, team in enumerate(tournament.teams):
        players_str = ", ".join(team["players"])
        teams_text += f"{replace_emojis('white_dot')} **{team['name']}** (Капитан: {team['captain']})\n"
        teams_text += f"{replace_emojis('white_arrow')} {players_str}\n"

    description = (
        f"{replace_emojis('white_dot')} **Информация о матче:**\n"
        f"{replace_emojis('sub_middle')} Формат: {format_name}\n"
        f"{replace_emojis('sub_middle')} Статус: Игра идет\n\n"
        f"{replace_emojis('white_dot')} **Комната:**\n"
        f"{room_text}\n\n"
        f"{replace_emojis('white_dot')} **Команды:**\n"
        f"{teams_text}\n"
        f"{replace_emojis('a_dot_smaller')} Введите данные комнаты и затем статистику"
    )

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} 🎮 MATCHMAKING ИГРА",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69)
    )

    return embed


def create_reporting_embed(tournament: "MMTournament") -> discord.Embed:
    """Create embed for reporting phase."""
    format_name = "Duo (2x2)" if tournament.format == MMFormat.DUO else "Squad (4x4)"

    # Teams with kills
    teams_text = ""
    for i, team in enumerate(tournament.teams):
        if team['kills'] > 0:
            status = f"Киллы: {team['kills']}"
        else:
            status = "*Ожидает ввода*"
        teams_text += f"{replace_emojis('white_dot')} **{team['name']}** (Капитан: {team['captain']})\n"
        teams_text += f"{replace_emojis('white_arrow')} {status}\n"

    description = (
        f"{replace_emojis('white_dot')} **Информация о матче:**\n"
        f"{replace_emojis('sub_middle')} Формат: {format_name}\n"
        f"{replace_emojis('sub_middle')} Статус: Ввод статистики\n\n"
        f"{replace_emojis('white_dot')} **Статистика команд:**\n"
        f"{teams_text}\n"
        f"{replace_emojis('a_dot_smaller')} Капитаны введите киллы своей команды"
    )

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} 📊 MATCHMAKING СТАТИСТИКА",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69)
    )

    return embed


def create_manual_winner_embed(tournament: "MMTournament", reason: str) -> discord.Embed:
    """Create embed for manual winner selection."""
    # Teams with kills
    teams_text = ""
    for i, team in enumerate(tournament.teams):
        teams_text += f"{replace_emojis('white_dot')} **{team['name']}** (Капитан: {team['captain']})\n"
        teams_text += f"{replace_emojis('white_arrow')} Киллы: {team['kills']}\n"

    description = (
        f"{replace_emojis('white_dot')} **Причина:** {reason}\n\n"
        f"{replace_emojis('white_dot')} **Статистика команд:**\n"
        f"{teams_text}\n"
        f"{replace_emojis('a_dot_smaller')} Выберите победителя вручную"
    )

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} ⚠️ ВЫБЕРИТЕ ПОБЕДИТЕЛЯ",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69)
    )

    return embed


def create_results_embed(tournament: "MMTournament") -> discord.Embed:
    """Create embed for results."""
    format_name = "Duo (2x2)" if tournament.format == MMFormat.DUO else "Squad (4x4)"

    winner_index = tournament.winner_team_index
    winner_team = tournament.teams[winner_index] if winner_index is not None else None

    # Winner section
    winner_text = ""
    if winner_team:
        players_str = ", ".join(winner_team['players'])
        winner_text = (
            f"{replace_emojis('white_arrow')} **{winner_team['name']}** (Капитан: {winner_team['captain']})\n"
            f"{replace_emojis('white_arrow')} Киллы: {winner_team['kills']}\n"
            f"{replace_emojis('white_arrow')} Состав: {players_str}"
        )

    # Loser section
    loser_text = ""
    for i, team in enumerate(tournament.teams):
        if i == winner_index:
            continue
        players_str = ", ".join(team['players'])
        loser_text += (
            f"{replace_emojis('white_dot')} **{team['name']}** (Капитан: {team['captain']})\n"
            f"{replace_emojis('white_arrow')} Киллы: {team['kills']}\n"
            f"{replace_emojis('white_arrow')} Состав: {players_str}\n"
        )

    description = (
        f"{replace_emojis('white_dot')} **Информация о матче:**\n"
        f"{replace_emojis('sub_middle')} Формат: {format_name}\n"
        f"{replace_emojis('sub_middle')} Статус: Завершен\n\n"
        f"{replace_emojis('white_dot')} **Победитель:**\n"
        f"{winner_text}\n\n"
        f"{replace_emojis('white_dot')} **Проигравшая команда:**\n"
        f"{loser_text}\n"
    )

    if tournament.manual_winner:
        description += f"{replace_emojis('a_dot_smaller')} Победитель выбран вручную\n"

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} {replace_emojis('winner')} ПОБЕДИТЕЛЬ МАТЧМАКИНГА",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69)
    )

    return embed
