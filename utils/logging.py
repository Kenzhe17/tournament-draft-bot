"""Discord logging system for bot events."""

import discord
from datetime import datetime

LOG_CHANNEL_ID = 1551965579798380705


async def send_log(bot: discord.Client, title: str, description: str, color: discord.Color = discord.Color.blue()) -> None:
    """Send a log message to the designated log channel."""
    try:
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
                timestamp=datetime.now()
            )
            await channel.send(embed=embed)
    except Exception as e:
        print(f"Failed to send log: {e}")


async def log_guild_join(bot: discord.Client, guild: discord.Guild) -> None:
    """Log when bot is added to a server."""
    owner = guild.owner
    member_count = guild.member_count

    description = f"""
🤖 **Бот добавлен на сервер**

👤 **Владелец:** {owner.name} ({owner.id})
🏷️ **Сервер:** {guild.name} ({guild.id})
👥 **Участников:** {member_count}
📅 **Создан:** {guild.created_at.strftime("%d.%m.%Y")}
    """

    await send_log(bot, "🎉 Новый сервер", description, discord.Color.green())


async def log_tournament_created(bot: discord.Client, guild: discord.Guild, user: discord.Member, tournament_name: str) -> None:
    """Log when a tournament is created."""
    description = f"""
🎮 **Турнир создан**

👤 **Создатель:** {user.name} ({user.id})
🏷️ **Сервер:** {guild.name} ({guild.id})
📋 **Название:** {tournament_name}
📅 **Дата:** {datetime.now().strftime("%d.%m.%Y %H:%M")}
    """

    await send_log(bot, "🎮 Новый турнир", description, discord.Color.blue())


async def log_tournament_completed(bot: discord.Client, guild: discord.Guild, tournament_name: str, winner_name: str, participant_count: int, duration_minutes: int) -> None:
    """Log when a tournament is completed."""
    description = f"""
🏆 **Турнир завершён**

🏷️ **Сервер:** {guild.name} ({guild.id})
📋 **Название:** {tournament_name}
🥇 **Победитель:** {winner_name}
👥 **Участников:** {participant_count}
⏱️ **Длительность:** {duration_minutes} минут
📅 **Дата:** {datetime.now().strftime("%d.%m.%Y %H:%M")}
    """

    await send_log(bot, "🏆 Турнир завершён", description, discord.Color.gold())


async def log_command_error(bot: discord.Client, guild: discord.Guild, user: discord.Member, command_name: str, error: str) -> None:
    """Log when a command encounters an error."""
    description = f"""
❌ **Ошибка в команде**

👤 **Пользователь:** {user.name} ({user.id})
🏷️ **Сервер:** {guild.name} ({guild.id})
⚙️ **Команда:** /{command_name}
📝 **Ошибка:** {error}
📅 **Дата:** {datetime.now().strftime("%d.%m.%Y %H:%M")}
    """

    await send_log(bot, "❌ Ошибка команды", description, discord.Color.red())
