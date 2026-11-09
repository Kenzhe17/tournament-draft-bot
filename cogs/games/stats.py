"""Админская статистика мини-игр: поставлено / выплачено / изъято."""

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID
from cogs.groups import games_group
from services.game_stats import get_stats

if False:  # TYPE_CHECKING
    from bot import TournamentBot


class GamesStatsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        from cogs.groups import bind_commands
        bind_commands(games_group, self, ["stats"])

    @games_group.command(name="stats", description="Статистика мини-игр (только для администраторов)")
    @app_commands.checks.has_permissions(administrator=True)
    async def game_stats(self, interaction: discord.Interaction):
        if interaction.user.id != BOT_OWNER_ID and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Недостаточно прав.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        await interaction.response.defer(ephemeral=True)

        stats = await get_stats(interaction.guild_id)
        if not stats:
            await interaction.followup.send("Статистика пуста — партии ещё не сыграны.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        names = {
            "coin_flip": "Монетка",
            "dice_roll": "Кубик",
            "roulette": "Рулетка",
            "guess_number": "Угадай число",
            "high_low": "High-Low",
        }
        lines = ["**Статистика мини-игр**\n"]
        total_w = total_p = 0
        for game, key in names.items():
            e = stats.get(game)
            if not e:
                continue
            w, p = e.get("wagered", 0), e.get("paid", 0)
            total_w += w
            total_p += p
            rtp = (p / w * 100) if w else 0
            lines.append(
                f"**{key}**: поставлено `{w:,}` | выплачено `{p:,}` | RTP {rtp:.1f}%"
            )
        net = total_w - total_p
        lines.append(
            f"\n**Итого**: поставлено `{total_w:,}` | выплачено `{total_p:,}` | "
            f"изъято системой `{net:,}`"
        )
        await interaction.followup.send("\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(GamesStatsCog(bot))
