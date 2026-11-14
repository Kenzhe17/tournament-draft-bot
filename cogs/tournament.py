"""Slash-команды турнира."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, Optional

import discord
from utils.design import compose, text_card, smart_view

from discord import app_commands
from discord.ext import commands

from config import TOURNAMENT_CHANNELS, BOT_OWNER_ID, replace_emojis, get_emoji, WELCOME_BANNER_URL, NOTIFICATION_ROLE_ID, NOTIFICATION_CHANNEL_ID
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
from utils.permissions import is_admin, is_org, is_bot_owner, is_channel_organizer_decorator
from config import BOT_OWNER_ID, replace_emojis, get_emoji, WELCOME_BANNER_URL
from views.org_role_view import setup_org_role_message
from cogs.groups import (
    player_group,
    player_set_group,
    economy_group,
    admin_group,
    server_group,
    games_group,
    bind_commands,
)

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

# Tournaments 2.0: /tournament create разрешает только эти режимы.
# Legacy-значения FormationMode (elo/skill/random/random-auto)
# остаются валидными для уже созданных турниров и /mmsquad.
ALLOWED_CREATE_FORMATIONS = (
    FormationMode.DRAFT, FormationMode.BALANCED_SR, FormationMode.RANDOM,
)


def parse_create_formation(raw: str) -> FormationMode | None:
    """Режим формирования для /tournament create или None.

    Возвращает FormationMode только для draft/balanced-sr/random;
    неизвестные и legacy-значения → None (серверная валидация
    дублирует Literal-выбор в UI).
    """
    try:
        mode = FormationMode(raw)
    except (ValueError, TypeError):
        return None
    return mode if mode in ALLOWED_CREATE_FORMATIONS else None


# Список «опасных» прав для проверки 2FA
DANGEROUS_PERMS = {
    "administrator": "Администратор",
    "manage_guild": "Управление сервером",
    "manage_roles": "Управление ролями",
    "manage_channels": "Управление каналом",
    "kick_members": "Выгнать участников",
    "ban_members": "Забанить участников",
    "manage_messages": "Управление сообщениями",
    "manage_threads": "Управление тредами",
    "manage_webhooks": "Управление вебхуками",
    "manage_emojis_and_stickers": "Управление эмодзи и стикерами",
    "moderate_members": "Таймаут участников",
    "create_events": "Создание событий",
    "manage_events": "Управление событиями",
}


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
    @app_commands.describe(size="Размер турнира: 8, 16 или 32 игрока", formation="Режим: draft (драфт капитанами), balanced-sr (балансировка по SR) или random (случайно)")
    @is_org()
    async def tournament_create(
        self,
        interaction: discord.Interaction,
        size: str,
        formation: Literal["draft", "balanced-sr", "random"] = "draft",
    ) -> None:
        """Создать турнир с указанным размером."""
        # Проверить если канал заблокирован поиском (только если есть активные игроки)
        from cogs.mm import LOCKED_CHANNELS
        if interaction.channel_id in LOCKED_CHANNELS:
            # Проверить если поиск действительно активен (есть игроки)
            from storage.mm_search_store import mm_search_store
            search_channel_id = LOCKED_CHANNELS[interaction.channel_id]
            search = mm_search_store.get(interaction.guild_id, search_channel_id)
            if search and search.players:
                await interaction.response.send_message(
                    "❌ Этот канал заблокирован для поиска MM. Используйте другой канал или дождитесь завершения поиска.",
                    ephemeral=True
                )
                return
            else:
                # Поиск пустой, разблокировать канал
                del LOCKED_CHANNELS[interaction.channel_id]

        existing = store.get(interaction.guild_id, interaction.channel_id)
        if existing:
            # Проверяем фазу существующего турнира
            if existing.phase != TournamentPhase.COMPLETE:
                await interaction.response.send_message(
                    replace_emojis("❌ На этом канале уже есть активный турнир. Удалите его с помощью `/tournament delete`, чтобы создать новый."),
                    ephemeral=True
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return
            # Если турнир завершен, можно удалить и создать новый
            if existing.message_id:
                try:
                    channel = interaction.guild.get_channel(existing.channel_id)
                    if channel:
                        message = await channel.fetch_message(existing.message_id)
                        await message.edit(view=smart_view(message, None))
                except Exception:
                    pass
            store.delete(interaction.guild_id, interaction.channel_id)

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

        # Validate formation mode — r!z3 CLASSIC: только draft,
        # balanced-sr и random. Legacy-значения FormationMode
        # сохраняются для старых турниров и /mmsquad, но создание
        # через /tournament create ими запрещено (проверка и на
        # сервере, не только в UI).
        formation_mode = parse_create_formation(formation)
        if formation_mode is None:
            await interaction.response.send_message(
                replace_emojis("❌ Неверный режим формирования. Доступны: draft, balanced-sr, random."),
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

        # Диагностика кнопок: что реально уходит в сообщение
        _composed = None
        _debug = "n/a"
        try:
            from utils.design.adapter import _layout_debug, _view_rows
            _rows = _view_rows(view) if view is not None else []
            _composed = compose(embed, view)
            _child_types = [
                f"{type(c).__name__}({len(getattr(c, 'children', []) or [])})"
                for c in (view.children if view is not None else [])
            ]
            _debug = (
                f"view={type(view).__name__ if view else None} "
                f"children={_child_types} "
                f"rows={len(_rows)} | {_layout_debug(_composed)}"
            )
            logger.info("TOURNAMENT-CREATE %s", _debug)
        except Exception:
            logger.exception("TOURNAMENT-CREATE diagnostics failed")

        # Send ephemeral confirmation first
        await interaction.response.send_message(
            replace_emojis(f"✅ Турнир создан\n-# {_debug}"),
            ephemeral=True
        )

        # Send main tournament message through channel.send to unbind it.
        # allowed_mentions=none: embed содержит <@id> капитанов/игроков
        # для кликабельности, но уведомления не рассылаются.
        message = await interaction.channel.send(
            view=_composed if _composed is not None else compose(embed, view),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        tournament.message_id = message.id
        store.set(tournament)

        # Send notification to notification channel
        await send_tournament_notification(tournament, interaction.guild, "Создан новый турнир")

        # Add organizer to channel organizers
        from storage.organizer_store import ChannelOrganizers, organizer_store
        channel_orgs = organizer_store.get(interaction.guild_id, interaction.channel_id)
        if not channel_orgs:
            channel_orgs = ChannelOrganizers()
        channel_orgs.organizer_id = interaction.user.id
        channel_orgs.tournament_id = tournament.id
        organizer_store.set(interaction.guild_id, interaction.channel_id, channel_orgs)

        # Update organizer setup embed if it exists
        from storage.organizer_store import organizer_store as org_store
        for channel_id in TOURNAMENT_CHANNELS:
            orgs = org_store.get(interaction.guild_id, channel_id)
            if orgs and orgs.message_id:
                try:
                    update_channel = interaction.guild.get_channel(channel_id)
                    if update_channel:
                        update_message = await update_channel.fetch_message(orgs.message_id)
                        from views.organizer_view import build_multi_channel_embed, OrganizerManagementButton
                        from discord.ui import View
                        new_embed = build_multi_channel_embed(interaction.guild)
                        new_view = View()
                        new_view.add_item(OrganizerManagementButton(interaction.guild_id, channel_id, interaction.guild))
                        await update_message.edit( view=compose(new_embed, new_view))
                except Exception:
                    pass

        logger.info("Турнир создан на сервере %s с размером %s и режимом %s", interaction.guild_id, size, formation)

    @tournament_group.command(name="delete", description="Удалить активный турнир")
    @is_channel_organizer_decorator()
    async def tournament_delete(self, interaction: discord.Interaction) -> None:
        """Удалить текущий турнир."""
        existing = store.get(interaction.guild_id, interaction.channel_id)
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

        store.delete(interaction.guild_id, interaction.channel_id)
        logger.info("Турнир удален на сервере %s", interaction.guild_id)

        # Разблокировать канал если он был заблокирован поиском
        if hasattr(self.bot, 'mm_cog'):
            from cogs.mm import LOCKED_CHANNELS
            if interaction.channel_id in LOCKED_CHANNELS:
                del LOCKED_CHANNELS[interaction.channel_id]
                logger.info(f"Channel {interaction.channel_id} unlocked after tournament deletion")

        # Сбросить лимит поиска если освободился турнир
        if hasattr(self.bot, 'mm_cog'):
            await self.bot.mm_cog.reset_search_limit(interaction.guild_id)

        # Update organizer setup embed if it exists
        from storage.organizer_store import organizer_store as org_store
        for channel_id in TOURNAMENT_CHANNELS:
            orgs = org_store.get(interaction.guild_id, channel_id)
            if orgs and orgs.message_id:
                try:
                    update_channel = interaction.guild.get_channel(channel_id)
                    if update_channel:
                        update_message = await update_channel.fetch_message(orgs.message_id)
                        from views.organizer_view import build_multi_channel_embed, OrganizerManagementButton
                        from discord.ui import View
                        new_embed = build_multi_channel_embed(interaction.guild)
                        new_view = View()
                        new_view.add_item(OrganizerManagementButton(interaction.guild_id, channel_id, interaction.guild))
                        await update_message.edit( view=compose(new_embed, new_view))
                except Exception:
                    pass

        await interaction.response.send_message(
            "🗑️ **Турнир успешно удален.**",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

    @tournament_group.command(name="deleteall", description="Удалить все активные турниры (только владелец бота)")
    async def tournament_deleteall(self, interaction: discord.Interaction):
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("❌ Только владелец бота.", ephemeral=True)
            return

        tournaments = store.get_by_guild(interaction.guild_id)
        if not tournaments:
            await interaction.response.send_message("✅ Активных турниров нет.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        deleted = 0
        for t in tournaments:
            try:
                if t.message_id:
                    msg = await self.bot.fetch_message(t.message_id)
                    await msg.delete()
            except Exception:
                pass
            try:
                store.delete(t.guild_id, t.channel_id)
                deleted += 1
            except Exception as e:
                logger.error(f"deleteall: store.delete failed for {t.guild_id}:{t.channel_id}: {e}")

        if getattr(self.bot, "mm_cog", None):
            try:
                await self.bot.mm_cog.reset_search_limit(interaction.guild_id)
            except Exception:
                pass

        await interaction.followup.send(f"🗑️ Удалено турниров: **{deleted}**", ephemeral=True)

    @tournament_group.command(name="test", description="Тестовые операции турниров")
    @is_bot_owner()
    @app_commands.describe(
        category="Тип турнира",
        operation="Тестовая операция",
        keep_players="(normal/fill) Сохранить реальных игроков",
        no_bots="(normal/fill) Только реальные игроки, без ботов",
        формат="(ultimatum/fill) Формат Ультиматума",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="Обычный", value="normal"),
            app_commands.Choice(name="Battle Royale", value="br"),
            app_commands.Choice(name="Ультиматум", value="ultimatum"),
        ],
        operation=[
            app_commands.Choice(name="Заполнить ботами", value="fill"),
            app_commands.Choice(name="Заполнить ботами-командами", value="fill_teams"),
            app_commands.Choice(name="Заполнить статистику", value="fill_stats"),
            app_commands.Choice(name="Запуск только с реальными", value="start_real"),
        ],
        формат=[
            app_commands.Choice(name="Ультиматум Соло", value="solo"),
            app_commands.Choice(name="Ультиматум Дуо", value="duo"),
        ],
    )
    async def tournament_test(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str],
        operation: app_commands.Choice[str],
        keep_players: bool = False,
        no_bots: bool = False,
        формат: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        """Единая команда тестовых операций: /tournament test."""
        cat, op = category.value, operation.value

        # Валидация допустимых комбинаций
        valid = {
            ("normal", "fill"),
            ("normal", "fill_stats"),
            ("normal", "start_real"),
            ("br", "fill"),
            ("br", "fill_teams"),
            ("br", "fill_stats"),
            ("ultimatum", "fill"),
        }
        if (cat, op) not in valid:
            await interaction.response.send_message(
                f"❌ Операция `{op}` недоступна для `{cat}`.",
                ephemeral=True,
            )
            return

        if cat == "normal":
            if op == "fill":
                await self.test_start(interaction, keep_players, no_bots)
            elif op == "fill_stats":
                await self.trstattest(interaction)
            else:
                await self.start_real_only(interaction)
            return

        # br / ultimatum — делегируем в соответствующие cog'и
        if cat == "br":
            cog = self.bot.get_cog("BRCog")
            group = __import__("cogs.br", fromlist=["br_group"]).br_group
            names = {"fill": "test_fill", "fill_teams": "test_teams",
                     "fill_stats": "test_stats"}
        else:
            cog = self.bot.get_cog("UltimatumCog")
            group = __import__("cogs.ultimatum", fromlist=["ultimatum_group"]).ultimatum_group
            names = {"fill": "test_fill"}

        cmd = group.get_command(names[op])
        if cog is None or cmd is None:
            await interaction.response.send_message(
                "❌ Целевой ког не загружен.", ephemeral=True
            )
            return
        if cat == "ultimatum":
            await cmd.callback(cog, interaction, формат)
        else:
            await cmd.callback(cog, interaction)

    @is_bot_owner()
    async def test_start(self, interaction: discord.Interaction, keep_players: bool = False, no_bots: bool = False) -> None:
        """Заполнить турнир тестовыми данными и запустить драфт."""
        tournament = store.get(interaction.guild_id, interaction.channel_id)
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

        # If no_bots=True, just start with current players (no filling)
        if no_bots:
            await interaction.response.send_message(
                f"🧪 Турнир запущен только с реальными игроками ({len(tournament.all_players)} чел)! Нажмите Старт для запуска.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            await self.bot.update_tournament_message(interaction.guild, tournament)
            return

        # Generate unique negative user_ids within int64 range
        # Use guild_id and channel_id to create unique base, but keep it small
        base_user_id = -((interaction.guild_id % 10000) * 10000 + (interaction.channel_id % 100) * 10)

        if not keep_players:
            # Clear all existing data and player_user_ids
            tournament.circle1 = []
            tournament.circle2 = []
            tournament.circle3 = []
            tournament.circle4 = []
            tournament.players_pool = []
            tournament.captains = []
            tournament.player_user_ids = {}

        if tournament.formation_mode in (FormationMode.RANDOM, FormationMode.BALANCED_SR):
            # RANDOM/BALANCED_SR modes: fill players_pool
            if keep_players:
                # Keep existing real players, add bots to fill remaining slots
                current_players = len(tournament.players_pool)
                needed = required_players - current_players
                for i in range(needed):
                    player_name = f"Player{current_players + i + 1}"
                    tournament.players_pool.append(player_name)
                    tournament.player_user_ids[player_name] = base_user_id - i
            else:
                # Fill with only bots
                tournament.players_pool = [f"Player{i+1}" for i in range(required_players)]
                for i, player_name in enumerate(tournament.players_pool):
                    tournament.player_user_ids[player_name] = base_user_id - i
        else:
            # ELO/SKILL modes: fill circles
            if keep_players:
                # Keep existing real players, add bots to fill remaining slots
                # Calculate needed players for each circle
                needed_captains = captain_count - len(tournament.circle1)
                needed_circle2 = captain_count - len(tournament.circle2)
                needed_circle3 = captain_count - len(tournament.circle3)
                needed_circle4 = required_players - (captain_count * 3) - len(tournament.circle4)

                # Add bots to fill circle1 (captains)
                for i in range(needed_captains):
                    player_name = f"Cap{len(tournament.circle1) + i + 1}"
                    tournament.circle1.append(player_name)
                    tournament.player_user_ids[player_name] = base_user_id - i

                # Add bots to fill circle2
                for i in range(needed_circle2):
                    player_name = f"P2-{len(tournament.circle2) + i}"
                    tournament.circle2.append(player_name)
                    tournament.player_user_ids[player_name] = base_user_id - 100 - i

                # Add bots to fill circle3
                for i in range(needed_circle3):
                    player_name = f"P3-{len(tournament.circle3) + i}"
                    tournament.circle3.append(player_name)
                    tournament.player_user_ids[player_name] = base_user_id - 200 - i

                # Add bots to fill circle4
                for i in range(needed_circle4):
                    player_name = f"P4-{len(tournament.circle4) + i}"
                    tournament.circle4.append(player_name)
                    tournament.player_user_ids[player_name] = base_user_id - 300 - i

                # Update captains list
                tournament.captains = list(tournament.circle1)
            else:
                # Fill with only bots
                tournament.captains = [f"Cap{i+1}" for i in range(captain_count)]
                tournament.circle1.extend(tournament.captains)
                tournament.circle2.extend([f"P2-{i}" for i in range(captain_count)])
                tournament.circle3.extend([f"P3-{i}" for i in range(captain_count)])
                # Add extra players to circle4 to reach required total
                remaining_players = required_players - (captain_count * 3)
                tournament.circle4.extend([f"P4-{i}" for i in range(remaining_players)])
                # Add unique user_ids for all players
                for i, player_name in enumerate(tournament.captains):
                    tournament.player_user_ids[player_name] = base_user_id - i
                for i, player_name in enumerate(tournament.circle2):
                    tournament.player_user_ids[player_name] = base_user_id - 100 - i
                for i, player_name in enumerate(tournament.circle3):
                    tournament.player_user_ids[player_name] = base_user_id - 200 - i
                for i, player_name in enumerate(tournament.circle4):
                    tournament.player_user_ids[player_name] = base_user_id - 300 - i

        store.set(tournament)

        message = f"🧪 Турнир заполнен {required_players} тестовыми игроками! Нажмите Старт для запуска."
        if keep_players:
            message = f"🧪 Турнир заполнен ботовыми игроками ({required_players} всего, реальные сохранены)! Нажмите Старт для запуска."

        await interaction.response.send_message(message, ephemeral=True)
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    @is_bot_owner()
    async def start_real_only(self, interaction: discord.Interaction) -> None:
        """Запустить турнир только с реальными игроками."""
        tournament = store.get(interaction.guild_id, interaction.channel_id)
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

        # Check if tournament has enough players
        current_players = len(tournament.all_players)
        required_players = int(tournament.size.value)

        if current_players < required_players:
            await interaction.response.send_message(
                f"❌ Недостаточно игроков. Нужно {required_players}, есть {current_players}. Используйте /test keep_players:true для добавления ботов.",
                ephemeral=True,
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Mark as test tournament but don't add bots
        tournament.is_test = True

        await interaction.response.send_message(
            f"🧪 Турнир готов к запуску с {len(tournament.all_players)} реальными игроками! Нажмите Старт для запуска.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

        await self.bot.update_tournament_message(interaction.guild, tournament)

    @app_commands.command(name="top", description="Показать таблицу лидеров")
    async def top(self, interaction: discord.Interaction) -> None:
        """Показать таблицу лидеров сервера."""
        from utils.embeds import build_leaderboard_embed
        from views.leaderboard_view import UniversalLeaderboardView

        view = UniversalLeaderboardView(interaction.guild_id, interaction.user.id, "elo")
        await view.initialize()

        embed = await build_leaderboard_embed(interaction.guild_id, page=1, leaderboard_type="elo", user_id=interaction.user.id)

        try:
            await interaction.response.send_message( view=compose(embed, view))
        except discord.NotFound:
            # Interaction expired, use followup
            await interaction.followup.send( view=compose(embed, view))

    @app_commands.command(name="help", description="Показать справку по командам")
    async def help_command(self, interaction: discord.Interaction) -> None:
        """Показать интерактивную справку."""
        embed = discord.Embed(
            title=f"  СПРАВКА ПО КОМАНДАМ  ",
            description=f" **Добро пожаловать в справку!**\nЗдесь вы найдёте информацию о всех командах бота.\n\n **Выберите категорию в меню ниже:**\n{replace_emojis('a_dot_smaller')} Турниры — Топ, Battle Royale, Ультиматум и Ranked Matchmaking\n{replace_emojis('a_dot_smaller')} Профиль — Карточка игрока, ранг и история матчей\n{replace_emojis('a_dot_smaller')} Социальное — Репутация, брак и дни рождения\n{replace_emojis('a_dot_smaller')} Экономика — Баланс, сейф, переводы и криминал\n{replace_emojis('a_dot_smaller')} Магазин — Покупка предметов и инвентарь\n{replace_emojis('a_dot_smaller')} Питомцы — RPG-питомцы сами\n{replace_emojis('a_dot_smaller')} Мини-игры — Казино-игры на монеты\n{replace_emojis('a_dot_smaller')} Система рангов — Ранги, уровни и ELO\n{replace_emojis('a_dot_smaller')} Организаторам — Турниры, инфо-центр и баннер\n{replace_emojis('a_dot_smaller')} Правила сервера — Свод правил и регламент",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        view = HelpGuideView(current=None)
        await interaction.response.send_message( view=compose(embed, view), ephemeral=False)

    @economy_group.command(name="balance", description="Показать ваш баланс")
    async def balance(self, interaction: discord.Interaction) -> None:
        """Показать баланс пользователя."""
        from storage.user_balance_store import user_balance_store
        from storage.user_bank_store import user_bank_store
        from config import get_emoji

        cash = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        safe = await user_bank_store.get_bank_balance(interaction.guild_id, interaction.user.id)
        total = cash + safe

        embed = discord.Embed(
            title=f" **БАЛАНС ПОЛЬЗОВАТЕЛЯ | /balance**",
            description=f" **Пользователь:** {interaction.user.mention}\n\n **Наличные:** **`{cash:,}`** {get_emoji('money')}\n **В сейфе:** **`{safe:,}`** {get_emoji('money')}\n **Всего:** **`{total:,}`** {get_emoji('money')}\n\n *Используйте `/daily` для получения награды.*",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        try:
            await interaction.response.send_message(
                view=compose(embed), ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none())
        except (discord.NotFound, discord.DiscordServerError):
            pass

    @server_group.command(name="check2fa", description="Найти роли и каналы, из-за которых просит 2FA")
    @app_commands.checks.has_permissions(administrator=True)
    async def check2fa(self, interaction: discord.Interaction):
        """Проверить права ролей и каналов на предмет требований 2FA."""
        await interaction.response.defer(thinking=True)
        guild = interaction.guild
        report = []

        # 1. Проверка ролей
        role_issues = []
        for role in guild.roles:
            perms = role.permissions
            dangerous_found = [
                name
                for key, name in DANGEROUS_PERMS.items()
                if getattr(perms, key, False)
            ]
            if dangerous_found:
                role_issues.append(
                    f"• Роль **{role.name}**: {', '.join(dangerous_found)}"
                )

        if role_issues:
            report.append(
                "**Опасные права в ролях:**\n" + "\n".join(role_issues[:10])
            )

        # 2. Проверка каналов (переопределения)
        channel_issues = []
        for channel in guild.channels:
            for target, overwrite in channel.overwrites.items():
                if isinstance(target, discord.Role):
                    channel_dangerous = [
                        name
                        for key, name in DANGEROUS_PERMS.items()
                        if getattr(overwrite, key, None) is True
                    ]
                    if channel_dangerous:
                        channel_issues.append(
                            f"• Канал `{channel.name}` (роль **{target.name}**): {', '.join(channel_dangerous)}"
                        )

        if channel_issues:
            report.append(
                "\n**Опасные права в каналах:**\n"
                + "\n".join(channel_issues[:10])
            )

        if not report:
            await interaction.followup.send(
                "✅ Опасных прав у ролей и каналов не обнаружено!"
            )
        else:
            full_text = "\n".join(report)
            if len(full_text) > 1900:
                full_text = (
                    full_text[:1900] + "\n\n*(Список слишком длинный, обрезан)*"
                )
            await interaction.followup.send(
                f"🔍 **Результаты проверки:**\n\n{full_text}"
            )

    @check2fa.error
    async def check2fa_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.errors.MissingPermissions):
            await interaction.response.send_message(
                "У вас нет прав администратора для использования этой команды.",
                ephemeral=True,
            )

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
            f"Добро пожаловать в игровой магазин \n"
            f"Выберите нужный раздел в выпадающем меню ниже, чтобы посмотреть доступные товары."
        )

        # Профиль пользователя
        embed.add_field(
            name=replace_emojis("• Ваш профиль:"),
            value=f" Баланс: {balance:,} {get_emoji('money')}\n"
                  f" Ранг: {rank}\n"
                  f" Инвентарь: {inventory_count}/{max_inventory}",
            inline=False
        )

        embed.add_field(
            name=replace_emojis("a_dot_smaller Для навигации используйте компоненты ниже"),
            value="",
            inline=False
        )

        # Создать View с выпадающим меню категорий
        view = ShopMainView()

        try:
            await interaction.response.send_message( view=compose(embed, view), ephemeral=True)
        except (discord.NotFound, discord.DiscordServerError):
            pass

    @app_commands.command(name="inventory", description="Ваш инвентарь")
    async def inventory(self, interaction: discord.Interaction) -> None:
        """Показать инвентарь косметики."""
        from storage.shop_store import inventory_store, shop_store
        from views.shop_view import InventoryEquipSelect, InventoryUnequipSelect

        # Получить инвентарь
        cosmetics = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)

        if not cosmetics:
            await interaction.response.send_message(
                replace_emojis("• Ваш инвентарь пуст. Используйте `/shop` для покупки косметики."),
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
                    equipped_tags.append(f" Тег: **{item.value}** `(ID: {item.id})` • {replace_emojis(rare_emoji)}")
                elif item.cosmetic_type.value == "icon":
                    icon_emoji = icon_map.get(item.value, "")
                    equipped_icons.append(f" Значок: {replace_emojis(icon_emoji)} **{item.name}** `(ID: {item.id})` • {replace_emojis(rare_emoji)}")
            else:
                unequipped_items.append((cosmetic.item_id, item.name))
                if item.cosmetic_type.value == "tag":
                    unequipped_tags.append(f" Тег: **{item.value}** `(ID: {item.id})` • {replace_emojis(rare_emoji)}")
                elif item.cosmetic_type.value == "icon":
                    icon_emoji = icon_map.get(item.value, "")
                    unequipped_icons.append(f" Значок: {replace_emojis(icon_emoji)} **{item.name}** `(ID: {item.id})` • {replace_emojis(rare_emoji)}")

        # Build description
        description_parts = ["Управление вашей экипировкой и предметами:\n"]

        # Экипировано
        description_parts.append(f"{replace_emojis('•')} **Экипировано:**")
        if equipped_tags:
            description_parts.extend(equipped_tags)
        if equipped_icons:
            description_parts.extend(equipped_icons)
        if not equipped_tags and not equipped_icons:
            description_parts.append(f" Ничего не экипировано")
        description_parts.append("")

        # В инвентаре
        description_parts.append(f"{replace_emojis('•')} **В инвентаре:**")
        if unequipped_tags:
            description_parts.extend(unequipped_tags)
        if unequipped_icons:
            description_parts.extend(unequipped_icons)
        if not unequipped_tags and not unequipped_icons:
            description_parts.append(f" Инвентарь пуст")
        description_parts.append("")

        # Управление
        description_parts.append(f"{replace_emojis('•')} **Управление:**")
        description_parts.append(f" Используйте выпадающие меню ниже для экипировки/снятия.")
        description_parts.append("")
        description_parts.append(f"{replace_emojis('a_dot_smaller')} Максимум 1 тег и 1 значок одновременно")

        # Создать embed
        embed = discord.Embed(
            title=f" ИНВЕНТАРЬ ПОЛЬЗОВАТЕЛЯ",
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

        await interaction.response.send_message( view=compose(embed, view), ephemeral=True)

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

    @games_group.command(name="list", description="Показать доступные мини-игры")
    async def games(self, interaction: discord.Interaction) -> None:
        """Показать список мини-игр."""
        from views.games_view import GamesMainView
        from storage.user_balance_store import user_balance_store
        from storage.games_config import get_all_games

        # Получить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)

        # Получить список игр по статусу
        all_games = get_all_games()
        ready_games = [g for g in all_games if g.status in ("ready", "available")]
        dev_games = [g for g in all_games if g.status == "dev"]

        embed = discord.Embed(
            title=f" МИНИ-ИГРЫ | Главное меню",
            description=f" {interaction.user.mention}\n\n **Информация:**\n{replace_emojis('a_dot_smaller')} Ваш баланс: {balance:,} {get_emoji('money')}\n\n **Статус игр:**\n{replace_emojis('a_dot_smaller')} 🔓 Реализовано: **{len(ready_games)}** игр\n{replace_emojis('a_dot_smaller')} 🔒 В разработке: **{len(dev_games)}** игр\n\n **Категории:**\n{replace_emojis('a_dot_smaller')} **Игры на удачу**\n{replace_emojis('a_dot_smaller')} Быстрые игры на риск: монетка, кубики, угадай число и др.\n{replace_emojis('a_dot_smaller')} **Викторины и головоломки**\n{replace_emojis('a_dot_smaller')} Интеллектуальные состязания, викторины и слова.\n{replace_emojis('a_dot_smaller')} **Казино и ставки**\n{replace_emojis('a_dot_smaller')} Слоты, рулетка, баккара, лотерея и высокие ставки.\n\n{replace_emojis('a_dot_smaller')} Выберите категорию в меню ниже для просмотра списка игр",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        view = GamesMainView(interaction.user.id, interaction.guild_id)
        await interaction.response.send_message( view=compose(embed, view), ephemeral=True)



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

    # @app_commands.command(name="tictactoe", description="Крестики-Нолики")
    # async def tictactoe(self, interaction: discord.Interaction) -> None:
    #     """Игра крестики-нолики."""
    #     from views.tictactoe_view import TicTacToeBetModal

    #     await interaction.response.send_modal(TicTacToeBetModal(interaction.guild_id, interaction.user.id))

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

    @player_group.command(name="profile", description="Показать ваш профиль")
    @app_commands.describe(user="Пользователь (пусто = ваш профиль)")
    async def profile(self, interaction: discord.Interaction, user: discord.Member = None) -> None:
        """Показать детальный профиль игрока (CV2, без PNG)."""
        from views.profile_view import build_profile_layout

        # Если пользователь не указан, показываем профиль автора
        target_user = user if user else interaction.user

        await interaction.response.defer()
        layout = await build_profile_layout(interaction.guild, target_user)
        await interaction.followup.send(
            view=layout,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @player_set_group.command(name="bio", description="Установить описание профиля (раз в 7 дней)")
    @app_commands.describe(value="Текст описания (до 256 символов)")
    async def player_set_bio(self, interaction: discord.Interaction, value: str) -> None:
        """Установить bio профиля."""
        text = value.strip()
        if not text:
            await interaction.response.send_message(
                "❌ Описание не может быть пустым.", ephemeral=True
            )
            return
        if len(text) > 256:
            await interaction.response.send_message(
                "❌ Описание слишком длинное (максимум 256 символов).", ephemeral=True
            )
            return
        if not await self._check_profile_field_cooldown(interaction, "description"):
            return
        await self._save_profile_field(interaction, description=text)
        await interaction.response.send_message(
            "✅ Описание профиля обновлено.", ephemeral=True
        )

    @player_set_group.command(
        name="birthday",
        description="Установить день рождения — ДД.ММ.ГГГГ (раз в 7 дней)",
    )
    @app_commands.describe(value="Дата: ДД.ММ.ГГГГ или ГГГГ-ММ-ДД («-» — убрать)")
    async def player_set_birthday(self, interaction: discord.Interaction, value: str) -> None:
        """Установить день рождения."""
        raw = value.strip()
        new_value = ""
        if raw not in ("-", "—", "none", "нет"):
            normalized = None
            for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                try:
                    normalized = datetime.strptime(raw, fmt).strftime("%d.%m.%Y")
                    break
                except ValueError:
                    continue
            if not normalized:
                await interaction.response.send_message(
                    "❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ "
                    "(например, 25.12.2004).",
                    ephemeral=True,
                )
                return
            new_value = normalized

        if not await self._check_profile_field_cooldown(interaction, "birthday"):
            return
        await self._save_profile_field(interaction, birthday=new_value)
        suffix = "" if new_value else " Дата убрана."
        await interaction.response.send_message(
            f"✅ День рождения обновлён.{suffix}", ephemeral=True
        )

    async def _check_profile_field_cooldown(
        self, interaction: discord.Interaction, field: str
    ) -> bool:
        """7-дневный кулдаун на смену поля профиля (как в старых настройках)."""
        if interaction.user.id == BOT_OWNER_ID:
            return True
        from storage.profile_cooldowns import format_remaining, profile_cooldowns

        left = profile_cooldowns.seconds_left(
            interaction.guild_id, interaction.user.id, field
        )
        if left > 0:
            await interaction.response.send_message(
                replace_emojis(
                    f"⏳ Это поле можно менять **раз в 7 дней**.\n"
                    f"Осталось: **{format_remaining(left)}**"
                ),
                ephemeral=True,
            )
            return False
        return True

    async def _save_profile_field(
        self, interaction: discord.Interaction, **fields
    ) -> None:
        """Сохранить поля профиля и отметить кулдаун."""
        from views.profile_view import _default_stats
        from storage.profile_cooldowns import profile_cooldowns

        stats = await player_stats_store.get(
            interaction.guild_id, interaction.user.id
        )
        created = stats is None
        if stats is None:
            stats = _default_stats(
                interaction.guild_id, interaction.user.id,
                interaction.user.display_name,
            )
        for key, value in fields.items():
            setattr(stats, key, value)
        if created:
            await player_stats_store.set(stats)
        else:
            await player_stats_store.update(
                interaction.guild_id, interaction.user.id, stats
            )
        if interaction.user.id != BOT_OWNER_ID:
            for field in fields:
                profile_cooldowns.mark_changed(
                    interaction.guild_id, interaction.user.id, field
                )

    @player_group.command(name="history", description="Показать историю матчей")
    @app_commands.describe(user="Пользователь (пусто = ваша история)", limit="Количество матчей (макс 30)")
    async def history(self, interaction: discord.Interaction, user: discord.Member = None, limit: int = 10) -> None:
        """Показать историю матчей игрока."""
        from storage.match_history_store import match_history_store
        from views.history_view import HistoryView, create_history_embed

        # Ограничить limit до 30
        limit = min(max(limit, 1), 30)

        # Если пользователь не указан, показываем историю автора
        target_user = user if user else interaction.user

        # Получить историю матчей
        matches = await match_history_store.get(interaction.guild_id, target_user.id)
        # Ограничить количество матчей
        matches = matches[:limit]

        # Получить статистику за последние limit матчей
        stats = await match_history_store.get_recent_stats(interaction.guild_id, target_user.id, limit=limit)

        # Ник в профиле = ник в Discord
        user_name = target_user.display_name

        # Создать view и embed
        view = HistoryView(interaction.guild_id, target_user.id, matches, stats, user_name, limit)
        embed = create_history_embed(interaction.guild_id, target_user.id, matches, stats, page=0, user_name=user_name, limit=limit)

        await interaction.response.send_message(
            view=compose(embed, view),
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="rep", description="Дать +1 репутацию (1 раз на человека, раз в 24 часа)")
    @app_commands.describe(user="Пользователь")
    async def rep(self, interaction: discord.Interaction, user: discord.Member) -> None:
        """Дать +1 репутацию: раз в 24 часа, один раз на пару навсегда."""
        from services import reputation as rep_svc
        from utils.design import colors
        from utils.design.layouts import DesignView

        guild_id = interaction.guild_id

        async def _err(title: str, text: str) -> None:
            await interaction.response.send_message(
                view=DesignView.simple(
                    title=title, body=text, accent=colors.ERROR,
                    timeout=None,
                ),
                ephemeral=True,
            )

        if user.id == interaction.user.id:
            await _err("ОШИБКА", "Нельзя выдать репутацию самому себе.")
            return
        if user.bot:
            await _err("ОШИБКА", "Нельзя выдать репутацию боту.")
            return
        if interaction.user.bot:
            await _err("ОШИБКА", "Боты не могут выдавать репутацию.")
            return

        # Legacy-история /rep (rep_given.json): пары, уже выдававшие
        # репутацию до Reputation 2.0, остаются заблокированными навсегда.
        try:
            from storage.rep_store import rep_store
            if rep_store.has_given(guild_id, interaction.user.id, user.id):
                await _err(
                    "РЕПУТАЦИЯ УЖЕ ВЫДАНА",
                    f"Вы уже выдавали репутацию {user.mention}.\n"
                    "Повторная выдача одному человеку запрещена.",
                )
                return
        except Exception:
            pass

        result = await rep_svc.give_manual_rep(
            guild_id=guild_id,
            giver_id=interaction.user.id,
            target_id=user.id,
            target_name=user.display_name,
        )

        if result.status == "ok":
            cooldown_ts = int(
                __import__("time").time() + rep_svc.REP_MANUAL_COOLDOWN_HOURS * 3600
            )
            body = "\n".join([
                f"Получатель: {user.mention}",
                f"Начислено: +{rep_svc.REP_MANUAL_AMOUNT} REP",
                f"Текущая репутация: {result.rep_after}",
                f"Следующая выдача доступна <t:{cooldown_ts}:R>.",
            ])
            await interaction.response.send_message(
                view=DesignView.simple(
                    title="РЕПУТАЦИЯ ВЫДАНА", body=body,
                    accent=colors.SUCCESS, timeout=None,
                )
            )
        elif result.status == "already_pair":
            await _err(
                "РЕПУТАЦИЯ УЖЕ ВЫДАНА",
                f"Вы уже выдавали репутацию {user.mention}.\n"
                "Повторная выдача одному человеку запрещена.",
            )
        elif result.status == "cooldown":
            ts = int(result.next_available_at.timestamp()) if result.next_available_at else 0
            await _err(
                "ЛИМИТ РЕПУТАЦИИ",
                f"Вы сможете выдать репутацию снова <t:{ts}:R> "
                f"({rep_svc.format_cooldown(result.cooldown_seconds)}).",
            )
        else:
            await _err("ОШИБКА", "Не удалось обработать выдачу репутации. Попробуйте позже.")

    @app_commands.command(name="marry", description="Предложить руку и сердце пользователю")
    @app_commands.describe(user="Пользователь")
    async def marry(self, interaction: discord.Interaction, user: discord.Member) -> None:
        """Отправить предложение о браке."""
        from services.social.proposal import (
            _get_or_create_stats, build_proposal_view,
        )
        from services.social.common import banner_file, BANNER_PROPOSAL

        if user.id == interaction.user.id:
            await interaction.response.send_message(
                replace_emojis("❌ Нельзя жениться на себе."), ephemeral=True
            )
            return
        if user.bot:
            await interaction.response.send_message(
                replace_emojis("❌ Нельзя жениться на боте."), ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        own_stats = await _get_or_create_stats(guild_id, interaction.user.id, interaction.user.display_name)
        target_stats = await _get_or_create_stats(guild_id, user.id, user.display_name)

        if own_stats.spouse_id:
            await interaction.response.send_message(
                replace_emojis("❌ Вы уже состоите в браке. Сначала разведитесь через `/player divorce`."),
                ephemeral=True,
            )
            return
        if target_stats.spouse_id:
            await interaction.response.send_message(
                replace_emojis(f"❌ {user.mention} уже состоит в браке."), ephemeral=True
            )
            return

        view = build_proposal_view(interaction.user, user, guild_id=guild_id)
        await interaction.response.send_message(
            view=view, file=banner_file(BANNER_PROPOSAL))

    @player_group.command(name="divorce", description="Разорвать брак")
    async def divorce(self, interaction: discord.Interaction) -> None:
        """Развестись с текущим супругом/ой."""
        stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
        spouse_id = getattr(stats, "spouse_id", 0) if stats else 0
        if not spouse_id:
            await interaction.response.send_message(
                replace_emojis("❌ Вы не состоите в браке."), ephemeral=True
            )
            return

        spouse_stats = await player_stats_store.get(interaction.guild_id, spouse_id)
        if stats:
            stats.spouse_id = 0
            await player_stats_store.set(stats)
        if spouse_stats:
            spouse_stats.spouse_id = 0
            await player_stats_store.set(spouse_stats)

        embed = discord.Embed(
            title=" РАЗВОД | /player divorce",
            description=f" {interaction.user.mention} подал(а) на развод. Брак расторгнут.",
            color=discord.Color.from_rgb(69, 69, 69),
        )
        await interaction.response.send_message(view=compose(embed))

    @app_commands.command(name="birthdays", description="Дни рождения на сервере (по ближайшим)")
    async def birthdays(self, interaction: discord.Interaction) -> None:
        """Список дней рождения участников сервера."""
        if not interaction.guild:
            await interaction.response.send_message(
                replace_emojis("❌ Команда доступна только на сервере."), ephemeral=True
            )
            return

        from views.birthdays_view import collect_entries
        from services.social.birthdays import (
            build_birthdays_view, birthdays_file,
        )

        entries = await collect_entries(interaction.guild)
        view = build_birthdays_view(entries)
        await interaction.response.send_message(
            view=view, file=birthdays_file(),
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="браки", description="Действующие браки на сервере")
    async def marriages_cmd(self, interaction: discord.Interaction) -> None:
        """Список действующих браков с пагинацией."""
        if not interaction.guild:
            await interaction.response.send_message(
                replace_emojis("❌ Команда доступна только на сервере."), ephemeral=True
            )
            return

        from services.social.marriages import (
            build_marriages_view, marriages_file,
        )
        from storage.marriage_store import marriage_store

        entries = await marriage_store.list_active(interaction.guild_id)
        view = build_marriages_view(
            entries,
            invoker_id=interaction.user.id,
            guild_id=interaction.guild_id,
        )
        await interaction.response.send_message(
            view=view, file=marriages_file(),
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="birthdaytest", description="Тест поздравления с днём рождения")
    @app_commands.describe(user="Пользователь для тестового поздравления")
    async def birthdaytest(self, interaction: discord.Interaction, user: discord.Member) -> None:
        """Отправить тестовое поздравление с днём рождения."""
        from services.social.birthday_congrats import (
            build_congrats_view, congrats_file,
        )

        await interaction.response.send_message(
            view=build_congrats_view(user), file=congrats_file())

    @app_commands.command(name="welcometest", description="Тест welcome-сообщения в настроенный канал")
    @app_commands.default_permissions(administrator=True)
    async def welcometest(self, interaction: discord.Interaction) -> None:
        """Отправить welcome-карточку как для нового участника (диагностика)."""
        from services.welcome_card import send_welcome_message

        if interaction.guild is None:
            await interaction.response.send_message(
                "Только на сервере.", ephemeral=True)
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            member = await interaction.guild.fetch_member(member.id)
        ok = await send_welcome_message(interaction.client, member)
        await interaction.response.send_message(
            "Welcome отправлен." if ok else
            "Не удалось отправить — проверь логи бота.",
            ephemeral=True)

    @player_group.command(name="rank", description="Показать ваш ранг и прогресс")
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
            title=f" УРОВЕНЬ ПОЛЬЗОВАТЕЛЯ | /rank",
            description=f" {interaction.user.mention}\n\n{replace_emojis('•')} **Текущий статус:**\n Уровень: Level {stats.level}\n Ранг: {rank_title}\n\n{replace_emojis('•')} **Прогресс опыта:**\n Прогресс: `{progress_bar}` ({progress_percent}%)\n До след. уровня: {xp_remaining:,} XP\n\n{replace_emojis('a_dot_smaller')} Накопить XP можно через участие в турнирах и победы",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        # Эмблема текущего подранга вместо аватара; fallback — аватар
        from utils.ranks import rank_badge_path

        badge_path = rank_badge_path(stats.level)
        files = []
        if badge_path is not None:
            embed.set_thumbnail(url=f"attachment://{badge_path.name}")
            files.append(
                discord.File(str(badge_path), filename=badge_path.name)
            )
        else:
            embed.set_thumbnail(url=interaction.user.display_avatar.url)

        await interaction.response.send_message(
            view=compose(embed),
            files=files or None,
            allowed_mentions=discord.AllowedMentions.none())

    @economy_group.command(name="bet", description="Показать вашу статистику ставок")
    async def betting_stats(self, interaction: discord.Interaction) -> None:
        """Показать статистику ставок пользователя."""
        from storage.betting_stats_store import betting_stats_store

        stats = await betting_stats_store.get_user_stats(interaction.guild_id, interaction.user.id)

        if not stats or stats["total_bets"] == 0:
            await interaction.response.send_message(
                replace_emojis("• У вас пока нет статистики ставок."),
                ephemeral=True
            )
            return

        accuracy = stats["success_rate"]
        lost_bets = stats["total_bets"] - stats["successful_bets"]

        embed = discord.Embed(
            title=f" СТАТИСТИКА СТАВОК | /bet",
            description=f"Ваша общая статистика по ставкам:\n\n{replace_emojis('•')} **Основное:**\n Всего ставок: {stats['total_bets']}\n Выигрышных: {stats['successful_bets']}\n Проигрышных: {lost_bets}\n Точность: {accuracy:.1f}%\n\n{replace_emojis('•')} **Баланс:**\n Выиграно: +{stats['total_won']} {get_emoji('money')}\n Проиграно: -{stats['total_lost']} {get_emoji('money')}\n\n{replace_emojis('a_dot_smaller')} Данные обновляются в реальном времени",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        await interaction.response.send_message(
            view=compose(embed),
            allowed_mentions=discord.AllowedMentions.none())

    # @app_commands.command(name="limit", description="Включить/выключить лимит для круга")
    # @app_commands.describe(
    #     circle="Номер круга (2, 3 или 4)",
    #     status="on для включения лимита, off для отключения"
    # )
    # @is_org()
    # async def set_circle_limit(
    #     self,
    #     interaction: discord.Interaction,
    #     circle: int,
    #     status: str
    # ) -> None:
    #     """Включить или выключить лимит для круга."""
    #     if circle not in [2, 3, 4]:
    #         await interaction.response.send_message(
    #             replace_emojis("❌ Круг должен быть 2, 3 или 4."),
    #             ephemeral=True
    #         )
    #         asyncio.create_task(_delete_ephemeral_later(interaction))
    #         return
    #
    #     if status.lower() not in ["on", "off"]:
    #         await interaction.response.send_message(
    #             replace_emojis("❌ Статус должен быть 'on' или 'off'."),
    #             ephemeral=True
    #         )
    #         asyncio.create_task(_delete_ephemeral_later(interaction))
    #         return
    #
    #     tournament = store.get(interaction.guild_id)
    #     if not tournament:
    #         await interaction.response.send_message(
    #             replace_emojis("❌ Нет активного турнира."),
    #             ephemeral=True,
    #         )
    #         asyncio.create_task(_delete_ephemeral_later(interaction))
    #         return
    #
    #     if tournament.phase != TournamentPhase.SETUP:
    #         await interaction.response.send_message(
    #             replace_emojis("❌ Лимиты можно менять только в фазе настройки."),
    #             ephemeral=True
    #         )
    #         asyncio.create_task(_delete_ephemeral_later(interaction))
    #         return
    #
    #     tournament.circle_limits_enabled[circle] = (status.lower() == "on")
    #     store.set(tournament)
    #
    #     status_text = "включен" if tournament.circle_limits_enabled[circle] else "отключен"
    #     await interaction.response.send_message(
    #         replace_emojis("✅ Лимит для круга {circle} {status_text}."),
    #         ephemeral=True
    #     )
    #     asyncio.create_task(_delete_ephemeral_later(interaction))
    #
    #     await self.bot.update_tournament_message(interaction.guild, tournament)

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

    #     await interaction.followup.send( view=compose(embed))

    @app_commands.command(name="booyah", description="Зал славы — рекорды сервера")
    async def booyah(self, interaction: discord.Interaction) -> None:
        """Интерактивный Зал славы: 7 категорий, 30+ рекордов."""
        await interaction.response.defer()

        from services.hall_of_fame.cache import get_cached
        from services.hall_of_fame.view import (
            HallOfFameView, banner_file,
        )

        try:
            data = await get_cached(interaction.guild_id)
        except Exception:
            logger.exception("booyah compute failed")
            await interaction.edit_original_response(
                view=text_card(replace_emojis("❌ Не удалось загрузить рекорды.")))
            return

        view = HallOfFameView(
            guild_id=interaction.guild_id,
            invoker_id=interaction.user.id,
            category_key="combat",
            data=data,
            guild=interaction.guild,
            timeout=None,
        )
        file = banner_file()
        kwargs = {"view": view}
        if file is not None:
            kwargs["file"] = file
        await interaction.edit_original_response(**kwargs)

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

    @admin_group.command(name="edit", description=f"Изменить ELO или {get_emoji('money')} игрока")
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
                await user_balance_store.set_balance(interaction.guild_id, player.id, new_balance)
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

    @admin_group.command(name="fix_userid", description="Исправить user_id игрока")
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

    @admin_group.command(name="fix", description="Сбросить статистику ботов (cap1, cap2 и т.д.)")
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
            # Player bots
            "Player1", "Player2", "Player3", "Player4", "Player5",
            "Player6", "Player7", "Player8", "Player9", "Player10",
            "Player11", "Player12", "Player13", "Player14", "Player15",
            "Player16", "Player17", "Player18", "Player19", "Player20",
            "Player21", "Player22", "Player23", "Player24", "Player25",
            "Player26", "Player27", "Player28", "Player29", "Player30",
            "Player31", "Player32",
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

    @economy_group.command(name="pay", description="Передать монеты другому игроку")
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

        # Check if sender has at least 1 game played
        sender_stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
        if not sender_stats or sender_stats.games < 1:
            await interaction.response.send_message(
                replace_emojis("❌ Вы должны сыграть хотя бы 1 игру в турнире для использования /pay"),
                ephemeral=True
            )
            return

        # Check if recipient has at least 1 game played
        recipient_stats = await player_stats_store.get(interaction.guild_id, user.id)
        if not recipient_stats or recipient_stats.games < 1:
            await interaction.response.send_message(
                replace_emojis(f"❌ {user.mention} должен сыграть хотя бы 1 игру в турнире для получения перевода"),
                ephemeral=True
            )
            return

        # Базовая комиссия 10%
        base_fee_percent = 0.1
        pet_bonus_text = ""

        # Применить бонус питомца (Щедрый перевод) через новый сервис
        try:
            from storage.pet_store import pet_store
            pet = await pet_store.get_user_pet(interaction.guild_id, interaction.user.id)

            if pet and pet.fullness > 0:
                # Списать энергию за использование способности
                energy_consumed = await pet_store.consume_energy_for_ability(interaction.guild_id, interaction.user.id, "pay", interaction.client)

                if energy_consumed:
                    # Получаем каталог питомца
                    from storage.pet_store import pet_catalog
                    pet_def = pet_catalog.get_pet(pet.pet_type.value)

                    if pet_def:  # Способности открыты с 1 возраста
                        ability = pet_def["abilities"][2]
                        if ability["name"] == "Щедрый перевод":
                            bonus_value = ability.get("max_val", 0)

                            fee_reduction = bonus_value / 100
                            base_fee_percent = base_fee_percent * (1 - fee_reduction)
                            logger.info(f"Applied pay fee reduction: {bonus_value}% from {pet.pet_name}")
                            pet_bonus_text = f"\n\n🐾 **Золотой лис ({pet.pet_name}):** Скидка **-{bonus_value:.1f}%** на комиссию перевода!"
        except Exception as e:
            logger.error(f"Error getting pet bonus: {e}")

        try:
            result = await user_balance_store.transfer_balance(
                interaction.guild_id, interaction.user.id,
                interaction.guild_id, user.id,
                amount, base_fee_percent
            )

            fee_display = int(base_fee_percent * 100)
            embed = discord.Embed(
                title=replace_emojis("Передача монет"),
                color=discord.Color.green()
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            embed.description = f"{interaction.user.mention}, Вы успешно **передали** {get_emoji('money')}\n\n{replace_emojis('•')} **Комиссия:** {fee_display}%\n{replace_emojis('•')} **Списалось:** {result['total_deducted']:,} {get_emoji('money')}{pet_bonus_text}"
            embed.add_field(name="Пользователь", value=f" {user.mention} **получил** — {result['amount']:,} {get_emoji('money')}", inline=False)

            try:
                await interaction.response.send_message( view=compose(embed))
            except (discord.NotFound, discord.DiscordServerError):
                pass
        except ValueError as e:
            try:
                await interaction.response.send_message(replace_emojis(f"❌ {str(e)}"), ephemeral=True)
            except (discord.NotFound, discord.DiscordServerError):
                pass

    @economy_group.command(name="gift", description="Передать предмет другому игроку")
    @app_commands.describe(user="Игрок", item_id="ID предмета")
    async def gift(self, interaction: discord.Interaction, user: discord.User, item_id: str) -> None:
        """Передать предмет с комиссией 10% от стоимости."""
        from storage.user_balance_store import user_balance_store
        from storage.shop_store import inventory_store, shop_store

        if user.id == interaction.user.id:
            await interaction.response.send_message(replace_emojis("• Нельзя подарить самому себе"), ephemeral=True)
            return

        # Check if sender has at least 1 game played
        sender_stats = await player_stats_store.get(interaction.guild_id, interaction.user.id)
        if not sender_stats or sender_stats.games < 1:
            await interaction.response.send_message(
                replace_emojis("❌ Вы должны сыграть хотя бы 1 игру в турнире для использования /gift"),
                ephemeral=True
            )
            return

        # Check if recipient has at least 1 game played
        recipient_stats = await player_stats_store.get(interaction.guild_id, user.id)
        if not recipient_stats or recipient_stats.games < 1:
            await interaction.response.send_message(
                replace_emojis(f"❌ {user.mention} должен сыграть хотя бы 1 игру в турнире для получения подарка"),
                ephemeral=True
            )
            return

        # Get item info
        item = shop_store.get_item(item_id)
        if not item:
            await interaction.response.send_message(replace_emojis("• Предмет не найден"), ephemeral=True)
            return

        # Check if sender has the item
        sender_inventory = inventory_store.get_player_inventory(interaction.guild_id, interaction.user.id)
        has_item = any(cosmetic.item_id == item_id for cosmetic in sender_inventory)

        if not has_item:
            await interaction.response.send_message(replace_emojis("• У вас нет этого предмета"), ephemeral=True)
            return

        # Calculate fee
        base_fee_percent = 0.1  # 10% комиссия

        # Применить бонус питомца (Торговый нюх)
        pet_bonus_text = ""
        try:
            from storage.pet_store import pet_store
            pet = await pet_store.get_user_pet(interaction.guild_id, interaction.user.id)

            if pet and pet.fullness > 0:
                # Списать энергию за использование способности
                energy_consumed = await pet_store.consume_energy_for_ability(interaction.guild_id, interaction.user.id, "gift", interaction.client)

                if energy_consumed:
                    from storage.pet_store import pet_catalog
                    pet_def = pet_catalog.get_pet(pet.pet_type.value)

                    if pet_def:  # Способности открыты с 1 возраста
                        ability = pet_def["abilities"][2]
                        if ability["name"] == "Торговый нюх":
                            bonus_value = ability.get("max_val", 0)

                            fee_reduction = bonus_value / 100
                            base_fee_percent = base_fee_percent * (1 - fee_reduction)
                            logger.info(f"Applied gift fee reduction: {bonus_value}% from {pet.pet_name}")
                            pet_bonus_text = f"\n\n🐾 **Мимик-собиратель ({pet.pet_name}):** Торговый сбор уменьшен на **-{bonus_value:.1f}%**!"
        except Exception as e:
            logger.error(f"Error getting pet bonus for gift: {e}")

        fee = int(item.price * base_fee_percent)

        # Check sender has enough balance for fee
        sender_balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if sender_balance < fee:
            await interaction.response.send_message(
                replace_emojis(f"• Недостаточно монет для комиссии. Нужно: {fee} {get_emoji('money')}, есть: {sender_balance} {get_emoji('money')}"),
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
                description=f"{interaction.user.mention}, Вы успешно **подарили** {item_display} • {replace_emojis(rare_emoji)}\n\n• **Комиссия:** {int(base_fee_percent * 100)}% ({fee} {get_emoji('money')})\n• **Стоимость:** {item.price} {get_emoji('money')}\n\n**Пользователь**\n {user.mention} **получил** — {item_display}{pet_bonus_text}",
                color=discord.Color.green()
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)

            await interaction.response.send_message( view=compose(embed), ephemeral=False)
        elif result['compensated']:
            # Return fee since transfer failed
            await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, fee)

            embed = discord.Embed(
                title="Передача не удалась",
                description=f"{interaction.user.mention}, у получателя уже есть этот предмет. Комиссия возвращена.",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)

            await interaction.response.send_message( view=compose(embed), ephemeral=False)
        else:
            # Return fee since transfer failed
            await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, fee)

            await interaction.response.send_message(
                replace_emojis(f"• Не удалось передать предмет: {result['item_name']}"),
                ephemeral=True
            )

    @admin_group.command(name="debug_emoji", description="Проверить конфигурацию эмодзи")
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
        
        # Game emojis - show ALL configured emojis (split into multiple fields)
        game_items = list(GAME_EMOJIS.items())
        chunk_size = 20  # ~1000 chars per chunk
        for i in range(0, len(game_items), chunk_size):
            chunk = game_items[i:i + chunk_size]
            game_text = ""
            for name, emoji_id in chunk:
                emoji = get_emoji(name)
                status = "✅" if emoji_id else "❌"
                game_text += f"{status} {name}: `{emoji}`\n"

            if game_text:
                field_name = replace_emojis("🎮 Игровые эмодзи") if i == 0 else replace_emojis("🎮 Игровые эмодзи (продолжение)")
                embed.add_field(name=field_name, value=game_text, inline=False)
        
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
        embed.add_field(name=replace_emojis("🧪 Тест (Grandmaster)"), value=f"`{test_emoji}`", inline=False)
        
        await interaction.response.send_message( view=compose(embed), ephemeral=True)

    @app_commands.command(name="whoami", description="Узнать кто ты на самом деле")
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
                        replace_emojis(f"• Вы уже узнали кто ты!\nСледующий вопрос: <t:{int((last_use + timedelta(hours=cooldown_hours)).timestamp())}:R>"),
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
            title=f" **Кто я?** ",
            description=f"  {response} ",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        await interaction.response.send_message( view=compose(embed))

    @app_commands.command(name="daily", description="Получить ежедневный бонус (лимит 500 монет)")
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
                        replace_emojis(f"• Вы уже получили бонус!\nСледующий бонус: <t:{int((last_claim + timedelta(hours=cooldown_hours)).timestamp())}:R>"),
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

            # Calculate reward: 100 on day 1, +10 for each additional day
            # Day 1: 100, Day 2: 110, Day 3: 120, Day 10: 190, Day 11: 200, etc.
            reward = 100 + (streak - 1) * 10

            # Cap reward at 500
            reward = min(reward, 500)

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
                title=f" ЕЖЕДНЕВНАЯ НАГРАДА | /daily",
                description=f" {interaction.user.mention}\n\n{replace_emojis('•')} **Ваша награда:**\n Получено: {reward} {get_emoji('money')}\n Серия заходов: {streak} дней {fire_emoji}\n\n{replace_emojis('a_dot_smaller')} Возвращайтесь через 12 часов, чтобы получить следующую награду!",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)

            await interaction.response.send_message(
                view=compose(embed), ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none())

    @server_group.command(name="role", description="Настроить сообщение для управления ролью организатора (только для админов)")
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

    @server_group.command(name="welcome", description="Показать информацию о сервере и боте (только для владельца)")
    @is_bot_owner()
    async def welcome(self, interaction: discord.Interaction) -> None:
        """Показать приветственное сообщение с гайдом."""
        # Single embed with image and text
        import os as _os
        _guide_desc = (
            " **Добро пожаловать на сервер!**\n"
            "Этот гайд поможет вам сориентироваться по каналам, узнать систему рангов и использовать команды нашего бота.\n\n"
            "  **НАВИГАЦИЯ ПО КАНАЛАМ:**\n"
            f"{replace_emojis('a_dot_smaller')} <#1200125075156910181>  Основное общение сообщества\n"
            f"{replace_emojis('a_dot_smaller')} <#1549809898643001484>  Проведение турниров\n"
            f"{replace_emojis('a_dot_smaller')} <#1514677029159567604>  Яркие моменты из игр\n"
            f"{replace_emojis('a_dot_smaller')} <#1551167853741219880>  Команды ботов и спам-игры\n"
            f"{replace_emojis('a_dot_smaller')} <#1250974603162026024>  Прослушивание треков\n"
            f"{replace_emojis('a_dot_smaller')} <#1242489553189732373>  Полезные файлы для FF\n\n"
            f" {replace_emojis('a_dot_smaller')} Выберите категорию в меню ниже, чтобы узнать больше"
        )
        embed = discord.Embed(
            title="✧ DISCORD SERVER r1z3 | ПУТЕВОДИТЕЛЬ ✧",
            description=_os.getenv("GUIDE_NAV_TEXT") or _guide_desc,
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_image(url="https://cdn.discordapp.com/attachments/1553458753800507532/1554375503572369449/a338360963724ad1957dd13a1730547c.png?ex=6abca87e&is=6abb56fe&hm=ed0782e9cd150cb625960c89d66a581e693c9ec382180e795e765f29c960d21c&")
        embed.set_footer(text="DISCORD SERVER r1z3")

        view = GuideView()

        # Send the main welcome message directly to channel (not as followup)
        await interaction.channel.send( view=compose(embed, view))

        # Send confirmation to bot owner only
        await interaction.response.send_message(
            replace_emojis("✅ Сообщение приветствия создано"),
            ephemeral=True
        )

    @admin_group.command(name="reset", description="Сбросить статистику игрока (только для владельца бота)")
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

    @admin_group.command(name="resetprogress", description="Сбросить прогресс ВСЕХ пользователей к дефолту (только владелец)")
    @is_bot_owner()
    async def resetprogress(self, interaction: discord.Interaction) -> None:
        """Двухшаговый сброс прогресса: dry-run отчёт + кнопка подтверждения."""
        from services.progress_reset import (
            collect_plan, format_plan, guard_problems, execute_plan,
            _existing_tables,
        )
        from storage.db import get_pool

        await interaction.response.defer(ephemeral=True)
        pool = await get_pool()
        async with pool.acquire() as conn:
            plan = await collect_plan(conn)
            problems = await guard_problems(conn, await _existing_tables(conn))

        view = _ResetProgressConfirmView(
            plan=plan, problems=problems, invoker_id=interaction.user.id)
        await interaction.followup.send(
            format_plan(plan, problems)
            + "\n\n**Это необратимо.** Нажми «Подтвердить сброс» для выполнения.",
            view=view, ephemeral=True)

    @is_bot_owner()
    async def trstattest(self, interaction: discord.Interaction) -> None:
        """Рандомно заполнить статистику для всех игроков в текущем турнире."""
        tournament = store.get(interaction.guild_id, interaction.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден в этом канале."),
                ephemeral=True
            )
            return

        # Determine which matches to fill based on phase
        matches_to_fill = []
        if tournament.phase == TournamentPhase.QUALIFIERS:
            for idx in range(len(tournament.qualifier_matches)):
                matches_to_fill.append(("qualifier", idx))
        elif tournament.phase == TournamentPhase.SEMIFINALS:
            for idx in range(len(tournament.semifinal_matches)):
                matches_to_fill.append(("semifinal", idx))
        elif tournament.phase == TournamentPhase.FINAL:
            matches_to_fill.append(("final", 0))
        else:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не в подходящей фазе. Нужно быть в qualifiers, semifinals или final."),
                ephemeral=True
            )
            return

        total_filled = 0
        for match_type, match_index in matches_to_fill:
            match_id = f"{tournament.id}_{match_type}_{match_index}"

            # Get teams for this match
            if match_type == "qualifier":
                match = tournament.qualifier_matches[match_index]
            elif match_type == "semifinal":
                match = tournament.semifinal_matches[match_index]
            else:  # final
                match = (tournament.final_teams[0], tournament.final_teams[1])

            team_a_idx, team_b_idx = match

            # Fill stats for both teams
            for team_idx in [team_a_idx, team_b_idx]:
                team_data = tournament.teams[team_idx] if team_idx < len(tournament.teams) else {}

                # Fill stats for circle1-4
                for circle in range(1, 5):
                    player = team_data.get(f"circle{circle}")
                    if player:
                        # Random stats: kills 0-5, deaths 0-5
                        kills = random.randint(0, 5)
                        deaths = random.randint(0, 5)

                        # Store in temp_match_stats
                        if match_id not in tournament.temp_match_stats:
                            tournament.temp_match_stats[match_id] = {}

                        tournament.temp_match_stats[match_id][player] = {
                            "kills": kills,
                            "deaths": deaths
                        }

            total_filled += 1

        # Save tournament
        store.set(tournament)

        await interaction.response.send_message(
            replace_emojis(f"✅ Статистика рандомно заполнена для {total_filled} матчей."),
            ephemeral=True
        )

    async def cog_load(self) -> None:
        """Привязка подкоманд общих групп к этому cog'у."""
        bind_commands(player_group, self, ["profile", "history", "rank", "divorce"])
        bind_commands(player_set_group, self, ["bio", "birthday"])
        bind_commands(economy_group, self, ["balance", "pay", "gift", "bet"])
        bind_commands(admin_group, self, [
            "edit", "fix", "fix_userid", "reset", "resetprogress",
            "debug_emoji",
        ])
        bind_commands(server_group, self, ["role", "welcome", "check2fa"])
        bind_commands(games_group, self, ["list"])

        # Сбросить старые кастомные ники: stats.name = Discord display_name
        asyncio.create_task(self._sync_profile_names())

    async def _sync_profile_names(self) -> None:
        """Привести сохранённые ники к Discord-никам (для кэшированных участников)."""
        try:
            for guild in self.bot.guilds:
                try:
                    stats_list = await player_stats_store.get_all(guild.id)
                except Exception:
                    continue
                for stats in stats_list:
                    member = guild.get_member(stats.user_id)
                    if member is None or stats.name == member.display_name:
                        continue
                    stats.name = member.display_name
                    try:
                        await player_stats_store.update(
                            guild.id, stats.user_id, stats
                        )
                    except Exception:
                        logger.debug("Profile name sync failed", exc_info=True)
        except Exception:
            logger.exception("_sync_profile_names failed")

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
    """Получить эмодзи и название ранга по уровню (utils.ranks)."""
    from config import get_emoji
    from utils.ranks import rank_name, rank_tier_name

    name = rank_name(level)
    emoji = get_emoji(rank_tier_name(level))
    return f"{emoji} {name}"


async def send_tournament_notification(tournament, guild, action: str) -> None:
    """Send tournament notification to notification channel."""
    try:
        channel = guild.get_channel(NOTIFICATION_CHANNEL_ID)
        if not channel:
            logger.warning(f"Notification channel {NOTIFICATION_CHANNEL_ID} not found")
            return

        role = guild.get_role(NOTIFICATION_ROLE_ID)
        role_mention = role.mention if role else ""

        embed = discord.Embed(
            title="Турнир Создан",
            description=(
                f"**Тип:** {tournament.size.value} игроков / {tournament.formation_mode.value.upper()}\n"
                f"**Регистрация** {replace_emojis("➡️")} <#{tournament.channel_id}>"
            ),
            color=discord.Color.blue()
        )

        await channel.send( view=compose(embed, text=role_mention), allowed_mentions=discord.AllowedMentions(roles=True))
        logger.info(f"Tournament notification sent: {action} in guild {guild.id}")
    except Exception as e:
        logger.error(f"Failed to send tournament notification: {e}")


def _help_category_embed(value: str) -> discord.Embed | None:
    """Embed раздела /help. Общий контент для Guide и HelpGuide меню."""
    from utils.ranks import RANKS

    dot = replace_emojis('a_dot_smaller')
    money = get_emoji('money')
    gray = discord.Color.from_rgb(69, 69, 69)
    org_color = discord.Color.from_rgb(100, 38, 56)
    footer = f"\n\n{dot}  Используйте выпадающее меню ниже для перехода в другие разделы "

    # Тиры (без поддивизионов): первая ступень каждого тира
    from utils.ranks import rank_tier_name

    tier_first: dict[str, tuple[str, int]] = {}
    for _rank_name, min_level_, _badge in RANKS:
        tier_ = rank_tier_name(min_level_)
        if tier_ not in tier_first:
            tier_first[tier_] = (tier_, min_level_)
    rank_lines = "\n".join(
        f"{dot} {get_emoji(tier)} **{tier}:** `{min_level}+ lvl`"
        for tier, min_level in reversed(list(tier_first.values()))
    )

    if value == "tournaments":
        return discord.Embed(
            title="  РАЗДЕЛ: ТУРНИРЫ  ",
            description=(
                f" **Турниры, рейтинговые матчи и статистика**\n\n"
                f" **Доступные команды:**\n"
                f"{dot}  `/top` — Таблица лидеров `(уровень / монеты / ELO / репутация)`\n"
                f"{dot}  `/booyah` — Зал славы, рекорды и история турниров\n"
                f"{dot}  `/mm squad` — Поиск матча 4×4 (Ranked Matchmaking)\n"
                f"{dot}  `/mm delete` — Удалить поиски на сервере (орг)\n"
                f"{dot}  `/tournament create|delete` — Турнирная сетка (орг)\n"
                f"{dot}  `/br create|delete` — Battle Royale (орг)\n"
                f"{dot}  `/tournament ультиматум создать|удалить` — Ультиматум (орг)\n"
                f"{dot}  `/team create|status|delete` — Команды для BR\n"
                f"{dot}  `/ff info|stats` — Статистика Free Fire\n"
                f"{dot}  `/player ulti`, `/player ulti_history` — Рейтинг Ультиматума (Соло/Дуо)\n"
                + footer
            ),
            color=gray,
        )

    if value == "profile":
        return discord.Embed(
            title="  РАЗДЕЛ: ПРОФИЛЬ  ",
            description=(
                f" **Персональный аккаунт и матчи**\n\n"
                f" **Доступные команды:**\n"
                f"{dot}  `/player profile` — PNG-карточка профиля со статой\n"
                f"{dot}  `/player history` — История матчей\n"
                f"{dot}  `/player rank` — Ранг и прогресс до следующего\n"
                f"{dot}  `/player set bio|birthday` — Поля профиля `(кулдаун 7 дней)`\n"
                f"{dot}  `/whoami` — Кто ты на самом деле\n"
                f"{dot}  `/birthdaytest` — Тест поздравления `(админ)`\n"
                + footer
            ),
            color=gray,
        )

    if value == "social":
        return discord.Embed(
            title="  РАЗДЕЛ: СОЦИАЛЬНОЕ  ",
            description=(
                f" **Репутация, брак и дни рождения**\n\n"
                f" **Доступные команды:**\n"
                f"{dot}  `/rep` — +1 репутация `(1 раз на человека, раз в 24ч)`\n"
                f"{dot}  `/reputation profile|history|stats` — Репутация\n"
                f"{dot}  `/reputation adjust` — Корректировка REP `(админ)`\n"
                f"{dot}  `/tag claim` — +5 REP за серверный тег\n"
                f"{dot}  `/invites` — Статистика приглашений\n"
                f"{dot}  `/marry @user` — Предложение руки и сердца\n"
                f"{dot}  `/player divorce` — Расторгнуть брак\n"
                f"{dot}  `/birthdays` — Дни рождения участников сервера\n"
                + footer
            ),
            color=gray,
        )

    if value == "economy":
        return discord.Embed(
            title="  РАЗДЕЛ: ЭКОНОМИКА  ",
            description=(
                f" **Финансы, сейф и криминал**\n\n"
                f" **Доступные команды:**\n"
                f"{dot}  `/economy balance` — Баланс `(наличные + сейф)`\n"
                f"{dot}  `/daily` — Ежедневный бонус\n"
                f"{dot}  `/economy pay @user сумма` — Перевод монет\n"
                f"{dot}  `/economy gift @user предмет` — Подарить предмет\n"
                f"{dot}  `/economy sell предмет` — Продать за 50% стоимости\n"
                f"{dot}  `/economy bet` — Статистика ставок\n"
                f"{dot}  `/economy bank status|deposit|withdraw` — Сейф `(комиссия 5% на пополнение)`\n"
                f"{dot}  `/crime rob @user` — Одиночное ограбление\n"
                f"{dot}  `/crime robgroup @user` — Групповое ограбление\n\n"
                f" **Защита средств:**\n"
                f"{dot} Наличные уязвимы для ограблений, сейф — нет\n"
                + footer
            ),
            color=gray,
        )

    if value == "shop":
        return discord.Embed(
            title="  РАЗДЕЛ: МАГАЗИН  ",
            description=(
                f" **Покупка товаров и инвентарь**\n\n"
                f" **Доступные команды:**\n"
                f"{dot}  `/shop` — Магазин: роли, питомцы и предметы\n"
                f"{dot}  `/inventory` — Ваш инвентарь\n\n"
                f" **Редкости предметов:**\n"
                f"{dot} Basic → Special — от `700` до `5,950` {money}\n\n"
                + footer
            ),
            color=gray,
        )

    if value == "pets":
        return discord.Embed(
            title="  РАЗДЕЛ: ПИТОМЦЫ  ",
            description=(
                f" **RPG-система питомцев с уникальными способностями**\n\n"
                f" **Доступные команды:**\n"
                f"{dot}  `/pet profile` — Профиль питомца, сытость, энергия\n"
                f"{dot}  `/pet catalog` — Каталог всех питомцев\n"
                f"{dot}  `/pet top` — Топ питомцев по уровню\n"
                f"{dot}  `/pet release` — Отпустить питомца `(удалить навсегда)`\n"
                f"{dot}  `/pet coowner @игрок` — Со-опекунство\n"
                f"{dot}  `/pet coowner_remove` — Убрать со-опекуна / выйти\n\n"
                f" **Покупка и уход:**\n"
                f"{dot} Питомцы покупаются через `/shop` → Питомцы\n"
                f"{dot} Кормление — 30 монет `(сытость, энергия, EXP)`\n"
                f"{dot} Таинственное яйцо — 500 монет, шанс 20%\n\n"
                f" **Цены по редкости:**\n"
                f"{dot} Обычный `3,000` / Необычный `8,000` {money}\n"
                f"{dot} Эпический `15,000` / Легендарный `30,000` {money}\n"
                + footer
            ),
            color=gray,
        )

    if value == "games":
        return discord.Embed(
            title="  РАЗДЕЛ: МИНИ-ИГРЫ  ",
            description=(
                f" **Казино-игры на монеты**\n\n"
                f" **Доступные команды:**\n"
                f"{dot}  `/games list` — Меню всех игр\n"
                f"{dot}  `/games coin_flip` — Монетка `(1.6x)`\n"
                f"{dot}  `/games dice_roll` — Кубик 1-6 `(4.8x)`\n"
                f"{dot}  `/games guess_number` — Число 1-100 `(2.5x)`\n"
                f"{dot}  `/games high_low` — Выше или ниже `(до 3.7x)`\n"
                f"{dot}  `/games roulette` — Рулетка `(1.6x-30x)`\n"
                f"{dot}  `/games mathquiz` — Математическая дуэль\n"
                f"{dot}  `/games stats` — Статистика казино `(админ)`\n\n"
                f" Все игры с честными шансами и лимитом ставки.\n"
                + footer
            ),
            color=gray,
        )

    if value == "ranks":
        return discord.Embed(
            title="  СИСТЕМА РАНГОВ  ",
            description=(
                f" **Система рангов по уровням:**\n{rank_lines}\n\n"
                f"{dot} `/top` — ELO-таблица лидеров\n"
                f"{dot} `/player rank` — Текущий ранг и прогресс\n"
                f"{dot} `/player ulti` — Рейтинг Ультиматума"
            ),
            color=gray,
        )

    if value == "organizers":
        return discord.Embed(
            title="  РАЗДЕЛ: ОРГАНИЗАТОРАМ  ",
            description=(
                f" **Турниры, инфо-центр и баннер `(админы/орги)`**\n\n"
                f" **Турниры:**\n"
                f"{dot}  `/tournament create|delete|deleteall|test` — Сетка турнира\n"
                f"{dot}  `/br create|delete|test_fill|test_teams|test_stats` — Battle Royale\n"
                f"{dot}  `/tournament ультиматум создать|удалить|сетка|статус|история|исправить` — Ультиматум\n"
                f"{dot}  `/mm squad|delete|test` — Matchmaking 4×4\n"
                f"{dot}  `/team create|delete|status` — Команды BR\n\n"
                f" **Сервер и контент:**\n"
                f"{dot}  `/server role|welcome|check2fa|off` — Настройки сервера\n"
                f"{dot}  `/info publish|update|status|validate` — Информационный центр\n"
                f"{dot}  `/banner preview|update|status|leaderboard|auto` — Дневной баннер\n"
                f"{dot}  `/getemojiid` — ID всех эмодзи сервера\n\n"
                f" **Админ-инструменты:**\n"
                f"{dot}  `/admin edit|fix|fix_userid|reset|debug_emoji` — Игроки и отладка\n"
                f"{dot}  `/admin forcedrop|isolate|delete_thread` — Экстренные действия\n"
                + footer
            ),
            color=org_color,
        )

    if value == "rules":
        embed = discord.Embed(
            title=" СВОД ПРАВИЛ И РЕГЛАМЕНТ R1Z3",
            color=gray,
        )
        embed.add_field(
            name="  1. Базовые правила сервера",
            value="• **Уважение.** Без оскорблений, провокаций и токсичности — нарушение: **мьют 3 часа**, при повторе бан.\n\n• **Без рекламы и спама.** Сторонние серверы и ссылки — **мьют или пермабан**.\n\n• **Без 18+, шок-контента и вредоносных файлов** — **мьют или пермабан**.",
            inline=False
        )
        embed.add_field(
            name="  2. Регламент турниров",
            value="Запись на турнир является обязательством участвовать:\n\n• **Неявка, уход с матча, игнор тиммейтов** — **VAC 30 минут**.\n\n• **Читы, багоюз, оскорбление организаторов** — **дисквалификация и пермабан**.",
            inline=False
        )
        embed.add_field(
            name="  3. Шкала варнов",
            value="**1, 2, 3 варна** — только **VAC 30 минут**, без мьюта.\n**4 варн** — **1 час мьюта**, дальше удвоение за каждый варн.\n\n> 📝 **Примечание:** Точные определения нарушений — на усмотрение администрации.",
            inline=False
        )
        return embed

    return None


_HELP_OPTIONS = [
    discord.SelectOption(label="Правила сервера", description="Свод правил и регламент турниров", value="rules"),
    discord.SelectOption(label="Турниры", description="Топ, BR, Ультиматум и Ranked MM", value="tournaments"),
    discord.SelectOption(label="Профиль", description="Карточка игрока, ранг и история матчей", value="profile"),
    discord.SelectOption(label="Социальное", description="Репутация, брак и дни рождения", value="social"),
    discord.SelectOption(label="Экономика", description="Баланс, сейф, переводы и криминал", value="economy"),
    discord.SelectOption(label="Магазин", description="Покупка предметов и инвентарь", value="shop"),
    discord.SelectOption(label="Питомцы", description="RPG-питомцы с способностями", value="pets"),
    discord.SelectOption(label="Мини-игры", description="Казино-игры на монеты", value="games"),
    discord.SelectOption(label="Система рангов", description="Ранги, уровни и ELO", value="ranks"),
    discord.SelectOption(label="Организаторам", description="Турниры, инфо-центр и баннер", value="organizers"),
]


class _ResetProgressConfirmView(discord.ui.View):
    """Подтверждение полного сброса прогресса (только для вызвавшего)."""

    def __init__(self, *, plan, problems, invoker_id: int) -> None:
        super().__init__(timeout=300)
        self.plan = plan
        self.problems = problems
        self.invoker_id = invoker_id
        self.done = False

    async def _check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Это подтверждение доступно только вызвавшему команду.",
                ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Подтвердить сброс", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check(interaction):
            return
        if self.done:
            await interaction.response.send_message(
                "Сброс уже выполнен.", ephemeral=True)
            return
        self.done = True
        for item in self.children:
            item.disabled = True
        await interaction.response.defer(ephemeral=True)

        from storage.db import get_pool
        from services.progress_reset import execute_plan

        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    results = await execute_plan(conn, self.plan)
            summary = "\n".join(f"`{r}`" for r in results[:40])
            if len(results) > 40:
                summary += f"\n…и ещё {len(results) - 40} операций"
            from services.progress_reset import (
                clear_memory_caches, reset_json_files)
            caches = clear_memory_caches()
            json_files = reset_json_files()
            await interaction.followup.send(
                "✅ **Сброс прогресса выполнен** (одна транзакция).\n"
                + summary
                + f"\nJSON-файлы прогресса → *.bak: {len(json_files)}"
                + f"\nIn-memory кэши очищены: {len(caches)}", ephemeral=True)
            logger.warning(
                f"User progress reset executed by {interaction.user.id}")
        except Exception as e:
            logger.exception("progress reset failed")
            await interaction.followup.send(
                f"❌ Ошибка сброса, транзакция откачена: {e}",
                ephemeral=True)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check(interaction):
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="Сброс отменён.", view=self)


class GuideSelectMenu(discord.ui.Select):
    """Выпадающее меню для гайда."""

    def __init__(self, current=None):
        options = [
            discord.SelectOption(
                label=opt.label,
                description=opt.description,
                value=opt.value,
                default=(opt.value == current),
            )
            for opt in _HELP_OPTIONS
        ]
        super().__init__(
            placeholder="Выберите категорию...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        embed = _help_category_embed(self.values[0])
        if embed is None:
            await interaction.response.defer(ephemeral=True)
            return
        view = GuideView(current=self.values[0])
        await interaction.response.send_message(view=compose(embed, view), ephemeral=True)


class GuideView(discord.ui.View):
    """View для гайда с выпадающим меню."""

    def __init__(self, current=None):
        super().__init__(timeout=None)
        self.add_item(GuideSelectMenu(current=current))


class HelpGuideSelectMenu(discord.ui.Select):
    """Выпадающее меню для справки (без главного экрана)."""
    def __init__(self, current=None):
        options = [
            discord.SelectOption(
                label=opt.label,
                description=opt.description,
                value=opt.value,
                default=(opt.value == current),
            )
            for opt in _HELP_OPTIONS
        ]
        super().__init__(
            placeholder="Выберите категорию...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        embed = _help_category_embed(self.values[0])
        if embed is None:
            await interaction.response.defer(ephemeral=True)
            return
        view = HelpGuideView(current=self.values[0])
        await interaction.response.send_message(view=compose(embed, view), ephemeral=True)


class HelpGuideView(discord.ui.View):
    """View для справки с выпадающим меню (без главного экрана)."""

    def __init__(self, current=None):
        super().__init__(timeout=None)
        self.add_item(HelpGuideSelectMenu(current=current))


# Модульный алиас: cogs.ultimatum присоединяет свою группу
# к /tournament через этот объект (тот же Group, что в когe).
tournament_group = TournamentCog.tournament_group


async def setup(bot: TournamentBot) -> None:
    """Загрузить ког."""
    await bot.add_cog(TournamentCog(bot))












