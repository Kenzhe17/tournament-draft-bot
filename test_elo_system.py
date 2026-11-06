"""Test script for the new balanced ELO system."""

from utils.rating_calculator import (
    calculate_balanced_elo_change,
    get_elo_multiplier,
    get_loss_reduction,
    calculate_personal_bonus,
    calculate_catch_up_bonus,
)

def test_elo_multipliers():
    """Test ELO multipliers for different rating levels."""
    print("=== Testing ELO Multipliers ===")
    test_cases = [
        (1000, "Very weak player"),
        (1100, "Weak player"),
        (1300, "Below average"),
        (1500, "Average player"),
        (1700, "Above average"),
        (1900, "Top player"),
        (2000, "Very top player"),
    ]

    for elo, description in test_cases:
        multiplier = get_elo_multiplier(elo)
        print(f"{description} ({elo} ELO): {multiplier}x")

def test_loss_reduction():
    """Test loss reduction for weak players."""
    print("\n=== Testing Loss Reduction ===")
    test_cases = [
        (1000, "Very weak player"),
        (1100, "Weak player"),
        (1300, "Average player"),
        (1500, "Above average"),
    ]

    for elo, description in test_cases:
        reduction = get_loss_reduction(elo)
        print(f"{description} ({elo} ELO): {reduction}x penalty")

def test_personal_bonus():
    """Test personal bonus for improvement."""
    print("\n=== Testing Personal Bonus ===")
    test_cases = [
        (8, 4, 3, 6, "Good improvement"),
        (6, 5, 6, 5, "Average performance"),
        (10, 3, 5, 4, "Excellent improvement"),
        (2, 8, 5, 4, "Poor performance"),
    ]

    for kills, deaths, avg_kills, avg_deaths, description in test_cases:
        bonus = calculate_personal_bonus(kills, deaths, avg_kills, avg_deaths)
        print(f"{description} ({kills}/{deaths} vs avg {avg_kills}/{avg_deaths}): +{bonus} ELO")

def test_catch_up_bonus():
    """Test catch-up bonus for players behind server average."""
    print("\n=== Testing Catch-up Bonus ===")
    avg_server_elo = 1500
    test_cases = [
        (1000, "Far behind"),
        (1200, "Behind"),
        (1400, "Slightly behind"),
        (1500, "At average"),
        (1600, "Above average"),
    ]

    for current_elo, description in test_cases:
        bonus = calculate_catch_up_bonus(current_elo, avg_server_elo)
        print(f"{description} ({current_elo} vs avg {avg_server_elo}): +{bonus} ELO")

def test_balanced_elo_change():
    """Test the complete balanced ELO calculation."""
    print("\n=== Testing Complete Balanced ELO Change ===")
    avg_server_elo = 1500

    test_cases = [
        # Weak player, good performance, win
        {
            "description": "Weak player (1000 ELO) wins with 8/4",
            "position": 2,
            "team_won": True,
            "kills": 8,
            "deaths": 4,
            "current_elo": 1000,
            "avg_kills": 3,
            "avg_deaths": 6,
        },
        # Average player, normal performance, win
        {
            "description": "Average player (1500 ELO) wins with 6/5",
            "position": 2,
            "team_won": True,
            "kills": 6,
            "deaths": 5,
            "current_elo": 1500,
            "avg_kills": 6,
            "avg_deaths": 5,
        },
        # Top player, good performance, win
        {
            "description": "Top player (2000 ELO) wins with 10/4",
            "position": 1,
            "team_won": True,
            "kills": 10,
            "deaths": 4,
            "current_elo": 2000,
            "avg_kills": 10,
            "avg_deaths": 4,
        },
        # Weak player, poor performance, loss
        {
            "description": "Weak player (1000 ELO) loses with 3/8",
            "position": 3,
            "team_won": False,
            "kills": 3,
            "deaths": 8,
            "current_elo": 1000,
            "avg_kills": 3,
            "avg_deaths": 6,
        },
        # Average player, poor performance, loss
        {
            "description": "Average player (1500 ELO) loses with 4/7",
            "position": 3,
            "team_won": False,
            "kills": 4,
            "deaths": 7,
            "current_elo": 1500,
            "avg_kills": 6,
            "avg_deaths": 5,
        },
    ]

    for case in test_cases:
        elo_change = calculate_balanced_elo_change(
            position=case["position"],
            team_won=case["team_won"],
            kills=case["kills"],
            deaths=case["deaths"],
            current_elo=case["current_elo"],
            avg_kills=case["avg_kills"],
            avg_deaths=case["avg_deaths"],
            avg_server_elo=avg_server_elo
        )
        print(f"{case['description']}: {elo_change:+d} ELO")

if __name__ == "__main__":
    test_elo_multipliers()
    test_loss_reduction()
    test_personal_bonus()
    test_catch_up_bonus()
    test_balanced_elo_change()
    print("\n=== All tests completed ===")