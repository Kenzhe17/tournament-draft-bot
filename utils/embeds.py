"""Построение Discord Embed-сообщений для турнира."""

from __future__ import annotations

import discord

# ДОБАВЛЕНО: TournamentSize в список импорта
from models.tournament import FormationMode, RegistrationState, Tournament, TournamentPhase, TournamentSize
from storage.bet_store import bet_store
from storage.player_stats_store import player_stats_store


def _circle_line(players: list[str], tournament: Tournament | None = None, elo_dict: dict[str, int] | None = None) -> str:
    """Строка игроков круга с ELO или пустой слот."""
    if not players:
        return ""

    player_strings = []
    for player_name in players:
        # Use game nickname for display if available
        display_name = player_name
        if tournament and player_name in tournament.player_game_nicknames:
            display_name = tournament.player_game_nicknames[player_name]

        if elo_dict and player_name in elo_dict:
            player_strings.append(f"{display_name} ({elo_dict[player_name]})")
        else:
            player_strings.append(display_name)

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
            if p_name:
                # Use game nickname for display if available
                display_name = tournament.player_game_nicknames.get(p_name, p_name)
                players.append(display_name)

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
    formation_text = "🎯 ELO" if tournament.formation_mode == FormationMode.ELO else "✋ Ручной"
    embed = discord.Embed(
        title=f"🏆 Турнир ({tournament.size.value} игроков) {status_emoji} | {formation_text}",
        color=discord.Color.gold(),
    )
    
    # Build ELO dictionary for all registered players
    elo_dict = {}
    for player_name, user_id in tournament.player_user_ids.items():
        stats = await player_stats_store.get(tournament.guild_id, user_id)
        if stats:
            elo_dict[player_name] = stats.elo
    
    # Show all 4 circles with dynamic limits
    for circle in range(1, 5):
        circle_list = getattr(tournament, f"circle{circle}")
        circle_name = "Капитаны" if circle == 1 else f"Круг {circle}"
        limit = tournament.circle_limit(circle)
        limit_enabled = tournament.circle_limits_enabled.get(circle, True) if circle != 1 else True

        if circle == 1:
            limit_info = ""
        elif limit_enabled:
            limit_info = f" (макс. {limit})"
        else:
            limit_info = " (без лимита)"

        value = _circle_line(circle_list, tournament, elo_dict) or "*"
        embed.add_field(
            name=f"{circle_name}{limit_info}",
            value=value,
            inline=False,
        )
    
    # Add info about registration
    if tournament.registration == RegistrationState.OPEN:
        embed.add_field(
            name="\u200b",
            value="Регистрация открыта! Нажмите на кнопку чтобы добавить себя.",
            inline=False,
        )
    else:
        embed.add_field(
            name="\u200b",
            value="Регистрация закрыта. Только админ может добавлять игроков.",
            inline=False,
        )
    
    return embed


async def build_draft_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed во время драфта."""
    embed = discord.Embed(
        title="Порядок капитанов",
        color=discord.Color.blue(),
    )

    # Helper function to get display name
    def get_display_name(player_name: str) -> str:
        return tournament.player_game_nicknames.get(player_name, player_name)

    # Порядок капитанов (show the shuffled order)
    order_lines = []
    for i, captain_name in enumerate(tournament.captains):
        display_name = get_display_name(captain_name)
        order_lines.append(f"{i + 1}. {display_name}")
    embed.description = "\n".join(order_lines)

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
            captain_display = get_display_name(captain_name)
            pick = tournament.picks.get(str(pos), {}).get(str(circle))
            if circle > tournament.current_circle or not pick:
                pick_display = "-"
            else:
                pick_display = get_display_name(pick)
            lines.append(f"{captain_display} → {pick_display}")

        status = ""
        if circle == tournament.current_circle:
            status = " ⬅"
        embed.add_field(
            name=f"Круг {circle}:{status}",
            value="\n".join(lines),
            inline=False,
        )

    # Кто сейчас выбирает
    picker_pos = tournament.current_picker_position()
    if picker_pos is not None:
        captain_name = tournament.captains[picker_pos]
        display_name = get_display_name(captain_name)
        embed.add_field(
            name="Сейчас выбирает",
            value=display_name,
            inline=False,
        )

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
    """Embed с итоговыми командами."""
    embed = discord.Embed(
        title="🏆 Сформированные Команды",
        color=discord.Color.green(),
    )
    await _add_teams_block_to_embed(embed, guild, tournament)
    return embed


async def build_qualifiers_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed отборочных матчей."""
    embed = discord.Embed(
        title="🏆 ТУРНИРНАЯ СЕТКА — ОТБОР",
        color=discord.Color.purple(),
    )

    for i, (team_a, team_b) in enumerate(tournament.qualifier_matches):
        # Get team names or default to captain names
        team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
        team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
        captain_a = team_a_data.get("captain", f"П{team_a + 1}")
        captain_b = team_b_data.get("captain", f"П{team_b + 1}")
        name_a = tournament.team_names.get(team_a, captain_a)
        name_b = tournament.team_names.get(team_b, captain_b)

        embed.add_field(
            name=f"🔥 Отбор #{i + 1}",
            value=f"**{name_a}** *vs* **{name_b}**",
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
        color=discord.Color.orange(),
    )

    for i, (team_a, team_b) in enumerate(tournament.semifinal_matches):
        # Get team names or default to captain names
        team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
        team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
        captain_a = team_a_data.get("captain", f"П{team_a + 1}")
        captain_b = team_b_data.get("captain", f"П{team_b + 1}")
        name_a = tournament.team_names.get(team_a, captain_a)
        name_b = tournament.team_names.get(team_b, captain_b)

        embed.add_field(
            name=f"🔥 Игра #{i + 1}",
            value=f"**{name_a}** *vs* **{name_b}**",
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

    embed = discord.Embed(
        title="🏆 ТУРНИРНАЯ СЕТКА — ФИНАЛ",
        color=discord.Color.red(),
    )
    embed.add_field(
        name="⚡ Главная битва турнира",
        value=f"**{name_a}** *vs* **{name_b}**",
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
            players.append(p_name)
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
    if phase == TournamentPhase.TEAMS:
        return await build_teams_embed(tournament, guild)
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

        line = f"{rank_emoji} **{player.name}** — {player.elo} ELO"
        lines.append(line)
    
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Страница {page}/{total_pages}")
    
    return embed