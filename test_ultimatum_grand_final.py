"""Test script for Ultimatum Grand Final logic."""

from dataclasses import dataclass
from typing import List


@dataclass
class MockPlayer:
    """Mock player for testing."""
    discord_id: int
    username: str
    wins: int
    losses: int
    status: str = "ACTIVE"
    final_place: int = None
    elimination_round: int = None


class PlayerStatus:
    """Mock PlayerStatus enum."""
    ACTIVE = "ACTIVE"
    ELIMINATED = "ELIMINATED"
    GRAND_FINALIST = "GRAND_FINALIST"
    CHAMPION = "CHAMPION"
    GRAND_FINAL_LOSER = "GRAND_FINAL_LOSER"


def should_create_grand_final(players: List[MockPlayer]) -> bool:
    """Check if grand final conditions are met.

    Grand final scenarios:
    1. 4-0 Direct Pass: One player has 4-0 record, other emerges from 3-1 bracket
    2. 5-1 Qualifier: Both players have at least 4 wins and max 1 loss

    Grand final is created when:
    - Exactly 2 active players remain
    - AND (one has 4-0 OR both have >= 4 wins with max 1 loss)
    """
    if len(players) != 2:
        return False

    p1, p2 = players[0], players[1]

    # Scenario 1: 4-0 Direct Pass
    # One player has perfect 4-0, other has any record
    has_4_0 = (p1.wins == 4 and p1.losses == 0) or (p2.wins == 4 and p2.losses == 0)
    if has_4_0:
        return True

    # Scenario 2: 5-1 Qualifier
    # Both players have at least 4 wins and max 1 loss
    # (This happens when both come from 3-1 bracket and win their matches)
    both_strong = (
        p1.wins >= 4 and p1.losses <= 1 and
        p2.wins >= 4 and p2.losses <= 1
    )
    if both_strong:
        return True

    return False


def test_4_0_direct_pass():
    """Test 4-0 direct pass scenario."""
    print("=== Testing 4-0 Direct Pass Scenario ===")

    # Case 1: One player with 4-0, other with 3-1
    players = [
        MockPlayer(1, "Player1", 4, 0),
        MockPlayer(2, "Player2", 3, 1),
    ]
    result = should_create_grand_final(players)
    print(f"4-0 vs 3-1: {result} (expected: True)")
    assert result == True, "Should create grand final for 4-0 vs 3-1"

    # Case 2: One player with 4-0, other with 2-2
    players = [
        MockPlayer(1, "Player1", 4, 0),
        MockPlayer(2, "Player2", 2, 2),
    ]
    result = should_create_grand_final(players)
    print(f"4-0 vs 2-2: {result} (expected: True)")
    assert result == True, "Should create grand final for 4-0 vs 2-2"

    # Case 3: One player with 4-0, other with 4-1
    players = [
        MockPlayer(1, "Player1", 4, 0),
        MockPlayer(2, "Player2", 4, 1),
    ]
    result = should_create_grand_final(players)
    print(f"4-0 vs 4-1: {result} (expected: True)")
    assert result == True, "Should create grand final for 4-0 vs 4-1"

    print("✅ All 4-0 direct pass tests passed\n")


def test_5_1_qualifier():
    """Test 5-1 qualifier scenario."""
    print("=== Testing 5-1 Qualifier Scenario ===")

    # Case 1: Both players with 4-1
    players = [
        MockPlayer(1, "Player1", 4, 1),
        MockPlayer(2, "Player2", 4, 1),
    ]
    result = should_create_grand_final(players)
    print(f"4-1 vs 4-1: {result} (expected: True)")
    assert result == True, "Should create grand final for 4-1 vs 4-1"

    # Case 2: Both players with 5-1
    players = [
        MockPlayer(1, "Player1", 5, 1),
        MockPlayer(2, "Player2", 5, 1),
    ]
    result = should_create_grand_final(players)
    print(f"5-1 vs 5-1: {result} (expected: True)")
    assert result == True, "Should create grand final for 5-1 vs 5-1"

    # Case 3: Both players with 4-0 (edge case - shouldn't happen normally)
    players = [
        MockPlayer(1, "Player1", 4, 0),
        MockPlayer(2, "Player2", 4, 0),
    ]
    result = should_create_grand_final(players)
    print(f"4-0 vs 4-0: {result} (expected: True)")
    assert result == True, "Should create grand final for 4-0 vs 4-0"

    # Case 4: Both players with 4-2 (should NOT create grand final)
    players = [
        MockPlayer(1, "Player1", 4, 2),
        MockPlayer(2, "Player2", 4, 2),
    ]
    result = should_create_grand_final(players)
    print(f"4-2 vs 4-2: {result} (expected: False)")
    assert result == False, "Should NOT create grand final for 4-2 vs 4-2"

    print("✅ All 5-1 qualifier tests passed\n")


def test_no_grand_final():
    """Test scenarios where grand final should NOT be created."""
    print("=== Testing No Grand Final Scenarios ===")

    # Case 1: More than 2 players
    players = [
        MockPlayer(1, "Player1", 3, 1),
        MockPlayer(2, "Player2", 3, 1),
        MockPlayer(3, "Player3", 2, 2),
    ]
    result = should_create_grand_final(players)
    print(f"3 players: {result} (expected: False)")
    assert result == False, "Should NOT create grand final with 3 players"

    # Case 2: Both players with too few wins
    players = [
        MockPlayer(1, "Player1", 3, 1),
        MockPlayer(2, "Player2", 3, 1),
    ]
    result = should_create_grand_final(players)
    print(f"3-1 vs 3-1: {result} (expected: False)")
    assert result == False, "Should NOT create grand final for 3-1 vs 3-1"

    # Case 3: Both players with too many losses
    players = [
        MockPlayer(1, "Player1", 4, 2),
        MockPlayer(2, "Player2", 4, 2),
    ]
    result = should_create_grand_final(players)
    print(f"4-2 vs 4-2: {result} (expected: False)")
    assert result == False, "Should NOT create grand final for 4-2 vs 4-2"

    # Case 4: One player with 3-0, other with 3-1
    players = [
        MockPlayer(1, "Player1", 3, 0),
        MockPlayer(2, "Player2", 3, 1),
    ]
    result = should_create_grand_final(players)
    print(f"3-0 vs 3-1: {result} (expected: False)")
    assert result == False, "Should NOT create grand final for 3-0 vs 3-1"

    print("✅ All no grand final tests passed\n")


def calculate_rankings(players: List[MockPlayer]) -> List[MockPlayer]:
    """Calculate final rankings 1-16.

    Ranking criteria (in order):
    1. Status (Champion > Grand Final Loser > Grand Finalist > Eliminated > Active)
    2. Total wins (more wins = better)
    3. Total losses (fewer losses = better)
    4. Elimination round (later = better, for eliminated players)
    """
    status_priority = {
        PlayerStatus.CHAMPION: 0,
        PlayerStatus.GRAND_FINAL_LOSER: 1,
        PlayerStatus.GRAND_FINALIST: 2,
        PlayerStatus.ELIMINATED: 3,
        PlayerStatus.ACTIVE: 4,
    }

    sorted_players = sorted(
        players,
        key=lambda p: (
            status_priority.get(p.status, 99),
            -p.wins,
            p.losses if p.losses else 0,
            -(p.elimination_round if p.elimination_round else 0)
        )
    )

    # Assign places
    for idx, player in enumerate(sorted_players):
        player.final_place = idx + 1

    return sorted_players


def test_ranking_calculation():
    """Test ranking calculation."""
    print("=== Testing Ranking Calculation ===")

    # Case 1: Full tournament with 16 players
    players = [
        MockPlayer(1, "Champion", 5, 0, PlayerStatus.CHAMPION),
        MockPlayer(2, "RunnerUp", 4, 1, PlayerStatus.GRAND_FINAL_LOSER),
        MockPlayer(3, "SemiFinalist1", 4, 1, PlayerStatus.ELIMINATED, elimination_round=4),
        MockPlayer(4, "SemiFinalist2", 4, 1, PlayerStatus.ELIMINATED, elimination_round=4),
        MockPlayer(5, "QuarterFinalist1", 3, 1, PlayerStatus.ELIMINATED, elimination_round=3),
        MockPlayer(6, "QuarterFinalist2", 3, 1, PlayerStatus.ELIMINATED, elimination_round=3),
        MockPlayer(7, "QuarterFinalist3", 3, 1, PlayerStatus.ELIMINATED, elimination_round=3),
        MockPlayer(8, "QuarterFinalist4", 3, 1, PlayerStatus.ELIMINATED, elimination_round=3),
        MockPlayer(9, "Round2Elim1", 2, 1, PlayerStatus.ELIMINATED, elimination_round=2),
        MockPlayer(10, "Round2Elim2", 2, 1, PlayerStatus.ELIMINATED, elimination_round=2),
        MockPlayer(11, "Round2Elim3", 2, 1, PlayerStatus.ELIMINATED, elimination_round=2),
        MockPlayer(12, "Round2Elim4", 2, 1, PlayerStatus.ELIMINATED, elimination_round=2),
        MockPlayer(13, "Round1Elim1", 1, 1, PlayerStatus.ELIMINATED, elimination_round=1),
        MockPlayer(14, "Round1Elim2", 1, 1, PlayerStatus.ELIMINATED, elimination_round=1),
        MockPlayer(15, "Round1Elim3", 1, 1, PlayerStatus.ELIMINATED, elimination_round=1),
        MockPlayer(16, "Round1Elim4", 1, 1, PlayerStatus.ELIMINATED, elimination_round=1),
    ]

    ranked = calculate_rankings(players)

    print("Final rankings:")
    for player in ranked[:5]:  # Show top 5
        print(f"  #{player.final_place}: {player.username} ({player.wins}-{player.losses}) - {player.status}")

    assert ranked[0].final_place == 1, "Champion should be #1"
    assert ranked[0].status == PlayerStatus.CHAMPION, "First place should be Champion"
    assert ranked[1].final_place == 2, "Runner-up should be #2"
    assert ranked[1].status == PlayerStatus.GRAND_FINAL_LOSER, "Second place should be Grand Final Loser"

    print("✅ Ranking calculation test passed\n")


def test_elimination_round_tracking():
    """Test elimination round tracking."""
    print("=== Testing Elimination Round Tracking ===")

    # Case 1: Players eliminated in different rounds
    players = [
        MockPlayer(1, "LateElim", 3, 2, PlayerStatus.ELIMINATED, elimination_round=5),
        MockPlayer(2, "EarlyElim", 1, 2, PlayerStatus.ELIMINATED, elimination_round=2),
        MockPlayer(3, "MidElim", 2, 2, PlayerStatus.ELIMINATED, elimination_round=3),
    ]

    ranked = calculate_rankings(players)

    print("Elimination round rankings:")
    for player in ranked:
        print(f"  #{player.final_place}: {player.username} (eliminated round {player.elimination_round})")

    assert ranked[0].elimination_round == 5, "Player eliminated later should rank higher"
    assert ranked[1].elimination_round == 3, "Player eliminated in middle should be second"
    assert ranked[2].elimination_round == 2, "Player eliminated earlier should be last"

    print("✅ Elimination round tracking test passed\n")


if __name__ == "__main__":
    print("=== Ultimatum Grand Final Logic Tests ===\n")

    test_4_0_direct_pass()
    test_5_1_qualifier()
    test_no_grand_final()
    test_ranking_calculation()
    test_elimination_round_tracking()

    print("=== All tests completed successfully ===")
