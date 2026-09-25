"""Coin flip game logic."""

import random
from enum import Enum


class CoinSide(Enum):
    """Coin sides."""
    HEADS = "heads"
    TAILS = "tails"

    @property
    def emoji(self) -> str:
        """Get emoji for side."""
        return {
            CoinSide.HEADS: "🦅",
            CoinSide.TAILS: "🪙",
        }[self]

    @property
    def name(self) -> str:
        """Get Russian name for side."""
        return {
            CoinSide.HEADS: "Орел",
            CoinSide.TAILS: "Решка",
        }[self]


def flip_coin() -> CoinSide:
    """Flip a coin randomly."""
    return random.choice(list(CoinSide))
