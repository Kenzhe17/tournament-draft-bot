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

            # Check if tournament is closed and user doesn't have guild tag
            if not tournament.is_open and not has_guild_tag(interaction.user):
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Турнир закрыт. Для участия нужен тег {get_emoji('guild_tag')}r!z3",
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
                    new_view = await build_ultimatum_setup_view(interaction.guild_id, interaction.channel_id, tournament)
                    await interaction.message.edit(embed=new_embed, view=new_view)
            except Exception as e:
                logger.error(f"Error updating tournament message after join: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error in UltimatumJoinButton callback: {e}", exc_info=True)
            await interaction.response.send_message(
                "❌ Произошла ошибка при входе в турнир.",
                ephemeral=True
            )


class UltimatumLeaveButton(discord.ui.Button):
    """Button for leaving Ultimatum tournament."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Выйти",
            custom_id=f"ultimatum_leave:{guild_id}:{channel_id}"
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
                    "❌ Можно выйти только во время регистрации.",
                    ephemeral=True
                )
                return

            # Check if user is in tournament
            existing_player = await ultimatum_store.get_player_by_discord_id(
                tournament.id, interaction.user.id
            )
            if not existing_player:
                await interaction.response.send_message(
                    "❌ Вы не участвуете в этом турнире.",
                    ephemeral=True
                )
                return

            # Remove player
            await ultimatum_store.remove_player(tournament.id, interaction.user.id)

            await interaction.response.send_message(
                "✅ Вы вышли из турнира.",
                ephemeral=True
            )

            # Update the original message with new embed and view
            try:
                from cogs.ultimatum import UltimatumCog
                cog = interaction.client.get_cog('UltimatumCog')
                if cog:
                    new_embed = await cog._build_tournament_embed(tournament)
                    new_view = await build_ultimatum_setup_view(interaction.guild_id, interaction.channel_id, tournament)
                    await interaction.message.edit(embed=new_embed, view=new_view)
            except Exception as e:
                logger.error(f"Error updating tournament message after leave: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Error in UltimatumLeaveButton callback: {e}", exc_info=True)
            await interaction.response.send_message(
                "❌ Произошла ошибка.",
                ephemeral=True
            )


class UltimatumOrgMenuButton(discord.ui.Button):
    """Button for admin org menu."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="⚙️ Орг",
            custom_id=f"ultimatum_org:{guild_id}:{channel_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только организаторы могут использовать это меню.",
                ephemeral=True
            )
            return

        tournament = await ultimatum_store.get_active_tournament(self.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Нет активного турнира.",
                ephemeral=True
            )
            return

        # Create org menu view
        view = UltimatumOrgMenuView(self.guild_id, self.channel_id, tournament)
        
        embed = discord.Embed(
            title="⚙️ Меню организатора",
            description=f"Турнир: {tournament.name}\nСтатус: {tournament.status.value}\nОткрыт: {'Да' if tournament.is_open else 'Нет'}",
            color=discord.Color.red()
        )
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class UltimatumOrgMenuView(discord.ui.View):
    """View for org menu options."""

    def __init__(self, guild_id: int, channel_id: int, tournament):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament

        # Start button
        start_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="▶️ Старт",
            custom_id="org_start"
        )
        start_btn.callback = self.start_callback
        self.add_item(start_btn)

        # Open/Close button
        open_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="🔓 Открыть/Закрыть",
            custom_id="org_toggle_open"
        )
        open_btn.callback = self.toggle_open_callback
        self.add_item(open_btn)

        # Remove player button
        remove_btn = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="🗑️ Удалить игрока",
            custom_id="org_remove_player"
        )
        remove_btn.callback = self.remove_player_callback
        self.add_item(remove_btn)

    async def start_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        
        from ultimatum.tournament import UltimatumTournament
        tournament_logic = UltimatumTournament(ultimatum_store)

        players = await ultimatum_store.get_players(self.tournament.id)
        if len(players) != 16:
            await interaction.followup.send(
                f"❌ Нужно 16 игроков для старта. Сейчас: {len(players)}",
                ephemeral=True
            )
            return

        try:
            await tournament_logic.start_tournament(self.tournament.id, interaction.client)
            await interaction.followup.send("✅ Турнир начался!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error starting tournament: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Ошибка: {str(e)}", ephemeral=True)

    async def toggle_open_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        
        new_state = not self.tournament.is_open
        await ultimatum_store.set_tournament_open(self.tournament.id, new_state)
        await interaction.followup.send(
            f"✅ Турнир {'открыт' if new_state else 'закрыт'}",
            ephemeral=True
        )

    async def remove_player_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        
        # Show select menu with players
        players = await ultimatum_store.get_players(self.tournament.id)
        
        if not players:
            await interaction.followup.send("Нет игроков в турнире", ephemeral=True)
            return

        options = [
            discord.SelectOption(
                label=f"{player.username} ({player.discord_id})",
                value=str(player.discord_id)
            )
            for player in players
        ]

        select = UltimatumRemovePlayerSelect(self.guild_id, self.channel_id, self.tournament.id)
        select.options = options
        
        view = discord.ui.View()
        view.add_item(select)
        
        embed = discord.Embed(
            title="🗑️ Удалить игрока",
            description="Выберите игрока для удаления из турнира",
            color=discord.Color.red()
        )
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def start_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        
        from ultimatum.tournament import UltimatumTournament
        tournament_logic = UltimatumTournament(ultimatum_store)

        players = await ultimatum_store.get_players(self.tournament.id)
        if len(players) != 16:
            await interaction.followup.send(
                f"❌ Нужно 16 игроков для старта. Сейчас: {len(players)}",
                ephemeral=True
            )
            return

        try:
            await tournament_logic.start_tournament(self.tournament.id, interaction.client)
            await interaction.followup.send("✅ Турнир начался!", ephemeral=True)
            
            # Delete the tournament setup message
            try:
                channel = interaction.client.get_channel(self.channel_id)
                if channel:
                    async for msg in channel.history(limit=10):
                        if msg.embeds and msg.embeds[0].title and "УЛЬТИМАТУМ" in msg.embeds[0].title:
                            await msg.delete()
                            logger.info("Deleted tournament setup message after start")
                            break
            except Exception as e:
                logger.error(f"Error deleting tournament message: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Error starting tournament: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Ошибка: {str(e)}", ephemeral=True)

    async def toggle_open_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        
        new_state = not self.tournament.is_open
        await ultimatum_store.set_tournament_open(self.tournament.id, new_state)
        await interaction.followup.send(
            f"✅ Турнир {'открыт' if new_state else 'закрыт'}",
            ephemeral=True
        )

    async def remove_player_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        
        # Show select menu with players
        players = await ultimatum_store.get_players(self.tournament.id)
        
        if not players:
            await interaction.followup.send("Нет игроков в турнире", ephemeral=True)
            return

        options = [
            discord.SelectOption(
                label=f"{player.username} ({player.discord_id})",
                value=str(player.discord_id)
            )
            for player in players
        ]

        select = UltimatumRemovePlayerSelect(self.guild_id, self.channel_id, self.tournament.id)
        select.options = options
        
        view = discord.ui.View()
        view.add_item(select)
        
        embed = discord.Embed(
            title="🗑️ Удалить игрока",
            description="Выберите игрока для удаления из турнира",
            color=discord.Color.red()
        )
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class UltimatumRemovePlayerSelect(discord.ui.Select):
    """Select menu for removing player."""

    def __init__(self, guild_id: int, channel_id: int, tournament_id: str):
        super().__init__(
            placeholder="Выберите игрока для удаления",
            custom_id=f"org_select_remove:{guild_id}:{channel_id}:{tournament_id}"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament_id = tournament_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        
        discord_id = int(self.values[0])
        
        await ultimatum_store.remove_player(self.tournament_id, discord_id)
        
        await interaction.followup.send("✅ Игрок удален", ephemeral=True)
        
        # Update original tournament message
        try:
            tournament = await ultimatum_store.get_tournament(self.tournament_id)
            from cogs.ultimatum import UltimatumCog
            cog = interaction.client.get_cog('UltimatumCog')
            if cog:
                new_embed = await cog._build_tournament_embed(tournament)
                new_view = await build_ultimatum_setup_view(self.guild_id, self.channel_id, tournament)
                # Get the tournament message from the channel
                channel = interaction.client.get_channel(self.channel_id)
                if channel:
                    async for msg in channel.history(limit=10):
                        if msg.embeds and msg.embeds[0].title and "УЛЬТИМАТУМ" in msg.embeds[0].title:
                            await msg.edit(embed=new_embed, view=new_view)
                            break
        except Exception as e:
            logger.error(f"Error updating tournament message after player removal: {e}", exc_info=True)


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

            # Allow start from REGISTRATION or READY status
            if tournament.status != TournamentStatus.REGISTRATION:
                await interaction.response.send_message(
                    f"❌ Турнир не в фазе регистрации. Текущий статус: {tournament.status.value}",
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
            await tournament_logic.start_tournament(tournament.id, interaction.client)

            await interaction.response.send_message(
                "✅ Турнир начался!",
                ephemeral=True
            )

            # Update the embed and send round view
            try:
                from cogs.ultimatum import UltimatumCog
                from views.ultimatum_view import build_ultimatum_round_embed, build_ultimatum_round_view

                cog = interaction.client.get_cog('UltimatumCog')
                if cog:
                    tournament = await ultimatum_store.get_tournament(tournament.id)
                    # Build round embed for round 1
                    new_embed = await build_ultimatum_round_embed(tournament.id, 1, interaction.guild)
                    new_view = await build_ultimatum_round_view(interaction.client, tournament.id, 1)
                    
                    logger.info(f"Built round embed: {new_embed}, view: {new_view}")
                    
                    # Send new message with round embed and view instead of editing
                    round_message = await interaction.channel.send(embed=new_embed, view=new_view)
                    logger.info(f"Sent round message: {round_message.id}")
                    
                    # Track the round message for future updates
                    from views.ultimatum_view import round_messages
                    round_messages[interaction.guild_id] = (interaction.channel_id, round_message.id)
                    
                    # Delete old setup message
                    await interaction.message.delete()
                    logger.info("Deleted old setup message")
                else:
                    # Fallback: just clear items
                    self.view.clear_items()
                    await interaction.message.edit(view=self.view)
            except discord.NotFound:
                pass
            except Exception as e:
                logger.error(f"Error updating tournament message after start: {e}", exc_info=True)

            logger.info(f"Ultimatum tournament {tournament.id} started by {interaction.user.id}")

        except Exception as e:
            logger.error(f"Error in UltimatumStartButton callback: {e}", exc_info=True)
            await interaction.response.send_message(
                "❌ Произошла ошибка при старте турнира.",
                ephemeral=True
            )


async def build_ultimatum_setup_view(guild_id: int, channel_id: int, tournament=None) -> discord.ui.View:
    """Build setup view for Ultimatum tournament."""
    view = discord.ui.View(timeout=None)

    # Get current player count
    current_players = 0
    if tournament:
        players = await ultimatum_store.get_players(tournament.id)
        current_players = len(players)

    # Add join button
    join_button = UltimatumJoinButton(guild_id, channel_id, current_players)
    view.add_item(join_button)

    # Add leave button
    leave_button = UltimatumLeaveButton(guild_id, channel_id)
    view.add_item(leave_button)

    # Add org menu button
    org_button = UltimatumOrgMenuButton(guild_id, channel_id)
    view.add_item(org_button)

    return view
