"""Guess color game logic."""

import random
from enum import Enum


class Color(Enum):
    """Colors."""
    RED = "red"
    BLUE = "blue"
    GREEN = "green"

    @property
    def emoji(self) -> str:
        """Get emoji for color."""
        return {
            Color.RED: "🔴",
            Color.BLUE: "🔵",
            Color.GREEN: "🟢",
        }[self]

    @property
    def name(self) -> str:
        """Get Russian name for color."""
        return {
            Color.RED: "Красный",
            Color.BLUE: "Синий",
            Color.GREEN: "Зелёный",
        }[self]


def random_color() -> Color:
    """Get random color."""
    return random.choice(list(Color))
