"""View для настройки турнира с кнопками выбора круга."""

from __future__ import annotations

from config import replace_emojis, GUILD_ID

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from utils.design import compose, text_card, smart_view

from discord import app_commands
from discord.ext import commands

from models.tournament import FormationMode, RegistrationState, Tournament, TournamentPhase, TournamentSize
from storage.json_store import store
from config import get_emoji

if TYPE_CHECKING:
    from bot import TournamentBot


async def safe_send_message(interaction: discord.Interaction, content: str, ephemeral: bool = True) -> None:
    """Safely send a message, handling rate limits and expired interactions."""
    try:
        await interaction.response.send_message(content, ephemeral=ephemeral)
    except discord.NotFound:
        # Interaction expired (rate limit), try followup
        try:
            await interaction.followup.send(content, ephemeral=ephemeral)
        except:
            pass
    except Exception:
        pass

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

    def __init__(self, players: list[tuple[str, str]], guild_id: int, channel_id: int, action: str, first_player: str = None):
        super().__init__(timeout=180)  # 3 minutes timeout instead of None
        # players: [(label, value)] — value это user_id (строкой)
        # или 'n:<имя>' для записей без Discord ID.
        self.players = sorted(players, key=lambda e: e[0].lower())
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.action = action  # "delete", "swap_first", or "swap_second"
        self.first_player = first_player  # For swap_second, the first selected player
        self.page = 1
        self.per_page = 16
        self.total_pages = (len(self.players) + self.per_page - 1) // self.per_page
        self.selected_callback = None  # Will be set by parent
        self.refresh_view()

    def set_callback(self, callback):
        """Set the callback for when a player is selected."""
        self.selected_callback = callback

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
            options=[
                discord.SelectOption(label=label[:100], value=value)
                for label, value in current_players
            ]
        )
        select.custom_id = f"player_select:{self.action}:{self.guild_id}:{self.channel_id}:{self.page}"

        # Set the callback to call the selected_callback with the chosen player
        async def select_callback(interaction: discord.Interaction):
            if self.selected_callback:
                await self.selected_callback(interaction, select.values[0])

        select.callback = select_callback
        self.add_item(select)

        # Add pagination buttons if needed
        if self.total_pages > 1:
            # Previous button
            prev_btn = discord.ui.Button(
                label="Назад",
                style=discord.ButtonStyle.primary,
                disabled=self.page == 1,
                custom_id=f"player_prev:{self.action}:{self.guild_id}:{self.channel_id}:{self.page}"
            )
            prev_btn.callback = self.prev_callback
            self.add_item(prev_btn)

            # Next button
            next_btn = discord.ui.Button(
                label="Вперёд",
                style=discord.ButtonStyle.primary,
                disabled=self.page == self.total_pages,
                custom_id=f"player_next:{self.action}:{self.guild_id}:{self.channel_id}:{self.page}"
            )
            next_btn.callback = self.next_callback
            self.add_item(next_btn)

    async def prev_callback(self, interaction: discord.Interaction) -> None:
        """Обработка кнопки предыдущей страницы."""
        if self.page > 1:
            self.page -= 1
            self.refresh_view()
            await interaction.response.edit_message(view=smart_view(interaction.message, self))

    async def next_callback(self, interaction: discord.Interaction) -> None:
        """Обработка кнопки следующей страницы."""
        if self.page < self.total_pages:
            self.page += 1
            self.refresh_view()
            await interaction.response.edit_message(view=smart_view(interaction.message, self))


class CircleSelectButton(discord.ui.Button):
    """Кнопка для выбора круга при добавлении игрока."""

    def __init__(self, guild_id: int, channel_id: int, circle: int, circle_name: str, count: int = 0, limit: int = 0):
        label = circle_name  # Убрали счётчики из label
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"circle_select:{guild_id}:{channel_id}:{circle}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.circle = circle
        self.circle_name = circle_name

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament or tournament.phase != TournamentPhase.SETUP:
                await interaction.response.send_message(
                    replace_emojis("Турнир не в фазе настройки."),
                    ephemeral=True
                )
                return

            from services import vac_service
            vac = await vac_service.check_vac(self.guild_id, interaction.user.id)
            if vac is not None:
                ts = int(vac["expires_at"].timestamp())
                await interaction.response.send_message(
                    vac_service.vac_blocked_text(
                        ts, reason=vac.get("reason"), level=vac.get("level")),
                    ephemeral=True,
                )
                return

            # Check if registration is open
            if tournament.registration == RegistrationState.ISOLATED:
                # Isolated mode - nobody can join
                await interaction.response.send_message(
                    f"Турнир изолирован, подождите пока Админ отменить действие.",
                    ephemeral=True
                )
                return
            elif tournament.registration == RegistrationState.CLOSED:
                # Allow users with guild tag to join if circle has space
                if not has_guild_tag(interaction.user):
                    await interaction.response.send_message(
                        f"Регистрация закрыта. Участники с тегом r!z3 могут входить в круги со свободными местами.",
                        ephemeral=True
                    )
                    return
                
                # Tag holders can only join circles with space
                if self.circle != 4:
                    circle_list = getattr(tournament, f"circle{self.circle}")
                    limit = tournament.circle_limit(self.circle)
                    if len(circle_list) >= limit:
                        await interaction.response.send_message(
                            f"Этот круг заполнен. Участники с тегом r!z3 могут входить только в круги со свободными местами.",
                            ephemeral=True
                        )
                        return

            # Check if circle is full (except circle4)
            if self.circle != 4:
                circle_list = getattr(tournament, f"circle{self.circle}")
                limit = tournament.circle_limit(self.circle)
                if len(circle_list) >= limit:
                    await interaction.response.send_message(
                        f"Круг {self.circle} уже заполнен (максимум {limit} игрока).",
                        ephemeral=True
                    )
                    return

            # Get user's nickname and clean it from emojis
            user_name = interaction.user.display_name
            from utils.cosmetics import clean_nickname
            cleaned_name = clean_nickname(user_name)

            # Fallback: if cleaned_name is empty, use username
            if not cleaned_name or cleaned_name.strip() == "":
                cleaned_name = interaction.user.name

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
                                replace_emojis("Вы уже находитесь в этом круге."),
                                ephemeral=True
                            )
                            return
                        # Remove from old circle
                        tournament.remove_player(user_name)
                        was_moved = True
                        break

            # Add player with user_id
            success = tournament.add_player_to_circle(self.circle, user_name, interaction.user.id, store)
            if not success:
                await interaction.response.send_message(
                    replace_emojis("Вы уже участвуете в другом активном турнире."),
                    ephemeral=True
                )
                return

            store.set(tournament)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)

            message = f"Вы перемещены в {circle_names[self.circle]}!" if was_moved else f"Вы добавлены в {circle_names[self.circle]}!"

            await safe_send_message(interaction, message, ephemeral=True)
        except Exception as e:
            logger.error(f"Error in CircleSelectButton callback: {e}", exc_info=True)
            await safe_send_message(interaction, replace_emojis("Произошла ошибка при добавлении игрока."), ephemeral=True)


class JoinPoolButton(discord.ui.Button):
    """Кнопка для входа в players_pool в режиме RANDOM."""

    def __init__(self, guild_id: int, channel_id: int, current: int, limit: int):
        label = f"Войти ({current}/{limit})"
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"join_pool:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.current = current
        self.limit = limit

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament or tournament.phase != TournamentPhase.SETUP:
                await interaction.response.send_message(
                    replace_emojis("Турнир не в фазе настройки."),
                    ephemeral=True
                )
                return

            from services import vac_service
            vac = await vac_service.check_vac(self.guild_id, interaction.user.id)
            if vac is not None:
                ts = int(vac["expires_at"].timestamp())
                await interaction.response.send_message(
                    vac_service.vac_blocked_text(
                        ts, reason=vac.get("reason"), level=vac.get("level")),
                    ephemeral=True,
                )
                return

            # Check if registration is open
            if tournament.registration == RegistrationState.ISOLATED:
                # Isolated mode - nobody can join
                await interaction.response.send_message(
                    f"Турнир изолирован, подождите пока Админ отменить действие.",
                    ephemeral=True
                )
                return
            elif tournament.registration == RegistrationState.CLOSED:
                # Allow users with guild tag to join if pool has space
                if not has_guild_tag(interaction.user):
                    await interaction.response.send_message(
                        f"Регистрация закрыта. Участники с тегом r!z3 могут входить если есть свободные места.",
                        ephemeral=True
                    )
                    return

            # Check if pool is full
            if len(tournament.players_pool) >= int(tournament.size.value):
                await interaction.response.send_message(
                    f"Турнир заполнен (максимум {tournament.size.value} игрока).",
                    ephemeral=True
                )
                return

            # Get user's nickname and clean it from emojis
            user_name = interaction.user.display_name
            from utils.cosmetics import clean_nickname
            cleaned_name = clean_nickname(user_name)

            # Fallback: if cleaned_name is empty, use username
            if not cleaned_name or cleaned_name.strip() == "":
                cleaned_name = interaction.user.name

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
                    replace_emojis("Вы уже участвуете в турнире."),
                    ephemeral=True
                )
                return

            # Check if user is active in another regular tournament
            all_tournaments = store.get_by_guild(self.guild_id)
            for other_tournament in all_tournaments:
                if other_tournament.id != tournament.id:
                    if other_tournament.is_player_active(interaction.user.id):
                        await interaction.response.send_message(
                            replace_emojis("Вы уже участвуете в другом активном турнире."),
                            ephemeral=True
                        )
                        return

            # Add player to pool
            tournament.players_pool.append(user_name)
            tournament.player_user_ids[user_name] = interaction.user.id

            # Start auto-timer if this is first player
            was_first = len(tournament.players_pool) == 1

            store.set(tournament)

            # Update message using bot from interaction
            bot = interaction.client
            await bot.update_tournament_message(interaction.guild, tournament)

            # Send response
            await safe_send_message(interaction, replace_emojis("Вы добавлены в турнир!"), ephemeral=True)
        except Exception as e:
            logger.error(f"Error in JoinPoolButton callback: {e}", exc_info=True)
            await safe_send_message(interaction, replace_emojis("Произошла ошибка при добавлении игрока."), ephemeral=True)


circle_names = {
    1: "Круг 1",
    2: "Круг 2",
    3: "Круг 3",
    4: "Круг 4",
}


# ---------------------------------------------------------------------------
# Идентификация игроков по Discord user_id
#
# Модель турнира хранит отображаемые имена в кругах, а user_id — в
# player_user_ids. Все org-операции (добавить/удалить/обменять/
# переместить) оперируют токеном=user_id, чтобы одинаковые
# display_name разных пользователей не смешивались.
# ---------------------------------------------------------------------------


def _name_for_uid(tournament: Tournament, user_id: int) -> str | None:
    """Отображаемое имя игрока по Discord user_id."""
    for name, uid in tournament.player_user_ids.items():
        if uid == user_id:
            return name
    return None


def _find_entry_by_uid(
    tournament: Tournament, user_id: int
) -> tuple[str | None, int | str | None]:
    """Запись игрока по user_id → (stored_name, location).

    location: 1-4 — номер круга, 'pool' — players_pool,
    None — имя есть в player_user_ids, но не найдено в кругах.
    Возвращает (None, None) если игрока нет в турнире.
    """
    name = _name_for_uid(tournament, user_id)
    if name is None:
        return None, None
    for circle in range(1, 5):
        if name in tournament.circle_list(circle):
            return name, circle
    if name in tournament.players_pool:
        return name, "pool"
    return name, None


def _unique_player_name(
    tournament: Tournament, base: str, user_id: int
) -> str:
    """Свободное отображаемое имя для user_id.

    Если display_name уже занят ДРУГИМ пользователем, добавляем
    короткий числовой суффикс — идентичность остаётся по user_id.
    """
    base = (base or "").strip() or str(user_id)
    taken = tournament.all_players | set(tournament.players_pool)
    if base not in taken:
        return base
    if tournament.player_user_ids.get(base, 0) == user_id:
        return base
    tag = f"·{user_id % 10000:04d}"
    candidate = f"{base} {tag}"
    n = 2
    while candidate in taken:
        candidate = f"{base} {tag}-{n}"
        n += 1
    return candidate


def _player_select_entries(tournament: Tournament) -> list[tuple[str, str]]:
    """(label, value) для PlayerSelectView.

    value = str(user_id) для записей с Discord ID, иначе 'n:<имя>'.
    """
    if tournament.formation_mode in (
        FormationMode.RANDOM, FormationMode.BALANCED_SR
    ):
        names = list(tournament.players_pool)
    else:
        names = list(tournament.all_players)
    entries = []
    for name in names:
        uid = tournament.player_user_ids.get(name, 0)
        value = str(uid) if uid and uid > 0 else f"n:{name}"
        entries.append((name, value))
    return entries


def _resolve_player_token(
    tournament: Tournament, token: str
) -> str | None:
    """value из PlayerSelectView → имя игрока в турнире."""
    if not token:
        return None
    if token.startswith("n:"):
        return token[2:]
    try:
        uid = int(token)
    except (TypeError, ValueError):
        return token
    return _name_for_uid(tournament, uid)


async def _resolve_discord_user(
    interaction: discord.Interaction, user_id: int
) -> tuple[str, str | None]:
    """Актуальный Discord-ник выбранного пользователя (без изменений)."""
    guild = interaction.guild
    member = guild.get_member(user_id) if guild is not None else None
    if member is None and guild is not None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            member = None
    if member is not None:
        try:
            avatar = str(member.display_avatar.url)
        except Exception:
            avatar = None
        return member.display_name, avatar

    resolved = (interaction.data.get("resolved") or {}).get("users") or {}
    payload = resolved.get(str(user_id)) or {}
    name = (
        payload.get("global_name")
        or payload.get("username")
        or str(user_id)
    )
    avatar_hash = payload.get("avatar")
    avatar = None
    if avatar_hash:
        ext = "gif" if avatar_hash.startswith("a_") else "png"
        avatar = (
            f"https://cdn.discordapp.com/avatars/"
            f"{user_id}/{avatar_hash}.{ext}"
        )
    return name, avatar


class AdminAddUserView(discord.ui.View):
    """Выбор пользователя Discord для добавления в круг (org only).

    После выбора показывается карточка с участником и кнопкой
    «Добавить»; если пользователь уже в другом круге этого турнира —
    предложение перемещения с подтверждением.
    """

    def __init__(self, guild_id: int, channel_id: int, circle: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.circle = circle
        select = discord.ui.UserSelect(
            placeholder="Найдите пользователя",
            min_values=1,
            max_values=1,
            custom_id=(
                f"admin_add_user:{guild_id}:{channel_id}:{circle}"
            ),
        )

        async def _cb(interaction: discord.Interaction, _s=select):
            await self._on_pick(interaction, _s)

        select.callback = _cb
        self.add_item(select)

    def _tournament(self):
        return store.get(self.guild_id, self.channel_id)

    async def _on_pick(
        self, interaction: discord.Interaction, select: discord.ui.UserSelect
    ) -> None:
        from utils.permissions import can_manage_channel_tournament

        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            await interaction.response.send_message(
                replace_emojis(
                    "Только организаторы (роль 'org') могут "
                    "добавлять игроков."
                ),
                ephemeral=True,
            )
            return

        tournament = self._tournament()
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                replace_emojis("Турнир не в фазе настройки."),
                ephemeral=True,
            )
            return

        try:
            user_id = int(select.values[0])
        except (TypeError, ValueError, IndexError):
            await interaction.response.send_message(
                replace_emojis("Некорректный выбор пользователя."),
                ephemeral=True,
            )
            return

        display_name, _avatar_url = await _resolve_discord_user(
            interaction, user_id
        )
        display_name = (display_name or "").strip() or str(user_id)

        existing_name, location = _find_entry_by_uid(tournament, user_id)
        if existing_name is not None:
            if location == self.circle:
                text = (
                    f"**{existing_name}** уже находится в "
                    f"{circle_names[self.circle]}."
                )
                view = None
            elif isinstance(location, int):
                text = (
                    f"**{existing_name}** уже состоит в "
                    f"{circle_names[location]}.\n"
                    f"Переместить в {circle_names[self.circle]}?"
                )
                view = _AdminMoveConfirmView(
                    self.guild_id, self.channel_id, self.circle,
                    user_id, display_name, location,
                )
            else:
                text = f"**{existing_name}** уже участвует в турнире."
                view = None
            await interaction.response.edit_message(
                view=compose(text=text, view=view)
            )
            return

        confirm = _AdminAddConfirmView(
            self.guild_id, self.channel_id, self.circle,
            user_id, display_name,
        )
        await interaction.response.edit_message(
            view=compose(
                text=(
                    f"**{display_name}**\n"
                    f"Добавить в {circle_names[self.circle]}?"
                ),
                view=confirm,
            )
        )


class _AdminAddConfirmView(discord.ui.View):
    """Подтверждение добавления выбранного пользователя в круг."""

    def __init__(
        self, guild_id: int, channel_id: int, circle: int,
        user_id: int, display_name: str,
    ):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.circle = circle
        self.user_id = user_id
        self.display_name = display_name

        add_btn = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Добавить",
            custom_id=(
                f"admin_add_ok:{guild_id}:{channel_id}:{circle}:{user_id}"
            ),
        )
        add_btn.callback = self._on_add
        self.add_item(add_btn)

        cancel_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Отмена",
            custom_id=f"admin_add_cancel:{guild_id}:{channel_id}",
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=compose(text="Добавление отменено.")
        )

    async def _on_add(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament

        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            await interaction.response.send_message(
                replace_emojis(
                    "Только организаторы (роль 'org') могут "
                    "добавлять игроков."
                ),
                ephemeral=True,
            )
            return

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            await interaction.response.edit_message(
                view=compose(
                    text=replace_emojis("Турнир не в фазе настройки.")
                )
            )
            return

        # Повторная регистрация — перепроверяем на свежих данных
        existing_name, location = _find_entry_by_uid(
            tournament, self.user_id
        )
        if existing_name is not None:
            where = (
                circle_names.get(location, "турнире")
                if isinstance(location, int)
                else "турнире"
            )
            await interaction.response.edit_message(
                view=compose(
                    text=f"**{existing_name}** уже участвует ({where})."
                )
            )
            return

        # VAC
        from services import vac_service
        vac = await vac_service.check_vac(self.guild_id, self.user_id)
        if vac is not None:
            await interaction.response.edit_message(
                view=compose(
                    text=vac_service.vac_blocked_text(
                        int(vac["expires_at"].timestamp()),
                        reason=vac.get("reason"),
                        level=vac.get("level"),
                    )
                )
            )
            return

        # Другой активный турнир
        for other in store.get_by_guild(self.guild_id):
            if other.id == tournament.id:
                continue
            if other.is_player_active(self.user_id):
                await interaction.response.edit_message(
                    view=compose(
                        text=replace_emojis(
                            "Этот пользователь уже участвует "
                            "в другом активном турнире."
                        )
                    )
                )
                return

        # Лимит круга (circle_limit сам возвращает inf для
        # неограниченных кругов)
        circle_list = tournament.circle_list(self.circle)
        limit = tournament.circle_limit(self.circle)
        if limit != float("inf") and len(circle_list) >= limit:
            await interaction.response.edit_message(
                view=compose(
                    text=(
                        f"{circle_names[self.circle]} уже "
                        f"заполнен (максимум {limit})."
                    )
                )
            )
            return

        player_name = _unique_player_name(
            tournament, self.display_name, self.user_id
        )
        success = tournament.add_player_to_circle(
            self.circle, player_name, self.user_id, store
        )
        if not success:
            await interaction.response.edit_message(
                view=compose(
                    text=replace_emojis(
                        "Не удалось добавить игрока. Возможно, "
                        "он уже участвует в другом активном турнире."
                    )
                )
            )
            return

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.edit_message(
            view=compose(
                text=(
                    f"**{player_name}** добавлен в "
                    f"{circle_names[self.circle]}."
                )
            )
        )


class _AdminMoveConfirmView(discord.ui.View):
    """Подтверждение перемещения игрока между кругами одного турнира."""

    def __init__(
        self, guild_id: int, channel_id: int, target_circle: int,
        user_id: int, display_name: str, from_circle: int,
    ):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.target_circle = target_circle
        self.user_id = user_id
        self.display_name = display_name
        self.from_circle = from_circle

        move_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="Переместить",
            custom_id=(
                f"admin_move_ok:{guild_id}:{channel_id}:"
                f"{target_circle}:{user_id}"
            ),
        )
        move_btn.callback = self._on_move
        self.add_item(move_btn)

        cancel_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Отмена",
            custom_id=f"admin_move_cancel:{guild_id}:{channel_id}",
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=compose(text="Перемещение отменено.")
        )

    async def _on_move(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament

        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            await interaction.response.send_message(
                replace_emojis(
                    "Только организаторы (роль 'org') могут "
                    "перемещать игроков."
                ),
                ephemeral=True,
            )
            return

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            await interaction.response.edit_message(
                view=compose(
                    text=replace_emojis("Турнир не в фазе настройки.")
                )
            )
            return

        player_name, location = _find_entry_by_uid(
            tournament, self.user_id
        )
        if player_name is None or not isinstance(location, int):
            await interaction.response.edit_message(
                view=compose(
                    text=replace_emojis(
                        "Игрок больше не найден в турнире."
                    )
                )
            )
            return
        if location == self.target_circle:
            await interaction.response.edit_message(
                view=compose(
                    text=(
                        f"**{player_name}** уже находится в "
                        f"{circle_names[self.target_circle]}."
                    )
                )
            )
            return

        # Лимит целевого круга
        target_list = tournament.circle_list(self.target_circle)
        limit = tournament.circle_limit(self.target_circle)
        if limit != float("inf") and len(target_list) >= limit:
            await interaction.response.edit_message(
                view=compose(
                    text=(
                        f"{circle_names[self.target_circle]} "
                        f"уже заполнен (максимум {limit})."
                    )
                )
            )
            return

        # Перемещение по свежим данным: убрать из текущего круга,
        # добавить в целевой. Идентичность — по user_id.
        source_list = tournament.circle_list(location)
        if player_name in source_list:
            source_list.remove(player_name)
            tournament.set_circle_list(location, source_list)
        if player_name in tournament.captains:
            tournament.captains.remove(player_name)

        target_list = tournament.circle_list(self.target_circle)
        target_list.append(player_name)
        tournament.set_circle_list(self.target_circle, target_list)

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.edit_message(
            view=compose(
                text=(
                    f"**{player_name}** перемещён: "
                    f"{circle_names[location]} "
                    f"{circle_names[self.target_circle]}."
                )
            )
        )


class ExitButton(discord.ui.Button):
    """Кнопка для выхода из турнира."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Выйти",
            custom_id=f"exit:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        import logging
        logger = logging.getLogger(__name__)

        tournament = store.get(interaction.guild_id, interaction.channel_id)
        if not tournament:
            try:
                await interaction.response.send_message(
                    replace_emojis("Турнир не найден."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        if tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    replace_emojis("Турнир не в фазе настройки."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        user_name = interaction.user.display_name
        from utils.cosmetics import clean_nickname
        cleaned_name = clean_nickname(user_name)
        user_name = cleaned_name
        user_id = interaction.user.id

        logger.info(f"ExitButton: user '{user_name}' (id={user_id}) attempting to exit tournament {tournament.id} in channel {interaction.channel_id}")

        # Handle different modes
        if tournament.formation_mode in (FormationMode.RANDOM, FormationMode.BALANCED_SR):
            # RANDOM mode: check players_pool
            if user_name not in tournament.players_pool:
                try:
                    await interaction.response.send_message(
                        replace_emojis("Вы не участвуете в турнире."),
                        ephemeral=True
                    )
                except discord.NotFound:
                    pass
                return

            # Remove from pool
            tournament.players_pool.remove(user_name)
            if user_name in tournament.player_user_ids:
                del tournament.player_user_ids[user_name]
            logger.info(f"ExitButton: user '{user_name}' (id={user_id}) removed from players_pool, tournament_id={tournament.id}")
        else:
            # Other modes: check circles
            if user_name not in tournament.all_players:
                try:
                    await interaction.response.send_message(
                        replace_emojis("Вы не участвуете в турнире."),
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
                        replace_emojis("Не удалось удалить игрока."),
                        ephemeral=True
                    )
                except discord.NotFound:
                    pass
                return

            logger.info(f"ExitButton: user '{user_name}' (id={user_id}) successfully removed from tournament {tournament.id}")

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        try:
            await interaction.response.send_message(
                replace_emojis("Вы вышли из турнира."),
                ephemeral=True
            )
        except discord.NotFound:
            pass
        asyncio.create_task(_delete_ephemeral_later(interaction))


class DeletePlayerButton(discord.ui.Button):
    """Кнопка для удаления игрока из турнира (только для org)."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Удалить",
            custom_id=f"delete_player:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament
        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            try:
                await interaction.response.send_message(
                    replace_emojis("Только организаторы (роль 'org') могут удалять игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(interaction.guild_id, interaction.channel_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    replace_emojis("Турнир не в фазе настройки."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Get list of all players
        if tournament.formation_mode in (FormationMode.RANDOM, FormationMode.BALANCED_SR):
            players = list(tournament.players_pool)
        else:
            players = list(tournament.all_players)

        # Debug logging
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"DeletePlayerButton: formation_mode={tournament.formation_mode.value}, players_count={len(players)}, players={players}")

        if not players:
            try:
                await interaction.response.send_message(
                    replace_emojis("Нет зарегистрированных игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Create select menu with pagination (value = Discord user_id)
        view = PlayerSelectView(
            _player_select_entries(tournament),
            self.guild_id, self.channel_id, "delete",
        )

        # Define the callback for player selection
        async def delete_callback(interaction: discord.Interaction, token: str):
            await interaction.response.defer()

            # Reload tournament from store to get fresh data
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                try:
                    await interaction.followup.send(
                        replace_emojis("Турнир не найден."),
                        ephemeral=True
                    )
                except discord.NotFound:
                    pass
                return

            player_name = _resolve_player_token(tournament, token)
            if player_name is None:
                try:
                    await interaction.followup.send(
                        replace_emojis("Игрок не найден."),
                        ephemeral=True
                    )
                except discord.NotFound:
                    pass
                return

            if tournament.formation_mode in (FormationMode.RANDOM, FormationMode.BALANCED_SR):
                if player_name not in tournament.players_pool:
                    try:
                        await interaction.followup.send(
                            f"Игрок `{player_name}` не найден.",
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
                            f"Игрок `{player_name}` не найден.",
                            ephemeral=True
                        )
                    except discord.NotFound:
                        pass
                    return

            store.set(tournament)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)

        view.set_callback(delete_callback)

        try:
            await interaction.response.send_message(
                view=compose(text="Выберите игрока для удаления:", view=view),
                ephemeral=True
            )
        except discord.InteractionResponded:
            pass


class SwapPlayersButton(discord.ui.Button):
    """Кнопка для обмена местами игроков в турнире (только для org)."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Поменять",
            custom_id=f"swap_players:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament
        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            try:
                await interaction.response.send_message(
                    replace_emojis("Только организаторы (роль 'org') могут менять местами игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(interaction.guild_id, interaction.channel_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    replace_emojis("Турнир не в фазе настройки."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Get list of all players
        if tournament.formation_mode in (FormationMode.RANDOM, FormationMode.BALANCED_SR):
            players = list(tournament.players_pool)
        else:
            players = list(tournament.all_players)

        # Debug logging
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"DeletePlayerButton: formation_mode={tournament.formation_mode.value}, players_count={len(players)}, players={players}")

        if not players:
            try:
                await interaction.response.send_message(
                    replace_emojis("Нет зарегистрированных игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Create select menu for first player (value = Discord user_id)
        entries = _player_select_entries(tournament)
        view = PlayerSelectView(entries, self.guild_id, self.channel_id, "swap_first")

        # Define the callback for first player selection
        async def swap_first_callback(interaction: discord.Interaction, token1: str):
            # Create second select menu (excluding first player)
            remaining_players = [e for e in entries if e[1] != token1]
            view2 = PlayerSelectView(remaining_players, self.guild_id, self.channel_id, "swap_second", token1)

            # Define the callback for second player selection
            async def swap_second_callback(interaction: discord.Interaction, token2: str):
                await interaction.response.defer()

                # Reload tournament from store to get fresh data
                tournament = store.get(self.guild_id, self.channel_id)
                if not tournament:
                    await interaction.followup.send(
                        replace_emojis("Турнир не найден."),
                        ephemeral=True
                    )
                    return

                # Resolve tokens → stored names по Discord user_id
                first_player = _resolve_player_token(tournament, token1)
                second_player = _resolve_player_token(tournament, token2)
                if not first_player or not second_player:
                    await interaction.followup.send(
                        replace_emojis("Не удалось найти позиции обоих игроков."),
                        ephemeral=True
                    )
                    return

                # Swap the players — каждый занимает ровно один круг,
                # меняем имена местами по позициям (работает и при
                # нахождении в одном круге).
                pos1 = pos2 = None
                for circle in range(1, 5):
                    circle_list = getattr(tournament, f"circle{circle}")
                    if first_player in circle_list:
                        pos1 = (circle, circle_list.index(first_player))
                    if second_player in circle_list:
                        pos2 = (circle, circle_list.index(second_player))

                if not pos1 or not pos2:
                    await interaction.followup.send(
                        replace_emojis("Не удалось найти позиции обоих игроков."),
                        ephemeral=True
                    )
                    return

                (c1, i1), (c2, i2) = pos1, pos2
                list1 = getattr(tournament, f"circle{c1}")
                list2 = getattr(tournament, f"circle{c2}")
                list1[i1], list2[i2] = second_player, first_player

                # Save
                store.set(tournament)

                # Update message
                bot: TournamentBot = interaction.client  # type: ignore[assignment]
                await bot.update_tournament_message(interaction.guild, tournament)

            view2.set_callback(swap_second_callback)

            first_name = (
                _resolve_player_token(tournament, token1) or token1
            )
            try:
                await interaction.response.send_message(
                    view=compose(text=f"Выберите игрока для обмена с `{first_name}`:", view=view2),
                    ephemeral=True
                )
            except discord.InteractionResponded:
                pass

        view.set_callback(swap_first_callback)

        try:
            await interaction.response.send_message(
                view=compose(text="Выберите первого игрока для обмена:", view=view),
                ephemeral=True
            )
        except discord.InteractionResponded:
            pass


class MovePlayerButton(discord.ui.Button):
    """Кнопка для перемещения игрока в другой круг (только для org)."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Переместить",
            custom_id=f"move_player:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament
        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            try:
                await interaction.response.send_message(
                    replace_emojis("Только организаторы (роль 'org') могут перемещать игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(interaction.guild_id, interaction.channel_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    replace_emojis("Турнир не в фазе настройки."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Get list of all players
        if tournament.formation_mode in (FormationMode.RANDOM, FormationMode.BALANCED_SR):
            players = list(tournament.players_pool)
        else:
            players = list(tournament.all_players)

        # Debug logging
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"DeletePlayerButton: formation_mode={tournament.formation_mode.value}, players_count={len(players)}, players={players}")

        if not players:
            try:
                await interaction.response.send_message(
                    replace_emojis("Нет зарегистрированных игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Create select menu for player (value = Discord user_id)
        entries = _player_select_entries(tournament)
        view = PlayerSelectView(entries, self.guild_id, self.channel_id, "move")

        # Define the callback for player selection
        async def move_callback(interaction: discord.Interaction, token: str):
            selected_player = _resolve_player_token(tournament, token)
            if selected_player is None:
                try:
                    await interaction.response.send_message(
                        replace_emojis("Игрок не найден."),
                        ephemeral=True
                    )
                except discord.NotFound:
                    pass
                return

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
                
                await interaction.response.defer()
                
                # Reload tournament from store to get fresh data
                tournament = store.get(self.guild_id, self.channel_id)
                if not tournament:
                    await interaction.followup.send(
                        replace_emojis("Турнир не найден."),
                        ephemeral=True
                    )
                    return
                
                # Remove player from current location (including captains/circle1)
                from_circle = None
                for circle in range(1, 5):
                    circle_list = getattr(tournament, f"circle{circle}")
                    if selected_player in circle_list:
                        circle_list.remove(selected_player)
                        from_circle = f"Круг {circle}"
                        break
                
                if selected_player in tournament.captains:
                    tournament.captains.remove(selected_player)
                    from_circle = "Капитаны"
                
                # Add to target circle
                circle_list = getattr(tournament, target_circle)
                circle_list.append(selected_player)

                # Save
                store.set(tournament)

                # Update message
                bot: TournamentBot = interaction.client  # type: ignore[assignment]
                await bot.update_tournament_message(interaction.guild, tournament)

            circle_select.callback = circle_callback

            try:
                await interaction.response.send_message(
                    view=compose(text=f"Выберите круг для перемещения игрока `{selected_player}`:", view=view2),
                    ephemeral=True
                )
            except discord.InteractionResponded:
                pass

        view.set_callback(move_callback)

        try:
            await interaction.response.send_message(
                view=compose(text="Выберите игрока для перемещения:", view=view),
                ephemeral=True
            )
        except discord.InteractionResponded:
            pass


class AdminAddButton(discord.ui.Button):
    """Кнопка для админа добавления игрока в конкретный круг."""

    def __init__(self, guild_id: int, channel_id: int, circle: int, circle_name: str):
        label = f"+ {circle_name}"
        super().__init__(
            style=discord.ButtonStyle.success,
            label=label,
            custom_id=f"admin_add:{guild_id}:{channel_id}:{circle}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.circle = circle

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament
        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            try:
                await interaction.response.send_message(
                    replace_emojis("Только организаторы (роль 'org') могут добавлять игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    replace_emojis("Турнир не в фазе настройки."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Ephemeral CV2-карточка с UserSelect — без ручного ввода ника
        view = AdminAddUserView(self.guild_id, self.channel_id, self.circle)
        try:
            await interaction.response.send_message(
                view=compose(
                    text=(
                        "### Добавить участника\n"
                        "-# Выберите пользователя Discord."
                    ),
                    view=view,
                ),
                ephemeral=True,
            )
        except discord.NotFound:
            pass


class AutoDistributeButton(discord.ui.Button):
    """Кнопка для автоматического распределения по ELO."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Распределить по ELO",
            custom_id=f"auto_distribute:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament
        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            try:
                await interaction.response.send_message(
                    replace_emojis("Только организаторы (роль 'org') могут распределять игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    replace_emojis("Турнир не в фазе настройки."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        if tournament.formation_mode != FormationMode.ELO:
            try:
                await interaction.response.send_message(
                    replace_emojis("Турнир создан не в режиме ELO. Используйте /tournament create с параметром formation=elo."),
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
                    f"Недостаточно игроков для распределения. Нужно {required_players}, есть {total_players}.",
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
                replace_emojis("Игроки распределены по кругам на основе ELO!"),
                ephemeral=True
            )
        except discord.NotFound:
            pass


class AutoDistributeAvgButton(discord.ui.Button):
    """Кнопка для автоматического распределения по Skill Rating."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Распределить по Skill Rating",
            custom_id=f"auto_distribute_avg:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament
        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            try:
                await interaction.response.send_message(
                    replace_emojis("Только организаторы (роль 'org') могут распределять игроков."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    replace_emojis("Турнир не в фазе настройки."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        if tournament.formation_mode != FormationMode.SKILL:
            try:
                await interaction.response.send_message(
                    replace_emojis("Турнир создан не в режиме Skill. Используйте /tournament create с параметром formation=skill."),
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
                    f"Недостаточно игроков для распределения. Нужно {required_players}, есть {total_players}.",
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
                replace_emojis("Игроки распределены по кругам на основе Skill Rating!"),
                ephemeral=True
            )
        except discord.NotFound:
            pass


class StartTournamentButton(discord.ui.Button):
    """Кнопка для запуска турнира."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Начать турнир",
            custom_id=f"start_tournament:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament
        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            try:
                await interaction.response.send_message(
                    replace_emojis("Только организаторы (роль 'org') могут запускать турнир."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            try:
                await interaction.response.send_message(
                    replace_emojis("Сначала создайте турнир командой `/tournament`."),
                    ephemeral=True,
                )
            except discord.NotFound:
                pass
            return

        if tournament.phase != TournamentPhase.SETUP:
            try:
                await interaction.response.send_message(
                    f"Турнир не в фазе настройки. Текущая фаза: {tournament.phase.value}",
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # VAC: исключить заблокированных до проверки готовности
        from services import vac_service
        vac_ids = await vac_service.vac_active_ids(
            self.guild_id, tournament.player_user_ids.values()
        )
        if vac_ids:
            removed_names = []
            for name, uid in list(tournament.player_user_ids.items()):
                if uid in vac_ids:
                    tournament.remove_player(name)
                    if name in tournament.players_pool:
                        tournament.players_pool.remove(name)
                        tournament.player_user_ids.pop(name, None)
                    removed_names.append(name)
            if removed_names:
                store.set(tournament)
                await interaction.response.send_message(
                    f"Перед стартом исключены игроки с активным VAC: "
                    + ", ".join(removed_names)
                    + ". Нажмите «Старт» ещё раз.",
                    ephemeral=True,
                )
                bot = interaction.client
                await bot.update_tournament_message(interaction.guild, tournament)
                return

        # Check if tournament is ready to start based on formation mode
        if tournament.formation_mode in (FormationMode.RANDOM, FormationMode.BALANCED_SR):
            # RANDOM mode: check if players_pool has enough players
            required_players = int(tournament.size.value)
            current_players = len(tournament.players_pool)
            if current_players < required_players:
                try:
                    await interaction.response.send_message(
                        f"Недостаточно игроков. Нужно {required_players}, есть {current_players}.",
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
                        f"Турнир заполнен не полностью. Нужно {captain_count} игрока в Капитан, минимум {captain_count} игрока в круге 2, минимум {captain_count} игрока в круге 3 и минимум {captain_count} игрока в круге 4.",
                        ephemeral=True
                    )
                except discord.NotFound:
                    pass
                return

        # r!z3 CLASSIC: без голосований за правила — только
        # Kazakhstan. Сразу формируем команды и запускаем турнир.
        await launch_tournament_formation(
            interaction.guild, interaction.channel, tournament,
            interaction.client, interaction=interaction,
        )

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        launch_text = (
            "Турнир запущен! Команды сбалансированы по Skill Rating."
            if tournament.formation_mode == FormationMode.BALANCED_SR
            else (
                "Турнир запущен! Игроки распределены случайно."
                if tournament.formation_mode == FormationMode.RANDOM
                else "Турнир запущен!"
            )
        )
        try:
            await interaction.response.send_message(
                replace_emojis(launch_text),
                ephemeral=True,
            )
        except discord.NotFound:
            pass


async def launch_tournament_formation(
    guild: discord.Guild,
    channel: discord.TextChannel,
    tournament: "Tournament",
    bot: "TournamentBot",
    interaction: discord.Interaction | None = None,
) -> None:
    """Сформировать команды и запустить турнир после голосования.

    Вызывается кнопкой «Старт» в фазе SETUP.
    (или восстановлением после рестарта). Повторный запуск
    защищён проверкой фазы RULES_VOTING на стороне сервиса.
    """
    # Handle automatic formation modes (random / balanced-sr): команды
    # строятся сразу, драфт пропускается
    if tournament.formation_mode in (FormationMode.RANDOM, FormationMode.BALANCED_SR):
        if tournament.formation_mode == FormationMode.BALANCED_SR:
            error = await tournament.distribute_balanced_sr(tournament.guild_id)
            if error:
                try:
                    await channel.send(error)
                except Exception:
                    pass
                return
        else:
            # Random mode: distribute randomly and skip draft
            tournament.distribute_randomly()
        store.set(tournament)

        # Initialize betting odds for matches
        from storage.bet_store import bet_store
        from utils.embeds import get_team_avg_skill

        if tournament.size == TournamentSize.EIGHT:
            # Final match
            match_id = f"{tournament.id}_final_0"
            team_a_data = tournament.teams[0] if 0 < len(tournament.teams) else {}
            team_b_data = tournament.teams[1] if 1 < len(tournament.teams) else {}
            avg_skill_a = await get_team_avg_skill(team_a_data, tournament)
            avg_skill_b = await get_team_avg_skill(team_b_data, tournament)
            captain_a = team_a_data.get("captain", f"П1")
            captain_b = team_b_data.get("captain", f"П2")
            name_a = tournament.team_names.get(0, captain_a)
            name_b = tournament.team_names.get(1, captain_b)
            await bet_store.initialize_match_odds_db(match_id, name_a, name_b, avg_skill_a, avg_skill_b)
        elif tournament.size == TournamentSize.SIXTEEN:
            # Semifinals + final
            for i, (team_a, team_b) in enumerate(tournament.semifinal_matches):
                match_id = f"{tournament.id}_semifinal_{i}"
                team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
                team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
                avg_skill_a = await get_team_avg_skill(team_a_data, tournament)
                avg_skill_b = await get_team_avg_skill(team_b_data, tournament)
                captain_a = team_a_data.get("captain", f"П{team_a + 1}")
                captain_b = team_b_data.get("captain", f"П{team_b + 1}")
                name_a = tournament.team_names.get(team_a, captain_a)
                name_b = tournament.team_names.get(team_b, captain_b)
                await bet_store.initialize_match_odds_db(match_id, name_a, name_b, avg_skill_a, avg_skill_b)
        elif tournament.size == TournamentSize.THIRTY_TWO:
            # Qualifiers
            for i, (team_a, team_b) in enumerate(tournament.qualifier_matches):
                match_id = f"{tournament.id}_qualifier_{i}"
                team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
                team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
                avg_skill_a = await get_team_avg_skill(team_a_data, tournament)
                avg_skill_b = await get_team_avg_skill(team_b_data, tournament)
                captain_a = team_a_data.get("captain", f"П{team_a + 1}")
                captain_b = team_b_data.get("captain", f"П{team_b + 1}")
                name_a = tournament.team_names.get(team_a, captain_a)
                name_b = tournament.team_names.get(team_b, captain_b)
                await bet_store.initialize_match_odds_db(match_id, name_a, name_b, avg_skill_a, avg_skill_b)

        if bot is not None:
            await bot.update_tournament_message(guild, tournament)

        # Публичных сообщений в канал нет — статус виден
        # в обновлённой карточке турнира.
    else:
        # ELO/SKILL modes: shuffle circles and start draft
        tournament.shuffle_circles()
        tournament.start_draft()
        store.set(tournament)

        if bot is not None:
            await bot.update_tournament_message(guild, tournament)

        # Send first draft message with captain ping
        first_picker_pos = tournament.current_picker_position()
        if first_picker_pos is not None:
            first_captain_name = tournament.captains[tournament.captain_order[first_picker_pos]]
            first_captain_id = tournament.player_user_ids.get(first_captain_name, 0)
            if first_captain_id > 0:
                draft_message = await channel.send(f" <@{first_captain_id}> - ваша очередь выбирать!")
            else:
                draft_message = await channel.send(f" {first_captain_name} - ваша очередь выбирать!")
            tournament.draft_message_id = draft_message.id
            store.set(tournament)


class ToggleRegistrationButton(discord.ui.Button):
    """Кнопка для переключения регистрации (открыть/закрыть)."""

    def __init__(self, guild_id: int, channel_id: int, is_open: bool):
        self.is_open = is_open
        label = "Закрыть набор" if is_open else "Открыть набор"
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            custom_id=f"toggle_registration:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament
        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            try:
                await interaction.response.send_message(
                    replace_emojis("Только организаторы (роль 'org') могут менять регистрацию."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            try:
                await interaction.response.send_message(
                    replace_emojis("Нет активного турнира."),
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

        # Update the org menu with new button state
        new_view = OrgMenuView(tournament)
        action = "закрыта" if new_state == RegistrationState.CLOSED else "открыта"
        try:
            await interaction.response.edit_message( view=compose(text=_org_menu_text(tournament), view=new_view)
            )
        except discord.NotFound:
            pass


class ToggleIsolationButton(discord.ui.Button):
    """Кнопка для переключения изоляции (Включить S/Отключить S)."""

    def __init__(self, guild_id: int, channel_id: int, is_isolated: bool):
        self.is_isolated = is_isolated
        label = "Отключить S" if is_isolated else "Включить S"
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            custom_id=f"toggle_isolation:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament
        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            try:
                await interaction.response.send_message(
                    replace_emojis("Только организаторы (роль 'org') могут менять изоляцию."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            try:
                await interaction.response.send_message(
                    replace_emojis("Нет активного турнира."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        if self.is_isolated:
            # Отключить изоляцию - вернуть предыдущее состояние
            new_state = RegistrationState.OPEN
        else:
            # Включить изоляцию - закрыть для всех
            new_state = RegistrationState.ISOLATED

        tournament.registration = new_state
        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        # Update the org menu with new button state
        new_view = OrgMenuView(tournament)
        action = "отключена" if new_state == RegistrationState.OPEN else "включена"
        try:
            await interaction.response.edit_message( view=compose(text=_org_menu_text(tournament), view=new_view)
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
        if tournament.formation_mode in (FormationMode.RANDOM, FormationMode.BALANCED_SR):
            # RANDOM mode: single join button with counter
            current = len(tournament.players_pool)
            limit = int(tournament.size.value)
            join_button = JoinPoolButton(tournament.guild_id, tournament.channel_id, current, limit)
            self.add_item(join_button)
        else:
            # ELO/SKILL modes: circle buttons
            circle_counts = tournament.get_circle_counts()

            # Always show all 4 circles with the same buttons
            # The button logic will handle open vs closed registration
            for circle in range(1, 5):
                count = circle_counts[circle]
                limit = tournament.circle_limit(circle) if circle != 4 else 0
                button = CircleSelectButton(tournament.guild_id, tournament.channel_id, circle, circle_names[circle], count, limit)
                self.add_item(button)

        # Add org menu button
        org_menu_button = OrgMenuButton(tournament.guild_id, tournament.channel_id)
        self.add_item(org_menu_button)

        # Add exit button
        exit_button = ExitButton(tournament.guild_id, tournament.channel_id)
        self.add_item(exit_button)


class OrgMenuButton(discord.ui.Button):
    """Кнопка для открытия организаторского меню."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Орг Меню",
            custom_id=f"org_menu:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament

        # Проверка прав на турнир этого канала (включает владельца бота)
        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            try:
                await interaction.response.send_message(
                    "Только владелец бота или организатор могут открывать орг меню.",
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(interaction.guild_id, interaction.channel_id)
        if not tournament:
            try:
                await interaction.response.send_message(
                    replace_emojis("Нет активного турнира."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Create org menu view
        view = OrgMenuView(tournament)

        try:
            await interaction.response.send_message(
                view=compose(text=_org_menu_text(tournament), view=view),
                ephemeral=True
            )
        except discord.InteractionResponded:
            pass


def _org_menu_text(tournament: Tournament) -> str:
    """Сводка карточки 'УПРАВЛЕНИЕ ТУРНИРОМ'."""
    size = int(tournament.size.value)
    if tournament.formation_mode in (
        FormationMode.ELO, FormationMode.SKILL, FormationMode.DRAFT
    ):
        total = sum(
            len(getattr(tournament, f"circle{i}", []))
            for i in range(1, 5)
        )
    else:
        total = len(tournament.players_pool)
    reg = {
        RegistrationState.OPEN: "набор открыт",
        RegistrationState.CLOSED: "набор закрыт",
        RegistrationState.ISOLATED: "изолирован",
    }.get(tournament.registration, str(tournament.registration))
    mode = getattr(tournament.formation_mode, "value",
                   str(tournament.formation_mode))
    return (
        "### УПРАВЛЕНИЕ ТУРНИРОМ\n"
        f"- Размер: **{size}** участников\n"
        f"- Участники: **{total}/{size}**\n"
        f"- Режим: **{mode}**\n"
        f"- Набор: {reg}"
    )


class OrgBackButton(discord.ui.Button):
    """Кнопка 'Назад' — вернуть основную карточку орг-меню."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Назад",
            custom_id=f"org_back:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            try:
                await interaction.response.send_message(
                    replace_emojis("Нет активного турнира."),
                    ephemeral=True)
            except discord.NotFound:
                pass
            return
        try:
            await interaction.response.edit_message(
                view=compose(text=_org_menu_text(tournament),
                             view=OrgMenuView(tournament)))
        except discord.NotFound:
            pass


class _OrgSubPanel(discord.ui.View):
    """Организаторская подпанель: контекстные действия + 'Назад'."""

    def __init__(self, tournament: Tournament, *buttons):
        super().__init__(timeout=None)
        self.tournament = tournament
        for b in buttons:
            self.add_item(b)
        self.add_item(OrgBackButton(
            tournament.guild_id, tournament.channel_id))


class OrgPlayersPanel(_OrgSubPanel):
    """Управление участниками: удалить / обменять / переместить / добавить."""

    def __init__(self, tournament: Tournament):
        gid, cid = tournament.guild_id, tournament.channel_id
        buttons = [
            DeletePlayerButton(gid, cid),
            SwapPlayersButton(gid, cid),
            MovePlayerButton(gid, cid),
        ]
        if tournament.formation_mode in (
            FormationMode.ELO, FormationMode.SKILL, FormationMode.DRAFT
        ):
            for circle in range(1, 5):
                buttons.append(AdminAddButton(
                    gid, cid, circle, circle_names[circle]))
        super().__init__(tournament, *buttons)


class OrgSettingsPanel(_OrgSubPanel):
    """Настройки регистрации: набор, изоляция, лимиты кругов."""

    def __init__(self, tournament: Tournament):
        gid, cid = tournament.guild_id, tournament.channel_id
        buttons = []
        if tournament.registration != RegistrationState.ISOLATED:
            buttons.append(ToggleRegistrationButton(
                gid, cid, tournament.registration == RegistrationState.OPEN))
        buttons.append(ToggleIsolationButton(
            gid, cid, tournament.registration == RegistrationState.ISOLATED))
        buttons.append(LimitButton(gid, cid))
        super().__init__(tournament, *buttons)


class OrgAutoPanel(_OrgSubPanel):
    """Автораспределение по ELO/SKILL."""

    def __init__(self, tournament: Tournament):
        gid, cid = tournament.guild_id, tournament.channel_id
        if tournament.formation_mode == FormationMode.ELO:
            button = AutoDistributeButton(gid, cid)
        else:
            button = AutoDistributeAvgButton(gid, cid)
        super().__init__(tournament, button)


class OrgNotifyPanel(_OrgSubPanel):
    """Уведомление о турнире."""

    def __init__(self, tournament: Tournament):
        super().__init__(
            tournament, NotifyButton(
                tournament.guild_id, tournament.channel_id))


class OrgActionSelect(discord.ui.Select):
    """Select 'Выберите действие' — вход в контекстные подпанели."""

    def __init__(self, guild_id: int, channel_id: int,
                 formation_mode):
        options = [
            discord.SelectOption(
                label="Управление игроками",
                value="players",
                description="Удалить, обменять, переместить, добавить"),
        ]
        if formation_mode in (FormationMode.ELO, FormationMode.SKILL):
            options.append(discord.SelectOption(
                label="Автораспределение",
                value="auto",
                description="Распределить участников автоматически"))
        options += [
            discord.SelectOption(
                label="Настройки регистрации",
                value="settings",
                description="Набор, изоляция, лимиты кругов"),
            discord.SelectOption(
                label="Уведомление",
                value="notify",
                description="Отправить анонс турнира"),
        ]
        super().__init__(
            placeholder="Выберите действие",
            options=options,
            custom_id=f"org_action:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            try:
                await interaction.response.send_message(
                    replace_emojis("Нет активного турнира."),
                    ephemeral=True)
            except discord.NotFound:
                pass
            return

        # Права проверяются на сервере при каждом выборе —
        # отозванный доступ прекращается без перезапуска бота.
        if not can_manage_channel_tournament(
            interaction.user, interaction.guild, self.channel_id
        ):
            try:
                await interaction.response.send_message(
                    replace_emojis("У вас нет прав управлять этим турниром."),
                    ephemeral=True)
            except discord.NotFound:
                pass
            return

        value = self.values[0] if self.values else ""
        panel_cls = {
            "players": OrgPlayersPanel,
            "settings": OrgSettingsPanel,
            "notify": OrgNotifyPanel,
        }.get(value)
        if value == "auto" and tournament.formation_mode in (
            FormationMode.ELO, FormationMode.SKILL
        ):
            panel_cls = OrgAutoPanel
        if panel_cls is None:
            try:
                await interaction.response.send_message(
                    replace_emojis("Действие недоступно в текущем режиме."),
                    ephemeral=True)
            except discord.NotFound:
                pass
            return

        titles = {
            "players": "Управление игроками",
            "auto": "Автораспределение",
            "settings": "Настройки регистрации",
            "notify": "Уведомление",
        }
        panel = panel_cls(tournament)
        try:
            await interaction.response.edit_message(
                view=compose(text=f"### {titles[value]}", view=panel))
        except discord.NotFound:
            pass


class OrgMenuView(discord.ui.View):
    """Орг-меню: сводка турнира + Select действий + главные кнопки."""

    def __init__(self, tournament: Tournament):
        super().__init__(timeout=None)
        self.tournament = tournament
        gid, cid = tournament.guild_id, tournament.channel_id

        self.add_item(OrgActionSelect(gid, cid, tournament.formation_mode))

        # Главные действия остаются на основной карточке
        self.add_item(StartTournamentButton(gid, cid))
        if tournament.registration != RegistrationState.ISOLATED:
            self.add_item(ToggleRegistrationButton(
                gid, cid,
                tournament.registration == RegistrationState.OPEN))


class NotifyButton(discord.ui.Button):
    """Кнопка для отправки уведомления о турнире."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Уведомить",
            custom_id=f"notify:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.cooldowns = {}  # {user_id: last_use_timestamp}

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament
        from config import NOTIFICATION_ROLE_ID, NOTIFICATION_CHANNEL_ID
        from datetime import datetime, timedelta

        # Проверка прав на турнир этого канала (включает владельца бота)
        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            try:
                await interaction.response.send_message(
                    "Только владелец бота или организатор могут отправлять уведомления.",
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Reload tournament from store
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            try:
                await interaction.response.send_message(
                    "Турнир не найден.",
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Check cooldown (1 minute from tournament creation)
        now = datetime.now()
        created_at = datetime.fromisoformat(tournament.created_at) if tournament.created_at else now
        time_since_creation = now - created_at

        if time_since_creation < timedelta(minutes=1):
            next_allowed = int((created_at + timedelta(minutes=1)).timestamp())
            try:
                await interaction.response.send_message(
                    f"Подождите <t:{next_allowed}:R> после создания турнира перед отправкой уведомления.",
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Check user-specific cooldown (1 minute between notifications)
        last_use = self.cooldowns.get(interaction.user.id)
        if last_use and (now - last_use) < timedelta(minutes=1):
            next_allowed = int((last_use + timedelta(minutes=1)).timestamp())
            try:
                await interaction.response.send_message(
                    f"Подождите <t:{next_allowed}:R> перед повторным уведомлением.",
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Build notification message based on tournament state
        channel = interaction.guild.get_channel(NOTIFICATION_CHANNEL_ID)
        if not channel:
            try:
                await interaction.response.send_message(
                    "Канал для уведомлений не найден.",
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        role = interaction.guild.get_role(NOTIFICATION_ROLE_ID)
        role_mention = role.mention if role else ""

        # Calculate missing players
        total_players = len(tournament.players_pool)
        total_needed = int(tournament.size.value)

        # For ELO/SKILL modes, count players in circles instead of pool
        if tournament.formation_mode in [FormationMode.ELO, FormationMode.SKILL, FormationMode.DRAFT]:
            total_players = 0
            for circle_num in range(1, 5):
                circle_data = getattr(tournament, f"circle{circle_num}", [])
                total_players += len(circle_data)

        missing = total_needed - total_players

        description = f"**Тип:** {tournament.size.value} игроков / {tournament.formation_mode.value.upper()}\n"
        description += f"**Регистрация** <#{tournament.channel_id}>\n"
        description += f"**Зарегистрировано:** {total_players}/{total_needed}\n"

        if missing > 0:
            description += f"**Не хватает:** {missing} игроков\n"

        # Add circle-specific info for ELO/SKILL modes
        if tournament.formation_mode in [FormationMode.ELO, FormationMode.SKILL, FormationMode.DRAFT]:
            size_value = int(tournament.size.value)
            circle_limits_map = {
                8: {1: 2, 2: 2, 3: 2, 4: 2},
                16: {1: 4, 2: 4, 3: 4, 4: 4},
                32: {1: 8, 2: 8, 3: 8, 4: 8},
            }
            circle_limits = circle_limits_map.get(size_value, {1: 2, 2: 2, 3: 2, 4: 2})

            circles_info = []
            for circle_num in range(1, 5):
                circle_data = getattr(tournament, f"circle{circle_num}", [])
                circle_count = len(circle_data)
                circle_limit = circle_limits.get(circle_num, 0)
                circle_missing = circle_limit - circle_count
                if circle_missing > 0:
                    circles_info.append(f"Круг {circle_num}: {circle_count}/{circle_limit} (нужно {circle_missing})")

            if circles_info:
                description += "\n**Игроки:**\n" + "\n".join(circles_info)

        embed = discord.Embed(
            title="Турнир",
            description=description,
            color=discord.Color.gold()
        )

        try:
            await channel.send( view=compose(embed, text=role_mention), allowed_mentions=discord.AllowedMentions(roles=True))
            await interaction.response.send_message(
                "Уведомление отправлено.",
                ephemeral=True
            )
        except discord.NotFound:
            pass

        # Update cooldown
        self.cooldowns[interaction.user.id] = now


class LimitButton(discord.ui.Button):
    """Кнопка для настройки лимитов кругов."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Лимит",
            custom_id=f"limit:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import can_manage_channel_tournament
        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            try:
                await interaction.response.send_message(
                    replace_emojis("Только организаторы (роль 'org') могут менять лимиты."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            try:
                await interaction.response.send_message(
                    replace_emojis("Нет активного турнира."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Create limit settings view
        view = LimitSettingsView(tournament)
        await interaction.response.send_message(
            view=compose(text="Настройка лимитов кругов:", view=view),
            ephemeral=True
        )


class LimitSettingsView(discord.ui.View):
    """View для настройки лимитов кругов."""

    def __init__(self, tournament: Tournament):
        super().__init__(timeout=None)
        self.tournament = tournament

        # Add limit toggle buttons for circles 2, 3, 4
        for circle in [2, 3, 4]:
            is_enabled = tournament.circle_limits_enabled.get(circle, True)
            button = LimitToggleButton(tournament.guild_id, tournament.channel_id, circle, is_enabled)
            self.add_item(button)


class LimitToggleButton(discord.ui.Button):
    """Кнопка для переключения лимита конкретного круга."""

    def __init__(self, guild_id: int, channel_id: int, circle: int, is_enabled: bool):
        self.circle = circle
        self.is_enabled = is_enabled
        label = f"Круг {circle}: {'Вкл' if is_enabled else 'Выкл'}"
        style = discord.ButtonStyle.primary if is_enabled else discord.ButtonStyle.secondary
        super().__init__(
            style=style,
            label=label,
            custom_id=f"limit_toggle:{guild_id}:{channel_id}:{circle}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            try:
                await interaction.response.send_message(
                    replace_emojis("Нет активного турнира."),
                    ephemeral=True
                )
            except discord.NotFound:
                pass
            return

        # Toggle limit
        new_status = not tournament.circle_limits_enabled.get(self.circle, True)
        tournament.circle_limits_enabled[self.circle] = new_status
        store.set(tournament)

        # Update view with new button state
        new_view = LimitSettingsView(tournament)
        try:
            await interaction.response.edit_message( view=compose(text="Настройка лимитов кругов:", view=new_view)
            )
        except discord.NotFound:
            pass

        # Update tournament message
        from bot import TournamentBot
        bot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)


def build_setup_view(tournament: Tournament) -> SetupView:
    """Создать View для фазы настройки."""
    if tournament.phase != TournamentPhase.SETUP:
        return None
    return SetupView(tournament)
