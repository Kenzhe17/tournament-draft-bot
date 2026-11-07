
"""View for managing K/D input for match teams."""

from __future__ import annotations

import logging

from config import replace_emojis, get_emoji

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from models.tournament import Tournament

logger = logging.getLogger(__name__)


class KDInputView(discord.ui.View):
    """View for inputting K/D statistics for both teams in a match."""

    def __init__(self, guild_id: int, channel_id: int, tournament: Tournament, match_info: dict):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.match_info = match_info

        # Add buttons for each team
        team1_name = match_info.get('team1_name', 'Team 1')
        team2_name = match_info.get('team2_name', 'Team 2')
        
        self.add_item(TeamKDButton(guild_id, channel_id, tournament, match_info, 0, team1_name))
        self.add_item(TeamKDButton(guild_id, channel_id, tournament, match_info, 1, team2_name))
        self.add_item(ProcessMatchButton(guild_id, channel_id, tournament, match_info))


class TeamKDButton(discord.ui.Button):
    """Button to open K/D input modal for a team."""

    def __init__(self, guild_id: int, channel_id: int, tournament: Tournament, match_info: dict, team_number: int, team_name: str):
        super().__init__(
            label=f"📊 {team_name} K/D",
            style=discord.ButtonStyle.primary,
            custom_id=f"team_kd:{guild_id}:{channel_id}:{team_number}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
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
        modal = TeamKDInputModal(self.guild_id, self.channel_id, self.team_number, self.team_name, players)
        
        # Store the view reference in the modal so it can update buttons
        view = self.view
        if isinstance(view, KDInputView):
            modal.parent_view = view
            modal.interaction = interaction  # Store interaction for editing
        
        await interaction.response.send_modal(modal)


class ProcessMatchButton(discord.ui.Button):
    """Button to process the match result after K/D is entered."""

    def __init__(self, guild_id: int, channel_id: int, tournament: Tournament, match_info: dict):
        super().__init__(
            label="✅ Обработать результат",
            style=discord.ButtonStyle.success,
            custom_id=f"process_match:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.match_info = match_info

    async def callback(self, interaction: discord.Interaction) -> None:
        # Get fresh tournament data with K/D
        from storage.json_store import store
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."),
                ephemeral=True
            )
            return

        # Get match stats from temp_match_stats
        match_id = f"{self.tournament.id}_{self.match_info.get('match_type', 'unknown')}_{self.match_info.get('match_index', 0)}"
        temp_stats = tournament.temp_match_stats.get(match_id, {})

        if not temp_stats:
            await interaction.response.send_message(
                replace_emojis("❌ Нет данных статистики для обработки. Сначала заполните статистику для обеих команд."),
                ephemeral=True
            )
            return

        # Check if we have stats for all players
        team1_players = self.match_info.get('team1_players', [])
        team2_players = self.match_info.get('team2_players', [])
        all_players = [p[0] for p in team1_players + team2_players]

        missing_players = [p for p in all_players if p not in temp_stats]
        if missing_players:
            await interaction.response.send_message(
                replace_emojis(f"❌ Отсутствует статистика для: {', '.join(missing_players)}"),
                ephemeral=True
            )
            return

        await interaction.response.defer()

        # Update match_info with stats data from tournament
        self.match_info['temp_kd_data'] = temp_stats

        # Log stats filling
        from utils.logging import log_stats_filled
        match_type = self.match_info.get('match_type', 'unknown')
        match_index = self.match_info.get('match_index', 0)

        # Build stats summary
        stats_summary = ""
        for player, kd in temp_stats.items():
            stats_summary += f"{get_emoji('white_dot')} {player}: {kd.get('kills', 0)}/{kd.get('deaths', 0)}\n"

        await log_stats_filled(interaction.client, interaction.guild, interaction.user, match_type, match_index, stats_summary)

        # Process the match result with new rating system
        await process_match_result(self.guild_id, tournament, self.match_info, interaction)

        # Update the tournament message
        from bot import TournamentBot
        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.followup.send(
            replace_emojis("✅ Результат матча обработан!"),
            ephemeral=True
        )


async def process_match_result(guild_id: int, tournament: Tournament, match_info: dict, interaction: discord.Interaction = None) -> None:
    """Process match result — новая ELO-система (FACEIT-style).

    Начисляет ELO один раз на матч (processed_elo_matches), XP и монеты.
    """
    from storage.player_stats_store import player_stats_store
    from storage.json_store import store
    from models.player_stats import PlayerStats
    from utils.rating_calculator import (
        compute_match_elo_changes,
        update_player_stats_from_match,
        get_leaderboard_rank,
    )

    match_type = match_info.get('match_type')
    match_index = match_info.get('match_index')
    winning_team_index = match_info.get('winning_team_index')
    team1_index = match_info.get('team1_index')
    team2_index = match_info.get('team2_index')

    match_id = f"{tournament.id}_{match_type}_{match_index}"

    # Idempotency: один подтверждённый матч начисляет ELO только один раз
    if match_id in tournament.processed_elo_matches:
        logger.warning("ELO already processed for match %s — skipping", match_id)
        return
    tournament.processed_elo_matches.add(match_id)

    # Get K/D data
    temp_kd_data = match_info.get('temp_kd_data', {})

    # ---- Сбор данных ВСЕХ игроков матча до любых записей ----
    # team_key -> won; stats_by_name; SR команды по всему составу (roster)
    stats_by_name: dict[str, PlayerStats] = {}
    user_id_by_name: dict[str, int] = {}
    team_sr_values: dict[str, float] = {}
    match_players: list[dict] = []

    for team_index, team_key in ((team1_index, "a"), (team2_index, "b")):
        won = team_index == winning_team_index
        team_data = tournament.teams[team_index] if team_index < len(tournament.teams) else {}

        if tournament.formation_mode == "random-auto":
            member_names = [n for n in team_data.get("players", []) if n]
        else:
            member_names = [
                team_data.get(f"circle{c}") for c in range(1, 5)
            ]
            member_names = [n for n in member_names if n]

        sr_values = []
        for name in member_names:
            user_id = tournament.player_user_ids.get(name, 0)
            user_id_by_name[name] = user_id
            stats = await player_stats_store.get(guild_id, user_id)
            if stats is None:
                stats = PlayerStats(
                    guild_id=guild_id, user_id=user_id, name=name, elo=1000
                )
            stats_by_name[name] = stats
            sr_values.append(stats.skill_rating)

            kd = temp_kd_data.get(name)
            if kd is None:
                continue  # Нет введённой статистики — ELO не начисляем

            match_players.append({
                "name": name,
                "team": team_key,
                "won": won,
                "kills": kd.get('kills', 0),
                "deaths": kd.get('deaths', 0),
                "assists": kd.get('assists', 0),
                "damage": kd.get('damage', 0),
                "skill_rating": stats.skill_rating,
            })

        if sr_values:
            team_sr_values[team_key] = sum(sr_values) / len(sr_values)

    # Ранги ELO-лидерборда — строго ДО начисления за этот матч
    for p in match_players:
        p["elo_rank"] = await get_leaderboard_rank(
            guild_id, user_id_by_name.get(p["name"], 0)
        )

    # ---- Расчёт ELO по новой системе ----
    elo_results = compute_match_elo_changes(match_players, team_sr_values)

    # ---- Применение: статистика → баланс ----
    for p in match_players:
        name = p["name"]
        user_id = user_id_by_name.get(name, 0)
        stats = stats_by_name[name]
        won = p["won"]
        result = elo_results[name]
        elo_change = result["elo_change"]

        # Update stats
        updated_stats = update_player_stats_from_match(
            stats=stats,
            kills=p["kills"],
            deaths=p["deaths"],
            elo_change=elo_change,
            team_won=won,
            assists=p.get("assists", 0),
            damage=p.get("damage", 0),
        )

        # Pet ELO bonus removed — pet ability no longer affects ELO
        updated_stats.last_pet_elo_bonus = 0

        # Increment games count for this match
        updated_stats.games += 1

        # Add XP rewards
        xp_reward = 30  # Base XP for participation
        if won:
            xp_reward += 30  # Bonus XP for winning
        new_level, old_level = updated_stats.add_xp(xp_reward)

        # Add coin rewards (50-100 per match)
        import random
        coin_reward = random.randint(50, 100)
        if won:
            coin_reward += 50  # Bonus coins for winning
        updated_stats.total_earnings += coin_reward

        # Add coins to user balance
        from storage.user_balance_store import user_balance_store
        await user_balance_store.add_balance(guild_id, user_id, coin_reward)

        logger.info(
            f"Saving stats for {name}: elo={updated_stats.elo}, elo_change={elo_change}, "
            f"games={updated_stats.games}, coins={coin_reward}"
        )

        await player_stats_store.set(updated_stats)

    # Clear temporary data
    if hasattr(tournament, 'temp_kd_data'):
        del tournament.temp_kd_data
    if hasattr(tournament, 'pending_match_result'):
        del tournament.pending_match_result

    # Copy stats to temp_match_stats for winner embed display
    if match_id not in tournament.temp_match_stats:
        tournament.temp_match_stats[match_id] = {}
    # Remove circle from stats for cleaner storage
    for player, kd in temp_kd_data.items():
        tournament.temp_match_stats[match_id][player] = {
            "kills": kd.get("kills", 0),
            "deaths": kd.get("deaths", 0),
            "assists": kd.get("assists", 0),
            "damage": kd.get("damage", 0),
        }
    store.set(tournament)
