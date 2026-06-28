"""Построение Discord Embed-сообщений для турнира."""

from __future__ import annotations

import discord

# ДОБАВЛЕНО: TournamentSize в список импорта
from models.tournament import RegistrationState, Tournament, TournamentPhase, TournamentSize


def _circle_line(players: list[str]) -> str:
    """Строка игроков круга или пустой слот."""
    if not players:
        return ""
    return " ".join(players)


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
                players.append(p_name)

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
    embed = discord.Embed(
        title=f"🏆 Турнир ({tournament.size.value} игроков) {status_emoji}",
        color=discord.Color.gold(),
    )
    
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

        value = _circle_line(circle_list) or "*"
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

    # Порядок капитанов (show the actual order they will pick in)
    order_lines = []
    from models.tournament import PICK_ORDERS
    circle_orders = PICK_ORDERS.get(tournament.captain_count, {})
    order_data = circle_orders.get(str(tournament.current_circle), {})
    pick_order = order_data.get("order", list(range(tournament.captain_count)))
    for i, pos in enumerate(pick_order):
        captain_name = tournament.captains[pos]
        order_lines.append(f"{i + 1}. {captain_name}")
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
            pick = tournament.picks.get(str(pos), {}).get(str(circle))
            if circle > tournament.current_circle or not pick:
                pick = "-"
            lines.append(f"{captain_name} → {pick}")

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
        embed.add_field(
            name="Сейчас выбирает",
            value=captain_name,
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