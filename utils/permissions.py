"""Проверки прав доступа."""

import discord
from discord import app_commands

from config import BOT_OWNER_ID
from storage.organizer_store import organizer_store


def is_admin() -> app_commands.check:
    """Декоратор: только администратор сервера, роль 'org' или создатель бота."""

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
            "❌ Эта команда доступна только администраторам или организаторам (роль 'org')."
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
    """Проверка: является ли пользователь администратором или имеет роль 'org'."""
    if user.guild_permissions.administrator or user.id == 1032544122600423427:
        return True

    # Check for 'org' role
    org_role = discord.utils.get(guild.roles, name="org")
    if org_role and org_role in user.roles:
        return True

    return False


def is_org_check(user: discord.Member, guild: discord.Guild) -> bool:
    """Проверка: является ли пользователь организатором (роль 'org') или администратором."""
    if user.guild_permissions.administrator or user.id == 1032544122600423427:
        return True

    # Check for 'org' role
    org_role = discord.utils.get(guild.roles, name="org")
    if org_role and org_role in user.roles:
        return True

    return False


def is_channel_organizer(user: discord.Member, guild: discord.Guild, channel_id: int) -> bool:
    """Проверка: является ли пользователь организатором канала."""
    # Check if user is global org/admin
    if is_org_check(user, guild):
        return True

    # Check channel organizers
    channel_orgs = organizer_store.get(guild.id, channel_id)
    if channel_orgs and channel_orgs.is_organizer(user.id):
        return True

    return False


def is_channel_organizer_decorator() -> app_commands.check:
    """Декоратор: только организатор канала или глобальный org/admin."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            raise app_commands.CheckFailure("❌ Эта команда доступна только на сервере.")

        if is_channel_organizer(interaction.user, interaction.guild, interaction.channel_id):
            return True

        raise app_commands.CheckFailure(
            "❌ Эта команда доступна только организаторам канала или глобальным организаторам."
        )

    return app_commands.check(predicate)


def is_bot_owner() -> app_commands.check:
    """Декоратор: только владелец бота."""

    async def predicate(interaction: discord.Interaction) -> bool:
        # Проверяем конкретный Discord ID владельца бота из конфига
        if interaction.user.id == BOT_OWNER_ID:
            return True

        raise app_commands.CheckFailure(
            "❌ Эта команда доступна только владельцу бота."
        )

    return app_commands.check(predicate)
