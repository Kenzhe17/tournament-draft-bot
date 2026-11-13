"""View for Ultimatum Solo Tournament setup with join button."""

import logging

import discord
from utils.design import compose

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

            # VAC — временная блокировка участия в турнирах
            from services import vac_service
            from services.vac_service import vac_blocked_text
            try:
                vac = await vac_service.check_vac(self.guild_id, interaction.user.id)
            except vac_service.VacStoreUnavailable:
                await interaction.response.send_message(
                    "⚠️ Сервис VAC временно недоступен. Попробуйте позже.",
                    ephemeral=True
                )
                return
            if vac is not None:
                await interaction.response.send_message(
                    vac_blocked_text(
                        int(vac["expires_at"].timestamp()),
                        reason=vac.get("reason"), level=vac.get("level"),
                    ),
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
                    await interaction.message.edit( view=compose(new_embed, new_view))
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
                    await interaction.message.edit( view=compose(new_embed, new_view))
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
        
        await interaction.response.send_message( view=compose(embed, view), ephemeral=True)


async def update_setup_message(
    bot, guild_id: int, channel_id: int, tournament
) -> None:
    """Обновить карточку регистрации по сохранённым ID (без history-scan)."""
    try:
        channel = bot.get_channel(channel_id) or (
            bot.get_channel(tournament.setup_channel_id)
            if tournament and tournament.setup_channel_id else None
        )
        message_id = tournament.setup_message_id if tournament else None
        if not channel or not message_id:
            return
        from cogs.ultimatum import UltimatumCog
        cog = bot.get_cog('UltimatumCog')
        if not cog:
            return
        new_embed = await cog._build_tournament_embed(tournament)
        new_view = await build_ultimatum_setup_view(guild_id, channel_id, tournament)
        message = await channel.fetch_message(message_id)
        await message.edit(view=compose(new_embed, new_view))
    except Exception as e:
        logger.error(f"Error updating tournament message: {e}", exc_info=True)


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

    async def _vac_sweep(self, interaction: discord.Interaction) -> bool:
        """True — были исключены VAC-игроки, старт нужно повторить.

        При недоступности VAC-хранилища возвращает "unavailable" —
        вызывающий код обязан прервать операцию.
        """
        from services import vac_service
        players = await ultimatum_store.get_players(self.tournament.id)
        try:
            vac_ids = await vac_service.vac_active_ids(
                self.guild_id, [p.discord_id for p in players]
            )
        except vac_service.VacStoreUnavailable:
            await interaction.followup.send(
                "⚠️ Сервис VAC временно недоступен. Старт отменён, попробуйте позже.",
                ephemeral=True
            )
            return "unavailable"
        if not vac_ids:
            return False
        for pid in vac_ids:
            try:
                await ultimatum_store.remove_player(self.tournament.id, pid)
            except Exception:
                logger.exception("VAC: failed to remove ultimatum player %s", pid)
        await interaction.followup.send(
            "⚠️ Перед стартом исключены игроки с активным VAC. "
            "Нажмите «Старт» ещё раз.",
            ephemeral=True
        )
        return True

    async def _fresh_tournament(self):
        return await ultimatum_store.get_tournament(self.tournament.id)

    async def start_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        from ultimatum.tournament import UltimatumTournament
        tournament_logic = UltimatumTournament(ultimatum_store)

        # Актуальный статус из БД — view мог устареть.
        self.tournament = await self._fresh_tournament() or self.tournament
        if self.tournament.status == TournamentStatus.COMPLETED or                 self.tournament.status == TournamentStatus.CANCELLED:
            await interaction.followup.send(
                f"❌ Турнир уже завершён ({self.tournament.status.value}).",
                ephemeral=True
            )
            return

        sweep = await self._vac_sweep(interaction)
        if sweep:
            return

        try:
            # begin_tournament внутри атомарно проверяет статус и 16 игроков.
            await tournament_logic.start_tournament(
                self.tournament.id, interaction.client
            )
            self.tournament = await self._fresh_tournament() or self.tournament
            await interaction.followup.send("✅ Турнир начался!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error starting tournament: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Ошибка: {str(e)}", ephemeral=True)

    async def toggle_open_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        self.tournament = await self._fresh_tournament() or self.tournament
        if self.tournament.status != TournamentStatus.REGISTRATION:
            await interaction.followup.send(
                "❌ Открыть/закрыть регистрацию можно только во время регистрации.",
                ephemeral=True
            )
            return

        new_state = not self.tournament.is_open
        await ultimatum_store.set_tournament_open(self.tournament.id, new_state)
        self.tournament = await self._fresh_tournament() or self.tournament

        await interaction.followup.send(
            f"✅ Турнир {'открыт' if new_state else 'закрыт'}",
            ephemeral=True
        )

        # Обновить карточку регистрации по сохранённому ID.
        await update_setup_message(
            interaction.client, self.guild_id, self.channel_id, self.tournament
        )

    async def remove_player_callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        self.tournament = await self._fresh_tournament() or self.tournament
        if self.tournament.status != TournamentStatus.REGISTRATION:
            await interaction.followup.send(
                "❌ Удалять игроков можно только во время регистрации.",
                ephemeral=True
            )
            return

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

        await interaction.followup.send( view=compose(embed, view), ephemeral=True)


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

        tournament = await ultimatum_store.get_tournament(self.tournament_id)
        if not tournament or tournament.status != TournamentStatus.REGISTRATION:
            await interaction.followup.send(
                "❌ Удалять игроков можно только во время регистрации.",
                ephemeral=True
            )
            return

        discord_id = int(self.values[0])

        await ultimatum_store.remove_player(self.tournament_id, discord_id)

        await interaction.followup.send("✅ Игрок удален", ephemeral=True)

        # Обновить карточку регистрации по сохранённому ID.
        await update_setup_message(
            interaction.client, self.guild_id, self.channel_id, tournament
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

            if tournament.status in (
                TournamentStatus.COMPLETED, TournamentStatus.CANCELLED
            ):
                await interaction.response.send_message(
                    f"❌ Турнир уже завершён ({tournament.status.value}).",
                    ephemeral=True
                )
                return

            # VAC: исключить заблокированных до старта
            from services import vac_service
            players = await ultimatum_store.get_players(tournament.id)
            try:
                vac_ids = await vac_service.vac_active_ids(
                    interaction.guild_id, [p.discord_id for p in players]
                )
            except vac_service.VacStoreUnavailable:
                await interaction.response.send_message(
                    "⚠️ Сервис VAC временно недоступен. Старт отменён, попробуйте позже.",
                    ephemeral=True
                )
                return
            if vac_ids:
                for pid in vac_ids:
                    try:
                        await ultimatum_store.remove_player(tournament.id, pid)
                    except Exception:
                        logger.exception("VAC: failed to remove player %s", pid)
                await interaction.response.send_message(
                    "⚠️ Перед стартом исключены игроки с активным VAC. "
                    "Нажмите «Старт» ещё раз.",
                    ephemeral=True
                )
                return

            # Import tournament logic here to avoid circular import
            from ultimatum.tournament import UltimatumTournament
            tournament_logic = UltimatumTournament(ultimatum_store)

            # Start tournament (идемпотентно: публикация карточки раунда
            # и удаление карточки регистрации внутри start_tournament).
            await tournament_logic.start_tournament(tournament.id, interaction.client)

            await interaction.response.send_message(
                "✅ Турнир начался!",
                ephemeral=True
            )

            logger.info(f"Ultimatum tournament {tournament.id} started by {interaction.user.id}")

        except Exception as e:
            logger.error(f"Error in UltimatumStartButton callback: {e}", exc_info=True)
            try:
                await interaction.response.send_message(
                    "❌ Произошла ошибка при старте турнира.",
                    ephemeral=True
                )
            except Exception:
                logger.exception("UltimatumStartButton error reply failed")


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
