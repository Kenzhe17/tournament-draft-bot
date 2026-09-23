"""View для настройки турнира с кнопками выбора круга."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from models.tournament import FormationMode, RegistrationState, Tournament, TournamentPhase, TournamentSize
from storage.json_store import store

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


class CircleSelectButton(discord.ui.Button):
    """Кнопка для выбора круга при добавлении игрока."""

    def __init__(self, guild_id: int, circle: int, circle_name: str, count: int = 0, limit: int = 0):
        label = circle_name  # Убрали счётчики из label
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"circle_select:{guild_id}:{circle}",
        )
        self.guild_id = guild_id
        self.circle = circle
        self.circle_name = circle_name

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            tournament = store.get(self.guild_id)
            if not tournament or tournament.phase != TournamentPhase.SETUP:
                await interaction.response.send_message(
                    "❌ Турнир не в фазе настройки.",
                    ephemeral=True
                )
                return

            # Check if registration is open
            if tournament.registration == RegistrationState.CLOSED:
                await interaction.response.send_message(
                    "❌ Регистрация закрыта. Невозможно добавить игроков.",
                    ephemeral=True
                )
                return

            # Check if circle is full (except circle4)
            if self.circle != 4:
                circle_list = getattr(tournament, f"circle{self.circle}")
                limit = tournament.circle_limit(self.circle)
                if len(circle_list) >= limit:
                    await interaction.response.send_message(
                        f"❌ Круг {self.circle} уже заполнен (максимум {limit} игрока).",
                        ephemeral=True
                    )
                    return

            # Get user's nickname
            user_name = interaction.user.display_name

            # Check if user already in tournament - if so, move them to new circle
            was_moved = False
            if user_name in tournament.all_players:
                # Find which circle they're in
                for circle in range(1, 5):
                    if user_name in getattr(tournament, f"circle{circle}"):
                        if circle == self.circle:
                            await interaction.response.send_message(
                                "❌ Вы уже находитесь в этом круге.",
                                ephemeral=True
                            )
                            return
                        # Remove from old circle
                        tournament.remove_player(user_name)
                        was_moved = True
                        break

            # Add player with user_id
            success = tournament.add_player_to_circle(self.circle, user_name, interaction.user.id)
            if not success:
                await interaction.response.send_message(
                    "❌ Не удалось добавить игрока.",
                    ephemeral=True
                )
                return

            store.set(tournament)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)

            if was_moved:
                await interaction.response.send_message(
                    f"✅ Вы перемещены в {circle_names[self.circle]}!",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"✅ Вы добавлены в {circle_names[self.circle]}!",
                    ephemeral=True
                )
        except Exception as e:
            logger.error(f"Error in CircleSelectButton callback: {e}", exc_info=True)
            try:
                await interaction.response.send_message(
                    "❌ Произошла ошибка при добавлении игрока.",
                    ephemeral=True
                )
            except:
                pass


class JoinPoolButton(discord.ui.Button):
    """Кнопка для входа в players_pool в режиме RANDOM."""

    def __init__(self, guild_id: int, current: int, limit: int):
        label = f"Войти ({current}/{limit})"
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"join_pool:{guild_id}",
        )
        self.guild_id = guild_id
        self.current = current
        self.limit = limit

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            tournament = store.get(self.guild_id)
            if not tournament or tournament.phase != TournamentPhase.SETUP:
                await interaction.response.send_message(
                    "❌ Турнир не в фазе настройки.",
                    ephemeral=True
                )
                return

            # Check if registration is open
            if tournament.registration == RegistrationState.CLOSED:
                await interaction.response.send_message(
                    "❌ Регистрация закрыта. Невозможно добавить игроков.",
                    ephemeral=True
                )
                return

            # Check if pool is full
            if len(tournament.players_pool) >= int(tournament.size.value):
                await interaction.response.send_message(
                    f"❌ Турнир заполнен (максимум {tournament.size.value} игрока).",
                    ephemeral=True
                )
                return

            # Get user's nickname
            user_name = interaction.user.display_name

            # Check if user already in pool
            if user_name in tournament.players_pool:
                await interaction.response.send_message(
                    "❌ Вы уже участвуете в турнире.",
                    ephemeral=True
                )
                return

            # Add player to pool
            tournament.players_pool.append(user_name)
            tournament.player_user_ids[user_name] = interaction.user.id
            store.set(tournament)

            # Send response first
            await interaction.response.send_message(
                "✅ Вы добавлены в турнир!",
                ephemeral=True
            )

            # Update message using bot from interaction
            bot = interaction.client
            await bot.update_tournament_message(interaction.guild, tournament)
        except Exception as e:
            logger.error(f"Error in JoinPoolButton callback: {e}", exc_info=True)
            try:
                await interaction.response.send_message(
                    "❌ Произошла ошибка при добавлении игрока.",
                    ephemeral=True
                )
            except:
                pass


circle_names = {
    1: "Капитан",
    2: "Круг 2",
    3: "Круг 3",
    4: "Круг 4",
}


class AdminAddModal(discord.ui.Modal):
    """Модальное окно для админа добавления игрока."""

    def __init__(self, guild_id: int, circle: int):
        super().__init__(title=f"Добавить в {circle_names[circle]}")
        self.guild_id = guild_id
        self.circle = circle

        self.name_input = discord.ui.TextInput(
            label="Имя игрока",
            placeholder="Введите имя игрока",
            required=True,
            max_length=50,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Турнир не в фазе настройки.", ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        player_name = self.name_input.value.strip()

        # Check if circle is full (except circle4)
        if self.circle != 4:
            circle_list = getattr(tournament, f"circle{self.circle}")
            limit = tournament.circle_limit(self.circle)
            if len(circle_list) >= limit:
                await interaction.response.send_message(
                    f"❌ Круг {self.circle} уже заполнен (максимум {limit} игрока).",
                    ephemeral=True
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

        # Check if player already in tournament
        if player_name in tournament.all_players:
            await interaction.response.send_message(
                "❌ Этот игрок уже участвует в турнире.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Get user_id from Discord member
        user_id = 0
        for member in interaction.guild.members:
            if member.display_name == player_name:
                user_id = member.id
                break

        # Add player with user_id
        success = tournament.add_player_to_circle(self.circle, player_name, user_id)
        if not success:
            await interaction.response.send_message(
                "❌ Не удалось добавить игрока.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            f"✅ Игрок {player_name} добавлен в {circle_names[self.circle]}!",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))


class ExitButton(discord.ui.Button):
    """Кнопка для выхода из турнира."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Выйти",
            custom_id=f"exit:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Турнир не найден.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Турнир не в фазе настройки.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        user_name = interaction.user.display_name

        # Handle different modes
        if tournament.formation_mode == FormationMode.RANDOM:
            # RANDOM mode: check players_pool
            if user_name not in tournament.players_pool:
                await interaction.response.send_message(
                    "❌ Вы не участвуете в турнире.",
                    ephemeral=True
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

            # Remove from pool
            tournament.players_pool.remove(user_name)
            if user_name in tournament.player_user_ids:
                del tournament.player_user_ids[user_name]
        else:
            # Other modes: check circles
            if user_name not in tournament.all_players:
                await interaction.response.send_message(
                    "❌ Вы не участвуете в турнире.",
                    ephemeral=True
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

            # Remove player
            success = tournament.remove_player(user_name)
            if not success:
                await interaction.response.send_message(
                    "❌ Не удалось удалить игрока.",
                    ephemeral=True
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            "✅ Вы вышли из турнира.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))


class DeletePlayerButton(discord.ui.Button):
    """Кнопка для удаления игрока из турнира (только для org)."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="🗑️ Удалить игрока",
            custom_id=f"delete_player:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только организаторы (роль 'org') могут удалять игроков.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament = store.get(interaction.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Турнир не в фазе настройки.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Get list of all players
        if tournament.formation_mode == FormationMode.RANDOM:
            players = tournament.players_pool
        else:
            players = tournament.all_players

        if not players:
            await interaction.response.send_message(
                "❌ Нет зарегистрированных игроков.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Create select menu
        select = discord.ui.Select(
            placeholder="Выберите игрока для удаления",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=player, value=player) for player in sorted(players)]
        )

        async def select_callback(interaction: discord.Interaction):
            player_name = select.values[0]

            if tournament.formation_mode == FormationMode.RANDOM:
                if player_name not in tournament.players_pool:
                    await interaction.response.send_message(
                        f"❌ Игрок `{player_name}` не найден.",
                        ephemeral=True
                    )
                    return
                tournament.players_pool.remove(player_name)
                if player_name in tournament.player_user_ids:
                    del tournament.player_user_ids[player_name]
            else:
                if not tournament.remove_player(player_name):
                    await interaction.response.send_message(
                        f"❌ Игрок `{player_name}` не найден.",
                        ephemeral=True
                    )
                    return

            store.set(tournament)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)

            await interaction.response.send_message(
                f"✅ Игрок `{player_name}` удален.",
                ephemeral=True
            )

        select.callback = select_callback

        view = discord.ui.View()
        view.add_item(select)

        await interaction.response.send_message(
            "Выберите игрока для удаления:",
            view=view,
            ephemeral=True
        )


class ReplacePlayerButton(discord.ui.Button):
    """Кнопка для замены игрока в турнире (только для org)."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="🔄 Заменить игрока",
            custom_id=f"replace_player:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только организаторы (роль 'org') могут заменять игроков.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament = store.get(interaction.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Турнир не в фазе настройки.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Get list of all players
        if tournament.formation_mode == FormationMode.RANDOM:
            players = tournament.players_pool
        else:
            players = tournament.all_players

        if not players:
            await interaction.response.send_message(
                "❌ Нет зарегистрированных игроков.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Show modal with user mentions
        modal = ReplacePlayerModal(self.guild_id, players)
        await interaction.response.send_modal(modal)


class ReplacePlayerModal(discord.ui.Modal, title="Заменить игрока"):
    """Модальное окно для замены игрока."""

    def __init__(self, guild_id: int, players: list):
        super().__init__()
        self.guild_id = guild_id
        self.players = players

        self.current_player_input = discord.ui.TextInput(
            label="Текущий игрок (@упоминание или имя)",
            placeholder="@Player или имя",
            required=True,
            max_length=50,
        )
        self.add_item(self.current_player_input)

        self.new_player_input = discord.ui.TextInput(
            label="Новый игрок (@упоминание)",
            placeholder="@NewPlayer",
            required=True,
            max_length=50,
        )
        self.add_item(self.new_player_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Турнир не в фазе настройки.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Handle @mentions - extract display name if it's a mention
        old_name = self.current_player_input.value.strip()
        new_name = self.new_player_input.value.strip()

        # Check if current_player is a mention and extract the name
        if old_name.startswith("<@") and old_name.endswith(">"):
            user_id = int(old_name.strip("<@!>"))
            member = interaction.guild.get_member(user_id)
            if member:
                old_name = member.display_name

        # Check if new_player is a mention and extract the name
        if new_name.startswith("<@") and new_name.endswith(">"):
            user_id = int(new_name.strip("<@!>"))
            member = interaction.guild.get_member(user_id)
            if member:
                new_name = member.display_name
                new_user_id = user_id
            else:
                await interaction.response.send_message(
                    "❌ Новый игрок не найден на сервере.",
                    ephemeral=True
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return
        else:
            # Try to find user by name
            new_user_id = 0
            for member in interaction.guild.members:
                if member.display_name == new_name:
                    new_user_id = member.id
                    break

        # Check if old player exists
        if tournament.formation_mode == FormationMode.RANDOM:
            if old_name not in tournament.players_pool:
                await interaction.response.send_message(
                    f"❌ Игрок `{old_name}` не найден.",
                    ephemeral=True
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

            # Replace in pool
            idx = tournament.players_pool.index(old_name)
            tournament.players_pool[idx] = new_name
            tournament.player_user_ids[new_name] = new_user_id
            if old_name in tournament.player_user_ids:
                del tournament.player_user_ids[old_name]
        else:
            if old_name not in tournament.all_players:
                await interaction.response.send_message(
                    f"❌ Игрок `{old_name}` не найден.",
                    ephemeral=True
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

            # Replace in circles
            for circle in range(1, 5):
                circle_list = getattr(tournament, f"circle{circle}")
                if old_name in circle_list:
                    idx = circle_list.index(old_name)
                    circle_list[idx] = new_name
                    break

            # Update player_user_ids
            tournament.player_user_ids[new_name] = new_user_id
            if old_name in tournament.player_user_ids:
                del tournament.player_user_ids[old_name]

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            f"✅ Игрок `{old_name}` заменен на `{new_name}`.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))


class AdminAddButton(discord.ui.Button):
    """Кнопка для админа добавления игрока в конкретный круг."""

    def __init__(self, guild_id: int, circle: int, circle_name: str):
        label = f"+ {circle_name}"
        super().__init__(
            style=discord.ButtonStyle.success,
            label=label,
            custom_id=f"admin_add:{guild_id}:{circle}",
        )
        self.guild_id = guild_id
        self.circle = circle

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только организаторы (роль 'org') могут добавлять игроков.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Show modal
        modal = AdminAddModal(self.guild_id, self.circle)
        await interaction.response.send_modal(modal)


class AutoDistributeButton(discord.ui.Button):
    """Кнопка для автоматического распределения по ELO."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="🎯 Распределить по ELO",
            custom_id=f"auto_distribute:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только организаторы (роль 'org') могут распределять игроков.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Турнир не в фазе настройки.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if tournament.formation_mode != FormationMode.ELO:
            await interaction.response.send_message(
                "❌ Турнир создан не в режиме ELO. Используйте /tournament create с параметром formation=elo.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Check if we have enough players
        total_players = len(tournament.all_players)
        required_players = int(tournament.size.value)
        if total_players < required_players:
            await interaction.response.send_message(
                f"❌ Недостаточно игроков для распределения. Нужно {required_players}, есть {total_players}.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        await interaction.response.defer()

        # Distribute by ELO
        await tournament.distribute_by_elo(self.guild_id)
        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.followup.send(
            "✅ Игроки распределены по кругам на основе ELO!",
            ephemeral=True
        )


class StartTournamentButton(discord.ui.Button):
    """Кнопка для запуска турнира."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="🚀 Старт",
            custom_id=f"start_tournament:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только организаторы (роль 'org') могут запускать турнир.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament = store.get(self.guild_id)
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

        # Check if tournament is ready to start based on formation mode
        if tournament.formation_mode == FormationMode.RANDOM:
            # RANDOM mode: check if players_pool has enough players
            required_players = int(tournament.size.value)
            current_players = len(tournament.players_pool)
            if current_players < required_players:
                await interaction.response.send_message(
                    f"❌ Недостаточно игроков. Нужно {required_players}, есть {current_players}.",
                    ephemeral=True
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return
        else:
            # MANUAL/ELO modes: check circles
            if not tournament.is_setup_complete:
                captain_count = tournament.captain_count
                msg = f"❌ Турнир заполнен не полностью. Нужно {captain_count} игрока в Капитан, минимум {captain_count} игрока в круге 2, минимум {captain_count} игрока в круге 3 и минимум {captain_count} игрока в круге 4."
                await interaction.response.send_message(msg, ephemeral=True)
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

        await interaction.response.defer()

        # Handle different formation modes
        if tournament.formation_mode == FormationMode.RANDOM:
            # Random mode: distribute randomly and skip draft
            tournament.distribute_randomly()
            store.set(tournament)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)

            await interaction.followup.send(
                "🎲 Турнир запущен! Игроки распределены случайно.",
                ephemeral=True
            )
        else:
            # Manual or ELO mode: shuffle circles and start draft
            tournament.shuffle_circles()
            tournament.start_draft()
            store.set(tournament)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)

            # Send first draft message with captain ping
            first_picker_pos = tournament.current_picker_position()
            if first_picker_pos is not None:
                first_captain_name = tournament.captains[tournament.captain_order[first_picker_pos]]
                first_captain_id = tournament.player_user_ids.get(first_captain_name, 0)
                if first_captain_id > 0:
                    draft_message = await interaction.channel.send(f"➡️ <@{first_captain_id}> - ваша очередь выбирать!")
                else:
                    draft_message = await interaction.channel.send(f"➡️ {first_captain_name} - ваша очередь выбирать!")
                tournament.draft_message_id = draft_message.id
                store.set(tournament)

            await interaction.followup.send(
                "🎲 Драфт запущен! Игроки перераспределены в кругах.",
                ephemeral=True
            )


class ToggleRegistrationButton(discord.ui.Button):
    """Кнопка для переключения регистрации (открыть/закрыть)."""

    def __init__(self, guild_id: int, is_open: bool):
        self.is_open = is_open
        label = "🔒 Закрыть" if is_open else "🔓 Открыть"
        style = discord.ButtonStyle.danger if is_open else discord.ButtonStyle.primary
        super().__init__(
            style=style,
            label=label,
            custom_id=f"toggle_registration:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только организаторы (роль 'org') могут менять регистрацию.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Нет активного турнира.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Переключить состояние
        new_state = RegistrationState.CLOSED if tournament.registration == RegistrationState.OPEN else RegistrationState.OPEN
        tournament.registration = new_state
        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        action = "закрыта" if new_state == RegistrationState.CLOSED else "открыта"
        await interaction.response.send_message(
            f"🔒 Регистрация {action}!",
            ephemeral=True
        )


class SetupView(discord.ui.View):
    """View с кнопками выбора круга для добавления игроков."""

    def __init__(self, tournament: Tournament):
        super().__init__(timeout=None)
        self.tournament = tournament
        self.registration_state = tournament.registration

        # Show different buttons based on formation mode
        if tournament.formation_mode == FormationMode.RANDOM:
            # RANDOM mode: single join button with counter
            current = len(tournament.players_pool)
            limit = int(tournament.size.value)
            join_button = JoinPoolButton(tournament.guild_id, current, limit)
            self.add_item(join_button)
        else:
            # MANUAL/ELO modes: circle buttons
            circle_counts = tournament.get_circle_counts()

            # Always show all 4 circles with the same buttons
            # The button logic will handle open vs closed registration
            for circle in range(1, 5):
                count = circle_counts[circle]
                limit = tournament.circle_limit(circle) if circle != 4 else 0
                button = CircleSelectButton(tournament.guild_id, circle, circle_names[circle], count, limit)
                self.add_item(button)

            # Add auto-distribute button if in ELO mode
            if tournament.formation_mode == FormationMode.ELO:
                auto_distribute_button = AutoDistributeButton(tournament.guild_id)
                self.add_item(auto_distribute_button)

        # Add management buttons (Start, Toggle Registration)
        start_button = StartTournamentButton(tournament.guild_id)
        self.add_item(start_button)

        toggle_button = ToggleRegistrationButton(tournament.guild_id, tournament.registration == RegistrationState.OPEN)
        self.add_item(toggle_button)

        # Add org-only buttons
        delete_button = DeletePlayerButton(tournament.guild_id)
        self.add_item(delete_button)

        replace_button = ReplacePlayerButton(tournament.guild_id)
        self.add_item(replace_button)

        # Add exit button
        exit_button = ExitButton(tournament.guild_id)
        self.add_item(exit_button)


def build_setup_view(tournament: Tournament) -> SetupView:
    """Создать View для фазы настройки."""
    if tournament.phase != TournamentPhase.SETUP:
        return None
    return SetupView(tournament)
