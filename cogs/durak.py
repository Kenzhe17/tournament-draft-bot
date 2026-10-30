"""Durak Discord Activity command."""

import discord
from discord import app_commands
from discord.ext import commands

# Discord Application ID for Durak Activity
DURAK_APP_ID = 1782609823367873392


class DurakCog(commands.Cog):
    """Durak game commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="durak", description="Запустить игру Дурак в голосовом канале")
    async def durak(self, interaction: discord.Interaction):
        """Запустить игру Дурак."""
        # Check if user is in a voice channel
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "❌ Вы должны быть в голосовом канале чтобы запустить игру!",
                ephemeral=True
            )
            return

        voice_channel = interaction.user.voice.channel

        # Create invite for embedded application
        try:
            invite = await voice_channel.create_invite(
                target_type=discord.InviteTarget.embedded_application,
                target_application_id=DURAK_APP_ID,
                max_age=3600,  # 1 hour
                max_uses=10,  # Max 10 uses
                reason="Durak game launch"
            )

            embed = discord.Embed(
                title="🃏 Дурак",
                description=(
                    f"Запускаю игру Дурак в голосовом канале {voice_channel.mention}!\n\n"
                    f"Нажмите на ссылку ниже чтобы присоединиться к игре:"
                ),
                color=discord.Color(0x5865f2)
            )
            embed.add_field(
                name="Ссылка на игру",
                value=f"[🎮 Запустить Дурак]({invite.url})",
                inline=False
            )
            embed.add_field(
                name="Инструкция",
                value=(
                    "1. Нажмите на ссылку\n"
                    "2. Дождитесь загрузки игры\n"
                    "3. Пригласите других игроков из голосового канала\n"
                    "4. Выберите режим и начните игру!"
                ),
                inline=False
            )
            embed.set_footer(text="r!z3 Tournament Bot • Discord Activity")

            await interaction.response.send_message(embed=embed)

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ У меня нет прав создавать приглашения в этом голосовом канале!",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Ошибка при запуске игры: {str(e)}",
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    """Setup the durak cog."""
    await bot.add_cog(DurakCog(bot))
