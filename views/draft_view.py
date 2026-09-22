"""View для драфта — Select Menu выбора игрока."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from models.tournament import Tournament, TournamentPhase
from storage.json_store import store
from utils.embeds import build_embed_for_phase

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class PlayerSelect(discord.ui.Select):
    """Select Menu с доступными игроками текущего круга."""

    def __init__(self, guild_id: int, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Выберите игрока...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"draft_select:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.DRAFT:
            await interaction.response.send_message(
                "❌ Драфт не активен.", ephemeral=True
            )
            return

        picker_pos = tournament.current_picker_position()
        if picker_pos is None:
            await interaction.response.send_message(
                "❌ Сейчас не ваш ход.", ephemeral=True
            )
            return

        # Check if it's the captain's turn (by nickname)
        expected_captain_name = tournament.captains[tournament.captain_order[picker_pos]]
        user_name = interaction.user.display_name
        
        # In test mode, allow anyone to pick
        if not tournament.is_test and user_name != expected_captain_name:
            await interaction.response.send_message(
                f"❌ Сейчас выбирает {expected_captain_name}",
                ephemeral=True,
            )
            return

        player = self.values[0]
        key = str(tournament.current_circle)
        if player not in tournament.available.get(key, []):
            await interaction.response.send_message(
                "❌ Этот игрок уже выбран.", ephemeral=True
            )
            return

        tournament.pick_player(picker_pos, player)
        draft_complete = tournament.advance_after_pick()
        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        if draft_complete:
            await interaction.response.defer()
        else:
            await interaction.response.defer()


class DraftView(discord.ui.View):
    """Persistent View с Select Menu для драфта."""

    def __init__(self, guild_id: int, available_players: list[str]):
        super().__init__(timeout=None)
        options = [
            discord.SelectOption(label=p, value=p) for p in available_players[:25]
        ]
        if options:
            self.add_item(PlayerSelect(guild_id, options))
        # Add warning if more than 25 players available
        if len(available_players) > 25:
            self._warning = f"⚠️ Показано 25 из {len(available_players)} игроков"


def build_draft_view(tournament: Tournament) -> DraftView | None:
    """Создать View для текущего состояния драфта."""
    if tournament.phase != TournamentPhase.DRAFT:
        return None
    key = str(tournament.current_circle)
    available = tournament.available.get(key, [])
    if not available:
        return None
    # Если сейчас автовыбор — Select не нужен
    picker_pos = tournament.current_picker_position()
    if picker_pos is None:
        return None
    return DraftView(tournament.guild_id, available)
