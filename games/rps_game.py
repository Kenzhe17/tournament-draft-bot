"""Rock-Paper-Scissors game logic."""

import random
from enum import Enum
from typing import Optional


class RPSChoice(Enum):
    """RPS choices."""
    ROCK = "rock"
    PAPER = "paper"
    SCISSORS = "scissors"

    @property
    def emoji(self) -> str:
        """Get emoji for choice."""
        return {
            RPSChoice.ROCK: "🪨",
            RPSChoice.PAPER: "📄",
            RPSChoice.SCISSORS: "✂️",
        }[self]

    @property
    def name(self) -> str:
        """Get Russian name for choice."""
        return {
            RPSChoice.ROCK: "Камень",
            RPSChoice.PAPER: "Бумага",
            RPSChoice.SCISSORS: "Ножницы",
        }[self]


def determine_winner(choice1: RPSChoice, choice2: RPSChoice) -> Optional[int]:
    """
    Determine winner of RPS game.
    Returns 1 if player1 wins, 2 if player2 wins, None if tie.
    """
    if choice1 == choice2:
        return None

    if (
        (choice1 == RPSChoice.ROCK and choice2 == RPSChoice.SCISSORS)
        or (choice1 == RPSChoice.PAPER and choice2 == RPSChoice.ROCK)
        or (choice1 == RPSChoice.SCISSORS and choice2 == RPSChoice.PAPER)
    ):
        return 1

    return 2


def get_bot_choice() -> RPSChoice:
    """Get random bot choice."""
    return random.choice(list(RPSChoice))
