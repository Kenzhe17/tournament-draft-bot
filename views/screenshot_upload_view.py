"""Views for uploading match result screenshots."""

import discord
from discord.ui import View, Button
from typing import TYPE_CHECKING, List
import tempfile
import os

if TYPE_CHECKING:
    from bot import TournamentBot


class MatchUploadView(View):
    """View for selecting a match to upload screenshot for."""

    def __init__(self, guild_id: int, tournament, matches: List[tuple], match_type: str):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.matches = matches
        self.match_type = match_type

        # Add buttons for each match
        for i, (team_a, team_b) in enumerate(matches):
            team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
            team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
            captain_a = team_a_data.get("captain", f"П{team_a + 1}")
            captain_b = team_b_data.get("captain", f"П{team_b + 1}")
            name_a = tournament.team_names.get(team_a, captain_a)
            name_b = tournament.team_names.get(team_b, captain_b)

            button = Button(
                label=f"📷 Матч #{i + 1}: {name_a} vs {name_b}",
                style=discord.ButtonStyle.primary,
                custom_id=f"upload_match_{i}"
            )
            button.callback = self._create_match_callback(i, team_a, team_b, name_a, name_b)
            self.add_item(button)

    def _create_match_callback(self, match_index: int, team_a: int, team_b: int, name_a: str, name_b: str):
        """Create a callback for a match button."""
        async def callback(interaction: discord.Interaction):
            await interaction.response.send_message(
                f"📷 Отправьте скриншот результатов матча #{match_index + 1} ({name_a} vs {name_b}).",
                ephemeral=True
            )
            # Store match info for the next message
            self.tournament.pending_screenshot_upload = {
                "match_index": match_index,
                "team_a": team_a,
                "team_b": team_b,
                "name_a": name_a,
                "name_b": name_b,
                "match_type": self.match_type,
                "user_id": interaction.user.id
            }
        return callback


class UploadResultsButton(Button):
    """Button to open screenshot upload flow."""

    def __init__(self, guild_id: int, tournament, matches: List[tuple], match_type: str):
        super().__init__(
            label="📷 Загрузить результаты матчей",
            style=discord.ButtonStyle.success,
            custom_id="upload_results"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.matches = matches
        self.match_type = match_type

    async def callback(self, interaction: discord.Interaction):
        """Open match selection view."""
        match_view = MatchUploadView(
            self.guild_id,
            self.tournament,
            self.matches,
            self.match_type
        )
        await interaction.response.send_message(
            content="📷 Выберите матч для загрузки скриншота:",
            view=match_view,
            ephemeral=True
        )
