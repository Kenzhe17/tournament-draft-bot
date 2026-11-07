"""Команда /getemojiid — выгрузка ID серверных эмодзи.

Отправляет файл emoji_ids.py в формате EMOJI_IDS для utils/design/emojis.py.
"""

from __future__ import annotations

import io

import discord
from discord import app_commands
from discord.ext import commands


class EmojiCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="getemojiid", description="Выгрузить ID всех эмодзи сервера (для EMOJI_IDS)"
    )
    @app_commands.default_permissions(administrator=True)
    async def getemojiid(self, interaction: discord.Interaction) -> None:
        emojis = sorted(
            interaction.guild.emojis, key=lambda e: (e.name or "").lower()
        )
        lines = ["EMOJI_IDS: dict[str, str] = {"]
        for emoji in emojis:
            lines.append(f'    "{emoji.name}": "{emoji.id}",')
        lines.append("}")
        content = "\n".join(lines)

        file = discord.File(
            io.BytesIO(content.encode("utf-8")), filename="emoji_ids.py"
        )
        await interaction.response.send_message(
            content=(
                f"✅ Эмодзи на сервере: **{len(emojis)}**.\n"
                "Файл `emoji_ids.py` — формат для `EMOJI_IDS` в `utils/design/emojis.py`."
            ),
            file=file,
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EmojiCog(bot))
