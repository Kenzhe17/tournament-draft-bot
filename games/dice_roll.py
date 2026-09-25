"""Dice roll game logic."""

import random


def roll_dice() -> int:
    """Roll a die (1-6)."""
    return random.randint(1, 6)
