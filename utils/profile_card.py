"""Shared builder for the player profile card.

Used by /player profile and by the profile edit modal so both render
the exact same interface.
"""

import logging

import discord

from config import get_emoji

logger = logging.getLogger(__name__)


async def build_profile_embed(guild_id: int, target_user, *, limit: int = 10) -> discord.Embed:
    """Build the profile embed for ``target_user`` (Member/User)."""
    from cogs.tournament import get_rank_emoji
    from storage.player_stats_store import player_stats_store
    from storage.user_balance_store import user_balance_store
    from storage.shop_store import inventory_store

    stats = await player_stats_store.get(guild_id, target_user.id)
    balance = await user_balance_store.get_balance(guild_id, target_user.id)

    # Создать профиль если его нет
    if not stats:
        from models.player_stats import PlayerStats
        stats = PlayerStats(
            guild_id=guild_id,
            user_id=target_user.id,
            name=target_user.display_name,
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
        await player_stats_store.set(stats)

    # Ранг и уровень
    rank_title = get_rank_emoji(stats.level)

    # Инвентарь
    cosmetics = inventory_store.get_player_inventory(guild_id, target_user.id)
    inventory_count = len(cosmetics)

    # Используем статистику турниров вместо мини-игр
    total_games_played = stats.games
    total_games_won = stats.wins

    # Винрейт
    win_rate = (total_games_won / total_games_played * 100) if total_games_played > 0 else 0

    # Last ELO Change
    elo_change = stats.last_elo_change if hasattr(stats, 'last_elo_change') else 0
    pet_elo_bonus = stats.last_pet_elo_bonus if hasattr(stats, 'last_pet_elo_bonus') else 0

    # Format ELO change with bonus
    if pet_elo_bonus != 0:
        elo_display = f"(Last: {elo_change:+d}{pet_elo_bonus:+d})"
    else:
        elo_display = f"(Last: {elo_change:+d})"

    # Build description
    description_parts = []

    # Прогресс
    description_parts.append("**Прогресс:**")
    description_parts.append(f" Ранг: {rank_title}")
    description_parts.append(f" ELO: {int(stats.elo):,} `{elo_display}`")
    description_parts.append(f" Уровень: Level {stats.level}")
    description_parts.append("")

    # Экономика
    description_parts.append("**Экономика:**")
    description_parts.append(f" Баланс: {balance:,} " + get_emoji("money"))
    description_parts.append(f" Предметов: {inventory_count} шт.")
    description_parts.append("")

    # Битва Отрядов
    description_parts.append("**Битва Отрядов:**")
    description_parts.append(f" Сыграно: {total_games_played} игр")
    description_parts.append(f" Побед: {total_games_won} `({win_rate:.1f}%)`")

    # Show K/D and kills only if player has played games
    if total_games_played > 0:
        description_parts.append(f" K/D Ratio: {stats.kd_ratio:.2f}")
        description_parts.append(f" AVG Kills: {stats.avg_kills:.2f}")
        description_parts.append(f" Skill Rating: {stats.skill_rating:.1f}/10.0")
        description_parts.append(f" Max Kills: {stats.best_match_kills}")
    else:
        description_parts.append(f" Ещё не играл в турниры")

    # Ultimatum статистика (после Max Kills)
    try:
        from ultimatum.elo_store import elo_store
        ultimatum_elo = await elo_store.get_player(target_user.id)
        logger.info(f"Ultimatum stats for {target_user.id}: {ultimatum_elo}")
        if ultimatum_elo:
            description_parts.append("")
            description_parts.append(f" **Ультиматум:**")
            description_parts.append(f" Сыграно: {ultimatum_elo.get('rated_matches', 0)} игр")
            description_parts.append(f" Побед: {ultimatum_elo.get('wins', 0)}")
            description_parts.append(f" ELO: {ultimatum_elo.get('elo', 1000)}")
        else:
            logger.info(f"No Ultimatum stats found for {target_user.id}")
            # Show default values even if no record
            description_parts.append("")
            description_parts.append(f" **Ультиматум:**")
            description_parts.append(f" Сыграно: 0 игр")
            description_parts.append(f" Побед: 0")
            description_parts.append(f" ELO: 1000")
    except Exception as e:
        logger.error(f"Error getting Ultimatum stats: {e}", exc_info=True)

    description_parts.append("")

    # Био
    if stats.description:
        description_parts.append(f" {stats.description}")
        description_parts.append("")

    # ELO History (last N matches)
    if total_games_played > 0:
        from storage.match_history_store import match_history_store
        match_history = await match_history_store.get_user_history(guild_id, target_user.id, limit=limit)

        if match_history:
            # Build ELO changes list (most recent first)
            elo_changes = []
            for m in match_history[:limit]:
                change = m.elo_change
                if change > 0:
                    elo_changes.append(f"+{change}")
                elif change < 0:
                    elo_changes.append(f"{change}")
                else:
                    elo_changes.append("0")

            if elo_changes:
                description_parts.append("")
                description_parts.append(f" ELO за последние {len(elo_changes)} матчей:")
                description_parts.append(f" ({', '.join(elo_changes)})")
                description_parts.append("")

    # Создать embed
    embed = discord.Embed(
        title=f" {stats.name}",
        description="\n".join(description_parts),
        color=discord.Color.from_rgb(69, 69, 69)
    )
    embed.set_footer(text="Основная информация и статистика игрока")
    embed.set_thumbnail(url=target_user.display_avatar.url)

    return embed
