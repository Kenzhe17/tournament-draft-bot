"""Построение Discord Embed-сообщений для турнира."""

from __future__ import annotations

import discord

from config import replace_emojis
# ДОБАВЛЕНО: TournamentSize в список импорта
from models.tournament import FormationMode, RegistrationState, Tournament, TournamentPhase, TournamentSize
from storage.bet_store import bet_store
from storage.player_stats_store import player_stats_store
from utils.cosmetics import format_player_name


# Helper функции для визуальных улучшений
def get_rarity_color(rarity: str) -> discord.Color:
    """Получить цвет по редкости предмета."""
    colors = {
        "common": discord.Color.light_grey(),
        "rare": discord.Color.blue(),
        "epic": discord.Color.purple(),
        "legendary": discord.Color.gold(),
        "mythic": discord.Color.from_rgb(255, 100, 100)
    }
    return colors.get(rarity, discord.Color.default())


def get_phase_color(phase: TournamentPhase) -> discord.Color:
    """Получить цвет по фазе турнира."""
    colors = {
        TournamentPhase.SETUP: discord.Color.dark_purple(),
        TournamentPhase.DRAFT: discord.Color.dark_blue(),
        TournamentPhase.QUALIFIERS: discord.Color.dark_green(),
        TournamentPhase.SEMIFINALS: discord.Color.dark_orange(),
        TournamentPhase.FINAL: discord.Color.dark_red(),
        TournamentPhase.COMPLETE: discord.Color.gold()
    }
    return colors.get(phase, discord.Color.default())


def create_progress_bar(current: int, total: int, length: int = 10) -> str:
    """Создать текстовый прогресс-бар."""
    if total == 0:
        progress = 0
    else:
        progress = int((current / total) * length)

    filled = "█" * progress
    empty = "░" * (length - progress)
    return f"{filled}{empty} {current}/{total}"


def build_help_category_embed(category: str) -> discord.Embed:
    """Создать embed для категории справки."""
    categories = {
        "tournament": {
            "emoji": "🏆",
            "title": "Турниры",
            "color": discord.Color.dark_purple(),
            "commands": [
                {
                    "name": "/tournament create",
                    "description": "Создать новый турнир",
                    "params": "size (8/16/32), formation (manual/elo/random)",
                    "example": "/tournament create size=16 formation=random",
                    "access": "орг"
                },
                {
                    "name": "/tournament start",
                    "description": "Запустить турнир",
                    "params": "нет",
                    "example": "/tournament start",
                    "access": "орг"
                },
                {
                    "name": "/tournament close",
                    "description": "Закрыть турнир",
                    "params": "нет",
                    "example": "/tournament close",
                    "access": "орг"
                },
                {
                    "name": "/tournament delete",
                    "description": "Удалить турнир",
                    "params": "нет",
                    "example": "/tournament delete",
                    "access": "орг"
                },
                {
                    "name": "/top",
                    "description": "Лидерборды",
                    "params": "type (level/money/elo)",
                    "example": "/top type=level",
                    "access": "все"
                },
                {
                    "name": "/leaderboard",
                    "description": "Лидерборд ELO",
                    "params": "нет",
                    "example": "/leaderboard",
                    "access": "все"
                },
                {
                    "name": "/test",
                    "description": "Тестовый запуск",
                    "params": "нет",
                    "example": "/test",
                    "access": "орг"
                }
            ]
        },
        "economy": {
            "emoji": "💰",
            "title": "Экономика",
            "color": discord.Color.dark_green(),
            "commands": [
                {
                    "name": "/balance",
                    "description": "Показать баланс",
                    "params": "нет",
                    "example": "/balance",
                    "access": "все"
                },
                {
                    "name": "/daily",
                    "description": "Ежедневный бонус",
                    "params": "нет",
                    "example": "/daily",
                    "access": "все"
                },
                {
                    "name": "/moneytop",
                    "description": "Лидерборд по монетам",
                    "params": "page (номер страницы)",
                    "example": "/moneytop page=1",
                    "access": "все"
                },
                {
                    "name": "/bet",
                    "description": "Статистика ставок",
                    "params": "нет",
                    "example": "/bet",
                    "access": "все"
                }
            ]
        },
        "shop": {
            "emoji": replace_emojis("shop"),
            "title": "Магазин",
            "color": discord.Color.dark_gold(),
            "commands": [
                {
                    "name": "/shop",
                    "description": "Магазин косметики и ролей",
                    "params": "нет",
                    "example": "/shop",
                    "access": "все"
                },
                {
                    "name": "/inventory",
                    "description": "Инвентарь",
                    "params": "нет",
                    "example": "/inventory",
                    "access": "все"
                },
                {
                    "name": "/cases",
                    "description": "Кейсы",
                    "params": "нет",
                    "example": "/cases",
                    "access": "все"
                }
            ]
        },
        "profile": {
            "emoji": "👤",
            "title": "Профиль",
            "color": discord.Color.dark_blue(),
            "commands": [
                {
                    "name": "/profile",
                    "description": "Профиль игрока",
                    "params": "player (опционально)",
                    "example": "/profile player=@User",
                    "access": "все"
                },
                {
                    "name": "/rank",
                    "description": "Ранг и прогресс",
                    "params": "нет",
                    "example": "/rank",
                    "access": "все"
                },
                {
                    "name": "/setbio",
                    "description": "Установить описание",
                    "params": "bio (текст)",
                    "example": "/setbio bio=Pro player",
                    "access": "все"
                },
                {
                    "name": "/setavatar",
                    "description": "Установить аватар",
                    "params": "url (ссылка на изображение)",
                    "example": "/setavatar url=https://example.com/image.png",
                    "access": "все"
                }
            ]
        },
        "admin": {
            "emoji": replace_emojis("settings"),
            "title": "Админ",
            "color": discord.Color.dark_red(),
            "commands": [
                {
                    "name": "/edit",
                    "description": "Изменить ELO/монеты",
                    "params": "player, type (elo/coins), value",
                    "example": "/edit player=@User type=elo value=1500",
                    "access": "админ"
                },
                {
                    "name": "/replace",
                    "description": "Заменить игрока",
                    "params": "current_player, new_player",
                    "example": "/replace current_player=Old new_player=New",
                    "access": "орг"
                },
                {
                    "name": "/delete_player",
                    "description": "Удалить игрока",
                    "params": "name",
                    "example": "/delete_player name=PlayerName",
                    "access": "орг"
                },
                {
                    "name": "/limit",
                    "description": "Лимиты кругов",
                    "params": "circle (2/3/4), status (on/off)",
                    "example": "/limit circle=2 status=on",
                    "access": "орг"
                },
                {
                    "name": "/reset_leaderboard",
                    "description": "Сброс лидерборда",
                    "params": "нет",
                    "example": "/reset_leaderboard",
                    "access": "админ"
                }
            ]
        }
    }

    cat_data = categories.get(category, categories["tournament"])
    embed = discord.Embed(
        title=f"{cat_data['emoji']} {cat_data['title']}",
        color=cat_data['color']
    )

    for cmd in cat_data['commands']:
        embed.add_field(
            name=f"**{cmd['name']}**",
            value=f"{cmd['description']}\n"
                   f"📝 Параметры: {cmd['params']}\n"
                   f"💡 Пример: `{cmd['example']}`\n"
                   f"🔒 Доступ: {cmd['access']}",
            inline=False
        )

    return embed


async def get_team_avg_elo(team: dict, tournament: Tournament) -> int:
    """Рассчитать среднее ELO команды."""
    total_elo = 0
    player_count = 0

    for circle in range(1, 5):
        player_name = team.get(f"circle{circle}", "")
        if player_name:
            user_id = tournament.player_user_ids.get(player_name)
            if user_id:
                stats = await player_stats_store.get(tournament.guild_id, user_id)
                if stats:
                    total_elo += stats.elo
                    player_count += 1

    avg_elo = total_elo // player_count if player_count > 0 else 0
    return avg_elo


async def build_player_stats_embed(guild_id: int, user: discord.Member) -> discord.Embed:
    """Создать embed с детальной статистикой игрока."""
    stats = await player_stats_store.get(guild_id, user.id)

    if not stats:
        embed = discord.Embed(
            title=f"📊 Статистика {user.display_name}",
            description="❌ Статистика не найдена. Игрок ещё не участвовал в турнирах.",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        return embed

    # Calculate win rate
    total_matches = stats.wins + stats.losses
    win_rate = (stats.wins / total_matches * 100) if total_matches > 0 else 0

    # Calculate average K/D
    avg_kd = stats.kills / stats.deaths if stats.deaths > 0 else stats.kills

    # Get server average ELO for comparison
    server_avg_elo = await get_server_average_elo(guild_id)
    elo_diff = stats.elo - server_avg_elo
    elo_diff_text = f"+{elo_diff}" if elo_diff > 0 else str(elo_diff)

    embed = discord.Embed(
        title=f"📊 Статистика {user.display_name}",
        color=discord.Color.dark_blue()
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    # Basic stats
    embed.add_field(
        name="🎯 Текущий ELO",
        value=f"{int(stats.elo)} ({elo_diff_text} от среднего)",
        inline=True
    )
    embed.add_field(
        name="⚔️ Матчи",
        value=f"{total_matches}",
        inline=True
    )
    embed.add_field(
        name="🏆 Победы",
        value=f"{stats.wins}",
        inline=True
    )

    # Performance stats
    embed.add_field(
        name="💀 Убийства",
        value=f"{stats.kills}",
        inline=True
    )
    embed.add_field(
        name="☠️ Смерти",
        value=f"{stats.deaths}",
        inline=True
    )
    embed.add_field(
        name="📈 K/D",
        value=f"{avg_kd:.2f}",
        inline=True
    )

    # Win rate
    embed.add_field(
        name="📊 Винрейт",
        value=f"{win_rate:.1f}%",
        inline=False
    )

    # Losses
    embed.add_field(
        name="❌ Поражения",
        value=f"{stats.losses}",
        inline=True
    )

    return embed


async def get_server_average_elo(guild_id: int) -> int:
    """Рассчитать средний ELO по серверу."""
    from storage.db import get_pool

    if not player_stats_store._use_db:
        return 1000  # Default ELO

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT AVG(elo) FROM player_stats WHERE guild_id = $1",
            guild_id
        )
        return int(result) if result else 1000


def _circle_line(players: list[str], elo_dict: dict[str, int] | None = None, tournament: Tournament = None, guild_id: int = None) -> str:
    """Строка игроков круга с ELO или пустой слот."""
    if not players:
        return ""

    player_strings = []
    for player_name in players:
        # Format name with cosmetics if tournament and guild_id provided
        if tournament and guild_id:
            user_id = tournament.player_user_ids.get(player_name)
            if user_id:
                formatted_name = format_player_name(guild_id, user_id, player_name)
            else:
                formatted_name = player_name
        else:
            formatted_name = player_name

        if elo_dict and player_name in elo_dict:
            player_strings.append(f"{formatted_name} ({int(elo_dict[player_name])})")
        else:
            player_strings.append(formatted_name)

    return " ".join(player_strings)


async def _add_betting_section_to_embed(embed: discord.Embed, tournament: Tournament, matches: list[tuple[int, int]], match_type: str) -> None:
    """Добавить секцию ставок в embed."""
    # Check if betting is open for this phase
    is_open = tournament.is_betting_open() and tournament.betting_phase == match_type
    
    if is_open:
        embed.add_field(
            name="━━━━━━━━━━━━━━\n\n💰 СТАВКИ",
            value="� СТАВКИ ОТКРЫТЫ",
            inline=False,
        )
    else:
        embed.add_field(
            name="━━━━━━━━━━━━━━\n\n💰 СТАВКИ",
            value="🔒 СТАВКИ ЗАКРЫТЫ",
            inline=False,
        )


async def _add_teams_block_to_embed(embed: discord.Embed, guild: discord.Guild, tournament: Tournament) -> None:
    """Вспомогательная функция для сквозного отображения команд красивым списком."""
    if not tournament.teams:
        return

    team_emojis = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣", 7: "7️⃣", 8: "8️⃣"}
    teams_text = []

    for i, team in enumerate(tournament.teams):
        captain = team.get("captain", "Unknown")

        # Get team name or default to captain name
        team_name = tournament.team_names.get(i, captain)

        # Собираем игроков из кругов драфта (всегда круги 1-4)
        players = []
        for circle in range(1, 5):
            p_name = team.get(f"circle{circle}", "")
            # Handle both string and tuple cases
            if isinstance(p_name, tuple):
                p_name = p_name[0] if p_name else ""
            if p_name and isinstance(p_name, str):
                # Format name with cosmetics
                user_id = tournament.player_user_ids.get(p_name)
                if user_id:
                    formatted_name = format_player_name(guild.id, user_id, p_name)
                else:
                    formatted_name = p_name
                players.append(formatted_name)

        players_str = ", ".join(players) if players else "*Ожидание игроков...*"
        emoji = team_emojis.get(i + 1, "🎮")

        teams_text.append(
            f"{emoji} **Команда {team_name}**\n"
            f"┣ **Капитан:** {captain}\n"
            f"┗ **Состав:** {players_str}\n"
        )

    # Добавляем блок команд в самый низ текущего Embed
    embed.add_field(name="\u200b", value="\n".join(teams_text), inline=False)


async def build_setup_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed настройки турнира."""
    status_emoji = replace_emojis("white_dot") if tournament.registration == RegistrationState.OPEN else replace_emojis("white_dot")
    formation_text = "ELO" if tournament.formation_mode == FormationMode.ELO else "Ручной" if tournament.formation_mode == FormationMode.MANUAL else "RANDOM"
    status_text = "Открыто" if tournament.registration == RegistrationState.OPEN else "Закрыто"

    # Get organizer info
    organizer_id = tournament.organizer_id
    organizer_mention = f"<@{organizer_id}>" if organizer_id else "Не указан"

    # Build ELO dictionary for all registered players
    elo_dict = {}
    for player_name, user_id in tournament.player_user_ids.items():
        stats = await player_stats_store.get(tournament.guild_id, user_id)
        if stats:
            elo_dict[player_name] = int(stats.elo)

    # Build description based on formation mode
    if tournament.formation_mode == FormationMode.RANDOM:
        # RANDOM mode: show players pool
        current = len(tournament.players_pool)
        limit = int(tournament.size.value)

        # Build player list with ELO
        player_strings = []
        for player_name in tournament.players_pool:
            # Format name with cosmetics
            user_id = tournament.player_user_ids.get(player_name)
            if user_id:
                formatted_name = format_player_name(guild.id, user_id, player_name)
            else:
                formatted_name = player_name

            if player_name in elo_dict:
                player_strings.append(f"{formatted_name} `({int(elo_dict[player_name])} ELO)`")
            else:
                player_strings.append(formatted_name)

        players_text = ", ".join(player_strings) if player_strings else "*"

        description = (
            f"{replace_emojis('white_arrow')} **Организатор:** {organizer_mention}\n\n"
            f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
            f"{replace_emojis('sub_middle')} Размер: {tournament.size.value}\n"
            f"{replace_emojis('sub_middle')} Режим формирования: {formation_text}\n"
            f"{replace_emojis('sub_directory')} Статус регистрации: {status_text}\n\n"
            f"{replace_emojis('white_dot')} **Пул игроков:**\n"
            f"{replace_emojis('white_arrow')} {players_text}\n\n"
            f"{replace_emojis('a_dot_smaller')} Используйте кнопки ниже для регистрации"
        )
    else:
        # MANUAL/ELO modes: show circles
        circle_counts = tournament.get_circle_counts()

        # Build circle sections
        circle_sections = []
        for circle in range(1, 5):
            circle_list = getattr(tournament, f"circle{circle}")
            circle_name = "Капитаны" if circle == 1 else f"Круг {circle}"
            limit = tournament.circle_limit(circle)
            limit_enabled = tournament.circle_limits_enabled.get(circle, True) if circle != 1 else True
            count = circle_counts[circle]

            if circle == 1:
                limit_info = f" ({count}/{limit})"
            elif limit_enabled:
                limit_info = f" ({count}/{limit})"
            else:
                limit_info = f" ({count}/∞)"

            value = _circle_line(circle_list, elo_dict, tournament, guild.id) or "*"
            circle_sections.append(f"{replace_emojis('white_dot')} **{circle_name}{limit_info}:**\n{replace_emojis('white_arrow')} {value}")

        description = (
            f"{replace_emojis('white_arrow')} **Организатор:** {organizer_mention}\n\n"
            f"{replace_emojis('white_dot')} **Информация о турнире:**\n"
            f"{replace_emojis('sub_middle')} Размер: {tournament.size.value}\n"
            f"{replace_emojis('sub_middle')} Режим формирования: {formation_text}\n"
            f"{replace_emojis('sub_directory')} Статус регистрации: {status_text}\n\n"
            + "\n\n".join(circle_sections) + "\n\n"
            f"{replace_emojis('a_dot_smaller')} Используйте кнопки ниже для регистрации или управления капитанами"
        )

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} {replace_emojis('winner')} {tournament.size.value} | {status_text} | {formation_text}",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69)
    )

    return embed


async def build_draft_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed во время драфта."""
    # Get current picker
    current_picker_pos = tournament.current_picker_position()
    current_line = ""
    current_captain_id = 0
    remaining_time = tournament.get_draft_pick_remaining_time()

    if current_picker_pos is not None:
        current_captain_name = tournament.captains[tournament.captain_order[current_picker_pos]]
        current_captain_id = tournament.player_user_ids.get(current_captain_name, 0)
        if current_captain_id > 0:
            current_line = f"{replace_emojis('white_arrow')} **Сейчас выбирает:** <@{current_captain_id}> `(Осталось: {remaining_time} сек)`"
        else:
            current_line = f"{replace_emojis('white_arrow')} **Сейчас выбирает:** {current_captain_name} `(Осталось: {remaining_time} сек)`"
    else:
        current_line = "Драфт завершён"

    # Show next 4 captains in order
    next_captains = []
    if current_picker_pos is not None:
        for i in range(4):
            pos = (current_picker_pos + i) % len(tournament.captain_order)
            captain_name = tournament.captains[tournament.captain_order[pos]]
            captain_id = tournament.player_user_ids.get(captain_name, 0)
            if captain_id > 0:
                next_captains.append(f"<@{captain_id}>")
            else:
                next_captains.append(captain_name)

    # Build picks table
    picks_sections = []
    for circle in range(2, 5):
        lines = []
        # Get pick order for this circle
        from models.tournament import PICK_ORDERS
        circle_orders = PICK_ORDERS.get(tournament.captain_count, {})
        order_data = circle_orders.get(str(circle), {})
        pick_order = order_data.get("order", list(range(tournament.captain_count)))

        for pos in pick_order:
            captain_name = tournament.captains[pos]
            captain_id = tournament.player_user_ids.get(captain_name, 0)
            pick = tournament.picks.get(str(pos), {}).get(str(circle))
            captain_mention = f"<@{captain_id}>" if captain_id > 0 else captain_name

            if circle > tournament.current_circle or not pick:
                lines.append(f"{replace_emojis('white_arrow')} {captain_mention} ➔ `[Ожидание]`")
            elif circle == tournament.current_circle and pick:
                pick_id = tournament.player_user_ids.get(pick, 0)
                pick_mention = f"<@{pick_id}>" if pick_id > 0 else pick
                lines.append(f"{replace_emojis('white_arrow')} {captain_mention} ➔ {pick_mention}")
            elif circle < tournament.current_circle:
                pick_id = tournament.player_user_ids.get(pick, 0)
                pick_mention = f"<@{pick_id}>" if pick_id > 0 else pick
                lines.append(f"{replace_emojis('white_arrow')} {captain_mention} ➔ {pick_mention}")

        status = ""
        if circle == tournament.current_circle:
            status = f" ◄ `[Текущий]`"

        picks_sections.append(f"{replace_emojis('sub_middle')} **Круг {circle}:{status}**\n" + "\n".join(lines))

    # Warning if more than 25 players available
    warning = ""
    key = str(tournament.current_circle)
    available = tournament.available.get(key, [])
    if len(available) > 25:
        warning = f"\n\n⚠️ **Внимание:** В пуле более 25 игроков! Выбор ограничен текущим кругом."

    description = (
        f"{current_line}\n\n"
        f"{replace_emojis('white_dot')} **Очередь выбора:**\n"
        f"{replace_emojis('white_arrow')} {', '.join(next_captains)}\n\n"
        f"{replace_emojis('white_dot')} **Выборы по кругам:**\n"
        + "\n\n".join(picks_sections)
        + warning
        + f"\n\n{replace_emojis('a_dot_smaller')} Сделайте выбор с помощью меню ниже до истечения таймера"
    )

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} ⚔️ Порядок капитанов | Фаза Драфта",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69)
    )

    return embed


async def build_teams_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed с итоговыми командами (deprecated - skipped)."""
    # This function is kept for backward compatibility but shouldn't be used
    # since we skip the TEAMS phase now
    embed = discord.Embed(
        title="🏆 Сформированные Команды",
        color=discord.Color.dark_green(),
    )
    await _add_teams_block_to_embed(embed, guild, tournament)
    return embed


async def build_qualifiers_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed отборочных матчей."""
    # Get organizer info
    organizer_id = tournament.organizer_id
    organizer_mention = f"<@{organizer_id}>" if organizer_id else "Не указан"

    # Build matches section
    matches_section = []
    for i, (team_a, team_b) in enumerate(tournament.qualifier_matches):
        # Get team names or default to captain names
        team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
        team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
        captain_a = team_a_data.get("captain", f"П{team_a + 1}")
        captain_b = team_b_data.get("captain", f"П{team_b + 1}")
        name_a = tournament.team_names.get(team_a, captain_a)
        name_b = tournament.team_names.get(team_b, captain_b)

        # Get average ELO for each team
        avg_elo_a = await get_team_avg_elo(team_a_data, tournament)
        avg_elo_b = await get_team_avg_elo(team_b_data, tournament)

        # Get room info
        room_data = tournament.qualifier_rooms.get(i, {})
        if room_data:
            room_info = f"{replace_emojis('sub_directory')} **Данные комнаты:** ID `{room_data['id']}` | Пароль `{room_data['password']}`"
        else:
            room_info = ""

        matches_section.append(
            f"{replace_emojis('white_arrow')} **Отбор #{i + 1}:**  **{name_a}:** `({int(avg_elo_a)} ELO)` vs  **{name_b}:** `({int(avg_elo_b)} ELO)`\n{room_info}"
        )

    # Build teams section
    teams_section = []
    for team_idx, team_data in enumerate(tournament.teams):
        captain = team_data.get("captain", f"П{team_idx + 1}")
        team_name = tournament.team_names.get(team_idx, captain)
        players = team_data.get("players", [])

        # Use player names instead of mentions
        player_names = [player for player in players if player]

        prefix = replace_emojis("sub_middle") if team_idx < len(tournament.teams) - 1 else replace_emojis("sub_directory")
        teams_section.append(f"{prefix}  **{team_name}:** {replace_emojis('white_arrow')} {captain}, {', '.join(player_names)}")

    # Build betting section
    betting_section = []
    if tournament.is_betting_open() and tournament.betting_phase == "qualifiers":
        for i, (team_a, team_b) in enumerate(tournament.qualifier_matches):
            team_a_name = tournament.team_names.get(team_a, f"П{team_a + 1}")
            team_b_name = tournament.team_names.get(team_b, f"П{team_b + 1}")
            betting_section.append(f"{replace_emojis('white_arrow')} **Отбор #{i + 1}:** П1 `1.85x` | П2 `1.95x`")

    # Build footer
    footer = ""
    if tournament.is_betting_open() and tournament.betting_phase == "qualifiers":
        remaining = tournament.get_betting_remaining_time()
        footer = f"⏳ Прием ставок закрывается через: {remaining // 60:02d}:{remaining % 60:02d}"

    description = (
        f"{replace_emojis('white_arrow')} **Организатор:** {organizer_mention}\n\n"
        f"{replace_emojis('white_dot')} **Отборочные матчи:**\n"
        + "\n\n".join(matches_section)
        + "\n\n"
        f"{replace_emojis('white_dot')} **Участники команд:**\n"
        + "\n".join(teams_section)
    )

    if betting_section:
        description += "\n\n" + f"{replace_emojis('white_dot')} **Ставки на матчи:**\n" + "\n".join(betting_section)

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} {replace_emojis('winner')} ТУРНИРНАЯ СЕТКА — ОТБОР",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69)
    )

    if footer:
        embed.set_footer(text=footer)

    # Add organizer avatar if available
    if organizer_id:
        try:
            organizer_member = guild.get_member(organizer_id)
            if organizer_member:
                embed.set_thumbnail(url=organizer_member.display_avatar.url)
        except:
            pass

    return embed
    return embed


async def build_semifinals_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed полуфиналов."""
    # Get organizer info
    organizer_id = tournament.organizer_id
    organizer_mention = f"<@{organizer_id}>" if organizer_id else "Не указан"

    # Build matches section
    matches_section = []
    for i, (team_a, team_b) in enumerate(tournament.semifinal_matches):
        # Get team names or default to captain names
        team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
        team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
        captain_a = team_a_data.get("captain", f"П{team_a + 1}")
        captain_b = team_b_data.get("captain", f"П{team_b + 1}")
        name_a = tournament.team_names.get(team_a, captain_a)
        name_b = tournament.team_names.get(team_b, captain_b)

        # Get average ELO for each team
        avg_elo_a = await get_team_avg_elo(team_a_data, tournament)
        avg_elo_b = await get_team_avg_elo(team_b_data, tournament)

        # Get room info
        room_data = tournament.semifinal_rooms.get(i, {})
        if room_data:
            room_info = f"{replace_emojis('sub_directory')} **Данные комнаты:** ID `{room_data['id']}` | Пароль `{room_data['password']}`"
        else:
            room_info = ""

        matches_section.append(
            f"{replace_emojis('white_arrow')} **Игра #{i + 1}:** {name_a} `({int(avg_elo_a)} ELO)` vs {name_b} `({int(avg_elo_b)} ELO)`\n{room_info}"
        )

    # Build teams section
    teams_section = []
    for team_idx, team_data in enumerate(tournament.teams):
        captain = team_data.get("captain", f"П{team_idx + 1}")
        team_name = tournament.team_names.get(team_idx, captain)
        players = team_data.get("players", [])

        # Use player names instead of mentions
        player_names = [player for player in players if player]

        prefix = replace_emojis("sub_middle") if team_idx < len(tournament.teams) - 1 else replace_emojis("sub_directory")
        teams_section.append(f"{prefix} **{team_name}:** {replace_emojis('white_arrow')} {captain}, {', '.join(player_names)}")

    # Build betting section
    betting_section = []
    if tournament.is_betting_open() and tournament.betting_phase == "semifinals":
        for i, (team_a, team_b) in enumerate(tournament.semifinal_matches):
            team_a_name = tournament.team_names.get(team_a, f"П{team_a + 1}")
            team_b_name = tournament.team_names.get(team_b, f"П{team_b + 1}")
            betting_section.append(f"{replace_emojis('white_arrow')} **Игра #{i + 1}:** П1 `1.75x` | П2 `2.05x`")

    # Build footer
    footer = ""
    if tournament.is_betting_open() and tournament.betting_phase == "semifinals":
        remaining = tournament.get_betting_remaining_time()
        footer = f"⏳ Прием ставок закрывается через: {remaining // 60:02d}:{remaining % 60:02d}"

    description = (
        f"{replace_emojis('white_arrow')} **Организатор:** {organizer_mention}\n\n"
        f"{replace_emojis('white_dot')} **Полуфинальные матчи:**\n"
        + "\n\n".join(matches_section)
        + "\n\n"
        f"{replace_emojis('white_dot')} **Участники команд:**\n"
        + "\n".join(teams_section)
    )

    if betting_section:
        description += "\n\n" + f"{replace_emojis('white_dot')} **Ставки на матчи:**\n" + "\n".join(betting_section)

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} {replace_emojis('winner')} ТУРНИРНАЯ СЕТКА — ПОЛУФИНАЛ",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69)
    )

    if footer:
        embed.set_footer(text=footer)

    # Add organizer avatar if available
    if organizer_id:
        try:
            organizer_member = guild.get_member(organizer_id)
            if organizer_member:
                embed.set_thumbnail(url=organizer_member.display_avatar.url)
        except:
            pass

    return embed
    return embed


async def build_final_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed финала."""
    # Get organizer info
    organizer_id = tournament.organizer_id
    organizer_mention = f"<@{organizer_id}>" if organizer_id else "Не указан"

    team_a = tournament.final_teams[0]
    team_b = tournament.final_teams[1]

    # Get team names or default to captain names
    team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
    team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
    captain_a = team_a_data.get("captain", f"П{team_a + 1}")
    captain_b = team_b_data.get("captain", f"П{team_b + 1}")
    name_a = tournament.team_names.get(team_a, captain_a)
    name_b = tournament.team_names.get(team_b, captain_b)

    # Get average ELO for each team
    avg_elo_a = await get_team_avg_elo(team_a_data, tournament)
    avg_elo_b = await get_team_avg_elo(team_b_data, tournament)

    # Get room info
    room_data = tournament.final_room
    if room_data:
        room_info = f"{replace_emojis('sub_directory')} **Данные комнаты:** ID `{room_data['id']}` | Пароль `{room_data['password']}`"
    else:
        room_info = ""

    # Build teams section
    teams_section = []
    for team_idx in [team_a, team_b]:
        team_data = tournament.teams[team_idx] if team_idx < len(tournament.teams) else {}
        captain = team_data.get("captain", f"П{team_idx + 1}")
        team_name = tournament.team_names.get(team_idx, captain)
        players = team_data.get("players", [])

        # Use player names instead of mentions
        player_names = [player for player in players if player]

        prefix = replace_emojis("sub_middle") if team_idx == team_a else replace_emojis("sub_directory")
        teams_section.append(f"{prefix} **{team_name}:** {replace_emojis('white_arrow')} {captain}, {', '.join(player_names)}")

    # Build betting section
    betting_section = []
    if tournament.is_betting_open() and tournament.betting_phase == "final":
        team_a_name = tournament.team_names.get(team_a, f"П{team_a + 1}")
        team_b_name = tournament.team_names.get(team_b, f"П{team_b + 1}")
        betting_section.append(f"{replace_emojis('white_arrow')} **Финал:** П1 `1.90x` | П2 `1.90x`")

    # Build footer
    footer = ""
    if tournament.is_betting_open() and tournament.betting_phase == "final":
        remaining = tournament.get_betting_remaining_time()
        footer = f"⏳ Прием ставок закрывается через: {remaining // 60:02d}:{remaining % 60:02d}"

    description = (
        f"{replace_emojis('white_arrow')} **Организатор:** {organizer_mention}\n\n"
        f"{replace_emojis('white_dot')} **Главная битва:**\n"
        f"{replace_emojis('white_arrow')} **Финал:** {name_a} `({int(avg_elo_a)} ELO)` vs {name_b} `({int(avg_elo_b)} ELO)`\n{room_info}\n\n"
        f"{replace_emojis('white_dot')} **Участники команд:**\n"
        + "\n".join(teams_section)
    )

    if betting_section:
        description += "\n\n" + f"{replace_emojis('white_dot')} **Ставки на матчи:**\n" + "\n".join(betting_section)

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} {replace_emojis('winner')} ТУРНИРНАЯ СЕТКА — ФИНАЛ",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69)
    )

    if footer:
        embed.set_footer(text=footer)

    # Add organizer avatar if available
    if organizer_id:
        try:
            organizer_member = guild.get_member(organizer_id)
            if organizer_member:
                embed.set_thumbnail(url=organizer_member.display_avatar.url)
        except:
            pass

    return embed


async def build_winner_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed победителя турнира."""
    # Get organizer info
    organizer_id = tournament.organizer_id
    organizer_mention = f"<@{organizer_id}>" if organizer_id else "Не указан"

    idx = tournament.winner_team_index
    if idx is None:
        return discord.Embed(
            title=f"{replace_emojis('a_star')} {replace_emojis('winner')} ПОБЕДИТЕЛЬ ТУРНИРА",
            description="Победитель не определен",
            color=discord.Color.from_rgb(69, 69, 69)
        )

    # Get winning team captain name and full roster
    winning_team = tournament.teams[idx] if idx < len(tournament.teams) else {}
    captain_name = winning_team.get("captain", "Unknown")
    team_name = tournament.team_names.get(idx, captain_name)

    # Build full roster string
    players = []
    for circle in range(1, 5):
        p_name = winning_team.get(f"circle{circle}", "")
        if p_name:
            # Format name with cosmetics
            user_id = tournament.player_user_ids.get(p_name)
            if user_id:
                formatted_name = format_player_name(guild.id, user_id, p_name)
            else:
                formatted_name = p_name
            players.append(formatted_name)
    roster_str = ", ".join(players) if players else "Нет игроков"

    # Get captain name (no mention)
    captain_name = winning_team.get("captain", f"П{idx + 1}")

    # Calculate real tournament statistics
    total_kills = 0
    total_deaths = 0
    total_matches = 0
    total_rounds = 0
    best_kd_player = ""
    best_kd = 0
    best_kills_player = ""
    best_kills = 0
    total_bet_pool = 0

    # Get all player stats from this tournament
    from storage.player_stats_store import player_stats_store
    from storage.bet_store import bet_store

    # Calculate from tournament match stats
    for match_idx in range(len(tournament.qualifier_matches)):
        match_stats = tournament.qualifier_match_stats.get(match_idx, {})
        if match_stats:
            total_matches += 1
            for player_name, stat in match_stats.items():
                total_kills += stat.get('kills', 0)
                total_deaths += stat.get('deaths', 0)
                kd = stat.get('kills', 0) / stat.get('deaths', 1) if stat.get('deaths', 0) > 0 else stat.get('kills', 0)
                if kd > best_kd:
                    best_kd = kd
                    best_kd_player = player_name
                if stat.get('kills', 0) > best_kills:
                    best_kills = stat.get('kills', 0)
                    best_kills_player = player_name

    for match_idx in range(len(tournament.semifinal_matches)):
        match_stats = tournament.semifinal_match_stats.get(match_idx, {})
        if match_stats:
            total_matches += 1
            for player_name, stat in match_stats.items():
                total_kills += stat.get('kills', 0)
                total_deaths += stat.get('deaths', 0)
                kd = stat.get('kills', 0) / stat.get('deaths', 1) if stat.get('deaths', 0) > 0 else stat.get('kills', 0)
                if kd > best_kd:
                    best_kd = kd
                    best_kd_player = player_name
                if stat.get('kills', 0) > best_kills:
                    best_kills = stat.get('kills', 0)
                    best_kills_player = player_name

    # Get final match stats
    final_stats = tournament.final_match_stats
    if final_stats:
        total_matches += 1
        for player_name, stat in final_stats.items():
            total_kills += stat.get('kills', 0)
            total_deaths += stat.get('deaths', 0)
            kd = stat.get('kills', 0) / stat.get('deaths', 1) if stat.get('deaths', 0) > 0 else stat.get('kills', 0)
            if kd > best_kd:
                best_kd = kd
                best_kd_player = player_name
            if stat.get('kills', 0) > best_kills:
                best_kills = stat.get('kills', 0)
                best_kills_player = player_name

    # Calculate total bet pool
    for match_type in ["qualifier", "semifinal", "final"]:
        match_count = len(tournament.qualifier_matches) if match_type == "qualifier" else len(tournament.semifinal_matches) if match_type == "semifinal" else 1
        for i in range(match_count):
            match_id = f"{match_type}_{i}"
            bets = await bet_store.get_bets_by_match(match_id)
            for bet in bets:
                total_bet_pool += bet.amount

    # Calculate average K/D
    avg_kd = total_kills / total_deaths if total_deaths > 0 else 0

    description = (
        f"{replace_emojis('white_arrow')} **Организатор:** {organizer_mention}\n\n"
        f"{replace_emojis('white_dot')} **Победитель:**\n"
        f"{replace_emojis('white_arrow')} **{team_name}** — {replace_emojis('winner')} {replace_emojis('white_arrow')} {captain_name}, {roster_str}\n\n"
        f"{replace_emojis('white_dot')} **Статистика турнира:**\n"
        f"{replace_emojis('sub_middle')} **Всего матчей:** {total_matches}\n"
        f"{replace_emojis('sub_middle')} **Всего киллов:** {total_kills}\n"
        f"{replace_emojis('sub_middle')} **Лучший K/D:** {best_kd_player} `({best_kd:.2f})`\n"
        f"{replace_emojis('sub_middle')} **Больше всего киллов:** {best_kills_player} `({best_kills})`\n"
        f"{replace_emojis('sub_middle')} **Средний K/D:** `{avg_kd:.2f}`\n"
        f"{replace_emojis('sub_directory')} **Общий банк ставок:** {total_bet_pool:,} {replace_emojis('money')}\n\n"
        f"{replace_emojis('a_dot_smaller')} Поздравляем победителей! Спасибо всем за участие"
    )

    embed = discord.Embed(
        title=f"{replace_emojis('a_star')} {replace_emojis('winner')} ПОБЕДИТЕЛЬ ТУРНИРА",
        description=description,
        color=discord.Color.from_rgb(69, 69, 69)
    )

    # Add organizer avatar if available
    if organizer_id:
        try:
            organizer_member = guild.get_member(organizer_id)
            if organizer_member:
                embed.set_thumbnail(url=organizer_member.display_avatar.url)
        except:
            pass

    return embed


async def build_embed_for_phase(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Выбрать нужный embed по текущей фазе."""
    phase = tournament.phase
    if phase == TournamentPhase.SETUP:
        return await build_setup_embed(tournament, guild)
    if phase == TournamentPhase.DRAFT:
        return await build_draft_embed(tournament, guild)
    # Skip TEAMS phase - go directly to bracket phases
    if phase == TournamentPhase.QUALIFIERS:
        return await build_qualifiers_embed(tournament, guild)
    if phase == TournamentPhase.SEMIFINALS:
        return await build_semifinals_embed(tournament, guild)
    if phase == TournamentPhase.FINAL:
        return await build_final_embed(tournament, guild)
    if phase == TournamentPhase.COMPLETE:
        return await build_winner_embed(tournament, guild)
    return discord.Embed(title="Ошибка", color=discord.Color.red())


def build_level_up_embed(stats, old_level: int, new_level: int) -> discord.Embed:
    """Embed для уведомления о повышении уровня."""
    rank_title = stats.get_rank_title()
    current_xp, xp_needed = stats.get_level_progress()
    progress_bar = create_progress_bar(current_xp, xp_needed)

    embed = discord.Embed(
        title="🎉 ПОВЫШЕНИЕ УРОВНЯ!",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name=f"{rank_title} {old_level} → {new_level}",
        value=f"XP: {progress_bar}",
        inline=False,
    )
    embed.set_footer(text=f"Поздравляем с новым уровнем!")

    return embed


async def build_leaderboard_embed(guild_id: int, page: int = 1, leaderboard_type: str = "elo") -> discord.Embed:
    """Embed лидерборда с пагинацией."""
    from storage.player_stats_store import player_stats_store
    from storage.user_balance_store import user_balance_store
    from storage.redis_client import get_leaderboard, set_leaderboard
    from cogs.tournament import get_rank_emoji
    from config import replace_emojis

    # Try to get from cache first
    cached_data = await get_leaderboard(guild_id, leaderboard_type)

    if cached_data and page == 1:  # Only cache first page for now
        players = cached_data
    else:
        # Get players based on type
        if leaderboard_type == "level":
            players = await player_stats_store.get_leaderboard_by_level(guild_id, page, per_page=10)
        elif leaderboard_type == "money":
            players = await player_stats_store.get_leaderboard_by_earnings(guild_id, page, per_page=10)
        else:  # elo
            players = await player_stats_store.get_leaderboard(guild_id, page, per_page=10)

        # Cache the result if it's the first page
        if page == 1:
            await set_leaderboard(guild_id, leaderboard_type, players, ttl=60)  # Cache for 1 minute

    # Set title and color based on type
    if leaderboard_type == "level":
        title = f"{replace_emojis('a_star')} ТАБЛИЦА ЛИДЕРОВ | Level"
        color = discord.Color.from_rgb(69, 69, 69)
    elif leaderboard_type == "money":
        title = f"{replace_emojis('a_star')} ТАБЛИЦА ЛИДЕРОВ | Money"
        color = discord.Color.from_rgb(69, 69, 69)
    else:  # elo
        title = f"{replace_emojis('a_star')} ТАБЛИЦА ЛИДЕРОВ | ELO"
        color = discord.Color.from_rgb(69, 69, 69)

    total_pages = await player_stats_store.get_total_pages(guild_id, per_page=10)

    embed = discord.Embed(
        title=title,
        color=color,
    )

    if not players:
        embed.description = replace_emojis("⚪ Пока нет данных. Сыграйте хотя бы один турнир!")
        return embed

    # For money leaderboard, we need to sort by balance
    if leaderboard_type == "money":
        # Get balances for all players and sort
        player_balances = []
        for player in players:
            balance = await user_balance_store.get_balance(guild_id, player.user_id)
            player_balances.append((player, balance))

        # Sort by balance (descending)
        player_balances.sort(key=lambda x: x[1], reverse=True)
        players = [p for p, b in player_balances]

    lines = []
    global_rank = (page - 1) * 10

    for i, player in enumerate(players):
        rank = global_rank + i + 1

        # Highlight top 3
        if rank == 1:
            rank_emoji = replace_emojis("medal_gold")
        elif rank == 2:
            rank_emoji = replace_emojis("medal_silver")
        elif rank == 3:
            rank_emoji = replace_emojis("medal_bronze")
        else:
            rank_emoji = f"{rank}."

        # Format name with cosmetics - use stored name from stats
        formatted_name = format_player_name(guild_id, player.user_id, player.name)

        # Get rank emoji for level leaderboard
        player_rank = get_rank_emoji(player.level)

        if leaderboard_type == "level":
            line = f"{rank_emoji} {formatted_name} | {player_rank} — lvl {player.level}"
        elif leaderboard_type == "money":
            # Get current balance (already sorted above)
            balance = await user_balance_store.get_balance(guild_id, player.user_id)
            line = f"{rank_emoji} {formatted_name} — {balance:,} {replace_emojis('money')}"
        else:  # elo
            line = f"{rank_emoji} {formatted_name} — {int(player.elo)} ELO"

        lines.append(line)

    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Страница {page}/{total_pages}")

    return embed