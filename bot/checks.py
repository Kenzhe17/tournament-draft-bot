"""Проверки прав доступа."""

import discord
from discord import app_commands


def is_admin() -> app_commands.check:
    """Только администратор сервера может управлять турниром."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            raise app_commands.CheckFailure("Команда доступна только на сервере.")
        member = interaction.user
        if isinstance(member, discord.Member):
            if member.guild_permissions.administrator:
                return True
        raise app_commands.CheckFailure("❌ Нужны права администратора.")

    return app_commands.check(predicate)
