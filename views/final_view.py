"""View для финала — кнопки победителя."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from models.tournament import TournamentPhase
from storage.json_store import store
from storage.player_stats_store import player_stats_store
from utils.embeds import build_embed_for_phase
from utils.permissions import is_admin_check

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class FinalWinnerButton(discord.ui.Button):
    """Кнопка выбора победителя финала."""

    def __init__(self, guild_id: int, team_index: int, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.danger,
            custom_id=f"final_win:{guild_id}:{team_index}",
        )
        self.guild_id = guild_id
        self.team_index = team_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только администраторы могут фиксировать результаты.",
                ephemeral=True,
            )
            return

        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.FINAL:
            await interaction.response.send_message(
                "❌ Финал не активен.", ephemeral=True
            )
            return

        if self.team_index not in tournament.final_teams:
            await interaction.response.send_message(
                "❌ Неверная команда.", ephemeral=True
            )
            return

        tournament.set_final_winner(self.team_index)
        store.set(tournament)

        # Update player statistics with ELO based on tournament size
        winning_team = tournament.teams[self.team_index]
        losing_team_index = tournament.final_teams[1] if tournament.final_teams[0] == self.team_index else tournament.final_teams[0]
        losing_team = tournament.teams[losing_team_index]

        # Determine result type based on tournament size
        from models.tournament import TournamentSize

        if tournament.size == TournamentSize.EIGHT:
            # 8 players: direct final
            winner_result = "final_win"  # +25
            loser_result = "final_loss"  # -25
        elif tournament.size == TournamentSize.SIXTEEN:
            # 16 players: semifinals + final
            winner_result = "semifinal_win_final_win"  # +50
            loser_result = "semifinal_win_final_loss"  # +25
        else:
            # 32 players: qualifiers + semifinals + final
            winner_result = "qualifier_win_semifinal_win_final_win"  # +100
            loser_result = "qualifier_win_semifinal_win_final_loss"  # +50

        # Update winning team stats
        from storage.user_balance_store import user_balance_store
        for circle in range(1, 5):
            player = winning_team.get(f"circle{circle}")
            if player:
                user_id = tournament.player_user_ids.get(player, 0)
                await player_stats_store.update_player(tournament.guild_id, user_id, player, result=winner_result, count_game=False)
                # Give tournament win reward
                await user_balance_store.add_balance(tournament.guild_id, user_id, 50)

        # Update losing team stats
        for circle in range(1, 5):
            player = losing_team.get(f"circle{circle}")
            if player:
                user_id = tournament.player_user_ids.get(player, 0)
                await player_stats_store.update_player(tournament.guild_id, user_id, player, result=loser_result, count_game=False)

        # Resolve betting for final match
        from storage.bets_store import bets_store
        from storage.betting_stats_store import betting_stats_store
        from storage.user_balance_store import user_balance_store

        payouts = await bets_store.resolve_match_bets(
            tournament.guild_id,
            str(tournament.guild_id),  # Use guild_id as tournament_id
            "final",
            0,  # Final has only one match (index 0)
            self.team_index
        )

        # Pay out winners and update statistics
        for user_id, payout in payouts.items():
            await user_balance_store.add_balance(tournament.guild_id, user_id, payout)
            await betting_stats_store.record_bet_result(tournament.guild_id, user_id, payout, won=True)

        # Update statistics for losers
        all_bets = await bets_store.get_match_bets(tournament.guild_id, str(tournament.guild_id), "final", 0)
        losing_bets = [b for b in all_bets if b.team_index != self.team_index]
        for bet in losing_bets:
            await betting_stats_store.record_bet_result(tournament.guild_id, bet.user_id, bet.amount, won=False)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)
        await interaction.response.defer()


class FinalView(discord.ui.View):
    """View с кнопками победителей финала."""

    def __init__(self, guild_id: int, final_teams: list[int], tournament):
        super().__init__(timeout=None)
        for team_idx in final_teams:
            # Get team name or default to captain name
            team_data = tournament.teams[team_idx] if team_idx < len(tournament.teams) else {}
            captain = team_data.get("captain", f"П{team_idx + 1}")
            team_name = tournament.team_names.get(team_idx, captain)

            self.add_item(
                FinalWinnerButton(
                    guild_id,
                    team_idx,
                    label=f"{team_name} победил",
                )
            )
        # Add betting button for final
        from views.betting_view import OpenBettingButton
        self.add_item(OpenBettingButton(guild_id, tournament, "final", 0))
