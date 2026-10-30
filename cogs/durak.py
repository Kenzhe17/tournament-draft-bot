"""Durak Discord Activity command."""

import discord
from discord import app_commands
from discord.ext import commands


class DurakCog(commands.Cog):
    """Durak game commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="durak", description="Инструкция по запуску игры Дурак")
    async def durak(self, interaction: discord.Interaction):
        """Показать инструкцию по запуску игры Дурак."""
        embed = discord.Embed(
            title="🃏 Как запустить Дурак",
            description=(
                "Игра Дурак открывается через меню активностей в голосовом канале.\n\n"
                "**Инструкция:**"
            ),
            color=discord.Color(0x5865f2)
        )
        embed.add_field(
            name="1. Зайдите в голосовой канал",
            value="Присоединитесь к любому голосовому каналу",
            inline=False
        )
        embed.add_field(
            name="2. Откройте меню активностей",
            value="Внизу слева от Discord нажмите на иконку 'Активности' (камера/планшет)",
            inline=False
        )
        embed.add_field(
            name="3. Выберите Дурак",
            value="В списке активностей найдите иконку Дурак (🃏)",
            inline=False
        )
        embed.add_field(
            name="4. Пригласите друзей",
            value="Другие игроки в голосовом канале могут присоединиться через меню активностей",
            inline=False
        )
        embed.add_field(
            name="Настройки игры",
            value="После запуска выберите режим (подкидной/переводной) и количество игроков",
            inline=False
        )
        embed.set_footer(text="r!z3 Tournament Bot • Discord Activity")

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    """Setup the durak cog."""
    await bot.add_cog(DurakCog(bot))
