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
    Calculate total ELO change for a player.

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
