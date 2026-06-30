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
    circle: int,
    position: int,  # 1-4 within team
    team_won: bool
) -> int:
    """
    Calculate base ELO change based on circle, position, and team result.
    
    Args:
        circle: Circle where player was drafted (1-4)
        position: Position within team (1-4, 1 = best)
        team_won: Whether the player's team won the match
    
    Returns:
        Base ELO change
    """
    # Define base ELO changes for each scenario
    # Format: {circle: {team_won: {position: elo_change}}}
    
    if team_won:
        # Team won
        base_changes = {
            1: {1: 5, 2: 0, 3: -5, 4: -10},   # Circle 1 (captains)
            2: {1: 10, 2: 5, 3: 0, 4: -5},    # Circle 2
            3: {1: 15, 2: 10, 3: 5, 4: 0},    # Circle 3
            4: {1: 20, 2: 15, 3: 10, 4: 5},   # Circle 4
        }
    else:
        # Team lost
        base_changes = {
            1: {1: -5, 2: -10, 3: -15, 4: -20},  # Circle 1 (captains)
            2: {1: 0, 2: -5, 3: -10, 4: -15},     # Circle 2
            3: {1: 5, 2: 0, 3: -5, 4: -10},       # Circle 3
            4: {1: 10, 2: 5, 3: 0, 4: -5},        # Circle 4
        }
    
    return base_changes.get(circle, {}).get(position, 0)


def calculate_kd_bonus(kills: int, deaths: int) -> int:
    """
    Calculate individual K/D bonus.
    
    Formula: (Kills × 2) - Deaths
    
    Args:
        kills: Number of kills
        deaths: Number of deaths
    
    Returns:
        K/D bonus ELO change
    """
    return (kills * 2) - deaths


def calculate_total_elo_change(
    circle: int,
    position: int,
    team_won: bool,
    kills: int,
    deaths: int
) -> int:
    """
    Calculate total ELO change for a player.
    
    Args:
        circle: Circle where player was drafted (1-4)
        position: Position within team (1-4, 1 = best)
        team_won: Whether the player's team won the match
        kills: Number of kills
        deaths: Number of deaths
    
    Returns:
        Total ELO change
    """
    base_change = get_base_elo_change(circle, position, team_won)
    kd_bonus = calculate_kd_bonus(kills, deaths)
    
    return base_change + kd_bonus


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
