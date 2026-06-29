"""Точка входа — Discord-бот для турнирного драфта."""

from __future__ import annotations

import logging
import sys
import os
import tempfile

# Add project directory to Python path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord
from discord.ext import commands

from config import DATABASE_URL, DISCORD_TOKEN
from models.tournament import Tournament, TournamentPhase
from storage.json_store import store
from storage.player_stats_store import player_stats_store
from storage.user_balance_store import user_balance_store
from storage.bet_store import bet_store
from storage.betting_stats_store import betting_stats_store
from utils.embeds import build_embed_for_phase
from views.draft_view import build_draft_view
from views.final_view import FinalView
from views.leaderboard_view import LeaderboardView
from views.matches_view import QualifiersView, SemifinalsView, TeamsView
from views.setup_view import build_setup_view
from services.image_analyzer import image_analyzer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class TournamentBot(commands.Bot):
    """Основной класс бота."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True  # Required to receive message content
        super().__init__(
            command_prefix="!",
            intents=intents,
            max_ratelimit_timeout=30.0,
            max_ratelimit_retries=5
        )
        self._registered_view_keys: set[str] = set()

    async def setup_hook(self) -> None:
        """Синхронизация slash-команд и восстановление View."""
        # Initialize database if DATABASE_URL is set
        if DATABASE_URL:
            try:
                from storage.db import init_db
                await init_db()
                player_stats_store.enable_db()
                user_balance_store.enable_db()
                bet_store.enable_db()
                betting_stats_store.enable_db()
                logger.info("Database initialized and enabled")
            except Exception as e:
                logger.error("Failed to initialize database: %s", e)

        await self.load_extension("cogs.tournament")
        await self.tree.sync()
        logger.info("Slash-команды синхронизированы")

        for tournament in store.all():
            view = self.build_view_for_tournament(tournament)
            self._register_view(view)

    def _view_key(self, view: discord.ui.View) -> str:
        """Уникальный ключ View по custom_id его компонентов."""
        ids = sorted(item.custom_id for item in view.children if item.custom_id)
        return "|".join(ids)

    def _register_view(self, view: discord.ui.View | None) -> None:
        """Зарегистрировать persistent View (без дубликатов)."""
        if view is None:
            return
        key = self._view_key(view)
        # Always add the view - Discord handles replacements
        self.add_view(view)
        if key not in self._registered_view_keys:
            self._registered_view_keys.add(key)
        logger.debug("Зарегистрирован View: %s", key)

    def build_view_for_tournament(
        self, tournament: Tournament
    ) -> discord.ui.View | None:
        """Построить View в зависимости от фазы турнира."""
        phase = tournament.phase
        gid = tournament.guild_id

        if phase == TournamentPhase.SETUP:
            return build_setup_view(tournament)

        if phase == TournamentPhase.DRAFT:
            return build_draft_view(tournament)

        if phase == TournamentPhase.TEAMS:
            return TeamsView(gid, tournament)

        if phase == TournamentPhase.QUALIFIERS:
            return QualifiersView(
                gid,
                tournament.qualifier_matches,
                tournament.qualifier_winners,
                tournament,
            )

        if phase == TournamentPhase.SEMIFINALS:
            return SemifinalsView(
                gid,
                tournament.semifinal_matches,
                tournament.semifinal_winners,
                tournament,
            )

        if phase == TournamentPhase.FINAL:
            return FinalView(gid, tournament.final_teams, tournament)

        if phase == TournamentPhase.COMPLETE:
            return None  # No buttons needed for completed tournament

        return None

    async def update_tournament_message(
        self, guild: discord.Guild, tournament: Tournament
    ) -> None:
        """Отредактировать главное сообщение турнира."""
        if not tournament.message_id:
            logger.warning("Нет message_id для сервера %s", guild.id)
            return

        channel = guild.get_channel(tournament.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(tournament.channel_id)
            except discord.HTTPException as exc:
                logger.error("Канал не найден: %s", exc)
                return

        try:
            message = await channel.fetch_message(tournament.message_id)
        except discord.HTTPException as exc:
            logger.error("Сообщение не найдено: %s", exc)
            return

        embed = await build_embed_for_phase(tournament, guild)
        view = self.build_view_for_tournament(tournament)
        self._register_view(view)

        try:
            await message.edit(embed=embed, view=view)
        except discord.HTTPException as exc:
            logger.error("Не удалось обновить сообщение: %s", exc)

    async def on_ready(self) -> None:
        logger.info("Бот запущен как %s (ID: %s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        """Handle incoming messages, including screenshot uploads and confirmations."""
        logger.info(f"Message received from {message.author.id} in guild {message.guild.id if message.guild else 'DM'}")

        # Ignore bot messages
        if message.author.bot:
            logger.info(f"Ignoring bot message from {message.author.id}")
            return

        # Check for manual confirmation response
        if message.content.lower() in ['подтвердить', 'отмена']:
            logger.info(f"Handling confirmation response: {message.content}")
            await self._handle_confirmation_response(message)
            return

        # Check if this is a screenshot upload
        if message.attachments and len(message.attachments) > 0:
            attachment = message.attachments[0]
            logger.info(f"Attachment detected: {attachment.content_type}")
            if attachment.content_type and attachment.content_type.startswith('image/'):
                logger.info(f"Image upload detected, calling handler")
                await self._handle_screenshot_upload(message, attachment)

    async def _handle_screenshot_upload(self, message: discord.Message, attachment: discord.Attachment) -> None:
        """Handle screenshot upload for match results."""
        from storage.json_store import store

        logger.info(f"Screenshot upload detected from user {message.author.id}")

        # Get tournament for this guild
        tournament = store.get(message.guild.id)
        if not tournament:
            logger.warning(f"No tournament found for guild {message.guild.id}")
            return

        if not hasattr(tournament, 'pending_screenshot_upload'):
            logger.warning(f"Tournament does not have pending_screenshot_upload attribute")
            return

        upload_info = tournament.pending_screenshot_upload
        if not upload_info:
            logger.warning(f"No pending screenshot upload info")
            return

        if upload_info['user_id'] != message.author.id:
            logger.warning(f"User ID mismatch: expected {upload_info['user_id']}, got {message.author.id}")
            return

        logger.info(f"Processing screenshot upload for match {upload_info.get('match_index')}")

        try:
            # Download image to temporary file
            async with message.channel.typing():
                with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp_file:
                    await attachment.save(tmp_file.name)
                    tmp_path = tmp_file.name

                # Get expected players for this match
                team_a_data = tournament.teams[upload_info['team_a']] if upload_info['team_a'] < len(tournament.teams) else {}
                team_b_data = tournament.teams[upload_info['team_b']] if upload_info['team_b'] < len(tournament.teams) else {}

                expected_players = []
                for circle in range(1, 5):
                    player_a = team_a_data.get(f"circle{circle}")
                    player_b = team_b_data.get(f"circle{circle}")
                    if player_a:
                        expected_players.append(player_a)
                    if player_b:
                        expected_players.append(player_b)

                # Analyze screenshot
                result = image_analyzer.analyze_screenshot(tmp_path, expected_players)

                # Clean up temporary file
                os.unlink(tmp_path)

                if not result:
                    await message.reply("❌ Не удалось распознать скриншот. Попробуйте еще раз или выберите победителя вручную.")
                    return

                # Check for low confidence matches
                low_confidence_players = []
                for player in result.team1_players + result.team2_players:
                    if player.confidence < 90:
                        low_confidence_players.append(player)

                if low_confidence_players:
                    # Ask for manual confirmation
                    await self._request_manual_confirmation(message, tournament, upload_info, result, low_confidence_players)
                    return

                # Process match result
                await self._process_match_result(message, tournament, upload_info, result)

                # Clear pending upload
                tournament.pending_screenshot_upload = None
                store.set(tournament)

        except Exception as e:
            logger.error(f"Error handling screenshot upload: {e}", exc_info=True)
            await message.reply(f"❌ Ошибка при обработке скриншота: {str(e)}")
            # Clear pending upload on error
            if hasattr(tournament, 'pending_screenshot_upload'):
                tournament.pending_screenshot_upload = None
                store.set(tournament)

    async def _handle_confirmation_response(self, message: discord.Message) -> None:
        """Handle manual confirmation response for low-confidence OCR matches."""
        from storage.json_store import store

        tournament = store.get(message.guild.id)
        if not tournament or not hasattr(tournament, 'pending_confirmation'):
            return

        confirmation = tournament.pending_confirmation
        if not confirmation or confirmation['user_id'] != message.author.id:
            return

        if message.content.lower() == 'подтвердить':
            # Process the result
            await self._process_match_result(message, tournament, confirmation['upload_info'], confirmation['result'])
            await message.reply("✅ Результат подтвержден и обработан.")
        elif message.content.lower() == 'отмена':
            await message.reply("❌ Обработка отменена. Загрузите скриншот заново.")

        # Clear pending confirmation
        tournament.pending_confirmation = None
        store.set(tournament)

    async def _request_manual_confirmation(self, message: discord.Message, tournament, upload_info, result, low_confidence_players) -> None:
        """Request manual confirmation for low-confidence OCR matches."""
        embed = discord.Embed(
            title="⚠️ Требуется подтверждение",
            description="Некоторые игроки были распознаны с низкой точностью. Пожалуйста, подтвердите или исправьте:",
            color=discord.Color.orange()
        )

        for player in low_confidence_players:
            embed.add_field(
                name=f"{player.nickname} (уверенность: {player.confidence:.1f}%)",
                value=f"Kills: {player.kills}, Deaths: {player.deaths}, Assists: {player.assists}, Damage: {player.damage}",
                inline=False
            )

        embed.add_field(
            name="Счет",
            value=result.score,
            inline=False
        )

        embed.set_footer(text="Ответьте 'подтвердить' для принятия или 'отмена' для повторной загрузки.")

        # Store result for confirmation
        tournament.pending_confirmation = {
            "upload_info": upload_info,
            "result": result,
            "user_id": message.author.id
        }
        store.set(tournament)

        await message.reply(embed=embed)

    async def _process_match_result(self, message: discord.Message, tournament, upload_info, result) -> None:
        """Process the match result from screenshot analysis."""
        from storage.player_stats_store import player_stats_store
        from storage.user_balance_store import user_balance_store
        from storage.bet_store import bet_store
        from storage.betting_stats_store import betting_stats_store
        from utils.rating_calculator import (
            calculate_team_position,
            calculate_total_elo_change,
            update_player_stats_from_match,
        )

        match_index = upload_info['match_index']
        team_a = upload_info['team_a']
        team_b = upload_info['team_b']
        match_type = upload_info['match_type']

        # Determine winner based on score
        score_parts = result.score.split(' - ')
        if len(score_parts) == 2:
            try:
                score_a = int(score_parts[0].strip())
                score_b = int(score_parts[1].strip())
                winning_team = team_a if score_a > score_b else team_b
                losing_team = team_b if score_a > score_b else team_a
            except ValueError:
                await message.reply("❌ Не удалось определить победителя по счету.")
                return
        else:
            await message.reply("❌ Неверный формат счета.")
            return

        # Prepare player stats for both teams
        team_a_stats = {}
        team_b_stats = {}

        for player in result.team1_players:
            team_a_stats[player.nickname] = {
                'kills': player.kills,
                'deaths': player.deaths,
                'assists': player.assists,
                'damage': player.damage
            }

        for player in result.team2_players:
            team_b_stats[player.nickname] = {
                'kills': player.kills,
                'deaths': player.deaths,
                'assists': player.assists,
                'damage': player.damage
            }

        # Update tournament state based on match type
        if match_type == "qualifier":
            if match_index < len(tournament.qualifier_winners):
                tournament.qualifier_winners[match_index] = winning_team
        elif match_type == "semifinal":
            if match_index < len(tournament.semifinal_winners):
                tournament.semifinal_winners[match_index] = winning_team
        elif match_type == "final":
            tournament.set_final_winner(winning_team)

        # Update player statistics
        winning_team_data = tournament.teams[winning_team] if winning_team < len(tournament.teams) else {}
        losing_team_data = tournament.teams[losing_team] if losing_team < len(tournament.teams) else {}

        # Process winning team
        for circle in range(1, 5):
            player = winning_team_data.get(f"circle{circle}")
            if player:
                stats = team_a_stats.get(player) if winning_team == team_a else team_b_stats.get(player)
                if stats:
                    await self._update_player_stats(
                        tournament.guild_id,
                        player,
                        stats['kills'],
                        stats['deaths'],
                        True,
                        circle,
                        winning_team_data
                    )

        # Process losing team
        for circle in range(1, 5):
            player = losing_team_data.get(f"circle{circle}")
            if player:
                stats = team_a_stats.get(player) if losing_team == team_a else team_b_stats.get(player)
                if stats:
                    await self._update_player_stats(
                        tournament.guild_id,
                        player,
                        stats['kills'],
                        stats['deaths'],
                        False,
                        circle,
                        losing_team_data
                    )

        # Resolve bets
        match_id = f"{match_type}_{match_index}"
        winning_team_name = tournament.team_names.get(winning_team, f"Team {winning_team}")
        payouts = await bet_store.resolve_match_bets(tournament.guild_id, match_id, winning_team_name)

        # Distribute winnings
        for user_id, amount in payouts.items():
            await user_balance_store.add_balance(tournament.guild_id, user_id, amount)
            await betting_stats_store.record_bet_result(tournament.guild_id, user_id, amount, won=True)

        # Update tournament phase if needed
        self._advance_tournament_phase(tournament)
        store.set(tournament)

        # Update tournament message
        await self.update_tournament_message(message.guild, tournament)

        # Send confirmation
        await message.reply(
            f"✅ Результаты матча #{match_index + 1} обработаны!\n"
            f"Счет: {result.score}\n"
            f"Победитель: {winning_team_name}"
        )

    async def _update_player_stats(self, guild_id: int, player_name: str, kills: int, deaths: int, team_won: bool, circle: int, team_data: dict) -> None:
        """Update player statistics after a match."""
        from storage.player_stats_store import player_stats_store
        from utils.rating_calculator import calculate_total_elo_change, update_player_stats_from_match

        user_id = team_data.get('player_user_ids', {}).get(player_name, 0)
        if not user_id:
            return

        stats = await player_stats_store.get(guild_id, user_id)
        if not stats:
            from models.player_stats import PlayerStats
            stats = PlayerStats(guild_id=guild_id, user_id=user_id, name=player_name)

        # Calculate position (simplified - use circle as position for now)
        position = circle

        # Calculate ELO change
        elo_change = calculate_total_elo_change(
            circle=circle,
            position=position,
            team_won=team_won,
            kills=kills,
            deaths=deaths
        )

        # Update stats
        stats = update_player_stats_from_match(
            stats=stats,
            kills=kills,
            deaths=deaths,
            elo_change=elo_change,
            team_won=team_won
        )

        await player_stats_store.update(guild_id, user_id, player_name, stats)

    def _advance_tournament_phase(self, tournament) -> None:
        """Advance tournament phase if all matches in current phase are complete."""
        from models.tournament import TournamentPhase

        if tournament.phase == TournamentPhase.QUALIFIERS:
            if all(w is not None for w in tournament.qualifier_winners):
                tournament.phase = TournamentPhase.SEMIFINALS
                tournament.generate_semifinals()

        elif tournament.phase == TournamentPhase.SEMIFINALS:
            if all(w is not None for w in tournament.semifinal_winners):
                tournament.phase = TournamentPhase.FINAL
                tournament.final_teams = tournament.semifinal_winners

        elif tournament.phase == TournamentPhase.FINAL:
            if tournament.final_winner is not None:
                tournament.phase = TournamentPhase.COMPLETE


def main() -> None:
    """Запуск бота."""
    if not DISCORD_TOKEN:
        logger.error(
            "DISCORD_TOKEN не задан. Скопируйте .env.example в .env и укажите токен."
        )
        sys.exit(1)

    bot = TournamentBot()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
