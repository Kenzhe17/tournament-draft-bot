"""Coin Flip game cog."""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

from games.coin_flip import create_coin_flip_game


class CoinFlipCog(commands.Cog):
    """Coin Flip game cog."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="coin_flip", description="Монетка - Орёл или Решка")
    @app_commands.describe(bet="Ставка в монетах (10-10,000)", opponent="Соперник (для PvP)")
    async def coin_flip(
        self,
        interaction: discord.Interaction,
        bet: int,
        opponent: Optional[discord.Member] = None
    ):
        """Запустить игру Монетка."""
        await create_coin_flip_game(interaction, bet, opponent)


async def setup(bot: commands.Bot):
    """Setup the cog."""
    await bot.add_cog(CoinFlipCog(bot))
