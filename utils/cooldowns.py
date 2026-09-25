"""Cooldown system using Redis."""

import discord
from functools import wraps
from typing import Callable

from storage.redis_client import set_cooldown, get_cooldown


def command_cooldown(ttl: int = 5):
    """Decorator for command cooldowns using Redis.

    Args:
        ttl: Time to live in seconds (default: 5 seconds)
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
            guild_id = interaction.guild_id
            user_id = interaction.user.id
            command_name = func.__name__

            # Check if user is on cooldown
            if await get_cooldown(guild_id, user_id, command_name):
                await interaction.response.send_message(
                    f"⏳ Подождите {ttl} секунд перед повторным использованием команды!",
                    ephemeral=True
                )
                return

            # Set cooldown
            await set_cooldown(guild_id, user_id, command_name, ttl)

            # Execute the function
            return await func(self, interaction, *args, **kwargs)

        return wrapper
    return decorator


def mini_game_cooldown(ttl: int = 3):
    """Decorator for mini-game cooldowns (shorter than regular commands).

    Args:
        ttl: Time to live in seconds (default: 3 seconds)
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
            guild_id = interaction.guild_id
            user_id = interaction.user.id
            command_name = f"minigame_{func.__name__}"

            # Check if user is on cooldown
            if await get_cooldown(guild_id, user_id, command_name):
                await interaction.response.send_message(
                    f"⏳ Подождите {ttl} секунд перед повторной игрой!",
                    ephemeral=True
                )
                return

            # Set cooldown
            await set_cooldown(guild_id, user_id, command_name, ttl)

            # Execute the function
            return await func(self, interaction, *args, **kwargs)

        return wrapper
    return decorator
