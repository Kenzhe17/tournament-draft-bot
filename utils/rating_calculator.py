"""Utility functions for calculating player ratings based on match performance."""

from typing import List, Tuple
from models.player_stats import PlayerStats


def calculate_team_position(
    players: List[Tuple[str, int, int, int]],  # (player_name, kills, deaths, current_elo)
) -> List[Tuple[str, int]]:
    """
    Calculate player positions within a team based on K/D and ELO.
    
    Args:
        players: List of (player_name, kills, deaths, current_elo)
    
    Returns:
        List of (player_name, position) where position is 1-based (1 = best)
    
    Priority:
    1. More kills = higher position
    2. If kills equal, fewer deaths = higher position
    3. If kills and deaths equal, higher current ELO = higher position
    """
    # Sort players by priority: kills (desc), deaths (asc), elo (desc)
    sorted_players = sorted(
        players,
        key=lambda x: (-x[1], x[2], -x[3])
    )
    
    # Assign positions (1-based)
    result = []
    for i, (player_name, _, _, _) in enumerate(sorted_players):
        result.append((player_name, i + 1))
    
    return result


def get_base_elo_change(
    position: int,  # 1-4 within team
    team_won: bool
) -> int:
    """
    Calculate base ELO change based on position and team result.
    Same for all circles (placement-based system).

    Args:
        position: Position within team (1-4, 1 = best)
        team_won: Whether the player's team won the match

    Returns:
        Base ELO change
    """
    # Define base ELO changes for each scenario
    # Format: {team_won: {position: elo_change}}

    if team_won:
        # Team won
        base_changes = {
            1: 12,   # 1st place
            2: 10,   # 2nd place
            3: 8,    # 3rd place
            4: 6,    # 4th place
        }
    else:
        # Team lost
        base_changes = {
            1: -2,   # 1st place
            2: -4,   # 2nd place
            3: -6,   # 3rd place
            4: -8,   # 4th place
        }

    return base_changes.get(position, 0)


def calculate_kd_bonus(kills: int, deaths: int) -> int:
    """
    Calculate individual K/D bonus.

    Formula: (Kills × 2) - (Deaths × 1)

    Args:
        kills: Number of kills
        deaths: Number of deaths

    Returns:
        K/D bonus ELO change
    """
    return (kills * 2) - deaths


def calculate_total_elo_change(
    position: int,
    team_won: bool,
    kills: int,
    deaths: int
) -> int:
    """
    Calculate total ELO change for a player (legacy version).

    Args:
        position: Position within team (1-4, 1 = best)
        team_won: Whether the player's team won the match
        kills: Number of kills
        deaths: Number of deaths

    Returns:
        Total ELO change
    """
    base_change = get_base_elo_change(position, team_won)
    kd_bonus = calculate_kd_bonus(kills, deaths)

    return base_change + kd_bonus


def get_elo_multiplier(current_elo: int) -> float:
    """
    Get ELO multiplier based on current rating (boost for weak players, reduce for top players).

    Args:
        current_elo: Current player ELO

    Returns:
        Multiplier (1.0 = normal, >1.0 = boosted, <1.0 = reduced)
    """
    if current_elo < 1200:
        return 1.5  # 150% for weak players
    elif current_elo < 1400:
        return 1.3  # 130%
    elif current_elo < 1500:
        return 1.1  # 110%
    elif current_elo < 1600:
        return 0.7  # 70% for top players
    elif current_elo < 1700:
        return 0.6  # 60%
    elif current_elo < 1800:
        return 0.5  # 50%
    else:
        return 0.4  # 40% for very top players


def get_loss_reduction(current_elo: int) -> float:
    """
    Get loss multiplier for players (more penalty for top players).

    Args:
        current_elo: Current player ELO

    Returns:
        Loss multiplier (1.0 = normal, >1.0 = more penalty, <1.0 = reduced penalty)
    """
    if current_elo < 1100:
        return 0.5  # Only 50% penalty for very weak players
    elif current_elo < 1300:
        return 1.0  # Normal penalty
    elif current_elo < 1500:
        return 1.5  # 150% penalty
    elif current_elo < 1700:
        return 2.0  # 200% penalty
    else:
        return 2.5  # 250% penalty for very top players


def calculate_personal_bonus(
    current_kills: int,
    current_deaths: int,
    avg_kills: float,
    avg_deaths: float
) -> int:
    """
    Calculate personal bonus for playing better than average.

    Args:
        current_kills: Kills in current match
        current_deaths: Deaths in current match
        avg_kills: Player's average kills per game
        avg_deaths: Player's average deaths per game

    Returns:
        Personal bonus ELO change (only positive)
    """
    if avg_kills <= 0:
        return 0  # No average data yet

    kills_improvement = current_kills - avg_kills
    deaths_improvement = avg_deaths - current_deaths

    # Bonus for improvement relative to average
    bonus = (kills_improvement * 2) + deaths_improvement
    return max(0, bonus)  # Only positive bonuses


def calculate_catch_up_bonus(current_elo: int, avg_server_elo: float) -> int:
    """
    Calculate catch-up bonus for players falling behind server average.

    Args:
        current_elo: Current player ELO
        avg_server_elo: Average ELO of all players on server

    Returns:
        Catch-up bonus ELO change
    """
    if avg_server_elo <= 0:
        return 0  # No server data yet

    elo_diff = avg_server_elo - current_elo

    if elo_diff > 300:
        return 5  # +5 ELO for players far behind
    elif elo_diff > 200:
        return 3  # +3 ELO
    elif elo_diff > 100:
        return 1  # +1 ELO
    return 0


def calculate_lobby_deviation_multiplier(player_elo: int, lobby_avg_elo: float) -> float:
    """
    Calculate ELO multiplier based on deviation from lobby average.

    Protects weaker players from farming by strong players.

    Args:
        player_elo: Current player ELO
        lobby_avg_elo: Average ELO of all players in the lobby

    Returns:
        Multiplier (1.0 = normal, <1.0 = reduced gain for high elo, >1.0 = boosted gain for low elo)
    """
    if lobby_avg_elo == 0:
        return 1.0

    deviation = (player_elo - lobby_avg_elo) / lobby_avg_elo

    if deviation > 0:
        # Player is above average - reduce gain
        multiplier = 1.0 - deviation
        return max(0.5, multiplier)  # Minimum 50% gain
    else:
        # Player is below average - boost gain
        multiplier = 1.0 + abs(deviation)
        return min(1.5, multiplier)  # Maximum 150% gain


def calculate_balanced_elo_change(
    position: int,
    team_won: bool,
    kills: int,
    deaths: int,
    current_elo: int = 1000,
    avg_kills: float = 0.0,
    avg_deaths: float = 0.0,
    avg_server_elo: float = 0.0,
    lobby_avg_elo: float = 0.0
) -> int:
    """
    Calculate balanced ELO change with multipliers and bonuses for weak players.

    Args:
        position: Position within team (1-4, 1 = best)
        team_won: Whether the player's team won the match
        kills: Number of kills
        deaths: Number of deaths
        current_elo: Current player ELO (default 1000)
        avg_kills: Player's average kills per game (default 0)
        avg_deaths: Player's average deaths per game (default 0)
        avg_server_elo: Average ELO of all players on server (default 0)
        lobby_avg_elo: Average ELO of all players in the lobby (default 0)

    Returns:
        Total balanced ELO change
    """
    # Calculate base change
    base_change = get_base_elo_change(position, team_won)

    # Add K/D bonus only for players <= 1500 ELO
    if current_elo <= 1500:
        kd_bonus = calculate_kd_bonus(kills, deaths)
    else:
        kd_bonus = 0

    total_base = base_change + kd_bonus

    # Apply ELO multiplier only for wins (reduce gain for top players)
    if team_won:
        elo_multiplier = get_elo_multiplier(current_elo)
        adjusted_change = int(total_base * elo_multiplier)
    else:
        # For losses, use normal multiplier (loss will be multiplied by loss_reduction later)
        adjusted_change = total_base

    # Apply lobby deviation multiplier (prevent farming on weaker players)
    lobby_multiplier = calculate_lobby_deviation_multiplier(current_elo, lobby_avg_elo)
    adjusted_change = int(adjusted_change * lobby_multiplier)

    # Apply loss reduction for weak players
    if adjusted_change < 0:
        loss_reduction = get_loss_reduction(current_elo)
        adjusted_change = int(adjusted_change * loss_reduction)

    # Add personal bonus for playing better than average (only for players <= 1500 ELO)
    if avg_kills > 0 and current_elo <= 1500:
        personal_bonus = calculate_personal_bonus(kills, deaths, avg_kills, avg_deaths)
        adjusted_change += personal_bonus

    # Add catch-up bonus for players behind server average (only for players <= 1500 ELO)
    if current_elo <= 1500:
        catch_up_bonus = calculate_catch_up_bonus(current_elo, avg_server_elo)
        adjusted_change += catch_up_bonus

    # Ensure final result is int
    return int(adjusted_change)


async def get_server_average_elo(guild_id: int) -> float:
    """
    Calculate average ELO for all players on a server.

    Args:
        guild_id: Discord guild ID

    Returns:
        Average ELO (0 if no players found)
    """
    from storage.player_stats_store import player_stats_store

    all_stats = await player_stats_store.get_all(guild_id)

    if not all_stats:
        return 0.0

    total_elo = sum(stats.elo for stats in all_stats)
    return int(total_elo / len(all_stats))


def update_player_stats_from_match(
    stats: PlayerStats,
    kills: int,
    deaths: int,
    elo_change: int,
    team_won: bool
) -> PlayerStats:
    """
    Update player stats after a match.
    
    Args:
        stats: Current player stats
        kills: Kills in this match
        deaths: Deaths in this match
        elo_change: ELO change from this match
        team_won: Whether the player's team won
    
    Returns:
        Updated player stats
    """
    # Update total kills and deaths
    stats.total_kills += kills
    stats.total_deaths += deaths
    
    # Update best match kills
    if kills > stats.best_match_kills:
        stats.best_match_kills = kills

    # Update best win streak
    if stats.current_streak > stats.best_win_streak:
        stats.best_win_streak = stats.current_streak

    # Check for win streak bonus (3 wins = +50 coins)
    if stats.current_streak >= 3 and stats.current_streak % 3 == 0:
        stats.streak_bonus_coins = getattr(stats, 'streak_bonus_coins', 0) + 50
    
    # Update total ELO change
    stats.total_elo_change += elo_change
    
    # Update last ELO change
    stats.last_elo_change = elo_change
    
    # Update current ELO
    stats.elo += elo_change
    
    # Update wins/finals
    if team_won:
        stats.wins += 1
    
    # Update streak
    if team_won:
        if stats.current_streak > 0:
            stats.current_streak += 1
        else:
            stats.current_streak = 1
        if stats.current_streak > stats.best_win_streak:
            stats.best_win_streak = stats.current_streak
    else:
        if stats.current_streak < 0:
            stats.current_streak -= 1
        else:
            stats.current_streak = -1
        if abs(stats.current_streak) > stats.best_loss_streak:
            stats.best_loss_streak = abs(stats.current_streak)

    return stats
