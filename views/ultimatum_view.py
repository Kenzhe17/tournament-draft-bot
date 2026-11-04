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

# Tracking for winner votes (temporary - in production should use database)
winner_votes = {}  # {match_id: {user_id: winner_id}}


class UltimatumMatchView(discord.ui.View):
    """View for Ultimatum match with room and winner selection."""

    def __init__(self, bot: commands.Bot, tournament_id: str, match_id: int, round_number: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.tournament_id = tournament_id
        self.match_id = match_id
        self.round_number = round_number
        self.tournament_logic = UltimatumTournament(ultimatum_store)

    def refresh_items(self, match) -> None:
        """Refresh view items based on match status."""
        self.clear_items()

        if match.status == MatchStatus.PENDING:
            # Room button
            room_btn = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="Комната",
                emoji="🏠",
                custom_id=f"ultimatum_room:{self.match_id}"
            )
            room_btn.callback = self.room_callback
            self.add_item(room_btn)

            # Winner selection buttons
            winner_select = discord.ui.Select(
                placeholder="Выберите победителя",
                min_values=1,
                max_values=1,
                custom_id=f"ultimatum_winner:{self.match_id}"
            )
            winner_select.options = [
                discord.SelectOption(label=f"Игрок 1", value="player1"),
                discord.SelectOption(label=f"Игрок 2", value="player2"),
            ]
            winner_select.callback = self.winner_callback
            self.add_item(winner_select)

        elif match.status == MatchStatus.IN_PROGRESS:
            # In progress - show room info
            self.add_item(discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="Матч в процессе",
                disabled=True
            ))

        elif match.status == MatchStatus.COMPLETED:
            # Completed - show result
            self.add_item(discord.ui.Button(
                style=discord.ButtonStyle.success,
                label="Завершено",
                disabled=True
            ))

    async def room_callback(self, interaction: discord.Interaction) -> None:
        """Handle room button click."""
        # Show modal for room ID and password
        modal = UltimatumRoomModal(self.match_id, self.tournament_id)
        await interaction.response.send_modal(modal)

    async def winner_callback(self, interaction: discord.Interaction) -> None:
        """Handle winner selection."""
        winner_value = interaction.data["values"][0]

        # Get match details
        match = await ultimatum_store.get_match_by_id(self.match_id)
        if not match:
            await interaction.response.send_message("❌ Матч не найден", ephemeral=True)
            return

        # Determine winner and loser
        if winner_value == "player1":
            winner_id = match.player1_id
            loser_id = match.player2_id
        else:
            winner_id = match.player2_id
            loser_id = match.player1_id

        # Complete the match
        try:
            await self.tournament_logic.complete_match(
                self.match_id, winner_id, loser_id
            )
            await interaction.response.send_message(
                "✅ Результат записан! Elo обновлен.",
                ephemeral=True
            )

            # Refresh the view
            updated_match = await ultimatum_store.get_match_by_id(self.match_id)
            self.refresh_items(updated_match)
            await interaction.edit_original_response(view=self)

        except Exception as e:
            logger.error(f"Error completing match: {e}")
            await interaction.response.send_message(
                f"❌ Ошибка: {str(e)}",
                ephemeral=True
            )


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

        if match.room_id:
            field_value += f"\n{replace_emojis('sub_middle')} Комната: {match.room_id}"

        embed.add_field(
            name=f"Матч #{match.id}",
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

        # Show select menu with matches
        matches = await ultimatum_store.get_matches(self.tournament_id, self.round_number)
        
        if not matches:
            await interaction.response.send_message("Нет матчей в этом раунде", ephemeral=True)
            return

        options = [
            discord.SelectOption(
                label=f"Матч #{match.id}: {match.player1_id} vs {match.player2_id}",
                value=str(match.id)
            )
            for match in matches
        ]

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
        # Show select menu with matches
        matches = await ultimatum_store.get_matches(self.tournament_id, self.round_number)
        
        if not matches:
            await interaction.response.send_message("Нет матчей в этом раунде", ephemeral=True)
            return

        options = [
            discord.SelectOption(
                label=f"Матч #{match.id}: {match.player1_id} vs {match.player2_id}",
                value=str(match.id)
            )
            for match in matches
        ]

        select = UltimatumSelectWinnerMatch(self.tournament_id, self.round_number, options)
        
        view = discord.ui.View()
        view.add_item(select)
        
        embed = discord.Embed(
            title="🏆 Выберите матч",
            description="Выберите матч для определения победителя",
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

    def __init__(self, tournament_id: str, round_number: int, options=None):
        super().__init__(
            placeholder="Выберите матч для определения победителя",
            custom_id=f"ultimatum_select_winner_match:{tournament_id}:{round_number}",
            options=options or []
        )
        self.tournament_id = tournament_id
        self.round_number = round_number

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
        
        # Create buttons for each player
        view = discord.ui.View()
        
        p1_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=p1.username,
            custom_id=f"ultimatum_vote_winner:{match_id}:{match.player1_id}"
        )
        p1_btn.callback = UltimatumVoteWinnerButton(match_id, match.player1_id, match.player2_id, self.tournament_id).callback
        view.add_item(p1_btn)
        
        p2_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=p2.username,
            custom_id=f"ultimatum_vote_winner:{match_id}:{match.player2_id}"
        )
        p2_btn.callback = UltimatumVoteWinnerButton(match_id, match.player2_id, match.player1_id, self.tournament_id).callback
        view.add_item(p2_btn)
        
        embed = discord.Embed(
            title="🏆 Выберите победителя",
            description=f"{p1.username} vs {p2.username}\n\nОба игрока должны нажать на одного и того же для подтверждения.",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class UltimatumVoteWinnerButton(discord.ui.Button):
    """Button for voting on match winner."""

    def __init__(self, match_id: int, voted_winner_id: int, other_player_id: int, tournament_id: str):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="",  # Will be set dynamically
            custom_id=f"ultimatum_vote_winner:{match_id}:{voted_winner_id}"
        )
        self.match_id = match_id
        self.voted_winner_id = voted_winner_id
        self.other_player_id = other_player_id
        self.tournament_id = tournament_id

    async def callback(self, interaction: discord.Interaction) -> None:
        # Check if user is one of the players
        if interaction.user.id not in [self.voted_winner_id, self.other_player_id]:
            await interaction.response.send_message(
                "❌ Только участники матча могут голосовать.",
                ephemeral=True
            )
            return
        
        # Record vote in global tracker
        if self.match_id not in winner_votes:
            winner_votes[self.match_id] = {}
        
        winner_votes[self.match_id][interaction.user.id] = self.voted_winner_id
        
        # Check votes
        match_votes = winner_votes[self.match_id]
        votes_for_other = sum(1 for v in match_votes.values() if v == self.other_player_id)
        votes_for_winner = sum(1 for v in match_votes.values() if v == self.voted_winner_id)
        
        total_votes = len(match_votes)
        
        if total_votes >= 2:
            # Both players voted - check consensus
            if votes_for_winner == 2:
                # Consensus! Complete match
                from ultimatum.tournament import UltimatumTournament
                tournament_logic = UltimatumTournament(ultimatum_store)
                await tournament_logic.complete_match(self.match_id, self.voted_winner_id, self.other_player_id)
                
                # Clear votes
                del winner_votes[self.match_id]
                
                await interaction.response.send_message(
                    f"✅ Победитель определен",
                    ephemeral=True
                )
                
                # TODO: Update round embed
            else:
                # No consensus - reset votes
                del winner_votes[self.match_id]
                await interaction.response.send_message(
                    "❌ Нет консенсуса. Игроки выбрали разных победителей. Попробуйте снова.",
                    ephemeral=True
                )
        else:
            await interaction.response.send_message(
                f"Голос записан. Ожидание голоса второго игрока...",
                ephemeral=True
            )
