"""View для драфта — Select Menu выбора игрока."""

from __future__ import annotations

from config import replace_emojis

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
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."),
                ephemeral=True,
            )
            return

        if tournament.phase != TournamentPhase.DRAFT:
            await interaction.response.send_message(
                replace_emojis("❌ Драфт не активен."),
                ephemeral=True,
            )
            return

        picker_pos = tournament.current_picker_position()
        if picker_pos is None:
            await interaction.response.send_message(
                replace_emojis("❌ Сейчас не ваш ход."),
                ephemeral=True,
            )
            return

        # Check if it's the captain's turn (by nickname)
        expected_captain_name = tournament.captains[tournament.captain_order[picker_pos]]
        user_name = interaction.user.display_name
        from utils.cosmetics import clean_nickname
        cleaned_name = clean_nickname(user_name)
        cleaned_captain_name = clean_nickname(expected_captain_name)
        
        # Automatically update user's nickname if it contains emojis
        if cleaned_name != user_name:
            try:
                await interaction.user.edit(nick=cleaned_name)
                user_name = cleaned_name
            except discord.Forbidden:
                # Bot doesn't have permission to edit nickname, use cleaned name anyway
                user_name = cleaned_name
        
        # In test mode, allow anyone to pick
        if not tournament.is_test and user_name != cleaned_captain_name:
            await interaction.response.send_message(
                replace_emojis(f"❌ Сейчас выбирает {expected_captain_name}"),
                ephemeral=True,
            )
            return

        player = self.values[0]
        key = str(tournament.current_circle)
        if player not in tournament.available.get(key, []):
            await interaction.response.send_message(
                replace_emojis("❌ Этот игрок уже выбран."),
                ephemeral=True,
            )
            return

        tournament.pick_player(picker_pos, player)
        draft_complete = tournament.advance_after_pick()
        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        # Delete old draft message if exists
        if tournament.draft_message_id > 0:
            try:
                draft_channel = interaction.channel
                old_message = await draft_channel.fetch_message(tournament.draft_message_id)
                await old_message.delete()
            except Exception:
                pass  # Message might not exist or already deleted

        # Send new draft message with next captain ping if draft not complete
        if not draft_complete:
            next_picker_pos = tournament.current_picker_position()
            if next_picker_pos is not None:
                next_captain_name = tournament.captains[tournament.captain_order[next_picker_pos]]
                next_captain_id = tournament.player_user_ids.get(next_captain_name, 0)
                if next_captain_id > 0:
                    new_message = await interaction.channel.send(f"{replace_emojis('white_arrow')} <@{next_captain_id}> - ваша очередь выбирать!")
                else:
                    new_message = await interaction.channel.send(f"{replace_emojis('white_arrow')} {next_captain_name} - ваша очередь выбирать!")
                tournament.draft_message_id = new_message.id
                store.set(tournament)

        if draft_complete:
            try:
                await interaction.response.send_message(
                    replace_emojis("✅ Драфт завершён!"),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
        else:
            try:
                await interaction.response.defer()
            except discord.NotFound:
                pass


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
            self._warning = replace_emojis(f"⚠️ Показано 25 из {len(available_players)} игроков")


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
