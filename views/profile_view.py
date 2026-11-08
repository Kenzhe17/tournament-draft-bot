"""View и хелперы профиля игрока."""
import logging

import discord

logger = logging.getLogger(__name__)


class ProfileView(discord.ui.View):
    """View профиля — кнопок нет, редактирование через /player set."""

    def __init__(self, guild_id: int, user_id: int, is_owner: bool):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id
        self.is_owner = is_owner


def _default_stats(guild_id: int, user_id: int, display_name: str):
    from models.player_stats import PlayerStats
    return PlayerStats(
        guild_id=guild_id,
        user_id=user_id,
        name=display_name,
        elo=1000,
        wins=0,
        finals=0,
        games=0,
        current_streak=0,
        best_win_streak=0,
        best_loss_streak=0,
        total_kills=0,
        total_deaths=0,
        best_match_kills=0,
        total_elo_change=0,
        last_elo_change=0,
        xp=0,
        level=1,
        xp_to_next_level=250,
        total_earnings=0,
        tournament_participations=0,
        description=""
    )
