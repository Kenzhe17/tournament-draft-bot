"""Logging configuration for the bot."""

import logging
import sys
from pathlib import Path

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / "bot.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


def log_game_play(guild_id: int, user_id: int, game_id: str, bet: int, result: str, winnings: int = 0) -> None:
    """Log a mini-game play."""
    logger.info(
        f"GAME_PLAY - Guild: {guild_id}, User: {user_id}, Game: {game_id}, "
        f"Bet: {bet}, Result: {result}, Winnings: {winnings}"
    )


def log_command_usage(guild_id: int, user_id: int, command: str) -> None:
    """Log a command usage."""
    logger.info(f"COMMAND - Guild: {guild_id}, User: {user_id}, Command: {command}")


def log_error(guild_id: int, user_id: int, error: str, context: str = "") -> None:
    """Log an error."""
    logger.error(
        f"ERROR - Guild: {guild_id}, User: {user_id}, Error: {error}, Context: {context}"
    )


def log_balance_change(guild_id: int, user_id: int, amount: int, reason: str) -> None:
    """Log a balance change."""
    logger.info(
        f"BALANCE_CHANGE - Guild: {guild_id}, User: {user_id}, "
        f"Amount: {amount:+d}, Reason: {reason}"
    )
