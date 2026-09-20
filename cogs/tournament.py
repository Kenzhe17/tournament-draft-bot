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

        # Простая логика кулдауна (в памяти)
        # В реальном проекте нужно использовать базу данных для хранения времени последнего получения
        key = f"daily:{interaction.guild_id}:{interaction.user.id}"

        # Проверяем можно ли получить бонус (упрощённая логика)
        # Для полной реализации нужно хранить время последнего получения
        await interaction.response.send_message(
            "🎁 Ежедневный бонус: +5 монет!\n\n(Функция кулдауна требует базы данных)",
            ephemeral=True
        )

        # Начисляем монеты
        await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, 5)

    @app_commands.command(name="shop", description="Магазин косметики")
    async def shop(self, interaction: discord.Interaction) -> None:
        """Показать магазин косметики."""
        from storage.user_balance_store import user_balance_store
        from views.shop_view import ShopMainView

        # Получить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

        # Создать embed
        embed = discord.Embed(
            title="🛒 Магазин косметики",
            description=f"Ваш баланс: {balance} 🪙",
            color=discord.Color.gold()
        )

        # Создать View с кнопками категорий
        view = ShopMainView()

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="inventory", description="Ваш инвентарь косметики")
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
        try:
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
                f"✅ Описание профиля обновлено: {bio}",
                ephemeral=True
            )
        except Exception as e:
            import logging
            logging.error(f"Error in /setbio: {e}", exc_info=True)
            await interaction.response.send_message(
                f"❌ Произошла ошибка: {str(e)}",
                ephemeral=True
            )

    @app_commands.command(name="history", description="История рекордов")
    async def history(self, interaction: discord.Interaction) -> None:
        """Показать историю исторических рекордов."""
        from models.records import record_store

        await interaction.response.defer()

        all_records = record_store.get_all_records(interaction.guild_id)

        if not all_records:
            await interaction.edit_original_response(
                content="❌ Пока нет исторических рекордов."
            )
            return

        # Group by record type
        record_types = {
            "max_kills": "🔥 Наибольшее количество киллов за матч",
            "best_win_streak": "🔥 Лучшая серия побед",
            "avg_kills": "🎯 Наибольшее AVG Kills",
            "kd_ratio": "⚔️ Наибольшее K/D",
            "win_rate": "🏆 Лучший WinRate",
        }

        embed = discord.Embed(
            title="📜 История Рекордов",
            color=discord.Color.gold()
        )

        for record_type, title in record_types.items():
            records = [r for r in all_records if r.record_type == record_type]
            if records:
                latest = records[-1]  # Most recent
                date_str = latest.timestamp.split("T")[0] if "T" in latest.timestamp else latest.timestamp
                embed.add_field(
                    name=title,
                    value=f"{latest.player_name} — {latest.value}\n📅 {date_str}",
                    inline=False
                )

        await interaction.followup.send(embed=embed)

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

        # Check for historical record breaks (compare current best with historical records)
        from models.records import record_store
        from utils.rating_calculator import check_and_update_record

        record_checks = [
            ("max_kills", best_match_kills, "🔥"),
            ("best_win_streak", best_win_streak_20, "🔥"),
            ("avg_kills", best_avg_kills_20, "🎯"),
            ("kd_ratio", highest_kd, "⚔️"),
            ("win_rate", highest_winrate, "🏆"),
        ]

        for record_type, record_holder, emoji in record_checks:
            if record_holder:
                new_value = getattr(record_holder, record_type.replace("best_", ""), 0)
                if isinstance(new_value, float):
                    new_value = int(new_value)

                record_broken, old_record = check_and_update_record(
                    guild_id=interaction.guild_id,
                    user_id=record_holder.user_id,
                    player_name=record_holder.name,
                    record_type=record_type,
                    new_value=new_value
                )

                if record_broken:
                    # Send notification to channel
                    record_channel_id = 1549809898643001484
                    channel = interaction.guild.get_channel(record_channel_id)
                    if channel:
                        if old_record:
                            await channel.send(
                                f"🎉 <@{record_holder.user_id}> побил исторический рекорд <@{old_record.user_id}> ({old_record.value})!\n"
                                f"{emoji} Новый исторический рекорд: {new_value}!"
                            )
                        else:
                            await channel.send(
                                f"🎉 <@{record_holder.user_id}> установил первый исторический рекорд!\n"
                                f"{emoji} Рекорд: {new_value}!"
                            )

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












