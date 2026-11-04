"""Elo Rating Calculator for 1v1 matches."""

import math
from dataclasses import dataclass
from typing import Tuple


@dataclass
class EloResult:
    """Result of Elo calculation."""
    winner_new_elo: int
    loser_new_elo: int
    winner_change: int
    loser_change: int
    winner_expected: float
    loser_expected: float
    k_factor: int


class EloCalculator:
    """Elo rating calculator."""

    STARTING_ELO = 1000
    MIN_ELO = 100
    MAX_ELO = None  # No upper limit

    # K-factor thresholds
    K_NEWBIE = 40  # For players with < 10 matches
    K_NORMAL = 32  # For players with 10-50 matches
    K_VETERAN = 24  # For players with > 50 matches

    def get_k_factor(self, rated_matches: int) -> int:
        """Get K-factor based on number of rated matches."""
        if rated_matches < 10:
            return self.K_NEWBIE
        elif rated_matches < 50:
            return self.K_NORMAL
        else:
            return self.K_VETERAN

    def calculate_expected_score(self, rating_a: int, rating_b: int) -> float:
        """
        Calculate expected score for player A.

        E_A = 1 / (1 + 10 ^ ((R_B - R_A) / 400))
        """
        return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))

    def calculate(
        self,
        winner_elo: int,
        loser_elo: int,
        winner_matches: int,
        loser_matches: int
    ) -> EloResult:
        """
        Calculate Elo changes for a match.

        Uses min(K_winner, K_loser) to ensure zero-sum.
        """
        # Get individual K-factors
        k_winner = self.get_k_factor(winner_matches)
        k_loser = self.get_k_factor(loser_matches)

        # Use minimum for zero-sum balance
        k_match = min(k_winner, k_loser)

        # Calculate expected scores
        winner_expected = self.calculate_expected_score(winner_elo, loser_elo)
        loser_expected = self.calculate_expected_score(loser_elo, winner_elo)

        # Calculate changes
        # Winner: New = Old + K * (1 - Expected)
        winner_change = round(k_match * (1.0 - winner_expected))

        # Loser: New = Old + K * (0 - Expected)
        loser_change = round(k_match * (0.0 - loser_expected))

        # Calculate new ratings
        winner_new_elo = winner_elo + winner_change
        loser_new_elo = loser_elo + loser_change

        # Ensure minimum Elo
        winner_new_elo = max(winner_new_elo, self.MIN_ELO)
        loser_new_elo = max(loser_new_elo, self.MIN_ELO)

        # Recalculate changes if minimum was applied
        if winner_new_elo == self.MIN_ELO:
            winner_change = winner_new_elo - winner_elo
        if loser_new_elo == self.MIN_ELO:
            loser_change = loser_new_elo - loser_elo

        # Verify zero-sum (should be 0)
        total_change = winner_change + loser_change
        if total_change != 0:
            # Adjust loser change to ensure zero-sum
            loser_change = -winner_change
            loser_new_elo = loser_elo + loser_change
            loser_new_elo = max(loser_new_elo, self.MIN_ELO)

        return EloResult(
            winner_new_elo=winner_new_elo,
            loser_new_elo=loser_new_elo,
            winner_change=winner_change,
            loser_change=loser_change,
            winner_expected=winner_expected,
            loser_expected=loser_expected,
            k_factor=k_match
        )

    def verify_zero_sum(self, result: EloResult) -> bool:
        """Verify that changes sum to zero."""
        return result.winner_change + result.loser_change == 0
