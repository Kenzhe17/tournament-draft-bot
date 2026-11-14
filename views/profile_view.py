"""Профиль игрока — Components V2 без PNG/Chromium (r!z3 CLASSIC).

Собирает данные через services.profile_card.collect_profile_data
(тот же источник, что у старой PNG-карточки) и рисует текстовый
CV2-профиль: ПРОФИЛЬ / ПРОГРЕСС / СТАТИСТИКА / ЭКОНОМИКА.
"""

import logging

import discord

from utils.design.layouts import DesignView

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


async def build_profile_layout(
    guild: discord.Guild, user: discord.User
) -> DesignView:
    """CV2-профиль игрока: текстовые блоки, без изображений."""
    from services.profile_card import collect_profile_data

    data = await collect_profile_data(guild, user)

    elo_delta = data.get("last_elo_change") or 0
    elo_sign = "+" if elo_delta >= 0 else ""

    view = DesignView(title=f"⭐ ПРОФИЛЬ — {data['name']}", timeout=180)
    view.text(f"**Участник:** <@{user.id}>")
    view.text(f"**Репутация:** {data.get('reputation', 0)}")
    if data.get("partner") and data["partner"] != "—":
        view.text(f"**Брак:** {data['partner']}")
    if data.get("birthday") and data["birthday"] != "—":
        view.text(f"**День рождения:** {data['birthday']}")
    if data.get("status") and data["status"] != "ок":
        view.text(f"**О себе:** {data['status']}")
    view.sep()

    view.text("**ПРОГРЕСС**")
    view.text(
        f"**Ранг:** {data.get('rank_name', '—')}\n"
        f"**ELO:** {data.get('elo', 0)} `{elo_sign}{elo_delta}`\n"
        f"**Уровень:** {data.get('level', 1)}"
    )
    view.sep()

    games = data.get("matches", 0)
    view.text("**СТАТИСТИКА**")
    if games > 0:
        view.text(
            f"**Матчи:** {games}\n"
            f"**Победы:** {data.get('wins', 0)} ({data.get('winrate', 0)}%)\n"
            f"{_kd_text(data)}\n"
            f"**Лучший результат по убийствам:** {data.get('max_kills', 0)}"
        )
        hist = data.get("hist_count", 0)
        if hist:
            view.text(
                f"**ELO за последние {hist} матчей:** "
                f"`{data.get('chart_delta', 0):+d}`"
            )
    else:
        view.text("Ещё не играл в турниры")
    view.sep()

    view.text("**ЭКОНОМИКА**")
    view.text(
        f"**Баланс:** {data.get('balance', 0)}\n"
        f"**Предметов:** {data.get('items', 0)} шт."
    )
    return view


def _kd_text(data: dict) -> str:
    """Строка K/D Ratio — без assists/damage."""
    kda = data.get("kda")
    avg_kills = data.get("avg_kills")
    parts = []
    if kda is not None:
        parts.append(f"**K/D Ratio:** {kda}")
    if avg_kills is not None:
        parts.append(f"**AVG Kills:** {avg_kills}")
    sr = data.get("skill_rating")
    if sr is not None:
        parts.append(f"**Skill Rating:** {sr}/10.0")
    return "\n".join(parts) if parts else "**K/D:** —"
