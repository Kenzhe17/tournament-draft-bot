"""Точка входа — Discord-бот для турнирного драфта."""

from __future__ import annotations

import logging
import sys

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
