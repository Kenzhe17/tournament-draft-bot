"""Discord logging system for bot events."""

import os
import discord
from utils.design import compose
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
            title=f" {title}",
            description=description,
            color=color,
            timestamp=datetime.now()
        )

        if guild:
            embed.set_footer(text=f"Сервер: {guild.name} (ID: {guild.id})")

        await channel.send( view=compose(embed))
    except Exception as e:
        print(f"Failed to send log: {e}")


async def log_guild_join(bot: discord.Client, guild: discord.Guild) -> None:
    """Log when bot is added to a server."""
    owner = guild.owner
    member_count = guild.member_count

    description = (
        f" **Владелец:** {owner.mention}\n"
        f" **Сервер:** {guild.name}\n"
        f" **Участников:** {member_count}\n"
        f" **Создан:** {guild.created_at.strftime('%d.%m.%Y')}\n"
    )

    await send_log(bot, "Бот добавлен на сервер", description, 0x57F287, guild)


async def log_tournament_created(bot: discord.Client, guild: discord.Guild, user: discord.Member, size: int, formation: str) -> None:
    """Log when a tournament is created."""
    description = (
        f" **Организатор:** {user.mention}\n"
        f" **Размер:** {size}\n"
        f" **Формат:** {formation}\n"
    )

    await send_log(bot, "Турнир создан", description, 0x2B2D31, guild)


async def log_tournament_deleted(bot: discord.Client, guild: discord.Guild, user: discord.Member) -> None:
    """Log when a tournament is deleted."""
    description = (
        f" **Организатор:** {user.mention}\n"
        f" **Действие:** Турнир был удален\n"
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

    teams_display = "\n".join([f" {name}" for name in team_names[:8]])
    if len(team_names) > 8:
        teams_display += f"\n ... и ещё {len(team_names) - 8} команд"

    description = (
        f" **Организатор:** {user.mention}\n"
        f" **Метод:** {method_display}\n"
        f" **Команды:**\n{teams_display}\n"
    )

    await send_log(bot, "Игроки распределены", description, 0x2B2D31, guild)


async def log_draft_completed(bot: discord.Client, guild: discord.Guild) -> None:
    """Log draft completion."""
    description = (
        f" **Действие:** Драфт завершен автоматически (таймер)\n"
    )

    await send_log(bot, "Драфт завершен", description, 0xF1C40F, guild)


async def log_stats_filled(bot: discord.Client, guild: discord.Guild, user: discord.Member, match_type: str, match_index: int, stats_summary: str) -> None:
    """Log statistics filling."""
    description = (
        f" **Заполнил:** {user.mention}\n"
        f" **Матч:** {match_type} #{match_index}\n"
        f" **Статистика:**\n{stats_summary}\n"
    )

    await send_log(bot, "Статистика заполнена", description, 0x2B2D31, guild)


async def log_winner_confirmed(bot: discord.Client, guild: discord.Guild, user: discord.Member, match_type: str, match_index: int, winning_team: str) -> None:
    """Log winner confirmation."""
    description = (
        f" **Подтвердил:** {user.mention}\n"
        f" **Матч:** {match_type} #{match_index}\n"
        f" **Победитель:** {winning_team}\n"
    )

    await send_log(bot, "Победитель подтвержден", description, 0x57F287, guild)


async def log_bets_resolved(bot: discord.Client, guild: discord.Guild, match_type: str, match_index: int, winning_team: str, total_payout: int, payouts_by_name: dict = None) -> None:
    """Log bet resolution."""
    description = (
        f" **Матч:** {match_type} #{match_index}\n"
        f" **Победитель:** {winning_team}\n"
        f" **Выплачено:** {total_payout} {get_emoji('money')}\n"
    )

    # Add individual payouts if provided
    if payouts_by_name:
        payouts_display = []
        for player_name, payout in payouts_by_name.items():
            if payout > 0:
                payouts_display.append(f" {player_name}: {payout} {get_emoji('money')}")
        if payouts_display:
            description += f"\n **Выплаты по игрокам:**\n" + "\n".join(payouts_display[:10])
            if len(payouts_display) > 10:
                description += f"\n ... и ещё {len(payouts_display) - 10} игроков"

    await send_log(bot, "Ставки разрешены", description, 0x2B2D31, guild)


async def log_tournament_completed(bot: discord.Client, guild: discord.Guild, tournament_name: str, winner_name: str, participant_count: int, duration_minutes: int) -> None:
    """Log when a tournament is completed."""
    description = (
        f" **Название:** {tournament_name}\n"
        f" **Победитель:** {winner_name}\n"
        f" **Участников:** {participant_count}\n"
        f" **Длительность:** {duration_minutes} минут\n"
    )

    await send_log(bot, "Турнир завершён", description, 0xF1C40F, guild)


async def log_command_error(bot: discord.Client, guild: discord.Guild, user: discord.Member, command_name: str, error: str) -> None:
    """Log when a command encounters an error."""
    description = (
        f" **Пользователь:** {user.mention}\n"
        f" **Команда:** /{command_name}\n"
        f" **Ошибка:** {error}\n"
    )

    await send_log(bot, "Ошибка команды", description, 0xED4245, guild)
