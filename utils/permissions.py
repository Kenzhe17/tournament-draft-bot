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