"""Slash-команды турнира."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
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
from storage.player_stats_store import player_stats_store
from storage.user_balance_store import user_balance_store
from storage.user_bank_store import user_bank_store
from storage.betting_stats_store import betting_stats_store
from utils.embeds import build_setup_embed
from utils.permissions import is_admin, is_org, is_bot_owner
from config import BOT_OWNER_ID, replace_emojis, WELCOME_BANNER_URL
from views.org_role_view import setup_org_role_message

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
    @app_commands.describe(size="Размер турнира: 8, 16 или 32 игрока", formation="Режим формирования кругов: skill, elo или random")
    @is_org()
    async def tournament_create(
        self, interaction: discord.Interaction, size: str, formation: str = "elo"
    ) -> None:
        """Создать турнир с указанным размером."""
        existing = store.get(interaction.guild_id)
        if existing and existing.phase != TournamentPhase.COMPLETE:
            await interaction.response.send_message(
                replace_emojis("❌ На сервере уже есть активный турнир."),
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Validate size
        try:
            tournament_size = TournamentSize(size)
        except ValueError:
            await interaction.response.send_message(
                replace_emojis("❌ Неверный размер. Используйте: 8, 16 или 32."),
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Validate formation mode
        try:
            formation_mode = FormationMode(formation)
        except ValueError:
            await interaction.response.send_message(
                replace_emojis("❌ Неверный режим формирования. Используйте: skill, elo или random."),
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament = Tournament(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            size=tournament_size,
            formation_mode=formation_mode,
            organizer_id=interaction.user.id,
        )
        embed = await build_setup_embed(tournament, interaction.guild)
        view = self.bot.build_view_for_tournament(tournament)
        self.bot._register_view(view)

        # Send ephemeral confirmation first
        await interaction.response.send_message(
            replace_emojis("✅ Турнир создан"),
            ephemeral=True
        )

        # Send main tournament message through channel.send to unbind it
        message = await interaction.channel.send(embed=embed, view=view)

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
                replace_emojis("❌ На этом сервере нет активного турнира."),
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Удалить сообщение турнира если есть
        if existing.message_id:
            try:
                channel = interaction.guild.get_channel(existing.channel_id)
                if channel:
                    message = await channel.fetch_message(existing.message_id)
                    await message.delete()
            except Exception as e:
                logger.warning(f"Не удалось удалить сообщение турнира: {e}")

        store.delete(interaction.guild_id)
        logger.info("Турнир удален на сервере %s", interaction.guild_id)

        await interaction.response.send_message(
            "🗑️ **Турнир успешно удален.**",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

    @app_commands.command(name="test", description="Тестовый запуск (заполнить турнир фиктивными именами)")
    @is_bot_owner()
    async def test_start(self, interaction: discord.Interaction) -> None:
        """Заполнить турнир тестовыми данными и запустить драфт."""
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Сначала создайте турнир командой `/tournament`."),
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир уже запущен."),
                ephemeral=True,
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
            # ELO/SKILL modes: fill circles
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
                replace_emojis(f"❌ Неверный тип. Доступные: {', '.join(valid_types)}"),
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

    @app_commands.command(name="help", description="Показать справку по командам")
    async def help_command(self, interaction: discord.Interaction) -> None:
        """Показать интерактивную справку."""
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')}  СПРАВКА ПО КОМАНДАМ  {replace_emojis('a_star')}",
            description=f"{replace_emojis('white_arrow')} **Добро пожаловать в справку!**\nЗдесь вы найдёте информацию о всех командах бота.\n\n{replace_emojis('white_dot')} **Выберите категорию в меню ниже:**\n{replace_emojis('a_dot_smaller')} Турниры — Сетка, топ ELO и статистика\n{replace_emojis('a_dot_smaller')} Профиль — Карточка игрока и текущий ранг\n{replace_emojis('a_dot_smaller')} Экономика — Баланс, переводы, продажи и ставки\n{replace_emojis('a_dot_smaller')} Магазин — Покупка ролей и инвентарь\n{replace_emojis('a_dot_smaller')} Мини-игры — Игровые треды и игры\n{replace_emojis('a_dot_smaller')} Система рангов — Информация о рангах и уровнях\n{replace_emojis('a_dot_smaller')} Организаторам — Создание турниров и управление\n{replace_emojis('a_dot_smaller')} Правила сервера — Свод правил и регламент",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        view = HelpGuideView(current=None)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

    @app_commands.command(name="balance", description="Показать ваш баланс")
    async def balance(self, interaction: discord.Interaction) -> None:
        """Показать баланс пользователя."""
        from storage.user_balance_store import user_balance_store
        from storage.user_bank_store import user_bank_store
        from config import get_emoji

        cash = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        safe = await user_bank_store.get_bank_balance(interaction.guild_id, interaction.user.id)
        total = cash + safe

        embed = discord.Embed(
            title=f"{get_emoji('a_star')} **БАЛАНС ПОЛЬЗОВАТЕЛЯ | /balance**",
            description=f"{get_emoji('a_sparkle')} **Пользователь:** {interaction.user.mention}\n\n{get_emoji('white_arrow')} **Наличные:** **`{cash:,}`** {get_emoji('money')}\n{get_emoji('white_arrow')} **В сейфе:** **`{safe:,}`** {get_emoji('money')}\n{get_emoji('white_arrow')} **Всего:** **`{total:,}`** {get_emoji('money')}\n\n{get_emoji('white_dot')} *Используйте `/daily` для получения награды или посетите магазин.*",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="shop", description="Магазин")
    async def shop(self, interaction: discord.Interaction) -> None:
        """Показать магазин."""
        from storage.user_balance_store import user_balance_store
        from storage.shop_store import inventory_store
        from storage.player_stats_store import player_stats_store
        from views.shop_view import ShopMainView
        from models.player_stats import PlayerStats

        # Получить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

        # Получить инвентарь
        cosmetics = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
        inventory_count = len(cosmetics)
        max_inventory = 20

        # Получить ранг
        stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
        rank = "Без ранга"
        if stats:
            from cogs.tournament import get_rank_emoji
            rank = get_rank_emoji(stats.level)

        # Создать embed в новом формате
        embed = discord.Embed(
            title=replace_emojis("МАГАЗИН СЕРВЕРА | Главное меню"),
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.description = (
            f"Добро пожаловать в игровой магазин {replace_emojis('a_star')}\n"
            f"Выберите нужный раздел в выпадающем меню ниже, чтобы посмотреть доступные товары."
        )

        # Профиль пользователя
        embed.add_field(
            name=replace_emojis("⚪ Ваш профиль:"),
            value=f"{replace_emojis('sub_middle')} Баланс: {balance:,} {replace_emojis('money')}\n"
                  f"{replace_emojis('sub_middle')} Ранг: {rank}\n"
                  f"{replace_emojis('sub_directory')} Инвентарь: {inventory_count}/{max_inventory}",
            inline=False
        )

        embed.add_field(
            name=replace_emojis("a_dot_smaller Для навигации используйте компоненты ниже"),
            value="",
            inline=False
        )

        # Создать View с выпадающим меню категорий
        view = ShopMainView()

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="inventory", description="Ваш инвентарь")
    async def inventory(self, interaction: discord.Interaction) -> None:
        """Показать инвентарь косметики."""
        from storage.shop_store import inventory_store, shop_store
        from views.shop_view import InventoryEquipSelect, InventoryUnequipSelect

        # Получить инвентарь
        cosmetics = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)

        if not cosmetics:
            await interaction.response.send_message(
                replace_emojis("⚪ Ваш инвентарь пуст. Используйте `/shop` для покупки косметики."),
                ephemeral=True
            )
            return

        # Icon map для значков
        icon_map = {
            "icon_letter": "icon_letter",
            "icon_paw": "icon_paw",
            "icon_bluestacks": "icon_bluestacks",
            "icon_teacup": "icon_teacup",
            "icon_ribbon": "icon_ribbon",
            "icon_18plus": "icon_18plus",
            "icon_heart": "icon_heart",
            "icon_v_badge": "icon_v_badge",
            "icon_cards": "icon_cards",
            "icon_cat_ears": "icon_cat_ears",
            "icon_wing": "icon_wing",
            "icon_fuck_you": "icon_fuck_you",
            "icon_zzz": "icon_zzz",
        }

        # Rare emoji map
        rare_map = {
            "basic": "rare_basic",
            "premium": "rare_premium",
            "elite": "rare_elite",
            "special": "rare_special",
        }

        # Сгруппировать по типам
        equipped_tags = []
        equipped_icons = []
        unequipped_tags = []
        unequipped_icons = []
        equipped_items = []
        unequipped_items = []

        for cosmetic in cosmetics:
            item = shop_store.get_item(cosmetic.item_id)
            if not item:
                continue

            rare_emoji = rare_map.get(item.rarity.value, "")
            if cosmetic.equipped:
                equipped_items.append((cosmetic.item_id, item.name))
                if item.cosmetic_type.value == "tag":
                    equipped_tags.append(f"{replace_emojis('sub_middle')} Тег: **{item.value}** `(ID: {item.id})` • {replace_emojis(rare_emoji)}")
                elif item.cosmetic_type.value == "icon":
                    icon_emoji = icon_map.get(item.value, "")
                    equipped_icons.append(f"{replace_emojis('sub_directory')} Значок: {replace_emojis(icon_emoji)} **{item.name}** `(ID: {item.id})` • {replace_emojis(rare_emoji)}")
            else:
                unequipped_items.append((cosmetic.item_id, item.name))
                if item.cosmetic_type.value == "tag":
                    unequipped_tags.append(f"{replace_emojis('sub_middle')} Тег: **{item.value}** `(ID: {item.id})` • {replace_emojis(rare_emoji)}")
                elif item.cosmetic_type.value == "icon":
                    icon_emoji = icon_map.get(item.value, "")
                    unequipped_icons.append(f"{replace_emojis('sub_directory')} Значок: {replace_emojis(icon_emoji)} **{item.name}** `(ID: {item.id})` • {replace_emojis(rare_emoji)}")

        # Build description
        description_parts = ["Управление вашей экипировкой и предметами:\n"]

        # Экипировано
        description_parts.append(f"{replace_emojis('⚪')} **Экипировано:**")
        if equipped_tags:
            description_parts.extend(equipped_tags)
        if equipped_icons:
            description_parts.extend(equipped_icons)
        if not equipped_tags and not equipped_icons:
            description_parts.append(f"{replace_emojis('sub_directory')} Ничего не экипировано")
        description_parts.append("")

        # В инвентаре
        description_parts.append(f"{replace_emojis('⚪')} **В инвентаре:**")
        if unequipped_tags:
            description_parts.extend(unequipped_tags)
        if unequipped_icons:
            description_parts.extend(unequipped_icons)
        if not unequipped_tags and not unequipped_icons:
            description_parts.append(f"{replace_emojis('sub_directory')} Инвентарь пуст")
        description_parts.append("")

        # Управление
        description_parts.append(f"{replace_emojis('⚪')} **Управление:**")
        description_parts.append(f"{replace_emojis('sub_directory')} Используйте выпадающие меню ниже для экипировки/снятия.")
        description_parts.append("")
        description_parts.append(f"{replace_emojis('a_dot_smaller')} Максимум 1 тег и 1 значок одновременно")

        # Создать embed
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} ИНВЕНТАРЬ ПОЛЬЗОВАТЕЛЯ",
            description="\n".join(description_parts),
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        # Создать View с select menu
        view = discord.ui.View()

        # Добавить select menu для экипировки и снятия
        if unequipped_items:
            view.add_item(InventoryEquipSelect(unequipped_items))

        if equipped_items:
            view.add_item(InventoryUnequipSelect(equipped_items))

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # @app_commands.command(name="rps", description="Камень-Ножницы-Бумага")
    # async def rps(self, interaction: discord.Interaction) -> None:
    #     """Игра в камень-ножницы-бумага."""
    #     from storage.redis_client import get_cooldown, set_cooldown
    #     from views.rps_view import BetModal

    #     # Check cooldown (3 seconds)
    #     if await get_cooldown(interaction.guild_id, interaction.user.id, "minigame_rps"):
    #         await interaction.response.send_message(
    #             "⏳ Подождите 3 секунды перед повторной игрой!",
    #             ephemeral=True
    #         )
    #         return

    #     await set_cooldown(interaction.guild_id, interaction.user.id, "minigame_rps", 3)
    #     await interaction.response.send_modal(BetModal())

    # @app_commands.command(name="coin_flip", description="Монетка")
    # async def coin_flip(self, interaction: discord.Interaction) -> None:
    #     """Игра в монетку."""
    #     from views.coin_flip_view import CoinBetModal

    #     await interaction.response.send_modal(CoinBetModal())

    # @app_commands.command(name="dice_roll", description="Кубик")
    # async def dice_roll(self, interaction: discord.Interaction) -> None:
    #     """Игра в кубик."""
    #     from views.dice_roll_view import DiceBetModal

    #     await interaction.response.send_modal(DiceBetModal())

    @app_commands.command(name="games", description="Показать доступные мини-игры")
    async def games(self, interaction: discord.Interaction) -> None:
        """Показать список мини-игр."""
        from views.games_view import GamesMainView
        from storage.user_balance_store import user_balance_store

        # Получить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} МИНИ-ИГРЫ | Главное меню",
            description=f"{replace_emojis('white_arrow')} {interaction.user.mention}\n\n{replace_emojis('white_dot')} **Информация:**\n{replace_emojis('a_dot_smaller')} Ваш баланс: {balance:,} {replace_emojis('money')}\n\n{replace_emojis('white_dot')} **Категории:**\n{replace_emojis('a_dot_smaller')} **Игры на удачу**\n{replace_emojis('a_dot_smaller')} Быстрые игры на риск: монетка, кубики, угадай число и др.\n{replace_emojis('a_dot_smaller')} **Викторины и головоломки**\n{replace_emojis('a_dot_smaller')} Интеллектуальные состязания, викторины и слова.\n{replace_emojis('a_dot_smaller')} **Казино и ставки**\n{replace_emojis('a_dot_smaller')} Слоты, рулетка, баккара, лотерея и высокие ставки.\n\n{replace_emojis('a_dot_smaller')} Выберите категорию в меню ниже для просмотра списка игр",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        view = GamesMainView(interaction.user.id, interaction.guild_id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)



    # @app_commands.command(name="guess_number", description="Угадай число от 1 до 100")
    # async def guess_number(self, interaction: discord.Interaction) -> None:
    #     """Игра в угадай число."""
    #     from storage.redis_client import get_cooldown, set_cooldown
    #     from views.guess_number_view import NumberBetModal

    #     # Check cooldown (3 seconds)
    #     if await get_cooldown(interaction.guild_id, interaction.user.id, "minigame_guess_number"):
    #         await interaction.response.send_message(
    #             "⏳ Подождите 3 секунды перед повторной игрой!",
    #             ephemeral=True
    #         )
    #         return

    #     await set_cooldown(interaction.guild_id, interaction.user.id, "minigame_guess_number", 3)
    #     await interaction.response.send_modal(NumberBetModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="guess_emoji", description="Угадай эмодзи по подсказкам")
    # async def guess_emoji(self, interaction: discord.Interaction) -> None:
    #     """Игра в угадай эмодзи."""
    #     from views.guess_emoji_view import EmojiBetModal

    #     await interaction.response.send_modal(EmojiBetModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="wheel", description="Колесо фортуны")
    # async def wheel(self, interaction: discord.Interaction) -> None:
    #     """Игра колесо фортуны."""
    #     from storage.redis_client import get_cooldown, set_cooldown
    #     from views.wheel_view import WheelBetModal

    #     # Check cooldown (3 seconds)
    #     if await get_cooldown(interaction.guild_id, interaction.user.id, "minigame_wheel"):
    #         await interaction.response.send_message(
    #             "⏳ Подождите 3 секунды перед повторной игрой!",
    #             ephemeral=True
    #         )
    #         return

    #     await set_cooldown(interaction.guild_id, interaction.user.id, "minigame_wheel", 3)
    #     await interaction.response.send_modal(WheelBetModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="tictactoe", description="Крестики-Нолики")
    # async def tictactoe(self, interaction: discord.Interaction) -> None:
    #     """Игра крестики-нолики."""
    #     from views.tictactoe_view import TicTacToeBetModal

    #     await interaction.response.send_modal(TicTacToeBetModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="spin_bottle", description="Бутылочка")
    # async def spin_bottle(self, interaction: discord.Interaction) -> None:
    #     """Игра бутылочка."""
    #     from views.spin_bottle_view import SpinBottleBetModal

    #     await interaction.response.send_modal(SpinBottleBetModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="math_quiz", description="Математическая викторина")
    # async def math_quiz(self, interaction: discord.Interaction) -> None:
    #     """Математическая викторина."""
    #     from games.math_quiz import MathQuizModal

    #     await interaction.response.send_modal(MathQuizModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="word_guess", description="Угадай слово")
    # async def word_guess(self, interaction: discord.Interaction) -> None:
    #     """Угадай слово."""
    #     from games.word_guess import WordGuessModal

    #     await interaction.response.send_modal(WordGuessModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="riddles", description="Загадки")
    # async def riddles(self, interaction: discord.Interaction) -> None:
    #     """Загадки."""
    #     from games.riddles import RiddlesModal

    #     await interaction.response.send_modal(RiddlesModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="hangman", description="Виселица")
    # async def hangman(self, interaction: discord.Interaction) -> None:
    #     """Виселица."""
    #     from games.hangman import HangmanModal

    #     await interaction.response.send_modal(HangmanModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="memory", description="Память")
    # async def memory(self, interaction: discord.Interaction) -> None:
    #     """Память."""
    #     from games.memory import MemoryModal

    #     await interaction.response.send_modal(MemoryModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="anagrams", description="Анаграммы")
    # async def anagrams(self, interaction: discord.Interaction) -> None:
    #     """Анаграммы."""
    #     from games.anagrams import AnagramsModal

    #     await interaction.response.send_modal(AnagramsModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="logic_puzzle", description="Логические задачи")
    # async def logic_puzzle(self, interaction: discord.Interaction) -> None:
    #     """Логические задачи."""
    #     from games.logic_puzzle import LogicPuzzleModal

    #     await interaction.response.send_modal(LogicPuzzleModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="word_chain", description="Словесные цепочки")
    # async def word_chain(self, interaction: discord.Interaction) -> None:
    #     """Словесные цепочки."""
    #     from games.word_chain import WordChainModal

    #     await interaction.response.send_modal(WordChainModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="millionaire", description="Кто хочет стать миллионером")
    # async def millionaire(self, interaction: discord.Interaction) -> None:
    #     """Кто хочет стать миллионером."""
    #     from games.millionaire import MillionaireModal

    #     await interaction.response.send_modal(MillionaireModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="roulette", description="Рулетка")
    # async def roulette(self, interaction: discord.Interaction) -> None:
    #     """Рулетка."""
    #     from games.roulette import RouletteModal

    #     await interaction.response.send_modal(RouletteModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="blackjack", description="Блэкджек")
    # async def blackjack(self, interaction: discord.Interaction) -> None:
    #     """Блэкджек."""
    #     from games.blackjack import BlackjackModal

    #     await interaction.response.send_modal(BlackjackModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="slots", description="Слоты")
    # async def slots(self, interaction: discord.Interaction) -> None:
    #     """Слоты."""
    #     from games.slots import SlotsModal

    #     await interaction.response.send_modal(SlotsModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="baccarat", description="Баккара")
    # async def baccarat(self, interaction: discord.Interaction) -> None:
    #     """Баккара."""
    #     from games.baccarat import BaccaratModal

    #     await interaction.response.send_modal(BaccaratModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="lottery", description="Лотерея")
    # async def lottery(self, interaction: discord.Interaction) -> None:
    #     """Лотерея."""
    #     from games.lottery import LotteryModal

    #     await interaction.response.send_modal(LotteryModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="highlow", description="High-Low")
    # async def highlow(self, interaction: discord.Interaction) -> None:
    #     """High-Low."""
    #     from games.highlow import HighLowModal

    #     await interaction.response.send_modal(HighLowModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="dicebet", description="Dicebet")
    # async def dicebet(self, interaction: discord.Interaction) -> None:
    #     """Dicebet."""
    #     from games.dicebet import DicebetModal

    #     await interaction.response.send_modal(DicebetModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="craps", description="Крэпс")
    # async def craps(self, interaction: discord.Interaction) -> None:
    #     """Крэпс."""
    #     from games.craps import CrapsModal

    #     await interaction.response.send_modal(CrapsModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="snap", description="Snap")
    # async def snap(self, interaction: discord.Interaction) -> None:
    #     """Snap."""
    #     from games.snap import SnapModal

    #     await interaction.response.send_modal(SnapModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="poker", description="Покер")
    # async def poker(self, interaction: discord.Interaction) -> None:
    #     """Покер."""
    #     from games.poker import PokerModal

    #     await interaction.response.send_modal(PokerModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="triple_chance", description="Тройной шанс")
    # async def triple_chance(self, interaction: discord.Interaction) -> None:
    #     """Тройной шанс."""
    #     from games.triple_chance import TripleChanceModal

    #     await interaction.response.send_modal(TripleChanceModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="flag_quiz", description="Угадай флаг")
    # async def flag_quiz(self, interaction: discord.Interaction) -> None:
    #     """Угадай флаг."""
    #     from games.flag_quiz import FlagQuizModal

    #     await interaction.response.send_modal(FlagQuizModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="movie_quiz", description="Угадай фильм")
    # async def movie_quiz(self, interaction: discord.Interaction) -> None:
    #     """Угадай фильм."""
    #     from games.movie_quiz import MovieQuizModal

    #     await interaction.response.send_modal(MovieQuizModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="song_quiz", description="Угадай песню")
    # async def song_quiz(self, interaction: discord.Interaction) -> None:
    #     """Угадай песню."""
    #     from games.song_quiz import SongQuizModal

    #     await interaction.response.send_modal(SongQuizModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="checkers", description="Шашки")
    # async def checkers(self, interaction: discord.Interaction) -> None:
    #     """Шашки."""
    #     from games.checkers import CheckersModal

    #     await interaction.response.send_modal(CheckersModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="reversi", description="Реверси")
    # async def reversi(self, interaction: discord.Interaction) -> None:
    #     """Реверси."""
    #     from games.reversi import ReversiModal

    #     await interaction.response.send_modal(ReversiModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="chess", description="Шахматы")
    # async def chess(self, interaction: discord.Interaction) -> None:
    #     """Шахматы."""
    #     from games.chess import ChessModal

    #     await interaction.response.send_modal(ChessModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="koth", description="Король горы")
    # async def koth(self, interaction: discord.Interaction) -> None:
    #     """Король горы."""
    #     from games.koth import KothModal

    #     await interaction.response.send_modal(KothModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="elo_battle", description="Битва ELO")
    # async def elo_battle(self, interaction: discord.Interaction) -> None:
    #     """Битва ELO."""
    #     from games.elo_battle import EloBattleModal

    #     await interaction.response.send_modal(EloBattleModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="wordle", description="Слово дня")
    # async def wordle(self, interaction: discord.Interaction) -> None:
    #     """Слово дня."""
    #     from games.wordle import WordleModal

    #     await interaction.response.send_modal(WordleModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="game_2048", description="2048")
    # async def game_2048(self, interaction: discord.Interaction) -> None:
    #     """2048."""
    #     from games.game_2048 import Game2048Modal

    #     await interaction.response.send_modal(Game2048Modal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="sudoku", description="Судоку")
    # async def sudoku(self, interaction: discord.Interaction) -> None:
    #     """Судоку."""
    #     from games.sudoku import SudokuModal

    #     await interaction.response.send_modal(SudokuModal(interaction.guild_id, interaction.user.id))

    # @app_commands.command(name="minigame_tournament", description="Турнир мини-игр")
    # async def minigame_tournament(self, interaction: discord.Interaction) -> None:
    #     """Турнир мини-игр."""
    #     from games.minigame_tournament import MinigameTournamentModal

    #     await interaction.response.send_modal(MinigameTournamentModal(interaction.guild_id, interaction.user.id))

    @app_commands.command(name="profile", description="Показать ваш профиль")
    @app_commands.describe(user="Пользователь (пусто = ваш профиль)")
    async def profile(self, interaction: discord.Interaction, user: discord.Member = None) -> None:
        """Показать детальный профиль игрока."""
        from storage.player_stats_store import player_stats_store
        from storage.user_balance_store import user_balance_store
        from storage.case_store import case_store
        from storage.shop_store import inventory_store, shop_store
        from storage.minigame_store import minigame_store
        from utils.cosmetics import format_player_name
        from views.profile_view import ProfileView

        # Если пользователь не указан, показываем профиль автора
        target_user = user if user else interaction.user
        is_owner = target_user.id == interaction.user.id

        stats = await player_stats_store.get(interaction.guild_id, target_user.id)
        balance = await user_balance_store.get_balance(interaction.guild_id, target_user.id)

        # Создать профиль если его нет
        if not stats:
            from models.player_stats import PlayerStats
            stats = PlayerStats(
                guild_id=interaction.guild_id,
                user_id=target_user.id,
                name=target_user.display_name,
                elo=1000,
                wins=0,
                finals=0,
                games=0,
                current_streak=0,
                best_win_streak=0,
                best_loss_streak=0,
                total_kills=0,
                total_deaths=0,
                best_match_kills=0,
                total_elo_change=0,
                last_elo_change=0,
                xp=0,
                level=1,
                xp_to_next_level=250,
                total_earnings=0,
                tournament_participations=0,
                description=""
            )
            await player_stats_store.set(stats)

        # Ранг и уровень
        rank_title = get_rank_emoji(stats.level)
        current_xp, xp_needed = stats.get_level_progress()

        # Инвентарь
        cosmetics = inventory_store.get_player_inventory(interaction.guild_id, target_user.id)
        inventory_count = len(cosmetics)

        # Используем статистику турниров вместо мини-игр
        total_games_played = stats.games
        total_games_won = stats.wins

        # Винрейт
        win_rate = (total_games_won / total_games_played * 100) if total_games_played > 0 else 0

        # Last ELO Change
        elo_change = stats.last_elo_change if hasattr(stats, 'last_elo_change') else 0

        # Build description
        description_parts = ["Основная информация и статистика игрока:\n"]

        # Игровой профиль
        description_parts.append(f"{replace_emojis('⚪')} **Игровой профиль:**")
        description_parts.append(f"{replace_emojis('sub_middle')} Ранг: {rank_title}")
        description_parts.append(f"{replace_emojis('sub_middle')} ELO: {int(stats.elo):,} `(Last: {elo_change:+d})`")
        description_parts.append(f"{replace_emojis('sub_directory')} Уровень: Level {stats.level}")
        description_parts.append("")

        # Статистика игр
        description_parts.append(f"{replace_emojis('⚪')} **Статистика игр:**")
        description_parts.append(f"{replace_emojis('sub_middle')} Сыграно: {total_games_played} игр")
        description_parts.append(f"{replace_emojis('sub_middle')} Побед: {total_games_won} `({win_rate:.1f}%)`")

        # Show K/D and kills only if player has played games
        if total_games_played > 0:
            description_parts.append(f"{replace_emojis('sub_middle')} K/D Ratio: {stats.kd_ratio:.2f}")
            description_parts.append(f"{replace_emojis('sub_middle')} AVG Kills: {stats.avg_kills:.2f}")
            description_parts.append(f"{replace_emojis('sub_middle')} Skill Rating: {stats.skill_rating:.1f}")
            description_parts.append(f"{replace_emojis('sub_directory')} Max Kills: {stats.best_match_kills}")
        else:
            description_parts.append(f"{replace_emojis('sub_directory')} Ещё не играл в турниры")
        
        description_parts.append("")

        # Био
        if stats.description:
            description_parts.append(f"{replace_emojis('⚪')} **О себе:**")
            description_parts.append(f"{replace_emojis('sub_directory')} {stats.description}")
            description_parts.append("")

        description_parts.append(f"{replace_emojis('a_dot_smaller')} Данные обновляются в реальном времени")

        # Создать embed
        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} ПРОФИЛЬ - {target_user.display_name}",
            description="\n".join(description_parts),
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=target_user.display_avatar.url)

        # Кнопки только для владельца
        view = ProfileView(interaction.guild_id, target_user.id, is_owner)

        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="rank", description="Показать ваш ранг и прогресс")
    async def rank(self, interaction: discord.Interaction) -> None:
        """Показать текущий ранг и прогресс до следующего уровня."""
        from storage.player_stats_store import player_stats_store
        from utils.embeds import create_progress_bar

        stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)

        # Создать профиль если его нет
        if not stats:
            from models.player_stats import PlayerStats
            stats = PlayerStats(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
                name=interaction.user.display_name,
                elo=1000,
                wins=0,
                finals=0,
                games=0,
                current_streak=0,
                best_win_streak=0,
                best_loss_streak=0,
                total_kills=0,
                total_deaths=0,
                best_match_kills=0,
                total_elo_change=0,
                last_elo_change=0,
                xp=0,
                level=1,
                xp_to_next_level=250,
                total_earnings=0,
                tournament_participations=0,
                description=""
            )
            await player_stats_store.set(stats)

        rank_title = get_rank_emoji(stats.level)
        current_xp, xp_needed = stats.get_level_progress()
        progress_percent = int((current_xp / xp_needed) * 100) if xp_needed > 0 else 0
        progress_bar = create_progress_bar(current_xp, xp_needed)
        xp_remaining = xp_needed - current_xp

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} УРОВЕНЬ ПОЛЬЗОВАТЕЛЯ | /rank",
            description=f"{replace_emojis('white_arrow')} {interaction.user.mention}\n\n{replace_emojis('⚪')} **Текущий статус:**\n{replace_emojis('sub_middle')} Уровень: Level {stats.level}\n{replace_emojis('sub_directory')} Ранг: {rank_title}\n\n{replace_emojis('⚪')} **Прогресс опыта:**\n{replace_emojis('sub_middle')} Прогресс: `{progress_bar}` ({progress_percent}%)\n{replace_emojis('sub_directory')} До след. уровня: {xp_remaining:,} XP\n\n{replace_emojis('a_dot_smaller')} Накопить XP можно через участие в турнирах и победы",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="bet", description="Показать вашу статистику ставок")
    async def betting_stats(self, interaction: discord.Interaction) -> None:
        """Показать статистику ставок пользователя."""
        from storage.betting_stats_store import betting_stats_store

        stats = await betting_stats_store.get_user_stats(interaction.guild_id, interaction.user.id)

        if not stats or stats["total_bets"] == 0:
            await interaction.response.send_message(
                replace_emojis("⚪ У вас пока нет статистики ставок."),
                ephemeral=True
            )
            return

        accuracy = stats["success_rate"]
        lost_bets = stats["total_bets"] - stats["successful_bets"]

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} СТАТИСТИКА СТАВОК | /bet",
            description=f"Ваша общая статистика по ставкам:\n\n{replace_emojis('⚪')} **Основное:**\n{replace_emojis('sub_middle')} Всего ставок: {stats['total_bets']}\n{replace_emojis('sub_middle')} Выигрышных: {stats['successful_bets']}\n{replace_emojis('sub_middle')} Проигрышных: {lost_bets}\n{replace_emojis('sub_directory')} Точность: {accuracy:.1f}%\n\n{replace_emojis('⚪')} **Баланс:**\n{replace_emojis('sub_middle')} Выиграно: +{stats['total_won']} {replace_emojis('money')}\n{replace_emojis('sub_directory')} Проиграно: -{stats['total_lost']} {replace_emojis('money')}\n\n{replace_emojis('a_dot_smaller')} Данные обновляются в реальном времени",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

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
                replace_emojis("❌ Круг должен быть 2, 3 или 4."),
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if status.lower() not in ["on", "off"]:
            await interaction.response.send_message(
                replace_emojis("❌ Статус должен быть 'on' или 'off'."),
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Нет активного турнира."),
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                replace_emojis("❌ Лимиты можно менять только в фазе настройки."),
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament.circle_limits_enabled[circle] = (status.lower() == "on")
        store.set(tournament)

        status_text = "включен" if tournament.circle_limits_enabled[circle] else "отключен"
        await interaction.response.send_message(
            replace_emojis("✅ Лимит для круга {circle} {status_text}."),
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    # @app_commands.command(name="setbio", description="Установить описание профиля")
    # @app_commands.describe(bio="Короткое описание (максимум 100 символов)")
    # async def setbio(self, interaction: discord.Interaction, bio: str) -> None:
    #     """Установить описание профиля."""
    #     # Ограничение длины
    #     if len(bio) > 100:
    #         await interaction.response.send_message(
    #             replace_emojis("❌ Описание должно быть не более 100 символов."),
    #             ephemeral=True
    #         )
    #         return

    #     from storage.player_stats_store import player_stats_store
    #     from models.player_stats import PlayerStats

    #     stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)

    #     if not stats:
    #         # Create default stats for new players
    #         stats = PlayerStats(
    #             guild_id=interaction.guild_id,
    #             user_id=interaction.user.id,
    #             name=interaction.user.display_name,
    #             bio=bio
    #         )
    #     else:
    #         stats.bio = bio

    #     await player_stats_store.set(interaction.guild_id, interaction.user.id, stats)

    #     await interaction.response.send_message(
    #         replace_emojis("✅ Био установлено: {bio}"),
    #         ephemeral=True
    #     )

    # @app_commands.command(name="setavatar", description="Установить аватар профиля")
    # @app_commands.describe(url="URL изображения аватара")
    # async def setavatar(self, interaction: discord.Interaction, url: str = None) -> None:
    #     """Установить аватар профиля."""
    #     from storage.player_stats_store import player_stats_store
    #     from models.player_stats import PlayerStats

    #     stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)

    #     avatar_url = url
    #     if not avatar_url:
    #         # Use Discord avatar by default
    #         avatar_url = interaction.user.display_avatar.url

    #     if not stats:
    #         # Create default stats for new players
    #         stats = PlayerStats(
    #             guild_id=interaction.guild_id,
    #             user_id=interaction.user.id,
    #             name=interaction.user.display_name,
    #             avatar_url=avatar_url
    #         )
    #     else:
    #         stats.avatar_url = avatar_url

    #     await player_stats_store.set(interaction.guild_id, interaction.user.id, stats)

    #     if url:
    #         await interaction.response.send_message(
    #             replace_emojis("✅ Аватар профиля обновлен."),
    #             ephemeral=True
    #         )
    #     else:
    #         await interaction.response.send_message(
    #             replace_emojis("✅ Аватар профиля установлен по умолчанию (из Discord).",
    #             ephemeral=True
    #         )

    #     embed = discord.Embed(
    #         title=replace_emojis("📊 Профиль: {formatted_name}"),
    #         color=discord.Color.blue(),
    #     )

    #     # Show avatar (use custom avatar_url if set, otherwise Discord avatar)
    #     avatar_url = stats.avatar_url if stats.avatar_url else target_user.display_avatar.url
    #     embed.set_thumbnail(url=avatar_url)

    #     # Показать био если есть
    #     if stats.bio:
    #         embed.description = f"📝 {stats.bio}"

    #     embed.add_field(name=replace_emojis("🏆 ELO", value=str(int(stats.elo)), inline=True)
    #     embed.add_field(name="🥇 Победы", value=str(stats.wins), inline=True)
    #     embed.add_field(name=replace_emojis("🎮 Игры", value=str(stats.games), inline=True)
    #     embed.add_field(name=replace_emojis("📈 Win Rate", value=f"{win_rate:.0f}%", inline=True)
    #     embed.add_field(name=replace_emojis("⚔️ K/D Ratio", value=f"{stats.kd_ratio:.2f}", inline=True)

    #     # Additional stats
    #     embed.add_field(name=replace_emojis("🎯 AVG Kills", value=f"{stats.avg_kills:.2f}", inline=True)
    #     embed.add_field(name=replace_emojis("🔥 Max Kills", value=str(stats.best_match_kills), inline=True)
    #     embed.add_field(name=replace_emojis("📊 Last ELO Change", value=f"{stats.last_elo_change:+.0f}", inline=True)

    #     await interaction.followup.send(embed=embed)

    @app_commands.command(name="booyah", description="Рекорды турнира")
    async def booyah(self, interaction: discord.Interaction) -> None:
        """Показать рекорды турнира."""
        await interaction.response.defer()

        from storage.player_stats_store import player_stats_store

        all_players = await player_stats_store.get_all(interaction.guild_id)

        if not all_players:
            await interaction.edit_original_response(
                content=replace_emojis("❌ Пока нет данных для рекордов."),
            )
            return

        # Find records (all players)
        most_wins = max(all_players, key=lambda p: p.wins)
        highest_elo = max(all_players, key=lambda p: p.elo)
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
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} РЕКОРДЫ ТУРНИРА (/booyah)  {replace_emojis('a_star')}",
            description=f"{replace_emojis('white_arrow')} **Зал славы и абсолютные рекорды сервера r1z3**\n\n{replace_emojis('white_dot')} **🏆 Боевые достижения:**",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        # Build description with all records
        desc = embed.description

        # AVG Kills (20+ games)
        if best_avg_kills_20:
            desc += f"\n{replace_emojis('sub_middle')} {replace_emojis('white_arrow')} **Наибольшее AVG Kills:** <@{best_avg_kills_20.user_id}> `({best_avg_kills_20.avg_kills:.2f} / {best_avg_kills_20.games} игр)`"

        # Best match kills (all players)
        desc += f"\n{replace_emojis('sub_middle')} {replace_emojis('white_arrow')} **Рекорд киллов за матч:** <@{best_match_kills.user_id}> `({best_match_kills.best_match_kills} kills)`"

        # K/D (20+ games)
        if highest_kd:
            desc += f"\n{replace_emojis('sub_middle')} {replace_emojis('white_arrow')} **Наибольшее K/D:** <@{highest_kd.user_id}> `({highest_kd.kd_ratio:.2f} K/D / {highest_kd.games} игр)`"

        # WinRate (20+ games)
        if highest_winrate:
            desc += f"\n{replace_emojis('sub_middle')} {replace_emojis('white_arrow')} **Лучший WinRate:** <@{highest_winrate.user_id}> `({highest_winrate.win_rate:.1f}% / {highest_winrate.games} игр)`"

        # Highest ELO (all players)
        desc += f"\n{replace_emojis('sub_directory')} {replace_emojis('white_arrow')} **Самый высокий ELO:** <@{highest_elo.user_id}> `({highest_elo.elo:,} ELO)`"

        # Win/Loss streaks (20+ games)
        desc += f"\n\n{replace_emojis('white_dot')} **🔥 Серии побед и поражений:**"
        if best_win_streak_20:
            desc += f"\n{replace_emojis('sub_middle')} {replace_emojis('white_arrow')} **Лучшая серия побед:** <@{best_win_streak_20.user_id}> `({best_win_streak_20.best_win_streak} подряд)`"
        if best_loss_streak_20:
            desc += f"\n{replace_emojis('sub_directory')} {replace_emojis('white_arrow')} **Худшая серия поражений:** <@{best_loss_streak_20.user_id}> `({best_loss_streak_20.best_loss_streak} подряд)`"

        # Financial records
        desc += f"\n\n{replace_emojis('white_dot')} **💰 Финансовые рекорды:**"
        if richest_player:
            desc += f"\n{replace_emojis('sub_middle')} {replace_emojis('white_arrow')} **Богатейший игрок:** <@{richest_player.user_id}> `({max_balance:,}` {replace_emojis('money')}`)`"
        if best_bettor:
            bet_stats = await betting_stats_store.get(interaction.guild_id, best_bettor.user_id)
            bet_winrate = f"{(bet_stats.bets_won / bet_stats.total_bets * 100):.1f}%" if bet_stats and bet_stats.total_bets > 0 else "0%"
            desc += f"\n{replace_emojis('sub_directory')} {replace_emojis('white_arrow')} **Лучший беттер:** <@{best_bettor.user_id}> `({max_single_win:,}` {replace_emojis('money')} ` / {bet_winrate}%)`"

        desc += f"\n\n{replace_emojis('a_dot_smaller')} Статистика обновляется автоматически после каждого турнирного матча "

        embed.description = desc

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

        await ctx.send(replace_emojis(f"✅ ELO игрока {player.display_name} изменен на {elo}."), delete_after=10)

    @app_commands.command(name="edit", description=f"Изменить ELO или {replace_emojis('money')} игрока")
    @app_commands.describe(
        player="Игрок",
        type="Тип изменения: elo или money",
        amount="Новое значение (для ELO) или количество монет (для money)",
        operation="Операция: set (установить), add (добавить), remove (убрать)"
    )
    @is_bot_owner()
    async def edit_player(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
        type: str,
        amount: int,
        operation: str = "set"
    ) -> None:
        """Изменить ELO или монеты игрока."""

        if type not in ["elo", "money"]:
            await interaction.response.send_message(
                replace_emojis("❌ Тип должен быть 'elo' или 'money'."),
                ephemeral=True
            )
            return

        if operation not in ["set", "add", "remove"]:
            await interaction.response.send_message(
                replace_emojis("❌ Операция должна быть 'set', 'add' или 'remove'."),
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
                replace_emojis("✅ ELO игрока {player.display_name}: {current_elo} → {new_elo}"),
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
                replace_emojis("✅ Монеты игрока {player.display_name}: {current_balance} → {new_balance}"),
                ephemeral=True
            )

    @tournament_group.command(name="fix_userid", description="Исправить user_id игрока")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(player="Игрок")
    @is_bot_owner()
    async def fix_userid(self, interaction: discord.Interaction, player: discord.Member) -> None:
        """Исправить user_id игрока в базе данных."""
        from storage.player_stats_store import player_stats_store
        from storage.db import get_pool

        if not player_stats_store._use_db:
            await interaction.response.send_message(replace_emojis("❌ База данных не включена."), ephemeral=True)
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Update all records with the player's name to use their user_id
            result = await conn.execute(
                "UPDATE player_stats SET user_id = $1 WHERE guild_id = $2 AND name = $3",
                player.id, interaction.guild_id, player.display_name
            )

        await interaction.response.send_message(replace_emojis(f"✅ Обновлено {result} записей для {player.display_name}."), ephemeral=True)

    @app_commands.command(name="fix", description="Сбросить статистику ботов (cap1, cap2 и т.д.)")
    @app_commands.default_permissions(administrator=True)
    @is_bot_owner()
    async def fix_bot_stats(self, interaction: discord.Interaction) -> None:
        """Сбросить статистику ботов в базе данных."""
        from storage.player_stats_store import player_stats_store
        from storage.db import get_pool

        if not player_stats_store._use_db:
            await interaction.response.send_message(replace_emojis("❌ База данных не включена."), ephemeral=True)
            return

        # Bot names to reset (both lowercase and uppercase variants)
        bot_names = [
            # Cap bots
            "cap1", "Cap1", "CAP1",
            "cap2", "Cap2", "CAP2",
            "cap3", "Cap3", "CAP3",
            "cap4", "Cap4", "CAP4",
            "cap5", "Cap5", "CAP5",
            "cap6", "Cap6", "CAP6",
            "cap7", "Cap7", "CAP7",
            "cap8", "Cap8", "CAP8",
            "cap9", "Cap9", "CAP9",
            "cap10", "Cap10", "CAP10",
            "cap11", "Cap11", "CAP11",
            "cap12", "Cap12", "CAP12",
            "cap13", "Cap13", "CAP13",
            "cap14", "Cap14", "CAP14",
            "cap15", "Cap15", "CAP15",
            "cap16", "Cap16", "CAP16",
            "cap17", "Cap17", "CAP17",
            "cap18", "Cap18", "CAP18",
            "cap19", "Cap19", "CAP19",
            "cap20", "Cap20", "CAP20",
            "cap21", "Cap21", "CAP21",
            "cap22", "Cap22", "CAP22",
            "cap23", "Cap23", "CAP23",
            "cap24", "Cap24", "CAP24",
            "cap25", "Cap25", "CAP25",
            "cap26", "Cap26", "CAP26",
            "cap27", "Cap27", "CAP27",
            "cap28", "Cap28", "CAP28",
            "cap29", "Cap29", "CAP29",
            "cap30", "Cap30", "CAP30",
            "cap31", "Cap31", "CAP31",
            "cap32", "Cap32", "CAP32",
            # P bots (P2-1, P3-1, P4-1, etc.)
            "P1-0", "P1-1", "P1-2", "P1-3",
            "P2-0", "P2-1", "P2-2", "P2-3",
            "P3-0", "P3-1", "P3-2", "P3-3",
            "P4-0", "P4-1", "P4-2", "P4-3",
            "P5-0", "P5-1", "P5-2", "P5-3",
            "P6-0", "P6-1", "P6-2", "P6-3",
            "P7-0", "P7-1", "P7-2", "P7-3",
            "P8-0", "P8-1", "P8-2", "P8-3",
        ]

        pool = await get_pool()
        reset_count = 0

        async with pool.acquire() as conn:
            for bot_name in bot_names:
                # Check if bot exists
                result = await conn.fetchrow(
                    "SELECT user_id FROM player_stats WHERE guild_id = $1 AND name = $2",
                    interaction.guild_id, bot_name
                )
                if result:
                    # Reset stats to default values
                    await conn.execute(
                        """UPDATE player_stats
                        SET elo = 1000, wins = 0, finals = 0, games = 0,
                            current_streak = 0, best_win_streak = 0, best_loss_streak = 0,
                            total_kills = 0, total_deaths = 0, best_match_kills = 0,
                            total_elo_change = 0, last_elo_change = 0,
                            xp = 0, level = 1, xp_to_next_level = 250,
                            total_earnings = 0, tournament_participations = 0
                        WHERE guild_id = $1 AND name = $2""",
                        interaction.guild_id, bot_name
                    )
                    reset_count += 1

        await interaction.response.send_message(
            replace_emojis(f"✅ Сброшена статистика для {reset_count} ботов."),
            ephemeral=True
        )

    # @app_commands.command(name="replace", description="Заменить игрока")
    # @app_commands.describe(
    #     current_player="Имя игрока которого нужно заменить (или @упоминание)",
    #     new_player="Имя нового игрока (или @упоминание)"
    # )
    # @is_org()
    # async def replace_player(
    #     self,
    #     interaction: discord.Interaction,
    #     current_player: str,
    #     new_player: str
    # ) -> None:
    #     """Заменить игрока в турнире."""
    #     tournament = store.get(interaction.guild_id)
    #     if not tournament:
    #         await interaction.response.send_message(
    #             replace_emojis("❌ Нет активного турнира."),
    #             ephemeral=True,
    #         )
    #         asyncio.create_task(_delete_ephemeral_later(interaction))
    #         return

    #     # Handle @mentions - extract display name if it's a mention
    #     old_name = current_player.strip()
    #     new_name = new_player.strip()

    #     # Check if current_player is a mention and extract the name
    #     if old_name.startswith("<@") and old_name.endswith(">"):
    #         user_id = int(old_name.strip("<@!>"))
    #         member = interaction.guild.get_member(user_id)
    #         if member:
    #             old_name = member.display_name

    #     # Check if new_player is a mention and extract the name
    #     if new_name.startswith("<@") and new_name.endswith(">"):
    #         user_id = int(new_player.strip("<@!>"))
    #         member = interaction.guild.get_member(user_id)
    #         if member:
    #             new_name = member.display_name

    #     if tournament.phase == TournamentPhase.SETUP or tournament.phase == TournamentPhase.DRAFT:
    #         # Replace in circles
    #         if old_name not in tournament.all_players:
    #             await interaction.response.send_message(
    #                 replace_emojis("❌ Игрок `{old_name}` не найден."),
    #                 ephemeral=True,
    #             )
    #             asyncio.create_task(_delete_ephemeral_later(interaction))
    #             return

    #         for circle in range(1, 5):
    #             circle_list = getattr(tournament, f"circle{circle}")
    #             if old_name in circle_list:
    #                 idx = circle_list.index(old_name)
    #                 circle_list[idx] = new_name
    #                 break
    #     elif tournament.phase == TournamentPhase.FINAL:
    #         # Replace in final teams
    #         found = False
    #         for team_idx in range(len(tournament.final_teams)):
    #             if tournament.final_teams[team_idx] == old_name:
    #                 tournament.final_teams[team_idx] = new_name
    #                 found = True
    #                 break

    #         if not found:
    #             await interaction.response.send_message(
    #                 replace_emojis("❌ Игрок `{old_name}` не найден в финальных командах."),
    #                 ephemeral=True,
    #             )
    #             asyncio.create_task(_delete_ephemeral_later(interaction))
    #             return
    #     else:
    #         # Replace in teams (TEAMS, QUALIFIERS, SEMIFINALS)
    #         found = False
    #         for team in tournament.teams:
    #             for key, value in team.items():
    #                 if value == old_name:
    #                     team[key] = new_name
    #                     found = True
    #                     break
    #             if found:
    #                 break

    #         if not found:
    #             await interaction.response.send_message(
    #                 replace_emojis("❌ Игрок `{old_name}` не найден в командах."),
    #                 ephemeral=True,
    #             )
    #             asyncio.create_task(_delete_ephemeral_later(interaction))
    #             return

    #     store.set(tournament)

    #     await interaction.response.send_message(
    #         replace_emojis("✅ Игрок `{old_name}` заменен на `{new_name}`."),
    #         ephemeral=True
    #     )
    #     asyncio.create_task(_delete_ephemeral_later(interaction))

    #     await self.bot.update_tournament_message(interaction.guild, tournament)

    # @app_commands.command(name="delete_player", description="Удалить игрока из турнира")
    # @app_commands.describe(name="Имя игрока которого нужно удалить")
    # @is_org()
    # async def delete_player(
    #     self,
    #     interaction: discord.Interaction,
    #     name: str
    # ) -> None:
    #     """Удалить игрока из турнира."""
    #     tournament = store.get(interaction.guild_id)
    #     if not tournament:
    #         await interaction.response.send_message(
    #             replace_emojis("❌ Нет активного турнира."),
    #             ephemeral=True,
    #         )
    #         asyncio.create_task(_delete_ephemeral_later(interaction))
    #         return

    #     name = name.strip()

    #     if tournament.phase == TournamentPhase.SETUP:
    #         if not tournament.remove_player(name):
    #             await interaction.response.send_message(
    #                 replace_emojis("❌ Игрок `{name}` не найден."),
    #                 ephemeral=True,
    #             )
    #             asyncio.create_task(_delete_ephemeral_later(interaction))
    #             return
    #     else:
    #         await interaction.response.send_message(
    #             replace_emojis("❌ Можно удалять игроков только на этапе настройки."),
    #             ephemeral=True,
    #         )
    #         asyncio.create_task(_delete_ephemeral_later(interaction))
    #         return

    #     store.set(tournament)

    #     await interaction.response.send_message(
    #         replace_emojis("✅ Игрок `{name}` удален."),
    #         ephemeral=True
    #     )
    #     asyncio.create_task(_delete_ephemeral_later(interaction))

    #     await self.bot.update_tournament_message(interaction.guild, tournament)

    @app_commands.command(name="pay", description="Передать монеты другому игроку")
    @app_commands.describe(user="Игрок", amount="Сумма")
    async def pay(self, interaction: discord.Interaction, user: discord.User, amount: int) -> None:
        """Передать монеты с комиссией 10%."""
        from storage.user_balance_store import user_balance_store
        
        if amount < 100:
            await interaction.response.send_message(replace_emojis("❌ Минимальная сумма: 100 🪙"), ephemeral=True)
            return
        
        if user.id == interaction.user.id:
            await interaction.response.send_message(replace_emojis("❌ Нельзя передать самому себе"), ephemeral=True)
            return
        
        try:
            result = await user_balance_store.transfer_balance(
                interaction.guild_id, interaction.user.id,
                interaction.guild_id, user.id,
                amount, 0.1
            )
            
            embed = discord.Embed(
                title=replace_emojis("Передача монет"),
                color=discord.Color.from_rgb(69, 69, 69)
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            embed.description = f"{interaction.user.mention}, Вы успешно **передали** {replace_emojis('money')}\n\n{replace_emojis('⚪')} **Комиссия:** 10%\n{replace_emojis('⚪')} **Списалось:** {result['total_deducted']:,} {replace_emojis('money')}"
            embed.add_field(name="Пользователь", value=f"{replace_emojis('white_arrow')} {user.mention} **получил** — {result['amount']:,} {replace_emojis('money')}", inline=False)
            
            await interaction.response.send_message(embed=embed)
        except ValueError as e:
            await interaction.response.send_message(replace_emojis(f"❌ {str(e)}"), ephemeral=True)

    @app_commands.command(name="gift", description="Передать предмет другому игроку")
    @app_commands.describe(user="Игрок", item_id="ID предмета")
    async def gift(self, interaction: discord.Interaction, user: discord.User, item_id: str) -> None:
        """Передать предмет с комиссией 10% от стоимости."""
        from storage.user_balance_store import user_balance_store
        from storage.shop_store import inventory_store, shop_store

        if user.id == interaction.user.id:
            await interaction.response.send_message(replace_emojis("⚪ Нельзя подарить самому себе"), ephemeral=True)
            return

        # Get item info
        item = shop_store.get_item(item_id)
        if not item:
            await interaction.response.send_message(replace_emojis("⚪ Предмет не найден"), ephemeral=True)
            return

        # Check if sender has the item
        sender_inventory = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
        has_item = any(cosmetic.item_id == item_id for cosmetic in sender_inventory)

        if not has_item:
            await interaction.response.send_message(replace_emojis("⚪ У вас нет этого предмета"), ephemeral=True)
            return

        # Calculate fee
        fee = int(item.price * 0.1)  # 10% комиссия

        # Check sender has enough balance for fee
        sender_balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if sender_balance < fee:
            await interaction.response.send_message(
                replace_emojis(f"⚪ Недостаточно монет для комиссии. Нужно: {fee} {replace_emojis('money')}, есть: {sender_balance} {replace_emojis('money')}"),
                ephemeral=True
            )
            return

        # Deduct fee
        await user_balance_store.subtract_balance(interaction.guild_id, interaction.user.id, fee)

        # Transfer item
        result = inventory_store.transfer_cosmetic(
            interaction.guild_id, interaction.user.id,
            interaction.guild_id, user.id,
            item_id
        )

        if result['success']:
            # Icon map для значков
            icon_map = {
                "icon_letter": "icon_letter",
                "icon_paw": "icon_paw",
                "icon_bluestacks": "icon_bluestacks",
                "icon_teacup": "icon_teacup",
                "icon_ribbon": "icon_ribbon",
                "icon_18plus": "icon_18plus",
                "icon_heart": "icon_heart",
                "icon_v_badge": "icon_v_badge",
                "icon_cards": "icon_cards",
                "icon_cat_ears": "icon_cat_ears",
                "icon_wing": "icon_wing",
                "icon_fuck_you": "icon_fuck_you",
                "icon_zzz": "icon_zzz",
            }

            # Rare emoji map
            rare_map = {
                "basic": "rare_basic",
                "premium": "rare_premium",
                "elite": "rare_elite",
                "special": "rare_special",
            }

            rare_emoji = rare_map.get(item.rarity.value, "")

            # Определить отображение предмета
            if item.cosmetic_type.value == "icon":
                icon_emoji = icon_map.get(item.value, "")
                item_display = f"{replace_emojis(icon_emoji)} **{item.name}**"
            else:  # tag
                item_display = f"**{item.value}**"

            embed = discord.Embed(
                title="Передача предмета",
                description=f"{interaction.user.mention}, Вы успешно **подарили** {item_display} • {replace_emojis(rare_emoji)}\n\n• **Комиссия:** 10% ({fee} {replace_emojis('money')})\n• **Стоимость:** {item.price} {replace_emojis('money')}\n\n**Пользователь**\n{replace_emojis('white_arrow')} {user.mention} **получил** — {item_display}",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)

            await interaction.response.send_message(embed=embed, ephemeral=False)
        elif result['compensated']:
            # Return fee since transfer failed
            await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, fee)

            embed = discord.Embed(
                title="Передача не удалась",
                description=f"{interaction.user.mention}, у получателя уже есть этот предмет. Комиссия возвращена.",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)

            await interaction.response.send_message(embed=embed, ephemeral=False)
        else:
            # Return fee since transfer failed
            await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, fee)

            await interaction.response.send_message(
                replace_emojis(f"⚪ Не удалось передать предмет: {result['item_name']}"),
                ephemeral=True
            )

    @app_commands.command(name="debug_emoji", description="Проверить конфигурацию эмодзи")
    @is_bot_owner()
    async def debug_emoji(self, interaction: discord.Interaction) -> None:
        """Проверить какие эмодзи загружены."""
        from config import RANK_EMOJIS, GAME_EMOJIS, get_emoji
        
        embed = discord.Embed(
            title=replace_emojis("🔍 Debug: Конфигурация эмодзи"),
            color=discord.Color.blue()
        )
        
        # Rank emojis
        rank_text = ""
        for rank, emoji_id in RANK_EMOJIS.items():
            emoji = get_emoji(rank)
            status = "✅" if emoji_id else "❌"
            rank_text += f"{status} {rank}: ID={emoji_id or 'Empty'} → `{emoji}`\n"
        
        embed.add_field(name=replace_emojis("🏆 Ранги"), value=rank_text or "Нет данных", inline=False)
        
        # Game emojis - show ALL configured emojis
        game_text = ""
        for name, emoji_id in GAME_EMOJIS.items():
            emoji = get_emoji(name)
            status = "✅" if emoji_id else "❌"
            game_text += f"{status} {name}: ID={emoji_id or 'Empty'} → `{emoji}`\n"
        
        if game_text:
            embed.add_field(name=replace_emojis("🎮 Игровые эмодзи (все)"), value=game_text, inline=False)
        else:
            embed.add_field(name=replace_emojis("🎮 Игровые эмодзи"), value="Нет сконфигурированных эмодзи", inline=False)
        
        # Count loaded emojis
        loaded_ranks = sum(1 for v in RANK_EMOJIS.values() if v)
        loaded_games = sum(1 for v in GAME_EMOJIS.values() if v)
        
        embed.add_field(
            name=replace_emojis("📊 Статистика"),
            value=f"Рангов загружено: {loaded_ranks}/{len(RANK_EMOJIS)}\nИгровых загружено: {loaded_games}/{len(GAME_EMOJIS)}",
            inline=False
        )
        
        # Test rank formatting
        test_emoji = get_rank_emoji(100)
        embed.add_field(name=replace_emojis("🧪 Тест (Radiant)"), value=f"`{test_emoji}`", inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ктоя", description="Узнать кто ты на самом деле")
    async def whoami(self, interaction: discord.Interaction) -> None:
        """Узнать кто ты на самом деле."""
        from storage.db import get_pool
        import random
        from storage.whoami_responses import WHOAMI_RESPONSES

        guild_id = interaction.guild_id
        user_id = interaction.user.id

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Get last use time
            record = await conn.fetchrow(
                "SELECT last_use FROM whoami_cooldowns WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id
            )

            now = datetime.now()
            cooldown_hours = 3

            if record and record["last_use"]:
                last_use = record["last_use"]
                time_passed = now - last_use

                if time_passed < timedelta(hours=cooldown_hours):
                    # Calculate remaining time
                    remaining = timedelta(hours=cooldown_hours) - time_passed
                    hours = int(remaining.total_seconds() // 3600)
                    minutes = int((remaining.total_seconds() % 3600) // 60)

                    await interaction.response.send_message(
                        replace_emojis(f"⚪ Вы уже узнали кто ты!\nСледующий вопрос через: {hours}ч {minutes}мин"),
                        ephemeral=True
                    )
                    return

            # Get random response
            response = random.choice(WHOAMI_RESPONSES)

            # Update last use time
            await conn.execute(
                """
                INSERT INTO whoami_cooldowns (guild_id, user_id, last_use)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET last_use = $3
                """,
                guild_id, user_id, now
            )

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} **Кто я?** {replace_emojis('a_star')}",
            description=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} {response} {replace_emojis('a_star')}",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="daily", description="Получить ежедневный бонус (100 монет раз в 12 часов)")
    async def daily(self, interaction: discord.Interaction) -> None:
        """Получить ежедневный бонус с серией."""
        from storage.db import get_pool
        from storage.user_balance_store import user_balance_store

        guild_id = interaction.guild_id
        user_id = interaction.user.id

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Get last claim time and streak
            record = await conn.fetchrow(
                "SELECT last_claim, last_streak_date, streak FROM bonus_cooldowns WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id
            )

            now = datetime.now()
            cooldown_hours = 12
            today = now.date()

            if record and record["last_claim"]:
                last_claim = record["last_claim"]
                time_passed = now - last_claim

                if time_passed < timedelta(hours=cooldown_hours):
                    # Calculate remaining time
                    remaining = timedelta(hours=cooldown_hours) - time_passed
                    hours = int(remaining.total_seconds() // 3600)
                    minutes = int((remaining.total_seconds() % 3600) // 60)

                    await interaction.response.send_message(
                        replace_emojis(f"⚪ Вы уже получили бонус!\nСледующий бонус через: {hours}ч {minutes}мин"),
                        ephemeral=True
                    )
                    return

            # Calculate streak
            streak = 1
            if record and record["last_streak_date"]:
                last_streak_date = record["last_streak_date"]
                last_claim_date = record["last_claim"].date()

                # If claimed yesterday, increment streak
                if (today - last_streak_date).days == 1:
                    streak = (record["streak"] or 0) + 1
                # If claimed today but it's been 12+ hours, don't increment
                elif last_claim_date == today:
                    streak = record["streak"] or 1
                # If streak was broken, reset to 1
                else:
                    streak = 1

            # Cap streak at 10
            streak = min(streak, 10)

            # Calculate reward: 100 on day 1, 200 on day 10, linear interpolation
            if streak == 1:
                reward = 100
            elif streak >= 10:
                reward = 200
            else:
                # Linear: 100 + (streak - 1) * (100 / 9)
                reward = int(100 + (streak - 1) * (100 / 9))

            # Give the bonus
            await user_balance_store.add_balance(guild_id, user_id, reward)

            # Update last claim time and streak
            await conn.execute(
                """
                INSERT INTO bonus_cooldowns (guild_id, user_id, last_claim, last_streak_date, streak)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET last_claim = $3, last_streak_date = $4, streak = $5
                """,
                guild_id, user_id, now, today, streak
            )

            # Fire emoji for streaks
            fire_emoji = "🔥" if streak >= 3 else ""

            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} ЕЖЕДНЕВНАЯ НАГРАДА | /daily",
                description=f"{replace_emojis('white_arrow')} {interaction.user.mention}\n\n{replace_emojis('⚪')} **Ваша награда:**\n{replace_emojis('sub_middle')} Получено: {reward} {replace_emojis('money')}\n{replace_emojis('sub_directory')} Серия заходов: {streak} дней {fire_emoji}\n\n{replace_emojis('a_dot_smaller')} Возвращайтесь через 12 часов, чтобы получить следующую награду!",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)

            await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="role", description="Настроить сообщение для управления ролью организатора (только для админов)")
    async def role(self, interaction: discord.Interaction) -> None:
        """Создать сообщение для управления ролью организатора."""
        # Check permissions manually
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ У вас нет прав для использования этой команды. Требуются права администратора.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            await setup_org_role_message(self.bot, interaction.guild, interaction.channel)
            await interaction.followup.send(
                "✅ Сообщение для управления ролью организатора создано",
                ephemeral=False
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Ошибка при создании сообщения: {str(e)}",
                ephemeral=True
            )

    @app_commands.command(name="welcome", description="Показать информацию о сервере и боте (только для владельца)")
    @is_bot_owner()
    async def welcome(self, interaction: discord.Interaction) -> None:
        """Показать приветственное сообщение с гайдом."""
        # Single embed with image and text
        embed = discord.Embed(
            title="✧ DISCORD SERVER r1z3 | ПУТЕВОДИТЕЛЬ ✧",
            description=f"{replace_emojis('white_arrow')} **Добро пожаловать на сервер!**\nЭтот гайд поможет вам сориентироваться по каналам, узнать систему рангов и использовать команды нашего бота.\n\n{replace_emojis('white_dot')} {replace_emojis('white_arrow')} **НАВИГАЦИЯ ПО КАНАЛАМ:**\n{replace_emojis('a_dot_smaller')} <#1200125075156910181> {replace_emojis('white_arrow')} Основное общение сообщества\n{replace_emojis('a_dot_smaller')} <#1549809898643001484> {replace_emojis('white_arrow')} Проведение турниров\n{replace_emojis('a_dot_smaller')} <#1514677029159567604> {replace_emojis('white_arrow')} Яркие моменты из игр\n{replace_emojis('a_dot_smaller')} <#1551167853741219880> {replace_emojis('white_arrow')} Команды ботов и спам-игры\n{replace_emojis('a_dot_smaller')} <#1250974603162026024> {replace_emojis('white_arrow')} Прослушивание треков\n{replace_emojis('a_dot_smaller')} <#1242489553189732373> {replace_emojis('white_arrow')} Полезные файлы для FF\n\n {replace_emojis('a_dot_smaller')} Выберите категорию в меню ниже, чтобы узнать больше",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_image(url="https://cdn.discordapp.com/attachments/1553458753800507532/1554375503572369449/a338360963724ad1957dd13a1730547c.png?ex=6abca87e&is=6abb56fe&hm=ed0782e9cd150cb625960c89d66a581e693c9ec382180e795e765f29c960d21c&")
        embed.set_footer(text="DISCORD SERVER r1z3")

        view = GuideView()

        # Send the main welcome message directly to channel (not as followup)
        await interaction.channel.send(embed=embed, view=view)

        # Send confirmation to bot owner only
        await interaction.response.send_message(
            replace_emojis("✅ Сообщение приветствия создано"),
            ephemeral=True
        )

    @app_commands.command(name="reset", description="Сбросить статистику игрока (только для владельца бота)")
    @app_commands.describe(user="Пользователь для сброса статистики")
    @is_bot_owner()
    async def reset(self, interaction: discord.Interaction, user: discord.Member) -> None:
        """Сбросить статистику игрока (только для владельца бота)."""

        guild_id = interaction.guild_id
        user_id = user.id
        user_name = user.display_name

        try:
            # Reset player stats
            await player_stats_store.reset_player(guild_id, user_id)
            
            # Reset user balance
            await user_balance_store.reset_user(guild_id, user_id)
            
            # Reset betting stats
            await betting_stats_store.reset_user(guild_id, user_id)
            
            await interaction.response.send_message(
                replace_emojis("✅ Статистика пользователя {user_name} успешно сброшена!"),
                ephemeral=True
            )
            logger.info(f"Reset stats for user {user_name} (ID: {user_id}) in guild {guild_id}")
        except Exception as e:
            logger.error(f"Error resetting user stats: {e}", exc_info=True)
            await interaction.response.send_message(
                replace_emojis("❌ Произошла ошибка при сбросе статистики: {e}"),
                ephemeral=True
            )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Обработка ошибок slash-команд."""
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error) or replace_emojis("❌ Недостаточно прав."),
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
        msg = replace_emojis("❌ Произошла ошибка при выполнении команды."),
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            asyncio.create_task(_delete_ephemeral_later(interaction))
        except discord.NotFound:
            # Interaction expired, can't respond
            pass


def get_rank_emoji(level: int) -> str:
    """Получить эмодзи и название ранга по уровню."""
    from config import get_emoji
    
    # Get rank name based on level
    if level >= 100:
        rank_name = "Radiant"
    elif level >= 93:
        rank_name = "Immortal I"
    elif level >= 86:
        rank_name = "Immortal II"
    elif level >= 80:
        rank_name = "Immortal III"
    elif level >= 73:
        rank_name = "Ascendant I"
    elif level >= 66:
        rank_name = "Ascendant II"
    elif level >= 60:
        rank_name = "Ascendant III"
    elif level >= 54:
        rank_name = "Diamond I"
    elif level >= 48:
        rank_name = "Diamond II"
    elif level >= 42:
        rank_name = "Diamond III"
    elif level >= 37:
        rank_name = "Platinum I"
    elif level >= 32:
        rank_name = "Platinum II"
    elif level >= 27:
        rank_name = "Platinum III"
    elif level >= 23:
        rank_name = "Gold I"
    elif level >= 19:
        rank_name = "Gold II"
    elif level >= 15:
        rank_name = "Gold III"
    elif level >= 12:
        rank_name = "Silver I"
    elif level >= 9:
        rank_name = "Silver II"
    elif level >= 6:
        rank_name = "Silver III"
    elif level >= 4:
        rank_name = "Bronze I"
    elif level >= 2:
        rank_name = "Bronze II"
    else:
        rank_name = "Bronze III"
    
    # Get the base rank name (without tier) for emoji lookup
    base_rank = rank_name.split()[0] if " " in rank_name else rank_name
    
    # Get custom or standard emoji
    emoji = get_emoji(base_rank)
    
    return f"{emoji} {rank_name}"


class GuideSelectMenu(discord.ui.Select):
    """Выпадающее меню для гайда."""
    
    def __init__(self, current=None):
        self.current = current
        options = [
            discord.SelectOption(
                label="Турниры",
                description="Сетка, топ ELO и статистика турниров",
                value="tournaments"
            ),
            discord.SelectOption(
                label="Профиль",
                description="Карточка игрока и текущий ранг",
                value="profile"
            ),
            discord.SelectOption(
                label="Экономика",
                description="Баланс, переводы, продажи и ставки",
                value="economy"
            ),
            discord.SelectOption(
                label="Магазин",
                description="Покупка ролей и инвентарь",
                value="shop"
            ),
            discord.SelectOption(
                label="Мини-игры",
                description="Игровые треды и игры",
                value="games"
            ),
            discord.SelectOption(
                label="Система рангов",
                description="Информация о рангах и уровнях",
                value="ranks"
            ),
            discord.SelectOption(
                label="Организаторам",
                description="Создание турниров и управление кругами",
                value="organizers"
            ),
            discord.SelectOption(
                label="Правила сервера",
                description="Свод правил и регламент турниров",
                value="rules"
            )
        ]
        
        # Set default option
        for opt in options:
            if opt.value == current:
                opt.default = True
                break
        
        super().__init__(
            placeholder="Выберите категорию...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        """Обработка выбора пункта меню."""
        from config import RANK_EMOJIS

        # Standard emojis as fallback
        standard_emojis = {
            "Radiant": "👑",
            "Immortal": "🔱",
            "Ascendant": "🎯",
            "Diamond": "💎",
            "Platinum": "🌪️",
            "Gold": "🥇",
            "Silver": "🥈",
            "Bronze": "🥉",
        }

        def format_emoji(name, value):
            if value and value.isdigit():
                return f"<:{name}:{value}>"
            elif value and value.startswith("<:") and value.endswith(">"):
                return value  # Already formatted
            return value  # Use as-is (standard emoji)

        radiant = format_emoji('Radiant', RANK_EMOJIS.get("Radiant", "") or standard_emojis["Radiant"])
        immortal = format_emoji('Immortal', RANK_EMOJIS.get("Immortal", "") or standard_emojis["Immortal"])
        ascendant = format_emoji('Ascendant', RANK_EMOJIS.get("Ascendant", "") or standard_emojis["Ascendant"])
        diamond = format_emoji('Diamond', RANK_EMOJIS.get("Diamond", "") or standard_emojis["Diamond"])
        platinum = format_emoji('Platinum', RANK_EMOJIS.get("Platinum", "") or standard_emojis["Platinum"])
        gold = format_emoji('Gold', RANK_EMOJIS.get("Gold", "") or standard_emojis["Gold"])
        silver = format_emoji('Silver', RANK_EMOJIS.get("Silver", "") or standard_emojis["Silver"])
        bronze = format_emoji('Bronze', RANK_EMOJIS.get("Bronze", "") or standard_emojis["Bronze"])

        if self.values[0] == "tournaments":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  РАЗДЕЛ: ТУРНИРЫ  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_arrow')} **Информация и статистика турнирной системы**\n\n{replace_emojis('white_dot')} **Доступные команды:**\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/top` — Таблица лучших игроков `(level / money / elo)`\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/booyah` — Зал славы, рекорды и история прошлых турниров\n\n{replace_emojis('a_dot_smaller')}  Используйте выпадающее меню ниже для перехода в другие разделы ",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            view = GuideView(current="tournaments")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "profile":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  РАЗДЕЛ: ПРОФИЛЬ  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_arrow')} **Управление персональным аккаунтом**\n\n{replace_emojis('white_dot')} **Доступные команды:**\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/profile` — Карточка игрока, общая статистика и достижения\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/rank` — Ваша карточка ранга и прогресс до следующего уровня\n\n{replace_emojis('a_dot_smaller')}  Используйте выпадающее меню ниже для перехода в другие разделы ",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            view = GuideView(current="profile")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "economy":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  РАЗДЕЛ: ЭКОНОМИКА  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_arrow')} **Управление финансами, подарками и ставками**\n\n{replace_emojis('white_dot')} **Доступные команды:**\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/balance` — Проверить свой текущий баланс монет\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/daily` — Получить бонус каждые 12 часов `(серия до 10 дней)`\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/pay` — Перевести монеты другому пользователю\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/gift` — Подарить предмет из инвентаря другому игроку\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/bet` — Личная статистика и история активных ставок\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/sell` — Продать предмет из инвентаря за 50% стоимости\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/rob @пользователь` — Одиночное ограбление (50% шанс, кулдаун 3ч)\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/robgroup @пользователь` — Групповое ограбление (2-6 игроков, шанс 50-80%)\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/bank status` — Статус банковского сейфа\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/bank deposit [сумма/all]` — Пополнить сейф (комиссия 5%)\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/bank withdraw [сумма/all]` — Снять со сейфа (без комиссии)\n\n{replace_emojis('white_dot')} **Защита средств:**\n{replace_emojis('a_dot_smaller')} Наличные уязвимы для /rob и /robgroup\n{replace_emojis('a_dot_smaller')} Деньги в банковском сейфе 100% защищены\n\n{replace_emojis('a_dot_smaller')}  Используйте выпадающее меню ниже для перехода в другие разделы ",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            view = GuideView(current="economy")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "shop":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  РАЗДЕЛ: МАГАЗИН  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_arrow')} **Покупка товаров и инвентарь**\n\n{replace_emojis('white_dot')} **Доступные команды:**\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/shop` — Магазин кастомных ролей и косметических предметов\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/inventory` — Просмотр вашего инвентаря купленных предметов\n\n{replace_emojis('white_dot')} **Редкости предметов:**\n{replace_emojis('a_dot_smaller')} Basic {replace_emojis('white_arrow')} `700` {replace_emojis('money')}\n{replace_emojis('a_dot_smaller')} Premium {replace_emojis('white_arrow')} `1,750` {replace_emojis('money')}\n{replace_emojis('a_dot_smaller')} Elite {replace_emojis('white_arrow')} `3,500` {replace_emojis('money')}\n{replace_emojis('a_dot_smaller')} Special {replace_emojis('white_arrow')} `5,950` {replace_emojis('money')}\n\n{replace_emojis('a_dot_smaller')}  Используйте выпадающее меню ниже для перехода в другие разделы ",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            view = GuideView(current="shop")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "games":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  РАЗДЕЛ: МИНИ-ИГРЫ  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_arrow')} **Развлечения и быстрые игры**\n\n{replace_emojis('white_dot')} **Доступные команды:**\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/games` — Единое интерактивное меню всех доступных игр\n\n{replace_emojis('white_dot')} **Категории игр в меню:**\n{replace_emojis('a_dot_smaller')} 🎰 Игры на удачу и слот-машины\n{replace_emojis('a_dot_smaller')} 🧠 Викторины, головоломки и виселица\n{replace_emojis('a_dot_smaller')} 🎲 Дуэли, камень-ножницы-бумага и казино\n\n{replace_emojis('a_dot_smaller')}  Используйте выпадающее меню ниже для перехода в другие разделы ",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            view = GuideView(current="games")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "ranks":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  СИСТЕМА РАНГОВ  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_dot')} **⭐ СИСТЕМА РАНГОВ ПО УРОВНЯМ:**\n{replace_emojis('a_dot_smaller')} {radiant} **Radiant:** `100+ lvl`\n{replace_emojis('a_dot_smaller')} {immortal} **Immortal:** `80+ lvl` {replace_emojis('white_arrow')} I `(93+)` | II `(86+)` | III `(80+)`\n{replace_emojis('a_dot_smaller')} {ascendant} **Ascendant:** `60+ lvl` {replace_emojis('white_arrow')} I `(73+)` | II `(66+)` | III `(60+)`\n{replace_emojis('a_dot_smaller')} {diamond} **Diamond:** `42+ lvl` {replace_emojis('white_arrow')} I `(54+)` | II `(48+)` | III `(42+)`\n{replace_emojis('a_dot_smaller')} {platinum} **Platinum:** `27+ lvl` {replace_emojis('white_arrow')} I `(37+)` | II `(32+)` | III `(27+)`\n{replace_emojis('a_dot_smaller')} {gold} **Gold:** `15+ lvl` {replace_emojis('white_arrow')} I `(23+)` | II `(19+)` | III `(15+)`\n{replace_emojis('a_dot_smaller')} {silver} **Silver:** `6+ lvl` {replace_emojis('white_arrow')} I `(12+)` | II `(9+)` | III `(6+)`\n{replace_emojis('a_dot_smaller')} {bronze} **Bronze:** `0+ lvl` {replace_emojis('white_arrow')} I `(4+)` | II `(2+)` | III `(0+)`",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            view = GuideView(current="ranks")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "organizers":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  РАЗДЕЛ: ОРГАНИЗАТОРАМ  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_arrow')} **Инструменты проведения турниров (только для оргов)**\n\n{replace_emojis('white_dot')} **Команды управления:**\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/tournament create` — Создать новый турнир `(8 / 16 / 32 слота)` | `formation: elo/random`\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/limit` — Настройка лимитов кругов `(circle 2/3/4)` | `status: on/off`\n\n{replace_emojis('a_dot_smaller')}  Используйте выпадающее меню ниже для перехода в другие разделы ",
                color=discord.Color.from_rgb(100, 38, 56)
            )
            view = GuideView(current="organizers")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "rules":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} СВОД ПРАВИЛ И РЕГЛАМЕНТ R1Z3",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} 1. Базовые правила сервера",
                value="• **Уважение и адекватность:** Запрещены оскорбления, провокации, разжигание межнациональной или иной розни, токсичное поведение в текстовых и голосовых каналах.\n> 🛑 **Наказание:** **Мут от 2 до 24 часов** *(при повторе — бан)*.\n\n• **Реклама и спам:** Запрещена реклама сторонних Discord-серверов, сторонних ресурсов, реферальных ссылок и спам/сообщений не по теме каналов.\n> 🛑 **Наказание:** **Мут на 12 часов** или **Пермабан** *(за сторонние ссылки/серверы)*.\n\n• **Медиа-контент:** Запрещена публикация контента 18+ (NSFW), шок-контента, вредоносных ссылок и файлов.\n> 🛑 **Наказание:** **Мут на 24 часа** или **Пермабан** *(за вредоносные ссылки)*.",
                inline=False
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} 2. Регламент турниров",
                value="Запись на турнир является обязательством участвовать. За нарушения предусмотрена система автоматических и административных мутов:\n\n• **Неявка на матч:** Зарегистрировались на турнир, но не зашли в комнату/игровое лобби до старта.\n> 🛑 **Наказание:** **Мут на 12 часов**.\n\n• **Уход с матча:** Вышли из комнаты/игры без уважительной причины до официального завершения.\n> 🛑 **Наказание:** **Мут на 12 часов**.\n\n• **Игнорирование тимейтов:** Не зашли в голосовой канал команды в Discord по просьбе сокомандников.\n> 🛑 **Наказание:** **Мут на 1 час**.\n\n• **Нечестная игра:** Использование читов, стороннего ПО, багоюз или оскорбление организаторов (`org`).\n> 🛑 **Наказание:** **Дисквалификация и Пермабан**.",
                inline=False
            )
            view = GuideView(current="rules")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class GuideView(discord.ui.View):
    """View для гайда с выпадающим меню."""

    def __init__(self, current=None):
        super().__init__(timeout=None)
        self.add_item(GuideSelectMenu(current=current))


class HelpGuideSelectMenu(discord.ui.Select):
    """Выпадающее меню для справки (без главного экрана)."""

    def __init__(self, current=None):
        self.current = current
        options = [
            discord.SelectOption(
                label="Турниры",
                description="Сетка, топ ELO и статистика турниров",
                value="tournaments"
            ),
            discord.SelectOption(
                label="Профиль",
                description="Карточка игрока и текущий ранг",
                value="profile"
            ),
            discord.SelectOption(
                label="Экономика",
                description="Баланс, переводы, продажи и ставки",
                value="economy"
            ),
            discord.SelectOption(
                label="Магазин",
                description="Покупка ролей и инвентарь",
                value="shop"
            ),
            discord.SelectOption(
                label="Мини-игры",
                description="Игровые треды и игры",
                value="games"
            ),
            discord.SelectOption(
                label="Система рангов",
                description="Информация о рангах и уровнях",
                value="ranks"
            ),
            discord.SelectOption(
                label="Организаторам",
                description="Создание турниров и управление кругами",
                value="organizers"
            ),
            discord.SelectOption(
                label="Правила сервера",
                description="Свод правил и регламент турниров",
                value="rules"
            )
        ]

        # Set default option
        for opt in options:
            if opt.value == current:
                opt.default = True
                break

        super().__init__(
            placeholder="Выберите категорию...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        """Обработка выбора пункта меню."""
        from config import RANK_EMOJIS

        # Standard emojis as fallback
        standard_emojis = {
            "Radiant": "👑",
            "Immortal": "🔱",
            "Ascendant": "🎯",
            "Diamond": "💎",
            "Platinum": "🌪️",
            "Gold": "🥇",
            "Silver": "🥈",
            "Bronze": "🥉",
        }

        def format_emoji(name, value):
            if value and value.isdigit():
                return f"<:{name}:{value}>"
            elif value and value.startswith("<:") and value.endswith(">"):
                return value  # Already formatted
            return value  # Use as-is (standard emoji)

        radiant = format_emoji('Radiant', RANK_EMOJIS.get("Radiant", "") or standard_emojis["Radiant"])
        immortal = format_emoji('Immortal', RANK_EMOJIS.get("Immortal", "") or standard_emojis["Immortal"])
        ascendant = format_emoji('Ascendant', RANK_EMOJIS.get("Ascendant", "") or standard_emojis["Ascendant"])
        diamond = format_emoji('Diamond', RANK_EMOJIS.get("Diamond", "") or standard_emojis["Diamond"])
        platinum = format_emoji('Platinum', RANK_EMOJIS.get("Platinum", "") or standard_emojis["Platinum"])
        gold = format_emoji('Gold', RANK_EMOJIS.get("Gold", "") or standard_emojis["Gold"])
        silver = format_emoji('Silver', RANK_EMOJIS.get("Silver", "") or standard_emojis["Silver"])
        bronze = format_emoji('Bronze', RANK_EMOJIS.get("Bronze", "") or standard_emojis["Bronze"])

        if self.values[0] == "tournaments":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  РАЗДЕЛ: ТУРНИРЫ  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_arrow')} **Информация и статистика турнирной системы**\n\n{replace_emojis('white_dot')} **Доступные команды:**\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/top` — Таблица лучших игроков `(level / money / elo)`\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/booyah` — Зал славы, рекорды и история прошлых турниров\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/help` — Справка по всем командам бота\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/welcome` — Проводник по серверу (только для владельца)\n\n{replace_emojis('a_dot_smaller')}  Используйте выпадающее меню ниже для перехода в другие разделы ",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            view = HelpGuideView(current="tournaments")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "profile":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  РАЗДЕЛ: ПРОФИЛЬ  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_arrow')} **Управление персональным аккаунтом**\n\n{replace_emojis('white_dot')} **Доступные команды:**\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/profile` — Карточка игрока, общая статистика и достижения\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/rank` — Ваша карточка ранга и прогресс до следующего уровня\n\n{replace_emojis('a_dot_smaller')}  Используйте выпадающее меню ниже для перехода в другие разделы ",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            view = HelpGuideView(current="profile")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "economy":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  РАЗДЕЛ: ЭКОНОМИКА  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_arrow')} **Управление финансами, подарками и ставками**\n\n{replace_emojis('white_dot')} **Доступные команды:**\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/balance` — Проверить свой текущий баланс монет\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/daily` — Получить бонус каждые 12 часов `(серия до 10 дней)`\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/pay` — Перевести монеты другому пользователю\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/gift` — Подарить предмет из инвентаря другому игроку\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/bet` — Личная статистика и история активных ставок\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/sell` — Продать предмет из инвентаря за 50% стоимости\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/rob @пользователь` — Одиночное ограбление (50% шанс, кулдаун 3ч)\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/robgroup @пользователь` — Групповое ограбление (2-6 игроков, шанс 50-80%)\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/bank status` — Статус банковского сейфа\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/bank deposit [сумма/all]` — Пополнить сейф (комиссия 5%)\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/bank withdraw [сумма/all]` — Снять со сейфа (без комиссии)\n\n{replace_emojis('white_dot')} **Защита средств:**\n{replace_emojis('a_dot_smaller')} Наличные уязвимы для /rob и /robgroup\n{replace_emojis('a_dot_smaller')} Деньги в банковском сейфе 100% защищены\n\n{replace_emojis('a_dot_smaller')}  Используйте выпадающее меню ниже для перехода в другие разделы ",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            view = HelpGuideView(current="economy")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "shop":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  РАЗДЕЛ: МАГАЗИН  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_arrow')} **Покупка товаров и инвентарь**\n\n{replace_emojis('white_dot')} **Доступные команды:**\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/shop` — Магазин кастомных ролей и косметических предметов\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/inventory` — Просмотр вашего инвентаря купленных предметов\n\n{replace_emojis('white_dot')} **Редкости предметов:**\n{replace_emojis('a_dot_smaller')} Basic {replace_emojis('white_arrow')} `700` {replace_emojis('money')}\n{replace_emojis('a_dot_smaller')} Premium {replace_emojis('white_arrow')} `1,750` {replace_emojis('money')}\n{replace_emojis('a_dot_smaller')} Elite {replace_emojis('white_arrow')} `3,500` {replace_emojis('money')}\n{replace_emojis('a_dot_smaller')} Special {replace_emojis('white_arrow')} `5,950` {replace_emojis('money')}\n\n{replace_emojis('a_dot_smaller')}  Используйте выпадающее меню ниже для перехода в другие разделы ",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            view = HelpGuideView(current="shop")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "games":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  РАЗДЕЛ: МИНИ-ИГРЫ  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_arrow')} **Развлечения и быстрые игры**\n\n{replace_emojis('white_dot')} **Доступные команды:**\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/thread` — Создать игровой тред\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/delete` — Удалить текущий игровой тред (только для оргов)\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/games` — Единое интерактивное меню всех доступных игр\n\n{replace_emojis('white_dot')} **Игры в тредах:**\n{replace_emojis('a_dot_smaller')} 🎲 `/coin_flip` — Орёл или решка (PvE)\n{replace_emojis('a_dot_smaller')} ⚔️ `/rps` — Камень-ножницы-бумага (PvE)\n{replace_emojis('a_dot_smaller')} 🧮 `/mathquiz` — Математическая дуэль (2-6 игроков)\n\n{replace_emojis('white_dot')} **Важно:** Игры запускаются только в игровых тредах! Сначала создайте тред через `/thread`.\n\n{replace_emojis('a_dot_smaller')}  Используйте выпадающее меню ниже для перехода в другие разделы ",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            view = HelpGuideView(current="games")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "ranks":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  СИСТЕМА РАНГОВ  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_dot')} **⭐ СИСТЕМА РАНГОВ ПО УРОВНЯМ:**\n{replace_emojis('a_dot_smaller')} {radiant} **Radiant:** `100+ lvl`\n{replace_emojis('a_dot_smaller')} {immortal} **Immortal:** `80+ lvl` {replace_emojis('white_arrow')} I `(93+)` | II `(86+)` | III `(80+)`\n{replace_emojis('a_dot_smaller')} {ascendant} **Ascendant:** `60+ lvl` {replace_emojis('white_arrow')} I `(73+)` | II `(66+)` | III `(60+)`\n{replace_emojis('a_dot_smaller')} {diamond} **Diamond:** `42+ lvl` {replace_emojis('white_arrow')} I `(54+)` | II `(48+)` | III `(42+)`\n{replace_emojis('a_dot_smaller')} {platinum} **Platinum:** `27+ lvl` {replace_emojis('white_arrow')} I `(37+)` | II `(32+)` | III `(27+)`\n{replace_emojis('a_dot_smaller')} {gold} **Gold:** `15+ lvl` {replace_emojis('white_arrow')} I `(23+)` | II `(19+)` | III `(15+)`\n{replace_emojis('a_dot_smaller')} {silver} **Silver:** `6+ lvl` {replace_emojis('white_arrow')} I `(12+)` | II `(9+)` | III `(6+)`\n{replace_emojis('a_dot_smaller')} {bronze} **Bronze:** `0+ lvl` {replace_emojis('white_arrow')} I `(4+)` | II `(2+)` | III `(0+)`",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            view = HelpGuideView(current="ranks")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "organizers":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')}  РАЗДЕЛ: ОРГАНИЗАТОРАМ  {replace_emojis('a_star')}",
                description=f"{replace_emojis('white_arrow')} **Инструменты проведения турниров (только для оргов)**\n\n{replace_emojis('white_dot')} **Команды управления:**\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/tournament create` — Создать новый турнир `(8 / 16 / 32 слота)` | `formation: elo/random`\n{replace_emojis('a_dot_smaller')} {replace_emojis('white_arrow')} `/limit` — Настройка лимитов кругов `(circle 2/3/4)` | `status: on/off`\n\n{replace_emojis('a_dot_smaller')}  Используйте выпадающее меню ниже для перехода в другие разделы ",
                color=discord.Color.from_rgb(100, 38, 56)
            )
            view = HelpGuideView(current="organizers")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif self.values[0] == "rules":
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} СВОД ПРАВИЛ И РЕГЛАМЕНТ R1Z3",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} 1. Базовые правила сервера",
                value="• **Уважение и адекватность:** Запрещены оскорбления, провокации, разжигание межнациональной или иной розни, токсичное поведение в текстовых и голосовых каналах.\n> 🛑 **Наказание:** **Мут от 2 до 24 часов** *(при повторе — бан)*.\n\n• **Реклама и спам:** Запрещена реклама сторонних Discord-серверов, сторонних ресурсов, реферальных ссылок и спам/сообщений не по теме каналов.\n> 🛑 **Наказание:** **Мут на 12 часов** или **Пермабан** *(за сторонние ссылки/серверы)*.\n\n• **Медиа-контент:** Запрещена публикация контента 18+ (NSFW), шок-контента, вредоносных ссылок и файлов.\n> 🛑 **Наказание:** **Мут на 24 часа** или **Пермабан** *(за вредоносные ссылки)*.",
                inline=False
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} 2. Регламент турниров",
                value="Запись на турнир является обязательством участвовать. За нарушения предусмотрена система автоматических и административных мутов:\n\n• **Неявка на матч:** Зарегистрировались на турнир, но не зашли в комнату/игровое лобби до старта.\n> 🛑 **Наказание:** **Мут на 12 часов**.\n\n• **Уход с матча:** Вышли из комнаты/игры без уважительной причины до официального завершения.\n> 🛑 **Наказание:** **Мут на 12 часов**.\n\n• **Игнорирование тимейтов:** Не зашли в голосовой канал команды в Discord по просьбе сокомандников.\n> 🛑 **Наказание:** **Мут на 1 час**.\n\n• **Нечестная игра:** Использование читов, стороннего ПО, багоюз или оскорбление организаторов (`org`).\n> 🛑 **Наказание:** **Дисквалификация и Пермабан**.",
                inline=False
            )
            view = HelpGuideView(current="rules")
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class HelpGuideView(discord.ui.View):
    """View для справки с выпадающим меню (без главного экрана)."""

    def __init__(self, current=None):
        super().__init__(timeout=None)
        self.add_item(HelpGuideSelectMenu(current=current))


async def setup(bot: TournamentBot) -> None:
    """Загрузить ког."""
    await bot.add_cog(TournamentCog(bot))












