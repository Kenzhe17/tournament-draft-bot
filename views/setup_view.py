"""View для настройки турнира с кнопками выбора круга."""

from __future__ import annotations

from config import replace_emojis, GUILD_ID

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from models.tournament import FormationMode, RegistrationState, Tournament, TournamentPhase, TournamentSize
from storage.json_store import store
from config import get_emoji

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


def has_guild_tag(user: discord.Member) -> bool:
    """Check if user has the guild tag for our server (primary_guild check)."""
    return user.primary_guild and user.primary_guild.id == GUILD_ID


async def _delete_ephemeral_later(interaction: discord.Interaction, delay: float = 4.0) -> None:
    """Удалить ephemeral-ответ через указанное время."""
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


class PlayerSelectView(discord.ui.View):
    """View с пагинацией для выбора игрока."""

    def __init__(self, players: list[str], guild_id: int, action: str, first_player: str = None):
        super().__init__(timeout=None)
        self.players = sorted(players)
        self.guild_id = guild_id
        self.action = action  # "delete", "swap_first", or "swap_second"
        self.first_player = first_player  # For swap_second, the first selected player
        self.page = 1
        self.per_page = 16
        self.total_pages = (len(self.players) + self.per_page - 1) // self.per_page
        self.refresh_view()

    def get_current_players(self) -> list[str]:
        """Получить игроков для текущей страницы."""
        start = (self.page - 1) * self.per_page
        end = start + self.per_page
        return self.players[start:end]

    def refresh_view(self) -> None:
        """Обновить view с новыми кнопками и select menu."""
        self.clear_items()

        # Add select menu with current page players
        current_players = self.get_current_players()
        select = discord.ui.Select(
            placeholder=f"Выберите игрока ({self.page}/{self.total_pages})",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=player, value=player) for player in current_players]
        )
        select.custom_id = f"player_select:{self.action}:{self.guild_id}:{self.page}"
        self.add_item(select)

        # Add pagination buttons if needed
        if self.total_pages > 1:
            # Previous button
            prev_btn = discord.ui.Button(
                label="⬅️",
                style=discord.ButtonStyle.secondary,
                disabled=self.page == 1,
                custom_id=f"player_prev:{self.action}:{self.guild_id}:{self.page}"
            )
            prev_btn.callback = self.prev_callback
            self.add_item(prev_btn)

            # Next button
            next_btn = discord.ui.Button(
                label="➡️",
                style=discord.ButtonStyle.secondary,
                disabled=self.page == self.total_pages,
                custom_id=f"player_next:{self.action}:{self.guild_id}:{self.page}"
            )
            next_btn.callback = self.next_callback
            self.add_item(next_btn)

    async def prev_callback(self, interaction: discord.Interaction) -> None:
        """Обработка кнопки предыдущей страницы."""
        if self.page > 1:
            self.page -= 1
            self.refresh_view()
            await interaction.response.edit_message(view=self)

    async def next_callback(self, interaction: discord.Interaction) -> None:
        """Обработка кнопки следующей страницы."""
        if self.page < self.total_pages:
            self.page += 1
            self.refresh_view()
            await interaction.response.edit_message(view=self)


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
                    replace_emojis("❌ Турнир не в фазе настройки."),
                    ephemeral=True
                )
                return

            # Check if registration is open
            if tournament.registration == RegistrationState.CLOSED:
                # Allow users with guild tag to join if circle has space
                if not has_guild_tag(interaction.user):
                    await interaction.response.send_message(
                        f"{replace_emojis('❌')} Регистрация закрыта. Участники с тегом r!z3 могут входить в круги со свободными местами.",
                        ephemeral=True
                    )
                    return
                
                # Tag holders can only join circles with space
                if self.circle != 4:
                    circle_list = getattr(tournament, f"circle{self.circle}")
                    limit = tournament.circle_limit(self.circle)
                    if len(circle_list) >= limit:
                        await interaction.response.send_message(
                            f"{replace_emojis('❌')} Этот круг заполнен. Участники с тегом r!z3 могут входить только в круги со свободными местами.",
                            ephemeral=True
                        )
                        return

            # Check if circle is full (except circle4)
            if self.circle != 4:
                circle_list = getattr(tournament, f"circle{self.circle}")
                limit = tournament.circle_limit(self.circle)
                if len(circle_list) >= limit:
                    await interaction.response.send_message(
                        f"{replace_emojis('❌')} Круг {self.circle} уже заполнен (максимум {limit} игрока).",
                        ephemeral=True
                    )
                    return

            # Get user's nickname and clean it from emojis
            user_name = interaction.user.display_name
            from utils.cosmetics import clean_nickname
            cleaned_name = clean_nickname(user_name)
            
            # Automatically update user's nickname if it contains emojis
            if cleaned_name != user_name:
                try:
                    await interaction.user.edit(nick=cleaned_name)
                    user_name = cleaned_name
                except discord.Forbidden:
                    # Bot doesn't have permission to edit nickname, use cleaned name anyway
                    user_name = cleaned_name

            # Check if user already in tournament - if so, move them to new circle
            was_moved = False
            if user_name in tournament.all_players:
                # Find which circle they're in
                for circle in range(1, 5):
                    if user_name in getattr(tournament, f"circle{circle}"):
                        if circle == self.circle:
                            await interaction.response.send_message(
                                replace_emojis("❌ Вы уже находитесь в этом круге."),
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
                    replace_emojis("❌ Не удалось добавить игрока."),
                    ephemeral=True
                )
                return

            store.set(tournament)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)

            if was_moved:
                await interaction.response.send_message(
                    f"{replace_emojis('✅')} Вы перемещены в {circle_names[self.circle]}!",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"{replace_emojis('✅')} Вы добавлены в {circle_names[self.circle]}!",
                    ephemeral=True
                )
        except Exception as e:
            logger.error(f"Error in CircleSelectButton callback: {e}", exc_info=True)
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Произошла ошибка при добавлении игрока."),
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
                    replace_emojis("❌ Турнир не в фазе настройки."),
                    ephemeral=True
                )
                return

            # Check if registration is open
            if tournament.registration == RegistrationState.CLOSED:
                # Allow users with guild tag to join if pool has space
                if not has_guild_tag(interaction.user):
                    await interaction.response.send_message(
                        f"{replace_emojis('❌')} Регистрация закрыта. Участники с тегом r!z3 могут входить если есть свободные места.",
                        ephemeral=True
                    )
                    return

            # Check if pool is full
            if len(tournament.players_pool) >= int(tournament.size.value):
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Турнир заполнен (максимум {tournament.size.value} игрока).",
                    ephemeral=True
                )
                return

            # Get user's nickname and clean it from emojis
            user_name = interaction.user.display_name
            from utils.cosmetics import clean_nickname
            cleaned_name = clean_nickname(user_name)
            
            # Automatically update user's nickname if it contains emojis
            if cleaned_name != user_name:
                try:
                    await interaction.user.edit(nick=cleaned_name)
                    user_name = cleaned_name
                except discord.Forbidden:
                    # Bot doesn't have permission to edit nickname, use cleaned name anyway
                    user_name = cleaned_name

            # Check if user already in pool
            if user_name in tournament.players_pool:
                await interaction.response.send_message(
                    replace_emojis("❌ Вы уже участвуете в турнире."),
                    ephemeral=True
                )
                return

            # Add player to pool
            tournament.players_pool.append(user_name)
            tournament.player_user_ids[user_name] = interaction.user.id
            store.set(tournament)

            # Send response
            await interaction.response.send_message(
                replace_emojis("✅ Вы добавлены в турнир!"),
                ephemeral=True
            )

            # Update message using bot from interaction
            bot = interaction.client
            await bot.update_tournament_message(interaction.guild, tournament)
        except Exception as e:
            logger.error(f"Error in JoinPoolButton callback: {e}", exc_info=True)
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Произошла ошибка при добавлении игрока."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            except:
                pass


circle_names = {
    1: "Круг 1",
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
                replace_emojis("❌ Турнир не в фазе настройки."), ephemeral=True)
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        player_name = self.name_input.value.strip()

        # Check if circle is full (except circle4)
        if self.circle != 4:
            circle_list = getattr(tournament, f"circle{self.circle}")
            limit = tournament.circle_limit(self.circle)
            if len(circle_list) >= limit:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Круг {self.circle} уже заполнен (максимум {limit} игрока).",
                    ephemeral=True
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

        # Check if player already in tournament
        if player_name in tournament.all_players:
            await interaction.response.send_message(
                replace_emojis("❌ Этот игрок уже участвует в турнире."),
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Get user_id from Discord member
        user_id = 0
        from utils.cosmetics import clean_nickname
        for member in interaction.guild.members:
            if clean_nickname(member.display_name) == player_name:
                user_id = member.id
                break

        # Add player with user_id
        success = tournament.add_player_to_circle(self.circle, player_name, user_id)
        if not success:
            await interaction.response.send_message(
                replace_emojis("❌ Не удалось добавить игрока."),
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            f"{replace_emojis('✅')} Игрок {player_name} добавлен в {circle_names[self.circle]}!",
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
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        if tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не в фазе настройки."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        user_name = interaction.user.display_name
        from utils.cosmetics import clean_nickname
        cleaned_name = clean_nickname(user_name)
        user_name = cleaned_name

        # Handle different modes
        if tournament.formation_mode == FormationMode.RANDOM:
            # RANDOM mode: check players_pool
            if user_name not in tournament.players_pool:
                try:
                    await interaction.response.send_message(
                        replace_emojis("❌ Вы не участвуете в турнире."),
                        ephemeral=True
                    )
                except discord.NotFound:
                    pass
                return

            # Remove from pool
            tournament.players_pool.remove(user_name)
            if user_name in tournament.player_user_ids:
                del tournament.player_user_ids[user_name]
        else:
            # Other modes: check circles
            if user_name not in tournament.all_players:
                try:
                    await interaction.response.send_message(
                        replace_emojis("❌ Вы не участвуете в турнире."),
                        ephemeral=True
                    )
                except discord.NotFound:
                    pass
                return

            # Remove player
            success = tournament.remove_player(user_name)
            if not success:
                try:
                    await interaction.response.send_message(
                        replace_emojis("❌ Не удалось удалить игрока."),
                        ephemeral=True
                    )
                except discord.NotFound:
                    pass
                return

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        try:
            await interaction.response.send_message(
                replace_emojis("✅ Вы вышли из турнира."),
                ephemeral=True
            )
        except discord.NotFound:
            pass
        asyncio.create_task(_delete_ephemeral_later(interaction))


class DeletePlayerButton(discord.ui.Button):
    """Кнопка для удаления игрока из турнира (только для org)."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="🗑️ Удалить",
            custom_id=f"delete_player:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Только организаторы (роль 'org') могут удалять игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(interaction.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не в фазе настройки."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Get list of all players
        if tournament.formation_mode == FormationMode.RANDOM:
            players = list(tournament.players_pool)
        else:
            players = list(tournament.all_players)

        if not players:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Нет зарегистрированных игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Create select menu with pagination
        view = PlayerSelectView(players, self.guild_id, "delete")
        view.refresh_view()

        # Handle selection
        select = view.children[0]  # The select menu

        async def select_callback(interaction: discord.Interaction):
            await interaction.response.defer()
            player_name = select.values[0]
            
            # Reload tournament from store to get fresh data
            tournament = store.get(self.guild_id)
            if not tournament:
                try:
                    await interaction.followup.send(
                        replace_emojis("❌ Турнир не найден."),
                        ephemeral=True
                    )
                except discord.NotFound:
                    pass
                return

            if tournament.formation_mode == FormationMode.RANDOM:
                if player_name not in tournament.players_pool:
                    try:
                        await interaction.followup.send(
                            f"{replace_emojis('❌')} Игрок `{player_name}` не найден.",
                            ephemeral=True
                        )
                    except discord.NotFound:
                        pass
                    return
                tournament.players_pool.remove(player_name)
                if player_name in tournament.player_user_ids:
                    del tournament.player_user_ids[player_name]
            else:
                if not tournament.remove_player(player_name):
                    try:
                        await interaction.followup.send(
                            f"{replace_emojis('❌')} Игрок `{player_name}` не найден.",
                            ephemeral=True
                        )
                    except discord.NotFound:
                        pass
                    return

            store.set(tournament)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)

            try:
                await interaction.followup.send(
                    f"{replace_emojis('✅')} Игрок `{player_name}` удален.",
                    ephemeral=True
                )
            except discord.NotFound:
                pass

        select.callback = select_callback

        try:
            await interaction.response.send_message(
                "Выберите игрока для удаления:",
                view=view,
                ephemeral=True
            )
        except discord.InteractionResponded:
            pass


class SwapPlayersButton(discord.ui.Button):
    """Кнопка для обмена местами игроков в турнире (только для org)."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="🔄 Поменять",
            custom_id=f"swap_players:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Только организаторы (роль 'org') могут менять местами игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(interaction.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не в фазе настройки."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Get list of all players
        if tournament.formation_mode == FormationMode.RANDOM:
            players = list(tournament.players_pool)
        else:
            players = list(tournament.all_players)

        if not players:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Нет зарегистрированных игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Create select menu for first player
        view = PlayerSelectView(players, self.guild_id, "swap_first")
        view.refresh_view()

        select = view.children[0]

        async def select_callback(interaction: discord.Interaction):
            first_player = select.values[0]
            
            # Create second select menu (excluding first player)
            remaining_players = [p for p in players if p != first_player]
            view2 = PlayerSelectView(remaining_players, self.guild_id, "swap_second", first_player)
            view2.refresh_view()

            select2 = view2.children[0]

            async def select2_callback(interaction: discord.Interaction):
                second_player = select2.values[0]
                
                # Reload tournament from store to get fresh data
                tournament = store.get(self.guild_id)
                if not tournament:
                    await interaction.response.send_message(
                        replace_emojis("❌ Турнир не найден."),
                        ephemeral=True
                    )
                    return
                
                # Swap the players
                # Find where both players are
                player1_pos = {}
                player2_pos = {}
                
                for circle in range(1, 5):
                    circle_list = getattr(tournament, f"circle{circle}")
                    if first_player in circle_list:
                        player1_pos[circle] = circle_list.index(first_player)
                    if second_player in circle_list:
                        player2_pos[circle] = circle_list.index(second_player)
                
                if not player1_pos or not player2_pos:
                    await interaction.response.send_message(
                        replace_emojis("❌ Не удалось найти позиции обоих игроков."),
                        ephemeral=True
                    )
                    return
                
                # Swap - temporarily remove both, then re-add at swapped positions
                for circle, idx in player1_pos.items():
                    circle_list = getattr(tournament, f"circle{circle}")
                    del circle_list[idx]
                
                for circle, idx in player2_pos.items():
                    circle_list = getattr(tournament, f"circle{circle}")
                    del circle_list[idx]
                
                # Re-add at swapped positions
                for circle, idx in player1_pos.items():
                    circle_list = getattr(tournament, f"circle{circle}")
                    circle_list.insert(idx, second_player)
                
                for circle, idx in player2_pos.items():
                    circle_list = getattr(tournament, f"circle{circle}")
                    circle_list.insert(idx, first_player)
                
                # Save
                store.set(tournament)
                
                # Update message
                bot: TournamentBot = interaction.client  # type: ignore[assignment]
                await bot.update_tournament_message(interaction.guild, tournament)
                
                await interaction.response.send_message(
                    replace_emojis(f"✅ Игроки `{first_player}` и `{second_player}` успешно поменялись местами!"),
                    ephemeral=True
                )

            select2.callback = select2_callback

            try:
                await interaction.response.send_message(
                    f"Выберите игрока для обмена с `{first_player}`:",
                    view=view2,
                    ephemeral=True
                )
            except discord.InteractionResponded:
                pass

        select.callback = select_callback

        try:
            await interaction.response.send_message(
                "Выберите первого игрока для обмена:",
                view=view,
                ephemeral=True
            )
        except discord.InteractionResponded:
            pass


class MovePlayerButton(discord.ui.Button):
    """Кнопка для перемещения игрока в другой круг (только для org)."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="↕️ Переместить",
            custom_id=f"move_player:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Только организаторы (роль 'org') могут перемещать игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(interaction.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не в фазе настройки."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Get list of all players
        if tournament.formation_mode == FormationMode.RANDOM:
            players = list(tournament.players_pool)
        else:
            players = list(tournament.all_players)

        if not players:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Нет зарегистрированных игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Create select menu for player
        view = PlayerSelectView(players, self.guild_id, "move")
        view.refresh_view()

        select = view.children[0]

        async def select_callback(interaction: discord.Interaction):
            player = select.values[0]
            
            # Create circle select menu
            circle_options = [
                discord.SelectOption(label="Круг 1", value="circle1"),
                discord.SelectOption(label="Круг 2", value="circle2"),
                discord.SelectOption(label="Круг 3", value="circle3"),
                discord.SelectOption(label="Круг 4", value="circle4"),
            ]
            
            circle_select = discord.ui.Select(
                placeholder="Выберите круг для перемещения",
                options=circle_options,
                custom_id="circle_select"
            )
            
            view2 = discord.ui.View()
            view2.add_item(circle_select)
            
            async def circle_callback(interaction: discord.Interaction):
                target_circle = circle_select.values[0]
                
                # Reload tournament from store to get fresh data
                tournament = store.get(self.guild_id)
                if not tournament:
                    await interaction.response.send_message(
                        replace_emojis("❌ Турнир не найден."),
                        ephemeral=True
                    )
                    return
                
                # Remove player from current location (including captains/circle1)
                for circle in range(1, 5):
                    circle_list = getattr(tournament, f"circle{circle}")
                    if player in circle_list:
                        circle_list.remove(player)
                        break
                
                if player in tournament.captains:
                    tournament.captains.remove(player)
                
                # Add to target circle
                circle_list = getattr(tournament, target_circle)
                circle_list.append(player)
                
                # Save
                store.set(tournament)
                
                # Update message
                bot: TournamentBot = interaction.client  # type: ignore[assignment]
                await bot.update_tournament_message(interaction.guild, tournament)
                
                circle_names = {
                    "circle1": "Круг 1",
                    "circle2": "Круг 2",
                    "circle3": "Круг 3",
                    "circle4": "Круг 4",
                }
                
                await interaction.response.send_message(
                    replace_emojis(f"✅ Игрок `{player}` перемещён в {circle_names[target_circle]}!"),
                    ephemeral=True
                )

            circle_select.callback = circle_callback

            try:
                await interaction.response.send_message(
                    f"Выберите круг для перемещения игрока `{player}`:",
                    view=view2,
                    ephemeral=True
                )
            except discord.InteractionResponded:
                pass

        select.callback = select_callback

        try:
            await interaction.response.send_message(
                "Выберите игрока для перемещения:",
                view=view,
                ephemeral=True
            )
        except discord.InteractionResponded:
            pass


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
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Только организаторы (роль 'org') могут добавлять игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Show modal
        modal = AdminAddModal(self.guild_id, self.circle)
        try:
            await interaction.response.send_modal(modal)
        except discord.NotFound:
            pass


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
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Только организаторы (роль 'org') могут распределять игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не в фазе настройки."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        if tournament.formation_mode != FormationMode.ELO:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир создан не в режиме ELO. Используйте /tournament create с параметром formation=elo."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Check if we have enough players
        total_players = len(tournament.all_players)
        required_players = int(tournament.size.value)
        if total_players < required_players:
            try:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Недостаточно игроков для распределения. Нужно {required_players}, есть {total_players}.",
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Distribute by ELO
        await tournament.distribute_by_elo(self.guild_id)
        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        try:
            await interaction.response.send_message(
                replace_emojis("✅ Игроки распределены по кругам на основе ELO!"),
                ephemeral=True
            )
        except discord.NotFound:
            pass


class AutoDistributeAvgButton(discord.ui.Button):
    """Кнопка для автоматического распределения по Skill Rating."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="🎯 Распределить по Skill Rating",
            custom_id=f"auto_distribute_avg:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Только организаторы (роль 'org') могут распределять игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не в фазе настройки."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        if tournament.formation_mode != FormationMode.SKILL:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир создан не в режиме Skill. Используйте /tournament create с параметром formation=skill."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Check if we have enough players
        total_players = len(tournament.all_players)
        required_players = int(tournament.size.value)
        
        if total_players < required_players:
            try:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Недостаточно игроков для распределения. Нужно {required_players}, есть {total_players}.",
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Distribute by Skill Rating
        await tournament.distribute_by_skill_rating(self.guild_id)
        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        try:
            await interaction.response.send_message(
                replace_emojis("✅ Игроки распределены по кругам на основе Skill Rating!"),
                ephemeral=True
            )
        except discord.NotFound:
            pass


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
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Только организаторы (роль 'org') могут запускать турнир."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(self.guild_id)
        if not tournament:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Сначала создайте турнир командой `/tournament`."),
                    ephemeral=True,
                )
            except discord.NotFound:
                pass
            return

        if tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Турнир не в фазе настройки. Текущая фаза: {tournament.phase.value}",
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Check if tournament is ready to start based on formation mode
        if tournament.formation_mode == FormationMode.RANDOM:
            # RANDOM mode: check if players_pool has enough players
            required_players = int(tournament.size.value)
            current_players = len(tournament.players_pool)
            if current_players < required_players:
                try:
                    await interaction.response.send_message(
                        f"{replace_emojis('❌')} Недостаточно игроков. Нужно {required_players}, есть {current_players}.",
                        ephemeral=True
                    )
                except discord.NotFound:
                    pass
                return
        else:
            # ELO/SKILL modes: check circles
            if not tournament.is_setup_complete:
                captain_count = tournament.captain_count
                try:
                    await interaction.response.send_message(
                        f"{replace_emojis('❌')} Турнир заполнен не полностью. Нужно {captain_count} игрока в Капитан, минимум {captain_count} игрока в круге 2, минимум {captain_count} игрока в круге 3 и минимум {captain_count} игрока в круге 4.",
                        ephemeral=True
                    )
                except discord.NotFound:
                    pass
                return

        # Handle different formation modes
        if tournament.formation_mode == FormationMode.RANDOM:
            # Random mode: distribute randomly and skip draft
            tournament.distribute_randomly()
            store.set(tournament)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)

            try:
                await interaction.response.send_message(
                    replace_emojis("🎲 Турнир запущен! Игроки распределены случайно."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
        else:
            # ELO/SKILL modes: shuffle circles and start draft
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
                    draft_message = await interaction.channel.send(f"{replace_emojis('white_arrow')} <@{first_captain_id}> - ваша очередь выбирать!")
                else:
                    draft_message = await interaction.channel.send(f"{replace_emojis('white_arrow')} {first_captain_name} - ваша очередь выбирать!")
                tournament.draft_message_id = draft_message.id
                store.set(tournament)
            
            try:
                await interaction.response.send_message(
                    replace_emojis("🚀 Турнир запущен! Драфт начался."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass


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
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Только организаторы (роль 'org') могут менять регистрацию."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(self.guild_id)
        if not tournament:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Нет активного турнира."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Переключить состояние
        new_state = RegistrationState.CLOSED if tournament.registration == RegistrationState.OPEN else RegistrationState.OPEN
        tournament.registration = new_state
        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        action = "закрыта" if new_state == RegistrationState.CLOSED else "открыта"
        try:
            await interaction.response.send_message(
                f"{replace_emojis('🔒')} Регистрация {action}!",
                ephemeral=True
            )
        except discord.NotFound:
            pass


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
            # ELO/SKILL modes: circle buttons
            circle_counts = tournament.get_circle_counts()

            # Always show all 4 circles with the same buttons
            # The button logic will handle open vs closed registration
            for circle in range(1, 5):
                count = circle_counts[circle]
                limit = tournament.circle_limit(circle) if circle != 4 else 0
                button = CircleSelectButton(tournament.guild_id, circle, circle_names[circle], count, limit)
                self.add_item(button)

        # Add org menu button
        org_menu_button = OrgMenuButton(tournament.guild_id)
        self.add_item(org_menu_button)

        # Add exit button
        exit_button = ExitButton(tournament.guild_id)
        self.add_item(exit_button)


class OrgMenuButton(discord.ui.Button):
    """Кнопка для открытия организаторского меню."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="⚙️ Орг Меню",
            custom_id=f"org_menu:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Только организаторы (роль 'org') могут открывать орг меню."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(interaction.guild_id)
        if not tournament:
            try:
                await interaction.response.send_message(
                    replace_emojis("❌ Нет активного турнира."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Create org menu view
        view = OrgMenuView(tournament)
        
        try:
            await interaction.response.send_message(
                "Организаторское меню:",
                view=view,
                ephemeral=True
            )
        except discord.InteractionResponded:
            pass


class OrgMenuView(discord.ui.View):
    """View с организаторскими кнопками."""

    def __init__(self, tournament: Tournament):
        super().__init__(timeout=None)
        self.tournament = tournament

        # Add all org buttons
        delete_button = DeletePlayerButton(tournament.guild_id)
        self.add_item(delete_button)

        swap_button = SwapPlayersButton(tournament.guild_id)
        self.add_item(swap_button)

        move_button = MovePlayerButton(tournament.guild_id)
        self.add_item(move_button)

        # Add auto-distribute buttons based on formation mode
        if tournament.formation_mode == FormationMode.ELO:
            auto_distribute_button = AutoDistributeButton(tournament.guild_id)
            self.add_item(auto_distribute_button)
        elif tournament.formation_mode == FormationMode.SKILL:
            auto_distribute_avg_button = AutoDistributeAvgButton(tournament.guild_id)
            self.add_item(auto_distribute_avg_button)

        start_button = StartTournamentButton(tournament.guild_id)
        self.add_item(start_button)

        toggle_button = ToggleRegistrationButton(tournament.guild_id, tournament.registration == RegistrationState.OPEN)
        self.add_item(toggle_button)


def build_setup_view(tournament: Tournament) -> SetupView:
    """Создать View для фазы настройки."""
    if tournament.phase != TournamentPhase.SETUP:
        return None
    return SetupView(tournament)
