"""Slash-команды турнира."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from models.tournament import (
    FormationMode,
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
    @app_commands.describe(size="Размер турнира: 8, 16 или 32 игрока", formation="Режим формирования кругов: manual или elo")
    @is_admin()
    async def tournament_create(
        self, interaction: discord.Interaction, size: str, formation: str = "manual"
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

        # Validate formation mode
        try:
            formation_mode = FormationMode(formation)
        except ValueError:
            await interaction.response.send_message(
                "❌ Неверный режим формирования. Используйте: manual или elo.", ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament = Tournament(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            size=tournament_size,
            formation_mode=formation_mode,
        )
        embed = await build_setup_embed(tournament, interaction.guild)
        view = self.bot.build_view_for_tournament(tournament)
        self.bot._register_view(view)
        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()

        tournament.message_id = message.id
        store.set(tournament)
        logger.info("Турнир создан на сервере %s с размером %s и режимом %s", interaction.guild_id, size, formation)

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

    @app_commands.command(name="reset_leaderboard", description="Сбросить статистику лидерборда")
    @is_admin()
    async def reset_leaderboard(self, interaction: discord.Interaction) -> None:
        """Сбросить всю статистику лидерборда сервера."""
        from storage.player_stats_store import player_stats_store
        from storage.db import get_pool

        if not player_stats_store._use_db:
            await interaction.response.send_message("❌ База данных не включена.", ephemeral=True)
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM player_stats WHERE guild_id = $1",
                interaction.guild_id
            )

        await interaction.response.send_message(
            f"✅ Статистика лидерборда сброшена. Удалено {result} записей.",
            ephemeral=True
        )

    @app_commands.command(name="coins", description="Показать таблицу лидеров по монетам")
    async def coins_leaderboard(self, interaction: discord.Interaction, page: int = 1) -> None:
        """Показать таблицу лидеров по монетам."""
        from storage.user_balance_store import user_balance_store
        from storage.player_stats_store import player_stats_store
        from storage.db import get_pool

        if not user_balance_store._use_db:
            await interaction.response.send_message("❌ База данных не включена.", ephemeral=True)
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            offset = (page - 1) * 10
            rows = await conn.fetch(
                """
                SELECT ub.guild_id, ub.user_id, ub.balance, ps.name
                FROM user_balance ub
                JOIN player_stats ps ON ub.guild_id = ps.guild_id AND ub.user_id = ps.user_id
                WHERE ub.guild_id = $1 AND ps.games > 0 AND ub.user_id > 0
                ORDER BY ub.balance DESC
                LIMIT 10 OFFSET $2
                """,
                interaction.guild_id, offset
            )

        if not rows:
            await interaction.response.send_message("❌ Пока нет данных для лидерборда монет.", ephemeral=True)
            return

        embed = discord.Embed(
            title="💰 Лидерборд монет",
            color=discord.Color.gold()
        )

        for i, row in enumerate(rows):
            rank = (page - 1) * 10 + i + 1
            medal = ""
            if rank == 1:
                medal = "🥇"
            elif rank == 2:
                medal = "🥈"
            elif rank == 3:
                medal = "🥉"
            embed.add_field(
                name=f"{medal} #{rank} {row['name']}",
                value=f"{row['balance']} 🪙",
                inline=False
            )

        await interaction.response.send_message(embed=embed)

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
        from storage.user_balance_store import user_balance_store
        from models.player_stats import PlayerStats

        stats = await player_stats_store.get(interaction.guild_id, target_user.id)

        if not stats:
            # Create default stats for new players
            stats = PlayerStats(guild_id=interaction.guild_id, user_id=target_user.id, name=target_user.display_name)

        win_rate = stats.win_rate
        balance = await user_balance_store.get_balance(interaction.guild_id, target_user.id)

        embed = discord.Embed(
            title=f"📊 Профиль: {stats.name}",
            color=discord.Color.blue(),
        )
        embed.add_field(name="🏆 ELO", value=str(stats.elo), inline=True)
        embed.add_field(name="🥇 Победы", value=str(stats.wins), inline=True)
        embed.add_field(name="🎮 Игры", value=str(stats.games), inline=True)
        embed.add_field(name="📈 Win Rate", value=f"{win_rate:.0f}%", inline=True)
        embed.add_field(name="⚔️ K/D Ratio", value=f"{stats.kd_ratio:.2f}", inline=True)
        embed.add_field(name="� Монеты", value=f"{balance} 🪙", inline=True)
        
        # Additional stats
        embed.add_field(name="🎯 Total Kills", value=str(stats.total_kills), inline=True)
        embed.add_field(name="💀 Total Deaths", value=str(stats.total_deaths), inline=True)
        embed.add_field(name="🔥 Max Kills", value=str(stats.best_match_kills), inline=True)
        embed.add_field(name="� Last ELO Change", value=f"{stats.last_elo_change:+.0f}", inline=True)

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
        most_kills = max(all_players, key=lambda p: p.total_kills)
        most_deaths = max(all_players, key=lambda p: p.total_deaths)
        highest_elo = max(all_players, key=lambda p: p.elo)
        best_kd = max(all_players, key=lambda p: p.kd_ratio)
        best_win_streak = max(all_players, key=lambda p: p.best_win_streak)
        best_loss_streak = max(all_players, key=lambda p: p.best_loss_streak)

        embed = discord.Embed(
            title=":trophy: Рекорды Турнира",
            color=discord.Color.gold(),
        )

        embed.add_field(
            name=":first_place: Наибольшее количество побед",
            value=f"{most_wins.name} — {most_wins.wins} побед",
            inline=False
        )
        embed.add_field(
            name=":man_detective: Наибольшее количество киллов",
            value=f"{most_kills.name} — {most_kills.total_kills} киллов",
            inline=False
        )
        embed.add_field(
            name=":skull: Наибольшее количество смертей",
            value=f"{most_deaths.name} — {most_deaths.total_deaths} смертей",
            inline=False
        )
        embed.add_field(
            name=":chart_with_upwards_trend: Самый высокий ELO",
            value=f"{highest_elo.name} — {highest_elo.elo} ELO",
            inline=False
        )
        embed.add_field(
            name=":dart: Наибольшее количество K/D",
            value=f"{best_kd.name} — {best_kd.kd_ratio:.2f} K/D",
            inline=False
        )
        embed.add_field(
            name=":fire: Лучшая серия побед",
            value=f"{best_win_streak.name} — {best_win_streak.best_win_streak} подряд",
            inline=False
        )
        embed.add_field(
            name=":snowflake: Худшая серия поражений",
            value=f"{best_loss_streak.name} — {best_loss_streak.best_loss_streak} подряд",
            inline=False
        )

        await interaction.edit_original_response(embed=embed)

    @commands.command(name="elo")
    @is_admin()
    async def set_elo(self, ctx: commands.Context, player: discord.Member, elo: int) -> None:
        """Изменить ELO игрока. Использование: !elo @player 1000"""
        from storage.player_stats_store import player_stats_store

        await player_stats_store.update_player(
            ctx.guild.id,
            player.id,
            player.display_name,
            result="none",
            set_elo=elo
        )

        await ctx.send(f"✅ ELO игрока {player.display_name} изменен на {elo}.", delete_after=10)

    @tournament_group.command(name="fix_userid", description="Исправить user_id игрока")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(player="Игрок")
    @is_admin()
    async def fix_userid(self, interaction: discord.Interaction, player: discord.Member) -> None:
        """Исправить user_id игрока в базе данных."""
        from storage.player_stats_store import player_stats_store
        from storage.db import get_pool

        if not player_stats_store._use_db:
            await interaction.response.send_message("❌ База данных не включена.", ephemeral=True)
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Update all records with the player's name to use their user_id
            result = await conn.execute(
                "UPDATE player_stats SET user_id = $1 WHERE guild_id = $2 AND name = $3",
                player.id, interaction.guild_id, player.display_name
            )

        await interaction.response.send_message(f"✅ Обновлено {result} записей для {player.display_name}.", ephemeral=True)

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












