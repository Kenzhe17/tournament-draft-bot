"""Утилиты для управления Discord ролями."""

import discord
from typing import Optional


async def apply_role(guild: discord.Guild, user_id: int, role_id: int) -> bool:
    """Назначить роль пользователю.

    Args:
        guild: Discord сервер
        user_id: ID пользователя
        role_id: ID роли для назначения

    Returns:
        True если успешно, False если ошибка
    """
    try:
        # Get guild member
        member = await guild.fetch_member(user_id)
        if not member:
            return False

        # Get role
        role = guild.get_role(role_id)
        if not role:
            return False

        # Check bot permissions
        bot_member = guild.me
        if not bot_member.guild_permissions.manage_roles:
            return False

        # Check if role is higher than bot's highest role
        if role.position >= bot_member.top_role.position:
            return False

        # Assign role
        await member.add_roles(role)
        return True

    except discord.Forbidden:
        return False
    except discord.HTTPException:
        return False
    except Exception:
        return False


async def remove_role(guild: discord.Guild, user_id: int, role_id: int) -> bool:
    """Снять роль у пользователя.

    Args:
        guild: Discord сервер
        user_id: ID пользователя
        role_id: ID роли для снятия

    Returns:
        True если успешно, False если ошибка
    """
    try:
        # Get guild member
        member = await guild.fetch_member(user_id)
        if not member:
            return False

        # Get role
        role = guild.get_role(role_id)
        if not role:
            return False

        # Check bot permissions
        bot_member = guild.me
        if not bot_member.guild_permissions.manage_roles:
            return False

        # Check if role is higher than bot's highest role
        if role.position >= bot_member.top_role.position:
            return False

        # Remove role
        await member.remove_roles(role)
        return True

    except discord.Forbidden:
        return False
    except discord.HTTPException:
        return False
    except Exception:
        return False


async def has_role(guild: discord.Guild, user_id: int, role_id: int) -> bool:
    """Проверить есть ли у пользователя роль.

    Args:
        guild: Discord сервер
        user_id: ID пользователя
        role_id: ID роли для проверки

    Returns:
        True если роль есть, False если нет
    """
    try:
        member = await guild.fetch_member(user_id)
        if not member:
            return False

        role = guild.get_role(role_id)
        if not role:
            return False

        return role in member.roles
    except Exception:
        return False
