"""Discord logging system for bot events."""

import os
import discord
from utils.design import compose
from datetime import datetime

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


async def log_command_error(bot: discord.Client, guild: discord.Guild, user: discord.Member, command_name: str, error: str) -> None:
    """Log when a command encounters an error."""
    description = (
        f" **Пользователь:** {user.mention}\n"
        f" **Команда:** /{command_name}\n"
        f" **Ошибка:** {error}\n"
    )

    await send_log(bot, "Ошибка команды", description, 0xED4245, guild)
