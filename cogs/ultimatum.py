"""Ultimatum Solo Tournament Cog."""

import logging
from typing import Optional

import discord
from utils.design import compose
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID, ULT_ROLE_ID, replace_emojis, get_emoji
from storage.db import get_pool
from ultimatum.models import TournamentStatus, PlayerStatus, Tournament
from ultimatum.store import ultimatum_store
from ultimatum.tournament import UltimatumTournament
from ultimatum.elo_store import elo_store
from views.ultimatum_view import build_ultimatum_round_view
from views.ultimatum_setup_view import build_ultimatum_setup_view, update_setup_message
from ultimatum.tournament import _log_event

logger = logging.getLogger(__name__)

ultimatum_group = app_commands.Group(
    name="ultimatum", description="Ultimatum Solo Tournament"
)


class UltimatumCog(commands.Cog):
    """Cog for Ultimatum Solo Tournament commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tournament_logic = UltimatumTournament(ultimatum_store)

    async def cog_load(self) -> None:
        """Вложить /tournament ultimatum в живую группу tournament + биндинг."""
        from cogs.groups import bind_commands

        tourney = self.bot.tree.get_command("tournament")
        if isinstance(tourney, app_commands.Group) and ultimatum_group.parent is None:
            try:
                tourney.add_command(ultimatum_group)
            except Exception as e:
                logger.warning("Не удалось добавить ultimatum_group: %s", e)
        bind_commands(ultimatum_group, self, ["create", "delete", "test_fill"])

    @ultimatum_group.command(name="create", description="Create new Ultimatum tournament")
    @app_commands.checks.has_permissions(administrator=True)
    @commands.is_owner()
    async def create_tournament(
        self,
        interaction: discord.Interaction
    ) -> None:
        """Create a new tournament."""
        # Check for existing active tournament
        existing = await ultimatum_store.get_active_tournament(interaction.guild_id)
        if existing:
            await interaction.response.send_message(
                "❌ Active tournament already exists. Complete or cancel it first.",
                ephemeral=True
            )
            return

        # Generate tournament name
        tournament_name = f"Ultimatum Solo #{interaction.created_at.strftime('%Y%m%d')}"

        # Create tournament
        tournament = await ultimatum_store.create_tournament(
            name=tournament_name,
            guild_id=interaction.guild_id,
            created_by=interaction.user.id
        )

        # Update status to REGISTRATION
        await ultimatum_store.update_tournament_status(
            tournament.id, TournamentStatus.REGISTRATION
        )
        # Reload tournament from database to get updated status
        tournament = await ultimatum_store.get_tournament(tournament.id)

        await interaction.response.send_message(
            f"✅ Tournament '{tournament_name}' created! ID: {tournament.id}",
            ephemeral=True
        )

        # Send setup embed with Join button
        embed = await self._build_tournament_embed(tournament)
        view = await build_ultimatum_setup_view(interaction.guild_id, interaction.channel_id, tournament)
        message = await interaction.channel.send( view=compose(embed, view))

        # Запоминаем ID карточки регистрации — вместо поиска по заголовку.
        await ultimatum_store.set_setup_message(
            tournament.id, interaction.channel_id, message.id
        )
        _log_event(
            "created", tournament.id,
            guild_id=interaction.guild_id, phase=tournament.status.value,
            channel_id=interaction.channel_id, message_id=message.id,
        )

        logger.info(f"Tournament created: {tournament.id} by {interaction.user.id}")



    @ultimatum_group.command(name="test_fill", description="Fill tournament with test players (for testing)")
    @app_commands.checks.has_permissions(administrator=True)
    async def test_fill_tournament(self, interaction: discord.Interaction) -> None:
        """Fill tournament with test players to reach 16."""
        tournament = await ultimatum_store.get_active_tournament(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ No active tournament found.",
                ephemeral=True
            )
            return

        if tournament.status != TournamentStatus.REGISTRATION:
            await interaction.response.send_message(
                "❌ Tournament is not in registration phase.",
                ephemeral=True
            )
            return

        # Get current players
        players = await ultimatum_store.get_players(tournament.id)
        current_count = len(players)
        needed = 16 - current_count

        if needed <= 0:
            await interaction.response.send_message(
                f"✅ Tournament is already full ({current_count}/16 players).",
                ephemeral=True
            )
            return

        # Add test players
        test_names = [
            "TestPlayer1", "TestPlayer2", "TestPlayer3", "TestPlayer4",
            "TestPlayer5", "TestPlayer6", "TestPlayer7", "TestPlayer8",
            "TestPlayer9", "TestPlayer10", "TestPlayer11", "TestPlayer12",
            "TestPlayer13", "TestPlayer14", "TestPlayer15", "TestPlayer16"
        ]

        added = 0
        for i in range(needed):
            # Use unique fake Discord IDs
            fake_id = 1000000 + i + current_count
            name = test_names[i + current_count] if (i + current_count) < len(test_names) else f"TestPlayer{i + current_count}"

            try:
                await ultimatum_store.add_player(
                    tournament.id,
                    fake_id,
                    name
                )
                added += 1
            except ValueError:
                # Player already exists, skip
                pass

        await interaction.response.send_message(
            f"✅ Added {added} test players. Total: {current_count + added}/16",
            ephemeral=True
        )

        # Update the tournament message to show new players
        try:
            tournament = await ultimatum_store.get_tournament(tournament.id)
            await update_setup_message(
                interaction.client, interaction.guild_id,
                interaction.channel_id, tournament
            )
        except Exception as e:
            logger.error(f"Error updating tournament message after test fill: {e}")

        logger.info(f"Test fill: added {added} players to tournament {tournament.id}")

    @ultimatum_group.command(name="delete", description="Delete active Ultimatum tournament")
    @app_commands.checks.has_permissions(administrator=True)
    async def delete_tournament(self, interaction: discord.Interaction) -> None:
        """Delete the active tournament."""
        tournament = await ultimatum_store.get_active_tournament(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ No active tournament found.",
                ephemeral=True
            )
            return

        # Remove tournament role from all players
        if tournament.guild_id:
            guild = interaction.guild
            if guild:
                role = guild.get_role(ULT_ROLE_ID)
                if role:
                    players = await ultimatum_store.get_players(tournament.id)
                    for player in players:
                        try:
                            member = guild.get_member(player.discord_id)
                            if member:
                                await member.remove_roles(role)
                        except Exception as e:
                            logger.error(f"Error removing role from player {player.discord_id}: {e}")

        # Delete tournament from database
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM ultimatum_tournaments WHERE id = $1",
                tournament.id
            )

        await interaction.response.send_message(
            f"✅ Tournament '{tournament.name}' deleted.",
            ephemeral=True
        )

        logger.info(f"Tournament {tournament.id} deleted by {interaction.user.id}")

    @ultimatum_group.command(
        name="resync",
        description="Восстановить карточку раундаUltimatum (после сбоя публикации)"
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        channel="Канал с карточкой турнира (для турниров без сохранённых ID)"
    )
    async def resync_tournament(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Довести публикацию карточки раунда до конца.

        Идемпотентно: не создаёт новых матчей, только восстанавливает
        Discord-сообщение и привязывает его к турниру.
        """
        await interaction.response.defer(ephemeral=True)
        tournament = await ultimatum_store.get_active_tournament(interaction.guild_id)
        if not tournament:
            await interaction.followup.send(
                "❌ Нет активного турнира.", ephemeral=True
            )
            return

        if channel is not None and not tournament.round_channel_id:
            await ultimatum_store.set_round_channel(tournament.id, channel.id)
            tournament = await ultimatum_store.get_tournament(tournament.id)

        round_number = tournament.current_round or 1
        try:
            await self.tournament_logic.publish_round_message(
                tournament.id, round_number, self.bot
            )
        except Exception as e:
            logger.error(f"resync publish failed: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Ошибка публикации: {e}", ephemeral=True
            )
            return

        # Для старых турниров без setup_message_id — найти и удалить
        # устаревшую карточку регистрации в указанном канале.
        removed_setup = False
        if not tournament.setup_message_id:
            target = channel or (
                interaction.guild.get_channel(tournament.round_channel_id)
                if tournament.round_channel_id else None
            )
            if target is not None:
                try:
                    async for msg in target.history(limit=50):
                        if msg.author != self.bot.user or not msg.embeds:
                            continue
                        emb = msg.embeds[0]
                        text = f"{emb.title or ''} {emb.description or ''}"
                        if "УЛЬТИМАТУМ" in text and "регистрац" in text.lower():
                            await msg.delete()
                            removed_setup = True
                            break
                except Exception as e:
                    logger.error(f"resync setup cleanup failed: {e}", exc_info=True)

        fresh = await ultimatum_store.get_tournament(tournament.id)
        await interaction.followup.send(
            f"✅ Синхронизация завершена.\n"
            f"Статус: {fresh.status.value}, раунд: {fresh.current_round}\n"
            f"Карточка раунда: "
            f"{'<#' + str(fresh.round_channel_id) + '> сообщение ' + str(fresh.round_message_id) if fresh.round_message_id else 'не привязана'}\n"
            f"Устаревшая карточка регистрации: "
            f"{'удалена' if removed_setup else 'не найдена/уже удалена'}",
            ephemeral=True
        )
        _log_event(
            "ui_sync_failed" if not fresh.round_message_id else "round_message_sent",
            tournament.id,
            guild_id=interaction.guild_id, phase=fresh.status.value,
            round_number=round_number,
            channel_id=fresh.round_channel_id,
            message_id=fresh.round_message_id,
        )

    # ------------------------------------------------------------------
    # Восстановление после рестарта
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Идемпотентное восстановление незавершённых турниров."""
        await self.recover_tournaments()

    async def recover_tournaments(self) -> None:
        try:
            tournaments = await ultimatum_store.list_unfinished_tournaments()
        except Exception:
            logger.exception("ultimatum_recovery_list_failed")
            return
        for t in tournaments:
            _log_event(
                "recovery_started", t.id,
                guild_id=t.guild_id, phase=t.status.value,
                round_number=t.current_round,
            )
            try:
                await self._recover_tournament(t)
            except Exception:
                logger.exception("ultimatum_recovery_failed tid=%s", t.id)
                _log_event(
                    "ui_sync_failed", t.id, guild_id=t.guild_id,
                    phase=t.status.value, round_number=t.current_round,
                    reason="recovery_exception",
                )
            else:
                _log_event(
                    "recovery_completed", t.id,
                    guild_id=t.guild_id, phase=t.status.value,
                    round_number=t.current_round,
                )

    async def _recover_tournament(self, tournament: Tournament) -> None:
        guild = self.bot.get_guild(tournament.guild_id)
        if not guild:
            return

        if tournament.status == TournamentStatus.REGISTRATION:
            await self._recover_setup_card(tournament, guild)
            return

        # Начавшийся турнир — гарантируем карточку текущего раунда.
        round_number = tournament.current_round or 1
        channel = None
        if tournament.round_channel_id:
            channel = guild.get_channel(tournament.round_channel_id)
        if channel is None and tournament.setup_channel_id:
            channel = guild.get_channel(tournament.setup_channel_id)

        message_alive = False
        if channel is not None and tournament.round_message_id:
            try:
                await channel.fetch_message(tournament.round_message_id)
                message_alive = True
            except discord.NotFound:
                message_alive = False

        if message_alive:
            # Сообщение живо — только перевешиваем view для interaction'ов.
            from views.ultimatum_view import round_messages
            view = await build_ultimatum_round_view(
                self.bot, tournament.id, round_number
            )
            self.bot.add_view(view, message_id=tournament.round_message_id)
            round_messages[guild.id] = (channel.id, tournament.round_message_id)
            return

        await self.tournament_logic.publish_round_message(
            tournament.id, round_number, self.bot
        )
        fresh = await ultimatum_store.get_tournament(tournament.id)
        if fresh and fresh.round_message_id:
            view = await build_ultimatum_round_view(
                self.bot, tournament.id, round_number
            )
            self.bot.add_view(view, message_id=fresh.round_message_id)

    async def _recover_setup_card(self, tournament: Tournament, guild) -> None:
        """Регистрация: восстановить view; при удалённой карточке — пересоздать."""
        if not tournament.setup_channel_id or not tournament.setup_message_id:
            return
        channel = guild.get_channel(tournament.setup_channel_id)
        if channel is None:
            return
        try:
            await channel.fetch_message(tournament.setup_message_id)
        except discord.NotFound:
            # Карточка удалена — отправляем новую в тот же канал.
            embed = await self._build_tournament_embed(tournament)
            view = await build_ultimatum_setup_view(
                tournament.guild_id, channel.id, tournament
            )
            message = await channel.send(view=compose(embed, view))
            await ultimatum_store.set_setup_message(
                tournament.id, channel.id, message.id
            )
            self.bot.add_view(view, message_id=message.id)
            return
        view = await build_ultimatum_setup_view(
            tournament.guild_id, channel.id, tournament
        )
        self.bot.add_view(view, message_id=tournament.setup_message_id)

    async def _build_tournament_embed(self, tournament) -> discord.Embed:
        """Build tournament status embed in regular tournament style."""
        status_emoji = {
            TournamentStatus.DRAFT: "📝",
            TournamentStatus.REGISTRATION: "📋",
            TournamentStatus.READY: "✅",
            TournamentStatus.IN_PROGRESS: "🎮",
            TournamentStatus.ROUND_IN_PROGRESS: "⚔️",
            TournamentStatus.ROUND_COMPLETED: "✅",
            TournamentStatus.GRAND_FINAL: "🏆",
            TournamentStatus.COMPLETED: "🎉",
            TournamentStatus.CANCELLED: "❌",
        }

        emoji = status_emoji.get(tournament.status, "❓")
        status_text = "Открыто" if tournament.status == TournamentStatus.REGISTRATION else tournament.status.value

        # Get players and format them with cosmetics
        players = await ultimatum_store.get_players(tournament.id)
        player_strings = []
        for player in players:
            # Format name with cosmetics like regular tournaments
            # For test players (fake IDs >= 1000000), just use username
            if player.discord_id >= 1000000:
                formatted_name = player.username
            else:
                from utils.cosmetics import format_player_name
                formatted_name = format_player_name(tournament.guild_id, player.discord_id, player.username)
            player_strings.append(formatted_name)

        players_text = ", ".join(player_strings) if player_strings else "*"

        # Build description in regular tournament style like build_setup_embed
        description = (
            f" **Организатор:** <@{tournament.created_by}>\n\n"
            f" **Информация о турнире:**\n"
            f" Размер: 16\n"
            f" Режим формирования: RANDOM\n"
            f" Статус регистрации: {status_text}\n\n"
            f" **Пул игроков:**\n"
            f" {players_text}\n\n"
            f"{replace_emojis('a_dot_smaller')} Используйте кнопки ниже для регистрации на турнир\n\n"
            f" Участники с тегом {get_emoji('guild_tag')}r!z3 могут входить в закрытые турниры при наличии свободных мест."
        )

        embed = discord.Embed(
            title=f" {replace_emojis('winner')} 16 | УЛЬТИМАТУМ СОЛО",
            description=description,
            color=discord.Color.from_rgb(69, 69, 69)
        )

        return embed





    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Handle custom interaction events for Ultimatum buttons."""
        if not interaction.data:
            return
        
        custom_id = interaction.data.get("custom_id")
        if not custom_id:
            return
        
        # Handle winner vote buttons
        if custom_id.startswith("ultimatum_vote_winner:"):
            parts = custom_id.split(":")
            if len(parts) >= 3:
                match_id = int(parts[1])
                voted_winner_id = int(parts[2])
                
                # Get match details
                match = await ultimatum_store.get_match_by_id(match_id)
                if not match:
                    await interaction.response.send_message("Матч не найден", ephemeral=True)
                    return
                
                # Determine other player
                other_player_id = match.player2_id if voted_winner_id == match.player1_id else match.player1_id
                
                # Check if user is one of the players
                if interaction.user.id not in [voted_winner_id, other_player_id]:
                    await interaction.response.send_message(
                        "❌ Только участники матча могут голосовать.",
                        ephemeral=True
                    )
                    return
                
                # Get tournament
                tournament = await ultimatum_store.get_tournament(match.tournament_id)
                if not tournament:
                    await interaction.response.send_message("Турнир не найден", ephemeral=True)
                    return
                
                # Record vote in database
                from ultimatum.store import ultimatum_store
                existing_vote = await ultimatum_store.get_vote(match_id, interaction.user.id)
                
                if existing_vote:
                    await interaction.response.send_message(
                        "❌ Вы уже проголосовали.",
                        ephemeral=True
                    )
                    return
                
                await ultimatum_store.record_vote(match_id, interaction.user.id, voted_winner_id)
                
                # Check all votes for this match
                votes = await ultimatum_store.get_votes(match_id)
                
                if len(votes) >= 2:
                    # Check consensus
                    vote_counts = {}
                    for vote in votes:
                        vote_counts[vote.voted_winner_id] = vote_counts.get(vote.voted_winner_id, 0) + 1
                    
                    if vote_counts.get(voted_winner_id, 0) >= 2:
                        # Consensus! Complete match
                        await self.tournament_logic.complete_match(match_id, voted_winner_id, other_player_id, self.bot)
                        
                        # Clear votes
                        await ultimatum_store.clear_votes(match_id)
                        
                        await interaction.followup.send(
                            f"✅ Победитель определен",
                            ephemeral=True
                        )
                        
                        # Update round embed
                        from views.ultimatum_view import update_round_embed
                        await update_round_embed(interaction.guild, match.tournament_id, match.round_number, self.bot)
                    else:
                        # No consensus - clear votes
                        await ultimatum_store.clear_votes(match_id)
                        await interaction.followup.send(
                            "❌ Нет консенсуса. Игроки выбрали разных победителей. Попробуйте снова.",
                            ephemeral=True
                        )
                else:
                    await interaction.followup.send(
                        f"Голос записан. Ожидание голоса второго игрока...",
                        ephemeral=True
                    )


async def setup(bot: commands.Bot) -> None:
    """Register cog."""
    await bot.add_cog(UltimatumCog(bot))
    logger.info("Cog 'ultimatum' loaded")
