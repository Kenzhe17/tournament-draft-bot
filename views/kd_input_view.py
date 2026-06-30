"""View for managing K/D input for match teams."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from models.tournament import Tournament


class KDInputView(discord.ui.View):
    """View for inputting K/D statistics for both teams in a match."""

    def __init__(self, guild_id: int, tournament: Tournament, match_info: dict):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_info = match_info

        # Add buttons for each team
        team1_name = match_info.get('team1_name', 'Team 1')
        team2_name = match_info.get('team2_name', 'Team 2')
        
        self.add_item(TeamKDButton(guild_id, tournament, match_info, 0, team1_name))
        self.add_item(TeamKDButton(guild_id, tournament, match_info, 1, team2_name))
        self.add_item(ProcessMatchButton(guild_id, tournament, match_info))


class TeamKDButton(discord.ui.Button):
    """Button to open K/D input modal for a team."""

    def __init__(self, guild_id: int, tournament: Tournament, match_info: dict, team_number: int, team_name: str):
        super().__init__(
            label=f"📊 {team_name} K/D",
            style=discord.ButtonStyle.primary,
            custom_id=f"team_kd:{guild_id}:{team_number}"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_info = match_info
        self.team_number = team_number  # 0 or 1
        self.team_name = team_name

    async def callback(self, interaction: discord.Interaction) -> None:
        # Get players for this team
        if self.team_number == 0:
            players = self.match_info.get('team1_players', [])
        else:
            players = self.match_info.get('team2_players', [])

        from views.kd_input_modal import TeamKDInputModal
        modal = TeamKDInputModal(self.guild_id, self.team_number, self.team_name, players, self.tournament)

        # Store the view reference in the modal so it can update buttons
        view = self.view
        if isinstance(view, KDInputView):
            modal.parent_view = view
            modal.interaction = interaction  # Store interaction for editing

        await interaction.response.send_modal(modal)


class ProcessMatchButton(discord.ui.Button):
    """Button to process the match result after K/D is entered."""

    def __init__(self, guild_id: int, tournament: Tournament, match_info: dict):
        super().__init__(
            label="✅ Обработать результат",
            style=discord.ButtonStyle.success,
            custom_id=f"process_match:{guild_id}",
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_info = match_info

    async def callback(self, interaction: discord.Interaction) -> None:
        # Get fresh tournament data with K/D
        from storage.json_store import store
        tournament = store.get(self.guild_id)
        if not tournament or not hasattr(tournament, 'temp_kd_data') or not tournament.temp_kd_data:
            await interaction.response.send_message(
                "❌ Нет данных K/D для обработки. Сначала введите статистику для обеих команд.",
                ephemeral=True
            )
            return

        # Check if we have K/D for all players
        team1_players = self.match_info.get('team1_players', [])
        team2_players = self.match_info.get('team2_players', [])
        all_players = [p[0] for p in team1_players + team2_players]
        
        missing_players = [p for p in all_players if p not in tournament.temp_kd_data]
        if missing_players:
            await interaction.response.send_message(
                f"❌ Отсутствует статистика для: {', '.join(missing_players)}",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        # Update match_info with K/D data from tournament
        self.match_info['temp_kd_data'] = tournament.temp_kd_data

        # Process the match result with new rating system
        await process_match_result(self.guild_id, tournament, self.match_info)

        # Update the tournament message
        from bot import TournamentBot
        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.followup.send(
            "✅ Результат матча обработан!",
            ephemeral=True
        )


async def process_match_result(guild_id: int, tournament: Tournament, match_info: dict) -> None:
    """Process match result with new rating system."""
    from storage.player_stats_store import player_stats_store
    from utils.rating_calculator import (
        calculate_team_position,
        calculate_total_elo_change,
        update_player_stats_from_match,
    )

    match_type = match_info.get('match_type')
    match_index = match_info.get('match_index')
    winning_team_index = match_info.get('winning_team_index')
    team1_index = match_info.get('team1_index')
    team2_index = match_info.get('team2_index')

    # Get K/D data
    temp_kd_data = match_info.get('temp_kd_data', {})

    # Process each team
    for team_index in [team1_index, team2_index]:
        team_won = (team_index == winning_team_index)
        team_data = tournament.teams[team_index] if team_index < len(tournament.teams) else {}

        # Collect player data for position calculation
        players_for_position = []
        
        for circle in range(1, 5):
            player = team_data.get(f"circle{circle}")
            if player and player in temp_kd_data:
                kd = temp_kd_data[player]
                kills = kd.get('kills', 0)
                deaths = kd.get('deaths', 0)
                
                # Get current ELO
                user_id = tournament.player_user_ids.get(player, 0)
                stats = await player_stats_store.get(guild_id, user_id)
                current_elo = stats.elo if stats else 1000
                
                players_for_position.append((player, kills, deaths, current_elo))

        # Calculate positions within team
        positions = calculate_team_position(players_for_position)
        position_map = {name: pos for name, pos in positions}

        # Update each player's stats
        for player_name, kills, deaths, current_elo in players_for_position:
            position = position_map.get(player_name, 1)
            circle = temp_kd_data[player_name].get('circle', 1)
            
            # Calculate ELO change
            elo_change = calculate_total_elo_change(
                circle=circle,
                position=position,
                team_won=team_won,
                kills=kills,
                deaths=deaths
            )

            # Get current stats
            user_id = tournament.player_user_ids.get(player_name, 0)
            stats = await player_stats_store.get(guild_id, user_id)
            if not stats:
                from models.player_stats import PlayerStats
                stats = PlayerStats(
                    guild_id=guild_id,
                    user_id=user_id,
                    name=player_name,
                    elo=1000
                )

            # Update stats
            updated_stats = update_player_stats_from_match(
                stats=stats,
                kills=kills,
                deaths=deaths,
                elo_change=elo_change,
                team_won=team_won
            )

            # Increment games count for this match
            updated_stats.games += 1

            await player_stats_store.set(updated_stats)

    # Set the match winner based on match type
    if match_type == "qualifier":
        tournament.set_qualifier_winner(match_index, winning_team_index)
    elif match_type == "semifinal":
        tournament.set_semifinal_winner(match_index, winning_team_index)
    elif match_type == "final":
        tournament.final_winner = winning_team_index
        tournament.winner_team_index = winning_team_index
        tournament.phase = tournament.phase.__class__.COMPLETE

    # Clear temporary data
    if hasattr(tournament, 'temp_kd_data'):
        del tournament.temp_kd_data
    if hasattr(tournament, 'pending_match_result'):
        del tournament.pending_match_result

    # Store tournament
    from storage.json_store import store
    store.set(tournament)
