"""View for Ultimatum Solo Tournament matches."""

import logging

import discord
from discord.ext import commands

from config import replace_emojis, get_emoji
from ultimatum.models import MatchStatus, MatchType
from ultimatum.store import ultimatum_store
from ultimatum.tournament import UltimatumTournament
from views.ultimatum_room_modal import UltimatumRoomModal

logger = logging.getLogger(__name__)


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
        title=f"{replace_emojis('a_star')} УЛЬТИМАТУМ СОЛО | Раунд {round_number}",
        description=f"{replace_emojis('white_dot')} Матчи раунда",
        color=discord.Color.gold()
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

        status_emoji = {
            MatchStatus.PENDING: "⏳",
            MatchStatus.IN_PROGRESS: "🎮",
            MatchStatus.COMPLETED: "✅"
        }.get(match.status, "❓")

        match_type = "Гранд-финал" if match.match_type == MatchType.GRAND_FINAL else "Обычный"

        field_value = f"{replace_emojis('white_arrow')} {p1_name} vs {p2_name}\n"
        field_value += f"{replace_emojis('sub_middle')} Статус: {status_emoji} {match.status.value}\n"
        field_value += f"{replace_emojis('sub_middle')} Тип: {match_type}"

        if match.room_id:
            field_value += f"\n{replace_emojis('sub_middle')} Комната: {match.room_id}"

        embed.add_field(
            name=f"Матч #{match.id}",
            value=field_value,
            inline=False
        )

    return embed


async def build_ultimatum_round_view(bot: commands.Bot, tournament_id: str, round_number: int) -> discord.ui.View:
    """Build view for Ultimatum round with all match controls."""
    view = discord.ui.View(timeout=None)

    # Get matches
    matches = await ultimatum_store.get_matches(tournament_id, round_number)

    # For each match, add controls
    for match in matches:
        match_view = UltimatumMatchView(bot, tournament_id, match.id, round_number)
        match_view.refresh_items(match)
        # Add match view items to main view
        for item in match_view.children:
            view.add_item(item)

    return view
