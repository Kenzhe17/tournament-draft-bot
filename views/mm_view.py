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

        # Check if already in another MM tournament
        user_id = interaction.user.id
        user_name = interaction.user.display_name

        all_mm = mm_store.get_by_guild(self.guild_id)
        for mm in all_mm:
            if mm.id != tournament.id and mm.phase.value != "complete":
                for player in mm.players:
                    if player["id"] == user_id:
                        await interaction.response.send_message(
                            f"❌ Вы уже участвуете в другом поиске в канале <#{mm.channel_id}>",
                            ephemeral=True
                        )
                        return

        # Get player ELO (used as skill rating)
        player_stats = await player_stats_store.get(self.guild_id, user_id)
        skill_rating = player_stats.elo if player_stats else 1000

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

            # Update to playing view
            embed = create_playing_embed(tournament)
            view = MMPlayingView(self.guild_id, self.channel_id, tournament)
            await interaction.edit_original_response(embed=embed, view=view)


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

        embed = create_playing_embed(self.tournament)
        view = MMPlayingView(self.guild_id, self.channel_id, self.tournament)
        await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send("✅ Данные комнаты сохранены!", ephemeral=True)


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

        embed = create_reporting_embed(self.tournament)
        view = MMReportingView(self.guild_id, self.channel_id, self.tournament)
        await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send(f"✅ Киллы команды {self.team_index + 1} сохранены: {kills}", ephemeral=True)


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
            await interaction.response.edit_message(embed=embed, view=view)
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
        await interaction.response.edit_message(embed=embed, view=view)


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
        await interaction.response.edit_message(embed=embed, view=view)


class MMCompleteView(View):
    """View для завершенного турнира."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id

        self.add_item(NewSearchButton(guild_id, channel_id))


class NewSearchButton(Button):
    """Кнопка для нового поиска."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Новый поиск",
            emoji="🔍",
            custom_id=f"mm_new:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from storage.mm_store import mm_store

        # Delete old tournament
        mm_store.delete(self.guild_id, self.channel_id)

        # Create new tournament (need format from original)
        # This button should recreate with same format - need to store format
        # For now, just delete and let user run /mm command again
        await interaction.response.send_message("❌ Запустите новый поиск через команду `/mm duo` или `/mm squad`", ephemeral=True)


# Embed builders

def create_searching_embed(tournament: "MMTournament") -> discord.Embed:
    """Create embed for searching phase."""
    format_name = "Duo (2x2)" if tournament.format == MMFormat.DUO else "Squad (4x4)"
    current = len(tournament.players)
    required = tournament.required_players

    embed = discord.Embed(
        title=f"🔍 Matchmaking - {format_name}",
        description=f"Поиск игроков: {current}/{required}",
        color=discord.Color.blue()
    )

    # Players list
    if tournament.players:
        players_text = "\n".join([f"{i+1}. {p['name']} (SR: {p['skill_rating']})" for i, p in enumerate(tournament.players)])
        embed.add_field(name="Игроки в поиске", value=players_text, inline=False)

    # Timer info
    if tournament.first_player_time:
        first_time = datetime.fromisoformat(tournament.first_player_time)
        elapsed = (datetime.now() - first_time).total_seconds()
        remaining = max(0, 600 - elapsed)  # 10 minutes
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        embed.add_field(name="Таймер", value=f"{minutes}:{seconds:02d}", inline=False)

    return embed


def create_playing_embed(tournament: "MMTournament") -> discord.Embed:
    """Create embed for playing phase."""
    format_name = "Duo (2x2)" if tournament.format == MMFormat.DUO else "Squad (4x4)"

    embed = discord.Embed(
        title=f"🎮 Matchmaking - {format_name}",
        description="Турнир идет! Введите данные комнаты и затем статистику.",
        color=discord.Color.green()
    )

    # Room data
    if tournament.room_data.get("id"):
        room_text = f"ID: {tournament.room_data['id']}"
        if tournament.room_data.get("password"):
            room_text += f"\nПароль: {tournament.room_data['password']}"
        embed.add_field(name="Комната", value=room_text, inline=False)

    # Teams
    for i, team in enumerate(tournament.teams):
        players_text = ", ".join(team["players"])
        embed.add_field(name=f"{team['name']} (Капитан: {team['captain']})", value=players_text, inline=False)

    return embed


def create_reporting_embed(tournament: "MMTournament") -> discord.Embed:
    """Create embed for reporting phase."""
    format_name = "Duo (2x2)" if tournament.format == MMFormat.DUO else "Squad (4x4)"

    embed = discord.Embed(
        title=f"📊 Matchmaking - {format_name}",
        description="Капитаны введите киллы своей команды.",
        color=discord.Color.orange()
    )

    # Teams with kills
    for i, team in enumerate(tournament.teams):
        status = f"Киллы: {team['kills']}" if team['kills'] > 0 else "Ожидает ввода"
        embed.add_field(name=f"{team['name']}", value=status, inline=False)

    return embed


def create_manual_winner_embed(tournament: "MMTournament", reason: str) -> discord.Embed:
    """Create embed for manual winner selection."""
    embed = discord.Embed(
        title="⚠️ Выберите победителя",
        description=reason,
        color=discord.Color.red()
    )

    for i, team in enumerate(tournament.teams):
        embed.add_field(name=f"{team['name']}", value=f"Киллы: {team['kills']}", inline=False)

    return embed


def create_results_embed(tournament: "MMTournament") -> discord.Embed:
    """Create embed for results."""
    format_name = "Duo (2x2)" if tournament.format == MMFormat.DUO else "Squad (4x4)"

    winner_index = tournament.winner_team_index
    winner_team = tournament.teams[winner_index] if winner_index is not None else None

    embed = discord.Embed(
        title=f"🏆 Matchmaking - {format_name}",
        color=discord.Color.gold()
    )

    if winner_team:
        embed.add_field(name="🎉 Победитель", value=winner_team['name'], inline=False)
        embed.add_field(name="Киллы победителя", value=str(winner_team['kills']), inline=False)

    # All teams
    for i, team in enumerate(tournament.teams):
        if i == winner_index:
            continue
        embed.add_field(name=f"{team['name']}", value=f"Киллы: {team['kills']}", inline=False)

    if tournament.manual_winner:
        embed.set_footer(text="Победитель выбран вручную")

    return embed
