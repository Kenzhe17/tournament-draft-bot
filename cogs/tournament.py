"""Slash-команды турнира."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from models.tournament import (
    RegistrationState,
    Tournament,
    TournamentPhase,
    TournamentSize,
)
from storage.json_store import store
from utils.embeds import build_setup_embed
from utils.permissions import is_admin

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


async def _delete_ephemeral_later(interaction: discord.Interaction, delay: float = 4.0) -> None:
    """Удалить ephemeral-ответ через указанное время."""
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


class TournamentCog(commands.Cog):
    """Ког с командами управления турниром."""

    def __init__(self, bot: TournamentBot):
        self.bot = bot

    tournament_group = app_commands.Group(name="tournament", description="Управление турнирами")

    @tournament_group.command(name="create", description="Создать новый турнир")
    @app_commands.describe(size="Размер турнира: 8, 16 или 32 игрока")
    @is_admin()
    async def tournament_create(
        self, interaction: discord.Interaction, size: str
    ) -> None:
        """Создать турнир с указанным размером."""
        existing = store.get(interaction.guild_id)
        if existing and existing.phase != TournamentPhase.COMPLETE:
            await interaction.response.send_message(
                "❌ На сервере уже есть активный турнир.", ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Validate size
        try:
            tournament_size = TournamentSize(size)
        except ValueError:
            await interaction.response.send_message(
                "❌ Неверный размер. Используйте: 8, 16 или 32.", ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament = Tournament(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            size=tournament_size,
        )
        embed = await build_setup_embed(tournament, interaction.guild)
        view = self.bot.build_view_for_tournament(tournament)
        self.bot._register_view(view)
        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()

        tournament.message_id = message.id
        store.set(tournament)
        logger.info("Турнир создан на сервере %s с размером %s", interaction.guild_id, size)

    @tournament_group.command(name="delete", description="Удалить активный турнир")
    @is_admin()
    async def tournament_delete(self, interaction: discord.Interaction) -> None:
        """Удалить текущий турнир."""
        existing = store.get(interaction.guild_id)
        if not existing:
            await interaction.response.send_message(
                "❌ На этом сервере нет активного турнира.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        store.delete(interaction.guild_id)
        logger.info("Турнир удален на сервере %s", interaction.guild_id)

        await interaction.response.send_message(
            "🗑️ **Турнир успешно удален.**",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

    @app_commands.command(name="start", description="Запустить драфт")
    @is_admin()
    async def start_draft(self, interaction: discord.Interaction) -> None:
        """Запустить драфт после заполнения игроков."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Сначала создайте турнир командой `/tournament`.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                f"❌ Турнир не в фазе настройки. Текущая фаза: {tournament.phase.value}",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if not tournament.is_setup_complete:
            captain_count = tournament.captain_count
            msg = f"❌ Турнир заполнен не полностью. Нужно {captain_count} игрока в Капитаны, минимум {captain_count} игрока в круге 2, минимум {captain_count} игрока в круге 3 и минимум {captain_count} игрока в круге 4."
            await interaction.response.send_message(msg, ephemeral=True)
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament.start_draft()
        store.set(tournament)

        await interaction.response.send_message(
            "🎲 Драфт запущен!", ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

        try:
            await self.bot.update_tournament_message(interaction.guild, tournament)
        except Exception as e:
            logger.error("Ошибка при обновлении сообщения турнира: %s", e)
            # Revert phase if update fails
            tournament.phase = TournamentPhase.SETUP
            store.set(tournament)
            await interaction.followup.send(
                "❌ Произошла ошибка при обновлении сообщения. Драфт не запущен. Фаза возвращена в настройку.",
                ephemeral=True
            )
            return

    @app_commands.command(name="close", description="Закрыть регистрацию (только админ добавляет)")
    @is_admin()
    async def close_registration(self, interaction: discord.Interaction) -> None:
        """Закрыть регистрацию игроков."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Нет активного турнира.", ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament.registration = RegistrationState.CLOSED
        store.set(tournament)

        await self.bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            "🔒 Регистрация закрыта. Только админ может добавлять игроков.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

    @app_commands.command(name="open", description="Открыть регистрацию (игроки добавляются сами)")
    @is_admin()
    async def open_registration(self, interaction: discord.Interaction) -> None:
        """Открыть регистрацию игроков."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Нет активного турнира.", ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament.registration = RegistrationState.OPEN
        store.set(tournament)

        await self.bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            "🔓 Регистрация открыта! Игроки могут добавляться через кнопки.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

    @app_commands.command(name="test", description="Тестовый запуск (заполнить турнир фиктивными именами)")
    @is_admin()
    async def test_start(self, interaction: discord.Interaction) -> None:
        """Заполнить турнир тестовыми данными и запустить драфт."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Сначала создайте турнир командой `/tournament`.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Турнир уже запущен.", ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Fill with test data based on tournament size
        tournament.is_test = True
        captain_count = tournament.captain_count
        tournament.captains = [f"Cap{i+1}" for i in range(captain_count)]

        # Fill circles based on tournament size
        tournament.circle1.extend(tournament.captains)
        tournament.circle2.extend([f"P2-{i}" for i in range(captain_count)])
        tournament.circle3.extend([f"P3-{i}" for i in range(captain_count)])
        tournament.circle4.extend([f"P4-{i}" for i in range(captain_count + 2)])  # captain_count + 2 players in circle4

        tournament.start_draft()
        store.set(tournament)

        await interaction.response.send_message(
            "🧪 Тестовый режим активирован! Турнир заполнен и драфт запущен.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    @app_commands.command(name="leaderboard", description="Показать таблицу лидеров")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        """Показать таблицу лидеров сервера."""
        from utils.embeds import build_leaderboard_embed
        from views.leaderboard_view import LeaderboardView

        embed = await build_leaderboard_embed(interaction.guild_id, page=1)
        view = LeaderboardView(interaction.guild_id, page=1)
        await view.initialize()

        try:
            await interaction.response.send_message(embed=embed, view=view)
        except discord.NotFound:
            # Interaction expired, use followup
            await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="limit", description="Включить/выключить лимит для круга")
    @app_commands.describe(
        circle="Номер круга (2, 3 или 4)",
        status="on для включения лимита, off для отключения"
    )
    @is_admin()
    async def set_circle_limit(
        self,
        interaction: discord.Interaction,
        circle: int,
        status: str
    ) -> None:
        """Включить или выключить лимит для круга."""
        if circle not in [2, 3, 4]:
            await interaction.response.send_message(
                "❌ Круг должен быть 2, 3 или 4.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if status.lower() not in ["on", "off"]:
            await interaction.response.send_message(
                "❌ Статус должен быть 'on' или 'off'.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Нет активного турнира.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Лимиты можно менять только в фазе настройки.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament.circle_limits_enabled[circle] = (status.lower() == "on")
        store.set(tournament)

        status_text = "включен" if tournament.circle_limits_enabled[circle] else "отключен"
        await interaction.response.send_message(
            f"✅ Лимит для круга {circle} {status_text}.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    @app_commands.command(name="profile", description="Просмотреть статистику игрока")
    @app_commands.describe(player="Игрок (оставьте пустым для просмотра своей статистики)")
    async def profile(
        self,
        interaction: discord.Interaction,
        player: discord.Member | None = None
    ) -> None:
        """Показать статистику игрока."""
        target_user = player if player else interaction.user

        from storage.player_stats_store import player_stats_store
        from models.player_stats import PlayerStats

        stats = await player_stats_store.get(interaction.guild_id, target_user.id)

        if not stats:
            # Create default stats for new players
            stats = PlayerStats(guild_id=interaction.guild_id, user_id=target_user.id, name=target_user.display_name)

        win_rate = (stats.wins / stats.games * 100) if stats.games > 0 else 0

        embed = discord.Embed(
            title=f"📊 Профиль: {stats.name}",
            color=discord.Color.blue(),
        )
        embed.add_field(name="🏆 ELO", value=str(stats.elo), inline=True)
        embed.add_field(name="🥇 Победы", value=str(stats.wins), inline=True)
        embed.add_field(name="🥈 Финалы", value=str(stats.finals), inline=True)
        embed.add_field(name="🎮 Игры", value=str(stats.games), inline=True)
        embed.add_field(name="📈 Win Rate", value=f"{win_rate:.1f}%", inline=True)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="booyah", description="Рекорды турнира")
    async def booyah(self, interaction: discord.Interaction) -> None:
        """Показать рекорды турнира."""
        await interaction.response.defer()

        from storage.player_stats_store import player_stats_store

        all_players = await player_stats_store.get_all(interaction.guild_id)

        if not all_players:
            await interaction.edit_original_response(
                content="❌ Пока нет данных для рекордов."
            )
            return

        # Find records
        most_wins = max(all_players, key=lambda p: p.wins)
        most_finals = max(all_players, key=lambda p: p.finals)
        highest_elo = max(all_players, key=lambda p: p.elo)
        most_games = max(all_players, key=lambda p: p.games)
        best_win_streak = max(all_players, key=lambda p: p.best_win_streak)
        best_loss_streak = max(all_players, key=lambda p: p.best_loss_streak)

        embed = discord.Embed(
            title="🏆 Рекорды Турнира",
            color=discord.Color.gold(),
        )

        embed.add_field(
            name="🥇 Наибольшее количество побед",
            value=f"{most_wins.name} — {most_wins.wins} побед",
            inline=False
        )
        embed.add_field(
            name="🥈 Наибольшее количество финалов",
            value=f"{most_finals.name} — {most_finals.finals} финалов",
            inline=False
        )
        embed.add_field(
            name="📈 Самый высокий ELO",
            value=f"{highest_elo.name} — {highest_elo.elo} ELO",
            inline=False
        )
        embed.add_field(
            name="🎮 Наибольшее количество игр",
            value=f"{most_games.name} — {most_games.games} игр",
            inline=False
        )
        embed.add_field(
            name="🔥 Лучшая серия побед",
            value=f"{best_win_streak.name} — {best_win_streak.best_win_streak} подряд",
            inline=False
        )
        embed.add_field(
            name="❄️ Худшая серия поражений",
            value=f"{best_loss_streak.name} — {best_loss_streak.best_loss_streak} подряд",
            inline=False
        )

        await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="replace", description="Заменить игрока")
    @app_commands.describe(
        old_name="Имя игрока которого нужно заменить",
        new_name="Имя нового игрока"
    )
    @is_admin()
    async def replace_player(
        self,
        interaction: discord.Interaction,
        old_name: str,
        new_name: str
    ) -> None:
        """Заменить игрока в турнире."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Нет активного турнира.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        old_name = old_name.strip()
        new_name = new_name.strip()

        if tournament.phase == TournamentPhase.SETUP or tournament.phase == TournamentPhase.DRAFT:
            # Replace in circles
            if old_name not in tournament.all_players:
                await interaction.response.send_message(
                    f"❌ Игрок `{old_name}` не найден.",
                    ephemeral=True,
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

            for circle in range(1, 5):
                circle_list = getattr(tournament, f"circle{circle}")
                if old_name in circle_list:
                    idx = circle_list.index(old_name)
                    circle_list[idx] = new_name
                    break
        elif tournament.phase == TournamentPhase.FINAL:
            # Replace in final teams
            found = False
            for team_idx in range(len(tournament.final_teams)):
                if tournament.final_teams[team_idx] == old_name:
                    tournament.final_teams[team_idx] = new_name
                    found = True
                    break
            
            if not found:
                await interaction.response.send_message(
                    f"❌ Игрок `{old_name}` не найден в финальных командах.",
                    ephemeral=True,
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return
        else:
            # Replace in teams (TEAMS, QUALIFIERS, SEMIFINALS)
            found = False
            for team in tournament.teams:
                for key, value in team.items():
                    if value == old_name:
                        team[key] = new_name
                        found = True
                        break
                if found:
                    break

            if not found:
                await interaction.response.send_message(
                    f"❌ Игрок `{old_name}` не найден в командах.",
                    ephemeral=True,
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

        store.set(tournament)

        await interaction.response.send_message(
            f"✅ Игрок `{old_name}` заменен на `{new_name}`.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    @app_commands.command(name="delete_player", description="Удалить игрока из турнира")
    @app_commands.describe(name="Имя игрока которого нужно удалить")
    @is_admin()
    async def delete_player(
        self,
        interaction: discord.Interaction,
        name: str
    ) -> None:
        """Удалить игрока из турнира."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Нет активного турнира.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        name = name.strip()

        if tournament.phase == TournamentPhase.SETUP:
            if not tournament.remove_player(name):
                await interaction.response.send_message(
                    f"❌ Игрок `{name}` не найден.",
                    ephemeral=True,
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return
        else:
            await interaction.response.send_message(
                "❌ Можно удалять игроков только на этапе настройки.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        store.set(tournament)

        await interaction.response.send_message(
            f"✅ Игрок `{name}` удален.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Обработка ошибок slash-команд."""
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error) or "❌ Недостаточно прав."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
                asyncio.create_task(_delete_ephemeral_later(interaction))
            except discord.NotFound:
                # Interaction expired, can't respond
                pass
            return

        logger.exception("Ошибка команды: %s", error)
        msg = "❌ Произошла ошибка при выполнении команды."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            asyncio.create_task(_delete_ephemeral_later(interaction))
        except discord.NotFound:
            # Interaction expired, can't respond
            pass


async def setup(bot: TournamentBot) -> None:
    """Загрузить ког."""
    await bot.add_cog(TournamentCog(bot))