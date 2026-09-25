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
from utils.permissions import is_admin, is_org

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
    @app_commands.describe(size="Размер турнира: 8, 16 или 32 игрока", formation="Режим формирования кругов: manual, elo или random")
    @is_org()
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
                "❌ Неверный режим формирования. Используйте: manual, elo или random.", ephemeral=True
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

        # Log tournament creation
        from utils.logging import log_tournament_created
        await log_tournament_created(self.bot, interaction.guild, interaction.user, f"Турнир {size} ({formation})")

    @tournament_group.command(name="delete", description="Удалить активный турнир")
    @is_org()
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

    @app_commands.command(name="test", description="Тестовый запуск (заполнить турнир фиктивными именами)")
    @is_org()
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

        # Fill with test data based on tournament size and formation mode
        tournament.is_test = True
        captain_count = tournament.captain_count
        required_players = int(tournament.size.value)

        if tournament.formation_mode == FormationMode.RANDOM:
            # RANDOM mode: fill players_pool
            tournament.players_pool = [f"Player{i+1}" for i in range(required_players)]
            # Add random user_ids
            for i, player_name in enumerate(tournament.players_pool):
                tournament.player_user_ids[player_name] = 1000 + i  # Fake user_ids
        else:
            # MANUAL/ELO modes: fill circles
            tournament.captains = [f"Cap{i+1}" for i in range(captain_count)]
            tournament.circle1.extend(tournament.captains)
            tournament.circle2.extend([f"P2-{i}" for i in range(captain_count)])
            tournament.circle3.extend([f"P3-{i}" for i in range(captain_count)])
            # Add extra players to circle4 to reach required total
            remaining_players = required_players - (captain_count * 3)
            tournament.circle4.extend([f"P4-{i}" for i in range(remaining_players)])
            # Add user_ids for all players
            for i, player_name in enumerate(tournament.captains):
                tournament.player_user_ids[player_name] = 1000 + i
            for i, player_name in enumerate(tournament.circle2):
                tournament.player_user_ids[player_name] = 2000 + i
            for i, player_name in enumerate(tournament.circle3):
                tournament.player_user_ids[player_name] = 3000 + i
            for i, player_name in enumerate(tournament.circle4):
                tournament.player_user_ids[player_name] = 4000 + i

        store.set(tournament)

        await interaction.response.send_message(
            f"🧪 Турнир заполнен {required_players} тестовыми игроками! Нажмите Старт для запуска.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    @app_commands.command(name="top", description="Показать таблицу лидеров")
    @app_commands.describe(type="Тип лидерборда (level/money/elo)")
    async def top(self, interaction: discord.Interaction, type: str = "elo") -> None:
        """Показать таблицу лидеров сервера."""
        from utils.embeds import build_leaderboard_embed
        from views.leaderboard_view import LeaderboardView

        # Validate type
        valid_types = ["level", "money", "elo"]
        if type not in valid_types:
            await interaction.response.send_message(
                f"❌ Неверный тип. Доступные: {', '.join(valid_types)}",
                ephemeral=True
            )
            return

        embed = await build_leaderboard_embed(interaction.guild_id, page=1, leaderboard_type=type)
        view = LeaderboardView(interaction.guild_id, page=1, leaderboard_type=type)
        await view.initialize()

        try:
            await interaction.response.send_message(embed=embed, view=view)
        except discord.NotFound:
            # Interaction expired, use followup
            await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="leaderboard", description="Показать таблицу лидеров (ELO)")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        """Показать таблицу лидеров сервера (ELO)."""
        from utils.embeds import build_leaderboard_embed
        from views.leaderboard_view import LeaderboardView

        embed = await build_leaderboard_embed(interaction.guild_id, page=1, leaderboard_type="elo")
        view = LeaderboardView(interaction.guild_id, page=1, leaderboard_type="elo")
        await view.initialize()

        try:
            await interaction.response.send_message(embed=embed, view=view)
        except discord.NotFound:
            # Interaction expired, use followup
            await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="help", description="Показать справку по командам")
    async def help_command(self, interaction: discord.Interaction) -> None:
        """Показать интерактивную справку."""
        from views.help_view import HelpMainView

        embed = discord.Embed(
            title="📚 Справка по командам",
            description="Выберите категорию для просмотра команд",
            color=discord.Color.dark_blue()
        )

        view = HelpMainView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="reset_leaderboard", description="Сбросить статистику лидерборда")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def reset_leaderboard(self, interaction: discord.Interaction) -> None:
        """Сбросить всю статистику лидерборда сервера."""
        # Только владелец бота может использовать эту команду
        if interaction.user.id != interaction.application_owner.id:
            await interaction.response.send_message("❌ Только владелец бота может использовать эту команду.", ephemeral=True)
            return

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

    @app_commands.command(name="balance", description="Показать ваш баланс")
    async def balance(self, interaction: discord.Interaction) -> None:
        """Показать баланс пользователя."""
        from storage.user_balance_store import user_balance_store

        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

        embed = discord.Embed(
            title="💰 Ваш баланс",
            color=discord.Color.gold()
        )
        embed.add_field(
            name="Монеты",
            value=f"{balance} 🪙",
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="daily", description="Получить ежедневный бонус")
    async def daily_bonus(self, interaction: discord.Interaction) -> None:
        """Получить ежедневный бонус монет."""
        from storage.user_balance_store import user_balance_store
        import datetime

        # Проверяем базу данных
        from storage.db import get_pool

        # Проверяем базу данных
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                # Получаем последний бонус и streak
                row = await conn.fetchrow(
                    "SELECT last_claim, streak_days FROM bonus_cooldowns WHERE guild_id = $1 AND user_id = $2",
                    guild_id, user_id
                )

                now = datetime.datetime.now(datetime.timezone.utc)
                today = now.date()

                if row:
                    last_claim = row["last_claim"]
                    streak_days = row["streak_days"]

                    # Проверяем прошло ли 24 часа
                    if last_claim:
                        time_diff = now - last_claim
                        if time_diff.total_seconds() < 86400:  # 24 часа
                            hours_left = 24 - time_diff.total_seconds() / 3600
                            await interaction.response.send_message(
                                f"❌ Вы уже получили бонус сегодня. Попробуйте через {int(hours_left)} часов.",
                                ephemeral=True
                            )
                            return

                    # Проверяем streak (пропуск дня сбрасывает)
                    if last_claim and (now.date() - last_claim.date()).days > 1:
                        streak_days = 0

                    # Увеличиваем streak
                    streak_days += 1
                else:
                    streak_days = 1

                # Рассчитываем бонус: 50 + streak * 10 (максимум +100)
                bonus = min(50 + streak_days * 10, 150)

                # Добавляем монеты
                await user_balance_store.add_balance(guild_id, user_id, bonus)

                # Обновляем cooldown
                await conn.execute(
                    """
                    INSERT INTO bonus_cooldowns (guild_id, user_id, last_claim, streak_days, last_streak_date)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET
                        last_claim = $3,
                        streak_days = $4,
                        last_streak_date = $5
                    """,
                    guild_id, user_id, now, streak_days, today
                )

                # Отправляем ответ
                embed = discord.Embed(
                    title="🎁 Ежедневный бонус",
                    color=discord.Color.gold()
                )
                embed.add_field(
                    name="💰 Получено",
                    value=f"{bonus} 🪙",
                    inline=True
                )
                embed.add_field(
                    name="🔥 Серия",
                    value=f"{streak_days} дней",
                    inline=True
                )
                embed.add_field(
                    name="📅 Следующий бонус",
                    value="Через 24 часа",
                    inline=False
                )
                embed.set_footer(text=f"Максимум: 150 🪙 (15 дней streak)")

                await interaction.response.send_message(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error in daily bonus: {e}", exc_info=True)
            await interaction.response.send_message(
                "❌ Ошибка при получении бонуса. Требуется база данных.",
                ephemeral=True
            )

    @app_commands.command(name="shop", description="Магазин")
    async def shop(self, interaction: discord.Interaction) -> None:
        """Показать магазин."""
        from storage.user_balance_store import user_balance_store
        from views.shop_view import ShopMainView

        # Получить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

        # Создать embed
        embed = discord.Embed(
            title="🛒 Магазин",
            description=f"💰 {balance} 🪙",
            color=discord.Color.gold()
        )

        # Создать View с кнопками категорий
        view = ShopMainView()

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="inventory", description="Ваш инвентарь")
    async def inventory(self, interaction: discord.Interaction) -> None:
        """Показать инвентарь косметики."""
        from storage.shop_store import inventory_store, shop_store
        from views.shop_view import InventoryEquipButton, InventoryUnequipButton

        # Получить инвентарь
        cosmetics = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)

        if not cosmetics:
            await interaction.response.send_message(
                "❌ Ваш инвентарь пуст. Используйте `/shop` для покупки косметики.",
                ephemeral=True
            )
            return

        # Создать embed
        embed = discord.Embed(
            title="🎒 Ваш инвентарь",
            color=discord.Color.blue()
        )

        # Создать View с кнопками
        view = discord.ui.View()

        # Сгруппировать по типам
        equipped_text = []
        unequipped_text = []

        for cosmetic in cosmetics:
            item = shop_store.get_item(cosmetic.item_id)
            if not item:
                continue

            status = "✅" if cosmetic.equipped else "❌"
            item_text = f"{status} **{item.name}** ({item.rarity.value})"

            if cosmetic.equipped:
                equipped_text.append(item_text)
                # Добавить кнопку снятия
                view.add_item(InventoryUnequipButton(cosmetic.item_id, f"Снять {item.name}"))
            else:
                unequipped_text.append(item_text)
                # Добавить кнопку экипировки
                view.add_item(InventoryEquipButton(cosmetic.item_id, f"Экипировать {item.name}"))

        if equipped_text:
            embed.add_field(
                name="👑 Экипировано",
                value="\n".join(equipped_text),
                inline=False
            )

        if unequipped_text:
            embed.add_field(
                name="📦 В инвентаре",
                value="\n".join(unequipped_text),
                inline=False
            )

        # Добавить инструкции
        embed.add_field(
            name="📖 Управление",
            value="Используйте кнопки ниже для экипировки/снятия.\n"
                   "Максимум 1 тег и 1 иконка одновременно.",
            inline=False
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="cases", description="Показать доступные кейсы")
    async def cases(self, interaction: discord.Interaction) -> None:
        """Показать доступные кейсы."""
        from storage.case_store import case_store
        from views.case_view import CasesMainView

        cases = case_store.get_all_cases()

        if not cases:
            await interaction.response.send_message(
                "❌ Нет доступных кейсов.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📦 Кейсы",
            description="Откройте кейсы для получения случайных предметов!",
            color=discord.Color.gold()
        )

        for case in cases:
            embed.add_field(
                name=f"{case.name} - {case.price} 🪙",
                value=case.description,
                inline=False
            )

        view = CasesMainView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="rps", description="Камень-Ножницы-Бумага")
    async def rps(self, interaction: discord.Interaction) -> None:
        """Игра в камень-ножницы-бумага."""
        from views.rps_view import BetModal

        await interaction.response.send_modal(BetModal())

    @app_commands.command(name="games", description="Показать доступные мини-игры")
    async def games(self, interaction: discord.Interaction) -> None:
        """Показать список мини-игр."""
        from views.games_view import GamesMainView

        embed = discord.Embed(
            title="🎮 Мини-игры",
            description="Выберите категорию игр и поставьте монеты!",
            color=discord.Color.dark_blue(),
        )

        view = GamesMainView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="profile", description="Показать ваш профиль")
    async def profile(self, interaction: discord.Interaction) -> None:
        """Показать детальный профиль игрока."""
        from storage.player_stats_store import player_stats_store
        from storage.user_balance_store import user_balance_store
        from storage.case_store import case_store
        from storage.shop_store import inventory_store, shop_store
        from utils.cosmetics import format_player_name
        from utils.embeds import create_progress_bar

        stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

        if not stats:
            await interaction.response.send_message(
                "❌ Сначала сыграйте хотя бы один турнир!",
                ephemeral=True
            )
            return

        rank_title = stats.get_rank_title()
        current_xp, xp_needed = stats.get_level_progress()
        xp_progress_bar = create_progress_bar(current_xp, xp_needed)

        # Get case history count
        case_history = case_store.get_case_history(interaction.guild_id, interaction.user.id)
        cases_opened = len(case_history)

        # Get equipped cosmetics
        equipped_cosmetics = inventory_store.get_equipped_cosmetics(interaction.guild_id, interaction.user.id)
        cosmetic_display = ""
        if equipped_cosmetics:
            cosmetic_items = []
            for cosmetic in equipped_cosmetics:
                item = shop_store.get_item(cosmetic.item_id)
                if item:
                    cosmetic_items.append(item.value)
            cosmetic_display = " ".join(cosmetic_items)

        embed = discord.Embed(
            title=f"🎮 {stats.name}",
            color=discord.Color.dark_blue()
        )

        # Level and XP with progress bar
        embed.add_field(
            name="📊 Уровень",
            value=f"Level {stats.level} ({xp_progress_bar} XP) ⭐",
            inline=False
        )

        # ELO and Money
        embed.add_field(
            name="⭐ ELO",
            value=f"{stats.elo}",
            inline=True
        )
        embed.add_field(
            name="💰 Монеты",
            value=f"{balance} 🪙",
            inline=True
        )

        # Stats
        embed.add_field(
            name="🏆 Победы",
            value=f"{stats.wins} / Игры: {stats.games}",
            inline=True
        )
        embed.add_field(
            name="📈 Win Rate",
            value=f"{stats.win_rate:.1f}%",
            inline=True
        )

        # K/D stats
        embed.add_field(
            name="AVG",
            value=f"{stats.avg_kills:.2f}",
            inline=True
        )
        embed.add_field(
            name="K/D",
            value=f"{stats.kd_ratio:.2f}",
            inline=True
        )
        embed.add_field(
            name="MAX",
            value=str(stats.best_match_kills),
            inline=True
        )

        # Cases
        embed.add_field(
            name="� Кейсы открыто",
            value=str(cases_opened),
            inline=False
        )

        # Equipped cosmetics
        if cosmetic_display:
            embed.add_field(
                name="🎨 Экипировано",
                value=f"{stats.name} {cosmetic_display}",
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="rank", description="Показать ваш ранг и прогресс")
    async def rank(self, interaction: discord.Interaction) -> None:
        """Показать текущий ранг и прогресс до следующего уровня."""
        from storage.player_stats_store import player_stats_store
        from utils.embeds import create_progress_bar

        stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)

        if not stats:
            await interaction.response.send_message(
                "❌ Сначала сыграйте хотя бы один турнир!",
                ephemeral=True
            )
            return

        rank_title = stats.get_rank_title()
        current_xp, xp_needed = stats.get_level_progress()
        progress_percent = int((current_xp / xp_needed) * 100) if xp_needed > 0 else 0
        progress_bar = create_progress_bar(current_xp, xp_needed)

        embed = discord.Embed(
            title=f"🎮 {rank_title} Level {stats.level}",
            color=discord.Color.dark_blue()
        )

        embed.add_field(
            name="📊 Прогресс",
            value=f"{progress_bar} ({progress_percent}%)",
            inline=False
        )

        embed.add_field(
            name="📈 До следующего уровня",
            value=f"Требуется: {xp_needed - current_xp} XP",
            inline=False,
        )

        embed.set_footer(text=f"Накопить XP можно через участие в турнирах и победы")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="bet", description="Показать вашу статистику ставок")
    async def betting_stats(self, interaction: discord.Interaction) -> None:
        """Показать статистику ставок пользователя."""
        from storage.betting_stats_store import betting_stats_store

        stats = await betting_stats_store.get_user_stats(interaction.guild_id, interaction.user.id)

        if not stats or stats["total_bets"] == 0:
            await interaction.response.send_message(
                "❌ У вас пока нет статистики ставок.",
                ephemeral=True
            )
            return

        accuracy = stats["success_rate"]

        embed = discord.Embed(
            title="💰 Ставки",
            color=discord.Color.gold()
        )
        embed.add_field(name="Всего ставок", value=str(stats["total_bets"]), inline=True)
        embed.add_field(name="Выигрышных", value=str(stats["successful_bets"]), inline=True)
        embed.add_field(name="Проигрышных", value=str(stats["total_bets"] - stats["successful_bets"]), inline=True)
        embed.add_field(name="Точность", value=f"{accuracy:.1f}%", inline=True)
        embed.add_field(name="Выиграно", value=f"+{stats['total_won']}", inline=True)
        embed.add_field(name="Проиграно", value=f"-{stats['total_lost']}", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="moneytop", description="Показать таблицу лидеров по монетам")
    async def coins_leaderboard(self, interaction: discord.Interaction, page: int = 1) -> None:
        """Показать таблицу лидеров по монетам."""
        from storage.user_balance_store import user_balance_store
        from storage.player_stats_store import player_stats_store

        # Get all players with stats
        all_stats = await player_stats_store.get_all(interaction.guild_id)

        if not all_stats:
            await interaction.response.send_message("❌ Пока нет данных для лидерборда монет.", ephemeral=True)
            return

        # Get balances for all players
        leaderboard_data = []
        for stats in all_stats:
            balance = await user_balance_store.get_balance(interaction.guild_id, stats.user_id)
            leaderboard_data.append({
                "user_id": stats.user_id,
                "name": stats.name,
                "balance": balance
            })

        # Sort by balance
        leaderboard_data.sort(key=lambda x: x["balance"], reverse=True)

        # Pagination
        per_page = 10
        offset = (page - 1) * per_page
        paginated_data = leaderboard_data[offset:offset + per_page]

        if not paginated_data:
            await interaction.response.send_message("❌ Страница не найдена.", ephemeral=True)
            return

        embed = discord.Embed(
            title="💰 Лидерборд монет",
            color=discord.Color.gold()
        )

        for i, data in enumerate(paginated_data):
            rank = (page - 1) * 10 + i + 1
            medal = ""
            if rank == 1:
                medal = "🥇"
            elif rank == 2:
                medal = "🥈"
            elif rank == 3:
                medal = "🥉"
            embed.add_field(
                name=f"{medal} #{rank} {data['name']}",
                value=f"{data['balance']} 🪙",
                inline=False
            )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="limit", description="Включить/выключить лимит для круга")
    @app_commands.describe(
        circle="Номер круга (2, 3 или 4)",
        status="on для включения лимита, off для отключения"
    )
    @is_org()
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

    @app_commands.command(name="setbio", description="Установить описание профиля")
    @app_commands.describe(bio="Короткое описание (максимум 100 символов)")
    async def setbio(self, interaction: discord.Interaction, bio: str) -> None:
        """Установить описание профиля."""
        # Ограничение длины
        if len(bio) > 100:
            await interaction.response.send_message(
                "❌ Описание должно быть не более 100 символов.",
                ephemeral=True
            )
            return

        from storage.player_stats_store import player_stats_store
        from models.player_stats import PlayerStats

        stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)

        if not stats:
            # Create default stats for new players
            stats = PlayerStats(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
                name=interaction.user.display_name,
                bio=bio
            )
        else:
            stats.bio = bio

        await player_stats_store.set(interaction.guild_id, interaction.user.id, stats)

        await interaction.response.send_message(
            f"✅ Био установлено: {bio}",
            ephemeral=True
        )

    @app_commands.command(name="setavatar", description="Установить аватар профиля")
    @app_commands.describe(url="URL изображения аватара")
    async def setavatar(self, interaction: discord.Interaction, url: str = None) -> None:
        """Установить аватар профиля."""
        from storage.player_stats_store import player_stats_store
        from models.player_stats import PlayerStats

        stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)

        avatar_url = url
        if not avatar_url:
            # Use Discord avatar by default
            avatar_url = interaction.user.display_avatar.url

        if not stats:
            # Create default stats for new players
            stats = PlayerStats(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
                name=interaction.user.display_name,
                avatar_url=avatar_url
            )
        else:
            stats.avatar_url = avatar_url

        await player_stats_store.set(interaction.guild_id, interaction.user.id, stats)

        if url:
            await interaction.response.send_message(
                f"✅ Аватар профиля обновлен.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"✅ Аватар профиля установлен по умолчанию (из Discord).",
                ephemeral=True
            )

    @app_commands.command(name="profile", description="Просмотреть статистику игрока")
    @app_commands.describe(player="Игрок (оставьте пустым для просмотра своей статистики)")
    async def profile(
        self,
        interaction: discord.Interaction,
        player: discord.Member | None = None
    ) -> None:
        """Показать статистику игрока."""
        await interaction.response.defer()

        target_user = player if player else interaction.user

        from storage.player_stats_store import player_stats_store
        from models.player_stats import PlayerStats

        stats = await player_stats_store.get(interaction.guild_id, target_user.id)

        if not stats:
            # Create default stats for new players
            stats = PlayerStats(guild_id=interaction.guild_id, user_id=target_user.id, name=target_user.display_name)

        win_rate = stats.win_rate

        # Format name with cosmetics
        from utils.cosmetics import format_player_name
        formatted_name = format_player_name(interaction.guild_id, target_user.id, stats.name)

        embed = discord.Embed(
            title=f"📊 Профиль: {formatted_name}",
            color=discord.Color.blue(),
        )

        # Show avatar (use custom avatar_url if set, otherwise Discord avatar)
        avatar_url = stats.avatar_url if stats.avatar_url else target_user.display_avatar.url
        embed.set_thumbnail(url=avatar_url)

        # Показать био если есть
        if stats.bio:
            embed.description = f"📝 {stats.bio}"

        embed.add_field(name="🏆 ELO", value=str(int(stats.elo)), inline=True)
        embed.add_field(name="🥇 Победы", value=str(stats.wins), inline=True)
        embed.add_field(name="🎮 Игры", value=str(stats.games), inline=True)
        embed.add_field(name="📈 Win Rate", value=f"{win_rate:.0f}%", inline=True)
        embed.add_field(name="⚔️ K/D Ratio", value=f"{stats.kd_ratio:.2f}", inline=True)

        # Additional stats
        embed.add_field(name="🎯 AVG Kills", value=f"{stats.avg_kills:.2f}", inline=True)
        embed.add_field(name="🔥 Max Kills", value=str(stats.best_match_kills), inline=True)
        embed.add_field(name="📊 Last ELO Change", value=f"{stats.last_elo_change:+.0f}", inline=True)

        await interaction.followup.send(embed=embed)

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

        # Find records (all players)
        most_wins = max(all_players, key=lambda p: p.wins)
        most_finals = max(all_players, key=lambda p: p.finals)
        highest_elo = max(all_players, key=lambda p: p.elo)
        most_games = max(all_players, key=lambda p: p.games)
        best_match_kills = max(all_players, key=lambda p: p.best_match_kills)  # Убрано ограничение 20 игр

        # Records only for players with 20+ matches
        players_20_plus = [p for p in all_players if p.games >= 20]
        highest_kd = max(players_20_plus, key=lambda p: p.kd_ratio) if players_20_plus else None
        highest_winrate = max(players_20_plus, key=lambda p: p.win_rate) if players_20_plus else None
        best_avg_kills_20 = max(players_20_plus, key=lambda p: p.avg_kills) if players_20_plus else None
        best_win_streak_20 = max(players_20_plus, key=lambda p: p.best_win_streak) if players_20_plus else None

        # New records: Most coins and Best bettor
        from storage.user_balance_store import user_balance_store
        from storage.betting_stats_store import betting_stats_store

        # Get all user balances
        richest_player = None
        max_balance = 0
        try:
            for player in all_players:
                balance = await user_balance_store.get_balance(interaction.guild_id, player.user_id)
                if balance > max_balance:
                    max_balance = balance
                    richest_player = player
        except Exception:
            # If balance store fails, skip this record
            pass

        # Get best bettor (max single win)
        best_bettor = None
        max_single_win = 0
        try:
            for player in all_players:
                bet_stats = await betting_stats_store.get(interaction.guild_id, player.user_id)
                if bet_stats and bet_stats.best_win > max_single_win:
                    max_single_win = bet_stats.best_win
                    best_bettor = player
        except Exception:
            # If betting stats store fails, skip this record
            pass
        best_loss_streak_20 = max(players_20_plus, key=lambda p: p.best_loss_streak) if players_20_plus else None
        best_match_kills_20 = max(players_20_plus, key=lambda p: p.best_match_kills) if players_20_plus else None

        embed = discord.Embed(
            title="🏆 Рекорды Турнира",
            color=discord.Color.gold(),
        )

        if best_avg_kills_20:
            embed.add_field(
                name="🎯 Наибольшее AVG Kills (20 игр)",
                value=f"{best_avg_kills_20.name} — {best_avg_kills_20.avg_kills:.2f}",
                inline=False
            )
        if best_match_kills:
            embed.add_field(
                name="🔥 Наибольшее количество киллов за матч",
                value=f"{best_match_kills.name} — {best_match_kills.best_match_kills}",
                inline=False
            )
        embed.add_field(
            name="📈 Самый высокий ELO",
            value=f"{highest_elo.name} — {highest_elo.elo} ELO",
            inline=False
        )
        if highest_kd:
            embed.add_field(
                name="⚔️ Наибольшее K/D (20 игр)",
                value=f"{highest_kd.name} — {highest_kd.kd_ratio:.2f}",
                inline=False
            )
        if highest_winrate:
            embed.add_field(
                name="🏆 Лучший WinRate (20 игр)",
                value=f"{highest_winrate.name} — {highest_winrate.win_rate:.1f}%",
                inline=False
            )
        if best_win_streak_20:
            embed.add_field(
                name="🔥 Лучшая серия побед (20 игр)",
                value=f"{best_win_streak_20.name} — {best_win_streak_20.best_win_streak} подряд",
                inline=False
            )
        if richest_player:
            embed.add_field(
                name="💰 Богатейший игрок",
                value=f"{richest_player.name} — {max_balance} 🪙",
                inline=False
            )
        if best_bettor:
            embed.add_field(
                name="🎲 Лучший беттор",
                value=f"{best_bettor.name} — {max_single_win} 🪙 (за ставку)",
                inline=False
            )
        if best_loss_streak_20:
            embed.add_field(
                name="❄️ Худшая серия поражений (20 игр)",
                value=f"{best_loss_streak_20.name} — {best_loss_streak_20.best_loss_streak} подряд",
                inline=False
            )

        await interaction.edit_original_response(embed=embed)

    @commands.command(name="elo")
    @is_org()
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

    @app_commands.command(name="edit", description="Изменить ELO или монеты игрока")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        player="Игрок",
        type="Тип изменения: elo или money",
        amount="Новое значение (для ELO) или количество монет (для money)",
        operation="Операция: set (установить), add (добавить), remove (убрать)"
    )
    @is_org()
    async def edit_player(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
        type: str,
        amount: int,
        operation: str = "set"
    ) -> None:
        """Изменить ELO или монеты игрока."""
        # Только владелец бота может использовать эту команду
        bot_owner_id = interaction.client.owner_id if interaction.client.owner_id else interaction.client.application.owner.id
        if interaction.user.id != bot_owner_id:
            await interaction.response.send_message("❌ Только владелец бота может использовать эту команду.", ephemeral=True)
            return

        if type not in ["elo", "money"]:
            await interaction.response.send_message(
                "❌ Тип должен быть 'elo' или 'money'.",
                ephemeral=True
            )
            return

        if operation not in ["set", "add", "remove"]:
            await interaction.response.send_message(
                "❌ Операция должна быть 'set', 'add' или 'remove'.",
                ephemeral=True
            )
            return

        if type == "elo":
            from storage.player_stats_store import player_stats_store

            stats = await player_stats_store.get(interaction.guild_id, player.id)
            current_elo = stats.elo if stats else 1000

            if operation == "set":
                new_elo = amount
            elif operation == "add":
                new_elo = current_elo + amount
            else:  # remove
                new_elo = current_elo - amount

            await player_stats_store.update_player(
                interaction.guild_id,
                player.id,
                player.display_name,
                result="none",
                set_elo=new_elo
            )

            await interaction.response.send_message(
                f"✅ ELO игрока {player.display_name}: {current_elo} → {new_elo}",
                ephemeral=True
            )
        else:  # money
            from storage.user_balance_store import user_balance_store

            current_balance = await user_balance_store.get_balance(interaction.guild_id, player.id)

            if operation == "set":
                new_balance = amount
                # Calculate difference to add/remove
                diff = new_balance - current_balance
                if diff > 0:
                    await user_balance_store.add_balance(interaction.guild_id, player.id, diff)
                elif diff < 0:
                    await user_balance_store.remove_balance(interaction.guild_id, player.id, abs(diff))
            elif operation == "add":
                new_balance = current_balance + amount
                await user_balance_store.add_balance(interaction.guild_id, player.id, amount)
            else:  # remove
                new_balance = current_balance - amount
                await user_balance_store.remove_balance(interaction.guild_id, player.id, amount)

            await interaction.response.send_message(
                f"✅ Монеты игрока {player.display_name}: {current_balance} → {new_balance}",
                ephemeral=True
            )

    @tournament_group.command(name="fix_userid", description="Исправить user_id игрока")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(player="Игрок")
    @is_org()
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
        current_player="Имя игрока которого нужно заменить (или @упоминание)",
        new_player="Имя нового игрока (или @упоминание)"
    )
    @is_org()
    async def replace_player(
        self,
        interaction: discord.Interaction,
        current_player: str,
        new_player: str
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

        # Handle @mentions - extract display name if it's a mention
        old_name = current_player.strip()
        new_name = new_player.strip()

        # Check if current_player is a mention and extract the name
        if old_name.startswith("<@") and old_name.endswith(">"):
            user_id = int(old_name.strip("<@!>"))
            member = interaction.guild.get_member(user_id)
            if member:
                old_name = member.display_name

        # Check if new_player is a mention and extract the name
        if new_name.startswith("<@") and new_name.endswith(">"):
            user_id = int(new_player.strip("<@!>"))
            member = interaction.guild.get_member(user_id)
            if member:
                new_name = member.display_name

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
    @is_org()
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












