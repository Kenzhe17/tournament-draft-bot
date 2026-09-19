"""Проверки прав доступа."""

import discord
from discord import app_commands


def is_admin() -> app_commands.check:
    """Декоратор: только администратор сервера или создатель бота."""

    async def predicate(interaction: discord.Interaction) -> bool:
        # Проверяем права админа ИЛИ ваш конкретный Discord ID
        if interaction.user.guild_permissions.administrator or interaction.user.id == 1032544122600423427:
            return True
        raise app_commands.CheckFailure(
            "❌ Эта команда доступна только администраторам сервера."
        )

    return app_commands.check(predicate)


def is_org() -> app_commands.check:
    """Декоратор: только роль 'org' или администратор сервера или создатель бота."""

    async def predicate(interaction: discord.Interaction) -> bool:
        # Проверяем права админа ИЛИ ваш конкретный Discord ID ИЛИ роль 'org'
        if interaction.user.guild_permissions.administrator or interaction.user.id == 1032544122600423427:
            return True

        # Check for 'org' role
        if interaction.guild:
            org_role = discord.utils.get(interaction.guild.roles, name="org")
            if org_role and org_role in interaction.user.roles:
                return True

        raise app_commands.CheckFailure(
            "❌ Эта команда доступна только организаторам (роль 'org') или администраторам."
        )

    return app_commands.check(predicate)


def is_admin_check(user: discord.Member, guild: discord.Guild) -> bool:
    """Проверка: является ли пользователь администратором."""
    return user.guild_permissions.administrator or user.id == 1032544122600423427


def is_org_check(user: discord.Member, guild: discord.Guild) -> bool:
    """Проверка: является ли пользователь организатором (роль 'org') или администратором."""
    if user.guild_permissions.administrator or user.id == 1032544122600423427:
        return True

    # Check for 'org' role
    org_role = discord.utils.get(guild.roles, name="org")
    if org_role and org_role in user.roles:
        return True

    return False