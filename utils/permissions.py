"""Проверки прав доступа."""

import discord
from discord import app_commands


def is_admin() -> app_commands.check:
    """Декоратор: только администратор сервера."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        raise app_commands.CheckFailure(
            "❌ Эта команда доступна только администраторам сервера."
        )

    return app_commands.check(predicate)
