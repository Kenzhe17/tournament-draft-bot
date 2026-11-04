"""View for Ultimatum Solo Tournament matches."""

import logging
import asyncio

import discord
from discord.ext import commands

from config import replace_emojis, get_emoji
from ultimatum.models import MatchStatus, MatchType
from ultimatum.store import ultimatum_store
from ultimatum.tournament import UltimatumTournament
from views.ultimatum_room_modal import UltimatumRoomModal

logger = logging.getLogger(__name__)

# Cooldown tracking for room submissions
room_cooldowns = {}

# Track round messages for updates: {guild_id: (channel_id, message_id)}
round_messages = {}


async def update_round_embed(guild: discord.Guild, tournament_id: str, round_number: int, bot: commands.Bot) -> None:
    """Update the round embed with current match information."""
    if guild.id not in round_messages:
        logger.warning(f"No round message tracked for guild {guild.id}")
        return
    
    try:
        channel_id, message_id = round_messages[guild.id]
        channel = guild.get_channel(channel_id)
        if not channel:
            logger.warning(f"Channel {channel_id} not found for guild {guild.id}")
            return
        
        message = await channel.fetch_message(message_id)
        new_embed = await build_ultimatum_round_embed(tournament_id, round_number, guild)
        new_view = await build_ultimatum_round_view(bot, tournament_id, round_number)
        await message.edit(embed=new_embed, view=new_view)
        logger.info(f"Updated round embed for tournament {tournament_id}, round {round_number}")
    except Exception as e:
        logger.error(f"Error updating round embed: {e}", exc_info=True)


async def build_ultimatum_round_embed(tournament_id: str, round_number: int, guild: discord.Guild) -> discord.Embed:
    """Build embed for Ultimatum round."""
    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} {replace_emojis('winner')} 16 | УЛЬТИМАТУМ СОЛО",
        description=f"{replace_emojis('white_dot')} Матчи:",
        color=discord.Color.from_rgb(69, 69, 69)
    )

    # Get matches for this round
    matches = await ultimatum_store.get_matches(tournament_id, round_number)

    if not matches:
        embed.description = "Нет матчей в этом раунде"
        return embed

    for match in matches:
        # Get player names
        p1 = guild.get_member(match.player1_id)
        p2 = guild.get_member(match.player2_id)
        p1_name = p1.display_name if p1 else f"<@{match.player1_id}>"
        p2_name = p2.display_name if p2 else f"<@{match.player2_id}>"

        field_value = f"{replace_emojis('white_arrow')} {p1_name} vs {p2_name}"

        embed.add_field(
            name="\u200b",  # Zero-width space
            value=field_value,
            inline=False
        )

    return embed


async def build_ultimatum_round_view(bot: commands.Bot, tournament_id: str, round_number: int) -> discord.ui.View:
    """Build view for Ultimatum round with 2 main buttons."""
    view = discord.ui.View(timeout=None)

    # Room data button
    room_btn = UltimatumRoomsButton(tournament_id, round_number, bot)
    view.add_item(room_btn)

    # Winner selection button
    winner_btn = UltimatumWinnerButton(tournament_id, round_number, bot)
    view.add_item(winner_btn)

    return view


class UltimatumRoomsButton(discord.ui.Button):
    """Button for managing room data."""

    def __init__(self, tournament_id: str, round_number: int, bot: commands.Bot):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="📋 Данные Комнаты",
            custom_id=f"ultimatum_rooms:{tournament_id}:{round_number}"
        )
        self.tournament_id = tournament_id
        self.round_number = round_number
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check if user is admin
        from utils.permissions import is_org_check
        is_admin = is_org_check(interaction.user, interaction.guild)
        
        # Check if user is in any match in this round
        matches = await ultimatum_store.get_matches(self.tournament_id, self.round_number)
        user_in_match = any(
            match.player1_id == interaction.user.id or match.player2_id == interaction.user.id
            for match in matches
        )
        
        if not is_admin and not user_in_match:
            await interaction.response.send_message(
                "❌ Только админы или участники матчей могут просматривать данные комнат.",
                ephemeral=True
            )
            return
        
        # Check cooldown
        cooldown_key = f"{self.tournament_id}:room"
        if cooldown_key in room_cooldowns:
            elapsed = asyncio.get_event_loop().time() - room_cooldowns[cooldown_key]
            if elapsed < 1.0:
                await interaction.response.send_message(
                    f"Подождите {1.0 - elapsed:.1f} сек перед отправкой данных",
                    ephemeral=True
                )
                return

        # Build options with player names
        options = []
        for match in matches:
            if match.room_id:
                continue  # Skip matches with room
            
            # Get player names
            p1 = await ultimatum_store.get_player_by_discord_id(self.tournament_id, match.player1_id)
            p2 = await ultimatum_store.get_player_by_discord_id(self.tournament_id, match.player2_id)
            p1_name = p1.username if p1 else f"<@{match.player1_id}>"
            p2_name = p2.username if p2 else f"<@{match.player2_id}>"
            
            options.append(
                discord.SelectOption(
                    label=f"{p1_name} vs {p2_name}",
                    value=str(match.id)
                )
            )
        
        if not options:
            await interaction.response.send_message(
                "Все матчи уже имеют комнаты.",
                ephemeral=True
            )
            return

        select = UltimatumSelectMatch(self.tournament_id, self.round_number)
        select.options = options
        
        view = discord.ui.View()
        view.add_item(select)
        
        embed = discord.Embed(
            title="📋 Выберите матч",
            description="Выберите матч для добавления данных комнаты",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class UltimatumWinnerButton(discord.ui.Button):
    """Button for selecting match winner."""

    def __init__(self, tournament_id: str, round_number: int, bot: commands.Bot):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="🏆 Выбрать Победителя",
            custom_id=f"ultimatum_winner:{tournament_id}:{round_number}"
        )
        self.tournament_id = tournament_id
        self.round_number = round_number
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check if user is admin
        from utils.permissions import is_org_check
        is_admin = is_org_check(interaction.user, interaction.guild)
        
        # Check if user is in any match in this round
        matches = await ultimatum_store.get_matches(self.tournament_id, self.round_number)
        user_in_match = any(
            match.player1_id == interaction.user.id or match.player2_id == interaction.user.id
            for match in matches
        )
        
        if not is_admin and not user_in_match:
            await interaction.response.send_message(
                "❌ Только админы или участники матчей могут выбирать победителя.",
                ephemeral=True
            )
            return

        # Check if user is admin
        from utils.permissions import is_org_check
        is_admin = is_org_check(interaction.user, interaction.guild)

        # Build options with player names
        options = []
        for match in matches:
            if match.status != MatchStatus.PENDING:
                continue  # Only show pending matches
            
            # Get player names
            p1 = await ultimatum_store.get_player_by_discord_id(self.tournament_id, match.player1_id)
            p2 = await ultimatum_store.get_player_by_discord_id(self.tournament_id, match.player2_id)
            p1_name = p1.username if p1 else f"<@{match.player1_id}>"
            p2_name = p2.username if p2 else f"<@{match.player2_id}>"
            
            options.append(
                discord.SelectOption(
                    label=f"{p1_name} vs {p2_name}",
                    value=str(match.id)
                )
            )
        
        if not options:
            await interaction.response.send_message(
                "Все матчи завершены.",
                ephemeral=True
            )
            return

        select = UltimatumSelectWinnerMatch(self.tournament_id, self.round_number, options, is_admin)
        
        view = discord.ui.View()
        view.add_item(select)
        
        embed = discord.Embed(
            title="🏆 Выберите матч",
            description="Выберите матч для определения победителя" + (" (Админ)" if is_admin else ""),
            color=discord.Color.from_rgb(69, 69, 69)
        )
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class UltimatumSelectMatch(discord.ui.Select):
    """Select menu for choosing match for room input."""

    def __init__(self, tournament_id: str, round_number: int, options=None):
        super().__init__(
            placeholder="Выберите матч для добавления комнаты",
            custom_id=f"ultimatum_select_match:{tournament_id}:{round_number}",
            options=options or []
        )
        self.tournament_id = tournament_id
        self.round_number = round_number

    async def callback(self, interaction: discord.Interaction) -> None:
        match_id = int(self.values[0])
        
        # Open modal for room input
        modal = UltimatumRoomModal(match_id, self.tournament_id, self.round_number)
        try:
            await interaction.response.send_modal(modal)
        except discord.NotFound:
            await interaction.followup.send(
                "Время взаимодействия истекло. Попробуйте снова.",
                ephemeral=True
            )


class UltimatumSelectWinnerMatch(discord.ui.Select):
    """Select menu for choosing match for winner selection."""

    def __init__(self, tournament_id: str, round_number: int, options=None, is_admin=False):
        super().__init__(
            placeholder="Выберите матч для определения победителя",
            custom_id=f"ultimatum_select_winner_match:{tournament_id}:{round_number}",
            options=options or []
        )
        self.tournament_id = tournament_id
        self.round_number = round_number
        self.is_admin = is_admin

    async def callback(self, interaction: discord.Interaction) -> None:
        # Defer to avoid timeout
        await interaction.response.defer(ephemeral=True)
        
        match_id = int(self.values[0])
        
        # Get match details
        match = await ultimatum_store.get_match_by_id(match_id)
        if not match:
            await interaction.followup.send("Матч не найден", ephemeral=True)
            return
        
        # Get players
        p1 = await ultimatum_store.get_player_by_discord_id(self.tournament_id, match.player1_id)
        p2 = await ultimatum_store.get_player_by_discord_id(self.tournament_id, match.player2_id)
        
        if not p1 or not p2:
            await interaction.followup.send("Игроки не найдены", ephemeral=True)
            return
        
        # Create buttons for each player
        view = discord.ui.View()
        
        p1_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=p1.username,
            custom_id=f"ultimatum_vote_winner:{match_id}:{match.player1_id}"
        )
        view.add_item(p1_btn)
        
        p2_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=p2.username,
            custom_id=f"ultimatum_vote_winner:{match_id}:{match.player2_id}"
        )
        view.add_item(p2_btn)
        
        description = f"{p1.username} vs {p2.username}\n\n"
        if self.is_admin:
            description += "Админ может напрямую выбрать победителя."
        else:
            description += "Оба игрока должны нажать на одного и того же для подтверждения."
        
        embed = discord.Embed(
            title="🏆 Выберите победителя",
            description=description,
            color=discord.Color.from_rgb(69, 69, 69)
        )
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
