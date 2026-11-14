"""Сбор данных профиля игрока (CV2; PNG-рендер удалён)."""

from __future__ import annotations

import logging

import discord

from models.player_stats import PlayerStats
from storage.player_stats_store import player_stats_store
from storage.user_balance_store import user_balance_store
from storage.shop_store import inventory_store
from storage.match_history_store import match_history_store
from utils.ranks import rank_for_level

logger = logging.getLogger(__name__)


def _default_stats(guild_id: int, user_id: int, name: str) -> PlayerStats:
    return PlayerStats(
        guild_id=guild_id, user_id=user_id, name=name, elo=1000,
    )


async def collect_profile_data(guild: discord.Guild,
                               user: discord.User) -> dict:
    """Собрать все значения для карточки из существующих сервисов."""
    stats = await player_stats_store.get(guild.id, user.id)
    if stats is not None and stats.name != user.display_name:
        # Ник в профиле = ник в Discord — сбрасываем старые кастомные ники
        stats.name = user.display_name
        try:
            await player_stats_store.update(guild.id, user.id, stats)
        except Exception:
            logger.debug("Profile name sync failed", exc_info=True)
    if stats is None:
        stats = _default_stats(guild.id, user.id, user.display_name)

    # История матчей — до 10 последних, хронологически
    history = await match_history_store.get_user_history(
        guild.id, user.id, limit=10
    )
    history = list(reversed(history))  # старые → новые
    deltas = [h.elo_change for h in history]
    wins = sum(1 for h in history if h.result == "win")
    losses = sum(1 for h in history if h.result == "loss")

    # Ультиматум (Соло + Дуо рейтинги)
    ult_matches = ult_wins = 0
    ult_elo = 1000
    try:
        from ultimatum.elo import ult_elo_store
        u = await ult_elo_store.get_rating(user.id, "solo")
        if u:
            ult_matches = int(u.get("rated_matches") or 0)
            ult_wins = int(u.get("wins") or 0)
            ult_elo = int(u.get("elo") or 1000)
    except Exception:
        logger.debug("Ultimatum elo unavailable", exc_info=True)

    # Экономика
    try:
        balance = await user_balance_store.get_balance(guild.id, user.id)
    except Exception:
        balance = 0
    try:
        items = len(inventory_store.get_player_inventory(guild.id, user.id))
    except Exception:
        items = 0

    # Брак
    partner = "—"
    if stats.spouse_id:
        member = guild.get_member(stats.spouse_id)
        if member is None:
            try:
                member = await guild.fetch_member(stats.spouse_id)
            except Exception:
                member = None
        if member is not None:
            partner = f"@{member.display_name}"
        else:
            partner = f"<@{stats.spouse_id}>"

    games = stats.games or 0
    rank_name = rank_for_level(stats.level)[0]

    return {
        "name": user.display_name,
        "status": stats.description or "ок",
        "reputation": stats.reputation or 0,
        "partner": partner if stats.spouse_id else "—",
        "birthday": stats.birthday or "—",
        "level": stats.level,
        "rank_name": rank_name,
        "elo": stats.elo,
        "last_elo_change": stats.last_elo_change or 0,
        "skill_rating": round(stats.skill_rating, 1),
        "matches": games,
        "wins": stats.wins or 0,
        "winrate": round(stats.win_rate, 1),
        "kda": round(stats.total_kills / max(stats.total_deaths, 1), 2),
        "avg_kills": round(stats.avg_kills, 2),
        "max_kills": stats.best_match_kills or 0,
        "balance": balance,
        "items": items,
        "elo_history": deltas,
        "hist_count": len(deltas),
        "hist_wins": wins,
        "hist_losses": losses,
        "hist_start_elo": (stats.elo - sum(deltas)) if deltas else stats.elo,
        "chart_delta": sum(deltas),
        "ult_matches": ult_matches,
        "ult_wins": ult_wins,
        "ult_elo": ult_elo,
        "ult_status": (
            f"Winrate {ult_wins / ult_matches * 100:.0f}%"
            if ult_matches else "Пока нет сыгранных матчей"
        ),
    }


