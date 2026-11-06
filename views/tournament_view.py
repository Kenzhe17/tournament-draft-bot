"""Unified tournament views for simplified UI."""

from __future__ import annotations

import logging
from typing import Optional

import discord

from core.models import TournamentState, TournamentPhase
from services.tournament_service import TournamentService
from utils.permissions import is_org_check

logger = logging.getLogger(__name__)


class TournamentView(discord.ui.View):
    """Unified view for tournament interactions."""

    def __init__(self, guild_id: int, channel_id: int, tournament: TournamentState):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

        # Add buttons based on tournament phase
        self._add_phase_buttons()

    def _add_phase_buttons(self) -> None:
        """Add buttons based on current tournament phase."""
        if self.tournament.phase == TournamentPhase.SETUP:
            self.add_item(StartTournamentButton(self.guild_id, self.channel_id))
            self.add_item(RegistrationButton(self.guild_id, self.channel_id))

        elif self.tournament.phase == TournamentPhase.QUALIFIERS:
            self.add_item(MatchButton(self.guild_id, self.channel_id, "qualifier"))
            self.add_item(RoomButton(self.guild_id, self.channel_id))

        elif self.tournament.phase == TournamentPhase.SEMIFINALS:
            self.add_item(MatchButton(self.guild_id, self.channel_id, "semifinal"))
            self.add_item(RoomButton(self.guild_id, self.channel_id))

        elif self.tournament.phase == TournamentPhase.FINAL:
            self.add_item(MatchButton(self.guild_id, self.channel_id, "final"))
            self.add_item(RoomButton(self.guild_id, self.channel_id))

        elif self.tournament.phase == TournamentPhase.COMPLETE:
            self.add_item(ResultsButton(self.guild_id, self.channel_id))

    async def update_embed(self, interaction: discord.Interaction) -> None:
        """Update tournament embed."""
        # TODO: build embed via utils.embeds once migrated
        await interaction.response.edit_message(view=self)


class StartTournamentButton(discord.ui.Button):
    """Button to start tournament."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            label="Start Tournament",
            style=discord.ButtonStyle.success,
            custom_id=f"start_tournament_{guild_id}_{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Handle start tournament."""
        # TODO: Implement start logic
        await interaction.response.send_message("Tournament started!", ephemeral=True)


class RegistrationButton(discord.ui.Button):
    """Button for registration."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            label="Register",
            style=discord.ButtonStyle.primary,
            custom_id=f"register_{guild_id}_{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Handle registration."""
        # TODO: Implement registration logic
        await interaction.response.send_message("Registration opened!", ephemeral=True)


class MatchButton(discord.ui.Button):
    """Button for match interactions."""

    def __init__(self, guild_id: int, channel_id: int, match_type: str):
        super().__init__(
            label=f"{match_type.title()} Match",
            style=discord.ButtonStyle.primary,
            custom_id=f"match_{match_type}_{guild_id}_{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type

    async def callback(self, interaction: discord.Interaction) -> None:
        """Handle match interaction."""
        # TODO: Implement match logic
        await interaction.response.send_message(f"{self.match_type} match selected", ephemeral=True)


class RoomButton(discord.ui.Button):
    """Button for room creation."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            label="Create Room",
            style=discord.ButtonStyle.secondary,
            custom_id=f"room_{guild_id}_{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Handle room creation."""
        # TODO: Implement room logic
        await interaction.response.send_message("Room creation modal opened", ephemeral=True)


class ResultsButton(discord.ui.Button):
    """Button for results."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            label="View Results",
            style=discord.ButtonStyle.secondary,
            custom_id=f"results_{guild_id}_{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Handle results view."""
        # TODO: Implement results logic
        await interaction.response.send_message("Tournament results", ephemeral=True)
