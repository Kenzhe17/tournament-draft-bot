"""Построение Discord Embed-сообщений для турнира."""

from __future__ import annotations

import discord

# ДОБАВЛЕНО: TournamentSize в список импорта
from models.tournament import FormationMode, RegistrationState, Tournament, TournamentPhase, TournamentSize
from storage.bet_store import bet_store
from storage.player_stats_store import player_stats_store
from utils.cosmetics import format_player_name


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
    if not tournament.betting_open:
        embed.add_field(
            name="━━━━━━━━━━━━━━\n\n💰 СТАВКИ",
            value="🔒 СТАВКИ ЗАКРЫТЫ",
            inline=False,
        )
        return

    betting_text = []
    for i, (team_a, team_b) in enumerate(matches):
        match_id = f"{match_type}_{i}"
        bets = await bet_store.get_bets_by_match(match_id)
        
        # Calculate team totals
        team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
        team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
        captain_a = team_a_data.get("captain", f"П{team_a + 1}")
        captain_b = team_b_data.get("captain", f"П{team_b + 1}")
        name_a = tournament.team_names.get(team_a, captain_a)
        name_b = tournament.team_names.get(team_b, captain_b)
        
        # Calculate bets per team
        team_a_amount = sum(b.amount for b in bets if b.team_name == name_a)
        team_b_amount = sum(b.amount for b in bets if b.team_name == name_b)
        total_bank = team_a_amount + team_b_amount
        
        # Calculate percentages
        team_a_pct = (team_a_amount / total_bank * 100) if total_bank > 0 else 0
        team_b_pct = (team_b_amount / total_bank * 100) if total_bank > 0 else 0
        
        match_text = f"🔥 Игра #{i + 1}\n{name_a} vs {name_b}\n\n💰 Банк: {total_bank} 🪙\n\n"
        
        if total_bank > 0:
            match_text += f"1️⃣ {name_a}\n┗ {team_a_amount} 🪙 ({team_a_pct:.0f}%)\n\n"
            match_text += f"2️⃣ {name_b}\n┗ {team_b_amount} 🪙 ({team_b_pct:.0f}%)\n"
        else:
            match_text += f"1️⃣ {name_a}\n┗ 0 🪙 (0%)\n\n"
            match_text += f"2️⃣ {name_b}\n┗ 0 🪙 (0%)\n"
        
        betting_text.append(match_text)
    
    if betting_text:
        embed.add_field(
            name="━━━━━━━━━━━━━━\n\n💰 СТАВКИ",
            value="\n".join(betting_text),
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
    status_emoji = "🔓" if tournament.registration == RegistrationState.OPEN else "🔒"
    formation_text = "🎯 ELO" if tournament.formation_mode == FormationMode.ELO else "✋ Ручной" if tournament.formation_mode == FormationMode.MANUAL else "🎲 Случайный"
    embed = discord.Embed(
        title=f"🏆 {tournament.size.value} | {status_emoji} | {formation_text}",
        color=discord.Color.dark_red(),
    )

    # Build ELO dictionary for all registered players
    elo_dict = {}
    for player_name, user_id in tournament.player_user_ids.items():
        stats = await player_stats_store.get(tournament.guild_id, user_id)
        if stats:
            elo_dict[player_name] = int(stats.elo)

    # Show different content based on formation mode
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
                player_strings.append(f"{formatted_name} ({int(elo_dict[player_name])})")
            else:
                player_strings.append(formatted_name)

        players_text = " ".join(player_strings) if player_strings else "*"

        embed.add_field(
            name=f"Игроки ({current}/{limit})",
            value=players_text,
            inline=False,
        )
    else:
        # MANUAL/ELO modes: show circles
        circle_counts = tournament.get_circle_counts()

        # Show all 4 circles with dynamic limits and counts
        for circle in range(1, 5):
            circle_list = getattr(tournament, f"circle{circle}")
            circle_name = "Капитан" if circle == 1 else f"Круг {circle}"
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
            embed.add_field(
                name=f"{circle_name}{limit_info}",
                value=value,
                inline=False,
            )

    # Add info about registration
    status_text = "Открыто" if tournament.registration == RegistrationState.OPEN else "Закрыто"
    embed.add_field(
        name="\u200b",
        value=status_text,
        inline=False,
    )

    return embed


async def build_draft_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed во время драфта."""
    embed = discord.Embed(
        title="⚔️ Порядок капитанов",
        color=discord.Color.dark_blue(),
    )

    # Get current picker
    current_picker_pos = tournament.current_picker_position()
    if current_picker_pos is not None:
        current_captain_name = tournament.captains[tournament.captain_order[current_picker_pos]]
        current_captain_id = tournament.player_user_ids.get(current_captain_name, 0)
        if current_captain_id > 0:
            current_line = f"🎯 Текущий: <@{current_captain_id}>"
        else:
            current_line = f"🎯 Текущий: {current_captain_name}"
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
                next_captains.append(f"{i + 1}. <@{captain_id}>")
            else:
                next_captains.append(f"{i + 1}. {captain_name}")

    embed.description = f"{current_line}\n\n**Очередь:**\n" + "\n".join(next_captains)

    # Таблица выборов по текущему и пройденным кругам (всегда круги 2, 3, 4)
    for circle in range(2, 5):
        lines = []
        # Get pick order for this circle
        from models.tournament import PICK_ORDERS
        circle_orders = PICK_ORDERS.get(tournament.captain_count, {})
        order_data = circle_orders.get(str(circle), {})
        pick_order = order_data.get("order", list(range(tournament.captain_count)))

        for pos in pick_order:
            captain_name = tournament.captains[pos]
            pick = tournament.picks.get(str(pos), {}).get(str(circle))
            if circle > tournament.current_circle or not pick:
                pick = "-"
            lines.append(f"{captain_name} → {pick}")

        status = ""
        if circle == tournament.current_circle:
            status = " ⬅"
        embed.add_field(
            name=f"⚔️ Круг {circle}:{status}",
            value="\n".join(lines),
            inline=False,
        )

    # Кто сейчас выбирает
    picker_pos = tournament.current_picker_position()
    if picker_pos is not None:
        captain_name = tournament.captains[picker_pos]
        captain_user_id = tournament.player_user_ids.get(captain_name, 0)
        if captain_user_id:
            embed.add_field(
                name="👤 Сейчас выбирает",
                value=f"**➡️ <@{captain_user_id}>**",
                inline=False,
            )
            # Добавить @mention в описание для уведомления
            embed.description = f"<@{captain_user_id}> - ваша очередь выбирать!"

    # Warning if more than 25 players available
    key = str(tournament.current_circle)
    available = tournament.available.get(key, [])
    if len(available) > 25:
        embed.add_field(
            name="⚠️ Внимание",
            value=f"Показано 25 из {len(available)} игроков в меню выбора.",
            inline=False,
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
    embed = discord.Embed(
        title="🏆 ТУРНИРНАЯ СЕТКА — ОТБОР",
        color=discord.Color.dark_purple(),  # Тёмно-фиолетовый для отборочных
    )

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
        room_info = ""
        if room_data:
            # Add separate fields for ID and password for easy copying
            embed.add_field(
                name=f"🔥 Отбор #{i + 1}",
                value=f"**{name_a} ({int(avg_elo_a)})** *vs* **{name_b} ({int(avg_elo_b)})**",
                inline=False,
            )
            embed.add_field(
                name="ID комнаты",
                value=room_data['id'],
                inline=True
            )
            embed.add_field(
                name="Пароль",
                value=room_data['password'],
                inline=True
            )
        else:
            embed.add_field(
                name=f"🔥 Отбор #{i + 1}",
                value=f"**{name_a} ({int(avg_elo_a)})** *vs* **{name_b} ({int(avg_elo_b)})**",
                inline=False,
            )

    # Добавляем отображение команд снизу под отборочными
    await _add_teams_block_to_embed(embed, guild, tournament)
    
    # Добавляем секцию ставок
    await _add_betting_section_to_embed(embed, tournament, tournament.qualifier_matches, "qualifier")
    
    return embed


async def build_semifinals_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed полуфиналов."""
    embed = discord.Embed(
        title="🏆 ТУРНИРНАЯ СЕТКА — ПОЛУФИНАЛ",
        color=discord.Color.light_gray(),  # Серебряный для полуфиналов
    )

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
        room_info = ""
        if room_data:
            # Add separate fields for ID and password for easy copying
            embed.add_field(
                name=f"🔥 Игра #{i + 1}",
                value=f"**{name_a} ({int(avg_elo_a)})** *vs* **{name_b} ({int(avg_elo_b)})**",
                inline=False,
            )
            embed.add_field(
                name="ID комнаты",
                value=room_data['id'],
                inline=True
            )
            embed.add_field(
                name="Пароль",
                value=room_data['password'],
                inline=True
            )
        else:
            embed.add_field(
                name=f"🔥 Игра #{i + 1}",
                value=f"**{name_a} ({int(avg_elo_a)})** *vs* **{name_b} ({int(avg_elo_b)})**",
                inline=False,
            )

    # Добавляем отображение команд снизу под полуфиналами
    await _add_teams_block_to_embed(embed, guild, tournament)
    
    # Добавляем секцию ставок
    await _add_betting_section_to_embed(embed, tournament, tournament.semifinal_matches, "semifinal")
    
    return embed


async def build_final_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed финала."""
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
        # Add separate fields for ID and password for easy copying
        embed = discord.Embed(
            title="🏆 ТУРНИРНАЯ СЕТКА — ФИНАЛ",
            color=discord.Color.gold(),  # Золотой для финала
        )
        embed.add_field(
            name="⚡ Главная битва турнира",
            value=f"**{name_a} ({int(avg_elo_a)})** *vs* **{name_b} ({int(avg_elo_b)})**",
            inline=False
        )
        embed.add_field(
            name="ID комнаты",
            value=room_data['id'],
            inline=True
        )
        embed.add_field(
            name="Пароль",
            value=room_data['password'],
            inline=True
        )
    else:
        embed = discord.Embed(
            title="🏆 ТУРНИРНАЯ СЕТКА — ФИНАЛ",
            color=discord.Color.gold(),  # Золотой для финала
        )
        embed.add_field(
            name="⚡ Главная битва турнира",
            value=f"**{name_a} ({int(avg_elo_a)})** *vs* **{name_b} ({int(avg_elo_b)})**",
            inline=False
        )

    # Добавляем отображение команд снизу под финалом
    await _add_teams_block_to_embed(embed, guild, tournament)
    
    # Добавляем секцию ставок для финала
    final_matches = [(tournament.final_teams[0], tournament.final_teams[1])]
    await _add_betting_section_to_embed(embed, tournament, final_matches, "final")
    
    return embed


async def build_winner_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed победителя турнира."""
    idx = tournament.winner_team_index
    if idx is None:
        return discord.Embed(title="🏆 ПОБЕДИТЕЛИ", color=discord.Color.gold())

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

    embed = discord.Embed(
        title=" ТУРНИР ЗАВЕРШЕН ",
        description=f"🥇 **Чемпион — {team_name}!**\n👥 **Состав: {roster_str}**",
        color=discord.Color.gold(),
    )

    # Добавляем отображение команд на финальном экране
    await _add_teams_block_to_embed(embed, guild, tournament)
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


async def build_leaderboard_embed(guild_id: int, page: int = 1) -> discord.Embed:
    """Embed лидерборда с пагинацией."""
    from storage.player_stats_store import player_stats_store

    players = await player_stats_store.get_leaderboard(guild_id, page, per_page=10)
    total_pages = await player_stats_store.get_total_pages(guild_id, per_page=10)

    embed = discord.Embed(
        title="🏆 Лидерборд Игроков",
        color=discord.Color.gold(),
    )

    if not players:
        embed.description = "Пока нет данных. Сыграйте хотя бы один турнир!"
        return embed

    lines = []
    global_rank = (page - 1) * 10

    for i, player in enumerate(players):
        rank = global_rank + i + 1

        # Highlight top 3
        if rank == 1:
            rank_emoji = "🥇"
        elif rank == 2:
            rank_emoji = "🥈"
        elif rank == 3:
            rank_emoji = "🥉"
        else:
            rank_emoji = f"{rank}."

        # Format name with cosmetics - use stored name from stats
        formatted_name = format_player_name(guild_id, player.user_id, player.name)
        line = f"{rank_emoji} {formatted_name} — {int(player.elo)} ELO"
        lines.append(line)
    
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Страница {page}/{total_pages}")
    
    return embed