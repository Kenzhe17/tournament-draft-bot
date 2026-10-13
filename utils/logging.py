"""Discord logging system for bot events."""

import os
import discord
from datetime import datetime

from config import get_emoji

LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))


async def send_log(bot: discord.Client, title: str, description: str, color: int = 0x2B2D31, guild: discord.Guild = None) -> None:
    """Send a log message to the designated log channel."""
    if LOG_CHANNEL_ID == 0:
        return

    try:
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if not channel:
            return

        embed = discord.Embed(
            title=f"{get_emoji('a_star')} {title}",
            description=description,
            color=color,
            timestamp=datetime.now()
        )

        if guild:
            embed.set_footer(text=f"Сервер: {guild.name} (ID: {guild.id})")

        await channel.send(embed=embed)
    except Exception as e:
        print(f"Failed to send log: {e}")


async def log_guild_join(bot: discord.Client, guild: discord.Guild) -> None:
    """Log when bot is added to a server."""
    owner = guild.owner
    member_count = guild.member_count

    description = (
        f"{get_emoji('a_sparkle')} **Владелец:** {owner.mention}\n"
        f"{get_emoji('white_arrow')} **Сервер:** {guild.name}\n"
        f"{get_emoji('white_arrow')} **Участников:** {member_count}\n"
        f"{get_emoji('white_dot')} **Создан:** {guild.created_at.strftime('%d.%m.%Y')}\n"
    )

    await send_log(bot, "Бот добавлен на сервер", description, 0x57F287, guild)


async def log_tournament_created(bot: discord.Client, guild: discord.Guild, user: discord.Member, size: int, formation: str) -> None:
    """Log when a tournament is created."""
    description = (
        f"{get_emoji('a_sparkle')} **Организатор:** {user.mention}\n"
        f"{get_emoji('white_arrow')} **Размер:** {size}x{size}\n"
        f"{get_emoji('white_arrow')} **Формат:** {formation}\n"
    )

    await send_log(bot, "Турнир создан", description, 0x2B2D31, guild)


async def log_tournament_deleted(bot: discord.Client, guild: discord.Guild, user: discord.Member) -> None:
    """Log when a tournament is deleted."""
    description = (
        f"{get_emoji('a_sparkle')} **Организатор:** {user.mention}\n"
        f"{get_emoji('white_arrow')} **Действие:** Турнир был удален\n"
    )

    await send_log(bot, "Турнир удален", description, 0xED4245, guild)


async def log_players_distributed(bot: discord.Client, guild: discord.Guild, user: discord.Member, method: str, team_names: list[str]) -> None:
    """Log player distribution."""
    method_names = {
        "elo": "по ELO",
        "skill_rating": "по Skill Rating",
        "random": "случайно"
    }
    method_display = method_names.get(method, method)

    teams_display = "\n".join([f"{get_emoji('white_dot')} {name}" for name in team_names[:8]])
    if len(team_names) > 8:
        teams_display += f"\n{get_emoji('white_dot')} ... и ещё {len(team_names) - 8} команд"

    description = (
        f"{get_emoji('a_sparkle')} **Организатор:** {user.mention}\n"
        f"{get_emoji('white_arrow')} **Метод:** {method_display}\n"
        f"{get_emoji('white_dot')} **Команды:**\n{teams_display}\n"
    )

    await send_log(bot, "Игроки распределены", description, 0x2B2D31, guild)


async def log_draft_started(bot: discord.Client, guild: discord.Guild, user: discord.Member) -> None:
    """Log draft start."""
    description = (
        f"{get_emoji('a_sparkle')} **Организатор:** {user.mention}\n"
        f"{get_emoji('white_arrow')} **Действие:** Начало драфта\n"
    )

    await send_log(bot, "Драфт начат", description, 0x2B2D31, guild)


async def log_draft_completed(bot: discord.Client, guild: discord.Guild) -> None:
    """Log draft completion."""
    description = (
        f"{get_emoji('a_sparkle')} **Действие:** Драфт завершен автоматически (таймер)\n"
    )

    await send_log(bot, "Драфт завершен", description, 0xF1C40F, guild)


async def log_stats_filled(bot: discord.Client, guild: discord.Guild, user: discord.Member, match_type: str, match_index: int, stats_summary: str) -> None:
    """Log statistics filling."""
    description = (
        f"{get_emoji('a_sparkle')} **Заполнил:** {user.mention}\n"
        f"{get_emoji('white_arrow')} **Матч:** {match_type} #{match_index}\n"
        f"{get_emoji('white_dot')} **Статистика:**\n{stats_summary}\n"
    )

    await send_log(bot, "Статистика заполнена", description, 0x2B2D31, guild)


async def log_winner_confirmed(bot: discord.Client, guild: discord.Guild, user: discord.Member, match_type: str, match_index: int, winning_team: str) -> None:
    """Log winner confirmation."""
    description = (
        f"{get_emoji('a_sparkle')} **Подтвердил:** {user.mention}\n"
        f"{get_emoji('white_arrow')} **Матч:** {match_type} #{match_index}\n"
        f"{get_emoji('white_dot')} **Победитель:** {winning_team}\n"
    )

    await send_log(bot, "Победитель подтвержден", description, 0x57F287, guild)


async def log_bets_resolved(bot: discord.Client, guild: discord.Guild, match_type: str, match_index: int, winning_team: str, total_payout: int, payouts_by_name: dict = None) -> None:
    """Log bet resolution."""
    description = (
        f"{get_emoji('a_sparkle')} **Матч:** {match_type} #{match_index}\n"
        f"{get_emoji('white_arrow')} **Победитель:** {winning_team}\n"
        f"{get_emoji('white_dot')} **Выплачено:** {total_payout} {get_emoji('money')}\n"
    )

    # Add individual payouts if provided
    if payouts_by_name:
        payouts_display = []
        for player_name, payout in payouts_by_name.items():
            if payout > 0:
                payouts_display.append(f"{get_emoji('sub_middle')} {player_name}: {payout} {get_emoji('money')}")
        if payouts_display:
            description += f"\n{get_emoji('white_dot')} **Выплаты по игрокам:**\n" + "\n".join(payouts_display[:10])
            if len(payouts_display) > 10:
                description += f"\n{get_emoji('sub_middle')} ... и ещё {len(payouts_display) - 10} игроков"

    await send_log(bot, "Ставки разрешены", description, 0x2B2D31, guild)


async def log_player_removed(bot: discord.Client, guild: discord.Guild, user: discord.Member, player_name: str) -> None:
    """Log player removal."""
    description = (
        f"{get_emoji('a_sparkle')} **Удалил:** {user.mention}\n"
        f"{get_emoji('white_arrow')} **Игрок:** {player_name}\n"
    )

    await send_log(bot, "Игрок удален", description, 0xED4245, guild)


async def log_player_moved(bot: discord.Client, guild: discord.Guild, user: discord.Member, player_name: str, from_circle: str, to_circle: str) -> None:
    """Log player movement."""
    description = (
        f"{get_emoji('a_sparkle')} **Переместил:** {user.mention}\n"
        f"{get_emoji('white_arrow')} **Игрок:** {player_name}\n"
        f"{get_emoji('white_dot')} **Из:** {from_circle}\n"
        f"{get_emoji('white_dot')} **В:** {to_circle}\n"
    )

    await send_log(bot, "Игрок перемещен", description, 0x2B2D31, guild)


async def log_players_swapped(bot: discord.Client, guild: discord.Guild, user: discord.Member, player1: str, player2: str) -> None:
    """Log player swap."""
    description = (
        f"{get_emoji('a_sparkle')} **Поменял:** {user.mention}\n"
        f"{get_emoji('white_arrow')} **Игрок 1:** {player1}\n"
        f"{get_emoji('white_arrow')} **Игрок 2:** {player2}\n"
    )

    await send_log(bot, "Игроки поменялись", description, 0x2B2D31, guild)


async def log_tournament_completed(bot: discord.Client, guild: discord.Guild, tournament_name: str, winner_name: str, participant_count: int, duration_minutes: int) -> None:
    """Log when a tournament is completed."""
    description = (
        f"{get_emoji('a_sparkle')} **Название:** {tournament_name}\n"
        f"{get_emoji('white_arrow')} **Победитель:** {winner_name}\n"
        f"{get_emoji('white_arrow')} **Участников:** {participant_count}\n"
        f"{get_emoji('white_dot')} **Длительность:** {duration_minutes} минут\n"
    )

    await send_log(bot, "Турнир завершён", description, 0xF1C40F, guild)


async def log_command_error(bot: discord.Client, guild: discord.Guild, user: discord.Member, command_name: str, error: str) -> None:
    """Log when a command encounters an error."""
    description = (
        f"{get_emoji('a_sparkle')} **Пользователь:** {user.mention}\n"
        f"{get_emoji('white_arrow')} **Команда:** /{command_name}\n"
        f"{get_emoji('white_dot')} **Ошибка:** {error}\n"
    )

    await send_log(bot, "Ошибка команды", description, 0xED4245, guild)
