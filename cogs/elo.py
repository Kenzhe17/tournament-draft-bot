"""Elo Rating System Cog — рейтинги «Ультиматума» (Соло/Дуо)."""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.groups import player_group, bind_commands
from ultimatum.elo import ult_elo_store

logger = logging.getLogger(__name__)

_fmt_choices = [
    app_commands.Choice(name="Ультиматум Соло", value="solo"),
    app_commands.Choice(name="Ультиматум Дуо", value="duo"),
]

_FMT_TITLE = {"solo": "Ультиматум Соло", "duo": "Ультиматум Дуо"}


class EloCog(commands.Cog):
    """Cog for Ultimatum Elo rating commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        bind_commands(player_group, self, ["ulti", "ulti_history"])

    @player_group.command(name="ulti", description="Показать рейтинг Ультиматума")
    @app_commands.describe(формат="Соло или Дуо")
    @app_commands.choices(формат=_fmt_choices)
    async def show_elo(
        self,
        interaction: discord.Interaction,
        формат: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        """Show personal Ultimatum Elo rating."""
        fmt = формат.value if формат else "solo"
        player = await ult_elo_store.get_rating(interaction.user.id, fmt)

        if not player:
            await interaction.response.send_message(
                f"❌ У вас пока нет рейтинга {_FMT_TITLE[fmt]}. "
                "Сыграйте рейтингованный матч!",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"🎮 {_FMT_TITLE[fmt]} — {player.get('username') or interaction.user.display_name}",
            color=discord.Color.from_rgb(168, 63, 255),
        )
        embed.add_field(name="Текущий ELO", value=str(player["elo"]), inline=True)
        embed.add_field(name="Пиковый ELO", value=str(player["peak_elo"]), inline=True)
        embed.add_field(name="Рейтингованных матчей", value=str(player["rated_matches"]), inline=True)
        embed.add_field(name="Победы", value=str(player["wins"]), inline=True)
        embed.add_field(name="Поражения", value=str(player["losses"]), inline=True)
        embed.add_field(name="K-Factor", value="32", inline=True)

        from utils.design import compose

        await interaction.response.send_message(
            view=compose(embed=embed), ephemeral=True
        )

    @player_group.command(name="ulti_history", description="История матчей рейтинга Ультиматума")
    @app_commands.describe(формат="Соло или Дуо")
    @app_commands.choices(формат=_fmt_choices)
    async def show_history(
        self,
        interaction: discord.Interaction,
        формат: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        """Show last 10 rated matches."""
        fmt = формат.value if формат else "solo"
        history = await ult_elo_store.get_history(interaction.user.id, fmt, 10)

        if not history:
            await interaction.response.send_message(
                "❌ Рейтингованных матчей не найдено.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"📊 {_FMT_TITLE[fmt]} — история",
            description=f"Последние {len(history)} матчей",
            color=discord.Color.blue(),
        )

        for entry in history:
            result_emoji = "✅" if entry["result"] == "WIN" else "❌"
            change = entry["elo_change"]
            sign = "+" if change > 0 else ""
            embed.add_field(
                name=f"{result_emoji} матч #{entry['match_id']}",
                value=(
                    f"{entry['elo_before']} → {entry['elo_after']} "
                    f"({sign}{change}) · команда avg {entry['side_avg_elo']} "
                    f"vs {entry['opp_avg_elo']}"
                ),
                inline=False,
            )

        from utils.design import compose

        await interaction.response.send_message(
            view=compose(embed=embed), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    """Register cog."""
    await bot.add_cog(EloCog(bot))
    logger.info("Cog 'elo' loaded")
