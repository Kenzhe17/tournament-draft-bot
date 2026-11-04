"""Views for Ultimatum Solo Tournament matches."""

from __future__ import annotations

from config import replace_emojis, get_emoji

from typing import TYPE_CHECKING

import discord
from discord.ui import View, Button

from ultimatum.models import Match, MatchStatus
from ultimatum.store import ultimatum_store
from ultimatum.tournament import UltimatumTournament

if TYPE_CHECKING:
    from bot import TournamentBot


class RoomButton(Button):
    """Button to create room for a match."""

    def __init__(self, match: Match, tournament_id: str):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Комната",
            emoji="🏠",
            custom_id=f"ultimatum_room:{match.id}"
        )
        self.match = match
        self.tournament_id = tournament_id

    async def callback(self, interaction: discord.Interaction):
        # Check if match already has room
        if self.match.room_id:
            await interaction.response.send_message(
                f"❌ Комната уже создана: {self.match.room_id} (пароль: {self.match.room_password})",
                ephemeral=True
            )
            return

        # Generate room ID and password
        import random
        import string
        room_id = ''.join(random.choices(string.digits, k=6))
        room_password = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))

        # Update match with room info
        await ultimatum_store.update_match_room(
            self.match.id, room_id, room_password
        )

        await interaction.response.send_message(
            f"🏠 Комната создана!\nID: {room_id}\nПароль: {room_password}",
            ephemeral=True
        )


class WinnerSelectButton(Button):
    """Button to select winner of a match."""

    def __init__(self, match: Match, player_id: int, player_name: str, is_player1: bool):
        super().__init__(
            style=discord.ButtonStyle.success,
            label=player_name,
            custom_id=f"ultimatum_winner:{match.id}:{player_id}"
        )
        self.match = match
        self.player_id = player_id
        self.player_name = player_name
        self.is_player1 = is_player1

    async def callback(self, interaction: discord.Interaction):
        # Check if match is already completed
        if self.match.status == MatchStatus.COMPLETED:
            await interaction.response.send_message(
                "❌ Матч уже завершен.",
                ephemeral=True
            )
            return

        # Check if user is one of the players
        if interaction.user.id not in [self.match.player1_id, self.match.player2_id]:
            # Allow admins to override
            if not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message(
                    "❌ Только участники матча могут выбирать победителя.",
                    ephemeral=True
                )
                return

        # Determine winner and loser
        winner_id = self.player_id
        loser_id = self.match.player2_id if self.is_player1 else self.match.player1_id

        # Get tournament logic
        from cogs.ultimatum import UltimatumCog
        cog = interaction.client.get_cog('UltimatumCog')
        if not cog:
            await interaction.response.send_message(
                "❌ Tournament system not available.",
                ephemeral=True
            )
            return

        # Complete match
        try:
            await cog.tournament_logic.complete_match(
                self.match.id, winner_id, loser_id
            )
            await interaction.response.send_message(
                f"✅ Победитель выбран: {self.player_name}",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Ошибка: {str(e)}",
                ephemeral=True
            )


class UltimatumMatchView(View):
    """View for Ultimatum tournament matches."""

    def __init__(self, bot: TournamentBot, tournament_id: str, round_number: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.tournament_id = tournament_id
        self.round_number = round_number

    async def initialize(self):
        """Initialize view with match buttons."""
        matches = await ultimatum_store.get_matches(self.tournament_id, self.round_number)

        for match in matches:
            # Add room button
            self.add_item(RoomButton(match, self.tournament_id))

            # Add winner selection buttons
            # Get player names
            player1_name = f"Player {match.player1_id}"  # Should fetch from players table
            player2_name = f"Player {match.player2_id}"

            player1_btn = WinnerSelectButton(match, match.player1_id, player1_name, True)
            player2_btn = WinnerSelectButton(match, match.player2_id, player2_name, False)

            self.add_item(player1_btn)
            self.add_item(player2_btn)


def build_ultimatum_round_embed(tournament_id: str, round_number: int) -> discord.Embed:
    """Build embed for Ultimatum round matches."""
    matches = await ultimatum_store.get_matches(tournament_id, round_number)

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} {replace_emojis('winner')} УЛЬТИМАТУМ СОЛО | Раунд {round_number}",
        description=replace_emojis(f":white_dot: Матчи: {len(matches)}"),
        color=discord.Color.from_rgb(69, 69, 69)
    )

    if not matches:
        embed.add_field(
            name="",
            value=replace_emojis("⚪ Нет матчей в этом раунде"),
            inline=False
        )
        return embed

    # Add each match
    for idx, match in enumerate(matches, 1):
        player1_name = f"Player {match.player1_id}"
        player2_name = f"Player {match.player2_id}"

        # Get player stats for display
        # This should fetch from ultimatum_players table
        # For now, use placeholders

        match_info = replace_emojis(
            f":white_arrow: Матч {idx}: {player1_name} vs {player2_name}"
        )

        if match.status == MatchStatus.COMPLETED:
            match_info += replace_emojis(f" ✅ Завершен")
        elif match.room_id:
            match_info += replace_emojis(f" 🏠 Комната: {match.room_id}")
        else:
            match_info += replace_emojis(f" ⏳ Ожидание")

        embed.add_field(
            name="",
            value=match_info,
            inline=False
        )

    return embed
