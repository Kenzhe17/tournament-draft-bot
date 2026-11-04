"""View for Ultimatum Solo Tournament setup with join button."""

import logging

import discord
from discord.ext import commands

from config import replace_emojis, get_emoji, GUILD_ID
from ultimatum.models import TournamentStatus
from ultimatum.store import ultimatum_store

logger = logging.getLogger(__name__)


def has_guild_tag(user: discord.Member) -> bool:
    """Check if user has the guild tag for our server (primary_guild check)."""
    return user.primary_guild and user.primary_guild.id == GUILD_ID


class UltimatumJoinButton(discord.ui.Button):
    """Button for joining Ultimatum tournament."""

    def __init__(self, guild_id: int, channel_id: int, current: int, limit: int = 16):
        label = f"Войти ({current}/{limit})"
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"ultimatum_join:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.current = current
        self.limit = limit

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            tournament = await ultimatum_store.get_active_tournament(self.guild_id)
            if not tournament:
                await interaction.response.send_message(
                    "❌ Нет активного турнира.",
                    ephemeral=True
                )
                return

            if tournament.status != TournamentStatus.REGISTRATION:
                await interaction.response.send_message(
                    "❌ Турнир не в фазе регистрации.",
                    ephemeral=True
                )
                return

            # Check if user has guild tag
            if not has_guild_tag(interaction.user):
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Для участия нужен тег {get_emoji('guild_tag')}r!z3",
                    ephemeral=True
                )
                return

            # Check if tournament is full
            players = await ultimatum_store.get_players(tournament.id)
            if len(players) >= self.limit:
                await interaction.response.send_message(
                    f"❌ Турнир заполнен (максимум {self.limit} игроков).",
                    ephemeral=True
                )
                return

            # Check if user already joined
            existing_player = await ultimatum_store.get_player_by_discord_id(
                tournament.id, interaction.user.id
            )
            if existing_player:
                await interaction.response.send_message(
                    "❌ Вы уже участвуете в турнире.",
                    ephemeral=True
                )
                return

            # Add player to tournament
            await ultimatum_store.add_player(
                tournament.id,
                interaction.user.id,
                interaction.user.display_name
            )

            # Update button label with new count
            players = await ultimatum_store.get_players(tournament.id)
            self.label = f"Войти ({len(players)}/{self.limit})"

            await interaction.response.send_message(
                f"✅ Вы вошли в турнир! Игроков: {len(players)}/{self.limit}",
                ephemeral=True
            )

            # Update the original message with new embed and view
            try:
                from cogs.ultimatum import UltimatumCog
                # Get the cog from the bot
                cog = interaction.client.get_cog('UltimatumCog')
                if cog:
                    new_embed = await cog._build_tournament_embed(tournament)
                    await interaction.message.edit(embed=new_embed, view=self.view)
                else:
                    await interaction.message.edit(view=self.view)
            except discord.NotFound:
                pass
            except Exception as e:
                logger.error(f"Error updating tournament message: {e}")

            logger.info(f"User {interaction.user.id} joined Ultimatum tournament {tournament.id}")

        except Exception as e:
            logger.error(f"Error in UltimatumJoinButton callback: {e}", exc_info=True)
            await interaction.response.send_message(
                "❌ Произошла ошибка при входе в турнир.",
                ephemeral=True
            )


class UltimatumStartButton(discord.ui.Button):
    """Button for starting Ultimatum tournament."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Старт",
            emoji="▶️",
            custom_id=f"ultimatum_start:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            tournament = await ultimatum_store.get_active_tournament(self.guild_id)
            if not tournament:
                await interaction.response.send_message(
                    "❌ Нет активного турнира.",
                    ephemeral=True
                )
                return

            if tournament.status != TournamentStatus.REGISTRATION:
                await interaction.response.send_message(
                    "❌ Турнир не в фазе регистрации.",
                    ephemeral=True
                )
                return

            # Check if tournament has enough players
            players = await ultimatum_store.get_players(tournament.id)
            if len(players) != 16:
                await interaction.response.send_message(
                    f"❌ Нужно 16 игроков для старта, сейчас {len(players)}.",
                    ephemeral=True
                )
                return

            # Import tournament logic here to avoid circular import
            from ultimatum.tournament import UltimatumTournament
            tournament_logic = UltimatumTournament(ultimatum_store)

            # Start tournament
            await tournament_logic.start_tournament(tournament.id)

            await interaction.response.send_message(
                "✅ Турнир начался!",
                ephemeral=True
            )

            # Update the embed and view
            try:
                from cogs.ultimatum import UltimatumCog
                cog = interaction.client.get_cog('UltimatumCog')
                if cog:
                    tournament = await ultimatum_store.get_tournament(tournament.id)
                    new_embed = await cog._build_tournament_embed(tournament)
                    # Clear view or update with round view
                    self.view.clear_items()
                    await interaction.message.edit(embed=new_embed, view=self.view)
                else:
                    self.view.clear_items()
                    await interaction.message.edit(view=self.view)
            except discord.NotFound:
                pass
            except Exception as e:
                logger.error(f"Error updating tournament message after start: {e}")

            logger.info(f"Ultimatum tournament {tournament.id} started by {interaction.user.id}")

        except Exception as e:
            logger.error(f"Error in UltimatumStartButton callback: {e}", exc_info=True)
            await interaction.response.send_message(
                "❌ Произошла ошибка при старте турнира.",
                ephemeral=True
            )


def build_ultimatum_setup_view(guild_id: int, channel_id: int, current_players: int = 0) -> discord.ui.View:
    """Build setup view for Ultimatum tournament."""
    view = discord.ui.View(timeout=None)

    # Add join button
    join_button = UltimatumJoinButton(guild_id, channel_id, current_players)
    view.add_item(join_button)

    # Add start button
    start_button = UltimatumStartButton(guild_id, channel_id)
    view.add_item(start_button)

    return view
