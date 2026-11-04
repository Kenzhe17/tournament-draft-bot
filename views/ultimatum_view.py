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
        # Get player names and stats
        p1 = guild.get_member(match.player1_id)
        p2 = guild.get_member(match.player2_id)
        p1_name = p1.display_name if p1 else f"<@{match.player1_id}>"
        p2_name = p2.display_name if p2 else f"<@{match.player2_id}>"
        
        # Get player stats
        p1_player = await ultimatum_store.get_player_by_discord_id(tournament_id, match.player1_id)
        p2_player = await ultimatum_store.get_player_by_discord_id(tournament_id, match.player2_id)
        
        p1_stats = f"({p1_player.wins}:{p1_player.losses})" if p1_player else "(0:0)"
        p2_stats = f"({p2_player.wins}:{p2_player.losses})" if p2_player else "(0:0)"

        field_value = f"{replace_emojis('white_arrow')} {p1_name}{p1_stats} vs {p2_name}{p2_stats}"

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
        
        if not is_admin:
            await interaction.response.send_message(
                "❌ Только админы могут выбирать победителя.",
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
        match_id = int(self.values[0])
        
        # Get match details
        match = await ultimatum_store.get_match_by_id(match_id)
        if not match:
            await interaction.response.send_message("Матч не найден", ephemeral=True)
            return
        
        # Get players
        p1 = await ultimatum_store.get_player_by_discord_id(self.tournament_id, match.player1_id)
        p2 = await ultimatum_store.get_player_by_discord_id(self.tournament_id, match.player2_id)
        
        if not p1 or not p2:
            await interaction.response.send_message("Игроки не найдены", ephemeral=True)
            return
        
        # Create custom view with button callbacks
        view = UltimatumWinnerSelectionView(match_id, match.player1_id, match.player2_id, p1.username, p2.username, self.tournament_id, self.is_admin)
        
        description = f"{p1.username} vs {p2.username}\n\nАдмин может напрямую выбрать победителя."
        
        embed = discord.Embed(
            title="🏆 Выберите победителя",
            description=description,
            color=discord.Color.from_rgb(69, 69, 69)
        )
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class UltimatumWinnerSelectionView(discord.ui.View):
    """View for winner selection with button callbacks."""
    
    def __init__(self, match_id: int, player1_id: int, player2_id: int, player1_name: str, player2_name: str, tournament_id: str, is_admin: bool):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.player1_id = player1_id
        self.player2_id = player2_id
        self.player1_name = player1_name
        self.player2_name = player2_name
        self.tournament_id = tournament_id
        self.is_admin = is_admin
        
        # Add buttons
        p1_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=player1_name,
            custom_id=f"winner_p1"
        )
        p1_btn.callback = self.p1_callback
        self.add_item(p1_btn)
        
        p2_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=player2_name,
            custom_id=f"winner_p2"
        )
        p2_btn.callback = self.p2_callback
        self.add_item(p2_btn)
    
    async def p1_callback(self, interaction: discord.Interaction) -> None:
        await self.handle_vote(interaction, self.player1_id, self.player2_id)
    
    async def p2_callback(self, interaction: discord.Interaction) -> None:
        await self.handle_vote(interaction, self.player2_id, self.player1_id)
    
    async def handle_vote(self, interaction: discord.Interaction, voted_winner_id: int, other_player_id: int) -> None:
        # Defer to avoid timeout
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            pass
        
        # Check if match is already completed
        match = await ultimatum_store.get_match_by_id(self.match_id)
        if not match:
            await interaction.followup.send("Матч не найден", ephemeral=True)
            return
        
        if match.status == MatchStatus.COMPLETED:
            await interaction.followup.send("❌ Матч уже завершен.", ephemeral=True)
            return
        
        # Check if user is admin
        from utils.permissions import is_org_check
        is_admin = is_org_check(interaction.user, interaction.guild)
        
        if not is_admin:
            await interaction.followup.send(
                "❌ Только админы могут выбирать победителя.",
                ephemeral=True
            )
            return
        
        # Admin can directly complete match
        from ultimatum.tournament import UltimatumTournament
        tournament_logic = UltimatumTournament(ultimatum_store)
        await tournament_logic.complete_match(self.match_id, voted_winner_id, other_player_id, interaction.client)
        
        await interaction.followup.send(
            f"✅ Победитель определен (админ)",
            ephemeral=True
        )
        
        # Update round embed
        from views.ultimatum_view import update_round_embed
        match = await ultimatum_store.get_match_by_id(self.match_id)
        await update_round_embed(interaction.guild, match.tournament_id, match.round_number, interaction.client)
