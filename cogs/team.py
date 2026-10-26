"""Team commands for BR tournament."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from config import replace_emojis
from models.team import Team
from storage.team_store import team_store
from views.team_view import TeamStatusView, TeamJoinModal


class TeamCog(commands.Cog):
    """Команды для управления командами."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    team = app_commands.Group(name="team", description="Управление командами")

    @team.command(name="create", description="Создать команду")
    @app_commands.describe(password="Пароль команды (обязательно)")
    async def create(self, interaction: discord.Interaction, password: str):
        """Создать новую команду."""
        # Check if user already in a team
        existing_team = team_store.get_by_member(interaction.user.id)
        if existing_team:
            await interaction.response.send_message(
                replace_emojis("❌ Вы уже состоите в команде!"),
                ephemeral=True
            )
            return

        # Generate unique team name
        base_name = f"Команда {interaction.user.display_name}"
        team_name = base_name
        counter = 1

        # Check if name already exists and find a unique one
        while team_store.get_by_name(interaction.guild_id, team_name):
            team_name = f"{base_name} {counter}"
            counter += 1

        # Create team
        team = Team(
            team_id="",
            team_name=team_name,
            password=password,
            captain_id=interaction.user.id,
            guild_id=interaction.guild_id
        )

        await team_store.create(team)

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} Команда создана!",
            description=(
                f"{replace_emojis('white_dot')} **Название:** {team.team_name}\n"
                f"{replace_emojis('white_dot')} **Пароль:** ||{password}||\n"
                f"{replace_emojis('white_dot')} **Капитан:** {interaction.user.mention}\n\n"
                f"{replace_emojis('white_arrow')} Используйте `/team status` для управления командой"
            ),
            color=discord.Color.from_rgb(114, 137, 218)  # Pastel blue
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @team.command(name="delete", description="Удалить команду")
    async def delete(self, interaction: discord.Interaction):
        """Удалить команду (только капитан)."""
        team = team_store.get_by_captain(interaction.user.id)
        if not team:
            await interaction.response.send_message(
                replace_emojis("❌ Вы не капитан команды!"),
                ephemeral=True
            )
            return

        # Defer response since we need to do async operations
        await interaction.response.defer(ephemeral=True)

        # Delete team status message if exists
        async for msg in interaction.channel.history(limit=50):
            if msg.author == interaction.client.user and msg.embeds:
                embed = msg.embeds[0]
                if embed.title and team.team_name in embed.title:
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                    break

        await team_store.delete(team.team_id)

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} Команда удалена",
            description=f"{replace_emojis('white_arrow')} Команда **{team.team_name}** была удалена.",
            color=discord.Color.from_rgb(235, 100, 100)  # Pastel red
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @team.command(name="status", description="Статус команды")
    @app_commands.describe(team_name="Название команды (опционально)")
    async def status(self, interaction: discord.Interaction, team_name: str = None):
        """Показать статус команды."""
        # If team_name is provided, search for that team
        if team_name:
            team = team_store.get_by_name(interaction.guild_id, team_name)
            if not team:
                await interaction.response.send_message(
                    replace_emojis("❌ Команда с таким названием не найдена!"),
                    ephemeral=True
                )
                return
        else:
            # Otherwise, get user's team
            team = team_store.get_by_member(interaction.user.id)
            if not team:
                await interaction.response.send_message(
                    replace_emojis("❌ Вы не состоите в команде! Укажите название команды или создайте свою."),
                    ephemeral=True
                )
                return

        # Build member list
        member_mentions = []
        for member_id in team.members:
            member = interaction.guild.get_member(member_id)
            if member:
                mention = member.mention
                if member_id == team.captain_id:
                    mention += " 👑"
                member_mentions.append(mention)

        # Determine status
        count = team.member_count()
        if count >= 4:
            status_emoji = "🟢"
            status_text = "(Готова)"
        else:
            status_emoji = "🔴"
            missing = 4 - count
            status_text = f"(Недостаёт {missing} игроков)"

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {team.team_name}",
            description=(
                f"{replace_emojis('white_dot')} **Состав:** {count}/4\n"
                f"{replace_emojis('white_arrow')} {', '.join(member_mentions) if member_mentions else 'Нет участников'}\n\n"
                f"{replace_emojis('white_dot')} **Статус:** {status_emoji} {status_text}"
            ),
            color=discord.Color.from_rgb(114, 137, 218)  # Pastel blue
        )

        # Create view with buttons for dynamic updates
        from views.team_view import TeamStatusView
        view = TeamStatusView(team, interaction.user.id)

        # Check if there's an existing status message
        message = None
        if team.status_message_id:
            try:
                message = await interaction.channel.fetch_message(team.status_message_id)
                # Update existing message
                await message.edit(embed=embed, view=view)
            except (discord.NotFound, discord.Forbidden):
                # Message doesn't exist or can't access, create new
                message = None

        if not message:
            # Create new message
            message = await interaction.channel.send(embed=embed, view=view)
            # Save message_id to team
            team.status_message_id = message.id
            await team_store.update_team(team)

        # Store message in view for dynamic updates
        view.message = message

        # Send ephemeral confirmation
        await interaction.response.send_message(
            replace_emojis("✅ Статус команды обновлен"),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    """Setup the team cog."""
    await bot.add_cog(TeamCog(bot))
