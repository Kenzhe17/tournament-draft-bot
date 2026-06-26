"""Построение Discord Embed-сообщений для турнира."""

from __future__ import annotations

import discord

from models.tournament import RegistrationState, Tournament, TournamentPhase


def _circle_line(players: list[str]) -> str:
    """Строка игроков круга или пустой слот."""
    if not players:
        return ""
    return " ".join(players)


async def _add_teams_block_to_embed(embed: discord.Embed, guild: discord.Guild, tournament: Tournament) -> None:
    """Вспомогательная функция для сквозного отображения команд красивым списком."""
    if not tournament.teams:
        return

    team_emojis = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣"}
    teams_text = []

    for i, team in enumerate(tournament.teams):
        captain = team.get("captain", "Unknown")

        # Собираем игроков из кругов драфта (всегда круги 1-4)
        players = []
        for circle in range(1, 5):
            p_name = team.get(f"circle{circle}", "")
            if p_name:
                players.append(p_name)

        players_str = ", ".join(players) if players else "*Ожидание игроков...*"
        emoji = team_emojis.get(i + 1, "🎮")

        teams_text.append(
            f"{emoji} **Команда П{i + 1}**\n"
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
    
    # Show circles based on tournament size
    if tournament.size == TournamentSize.EIGHT:
        # 8 players: circle1 + circle2
        for circle in range(1, 3):
            circle_list = getattr(tournament, f"circle{circle}")
            circle_name = "Капитаны" if circle == 1 else f"Круг {circle}"
            value = _circle_line(circle_list) or "*"
            embed.add_field(
                name=f"{circle_name}",
                value=value,
                inline=False,
            )
    elif tournament.size == TournamentSize.SIXTEEN:
        # 16 players: circle1 + circle2 + circle3
        for circle in range(1, 4):
            circle_list = getattr(tournament, f"circle{circle}")
            circle_name = "Капитаны" if circle == 1 else f"Круг {circle}"
            value = _circle_line(circle_list) or "*"
            embed.add_field(
                name=f"{circle_name}",
                value=value,
                inline=False,
            )
    else:
        # 32 players: all 4 circles
        for circle in range(1, 5):
            circle_list = getattr(tournament, f"circle{circle}")
            circle_name = "Капитаны" if circle == 1 else f"Круг {circle}"
            limit_info = "" if circle == 4 else " (макс. 4)"
            value = _circle_line(circle_list) or "*"
            embed.add_field(
                name=f"{circle_name}{limit_info}",
                value=value,
                inline=False,
            )
    
    # Add info about registration
    if tournament.registration == RegistrationState.OPEN:
        embed.add_field(
            name="ℹ️",
            value="Регистрация открыта! Нажмите на кнопку чтобы добавить себя.",
            inline=False,
        )
    else:
        embed.add_field(
            name="ℹ️",
            value="Регистрация закрыта. Только админ может добавлять игроков.",
            inline=False,
        )
    
    return embed


async def build_draft_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed во время драфта."""
    embed = discord.Embed(
        title="🎲 Порядок капитанов",
        color=discord.Color.blue(),
    )

    # Порядок капитанов
    order_lines = []
    for i, cap_idx in enumerate(tournament.captain_order):
        captain_name = tournament.captains[cap_idx]
        order_lines.append(f"{i + 1}. {captain_name}")
    embed.description = "\n".join(order_lines)

    # Таблица выборов по текущему и пройденным кругам (на основе размера турнира)
    if tournament.size == TournamentSize.EIGHT:
        circles = [2]  # only circle2
    elif tournament.size == TournamentSize.SIXTEEN:
        circles = [2, 3]  # circle2 + circle3
    else:
        circles = [2, 3, 4]  # circle2 + circle3 + circle4

    for circle in circles:
        lines = []
        for pos in range(4):
            cap_idx = tournament.captain_order[pos]
            captain_name = tournament.captains[cap_idx]
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
        captain_name = tournament.captains[tournament.captain_order[picker_pos]]
        embed.add_field(
            name="Сейчас выбирает",
            value=captain_name,
            inline=False,
        )

    if tournament.last_auto_pick_message:
        embed.add_field(
            name="Автовыбор",
            value=tournament.last_auto_pick_message,
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
        name_a = f"П{team_a + 1}"
        name_b = f"П{team_b + 1}"
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
        name_a = f"П{team_a + 1}"
        name_b = f"П{team_b + 1}"
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
    embed = discord.Embed(
        title="🏆 ТУРНИРНАЯ СЕТКА — ФИНАЛ",
        color=discord.Color.red(),
    )
    embed.add_field(
        name="⚡ Главная битва турнира",
        value=f"Победитель матча **П{team_a + 1}** *vs* Победитель матча **П{team_b + 1}**",
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

    embed = discord.Embed(
        title=" ТУРНИР ЗАВЕРШЕН ",
        description=f"🥇 **Чемпион — П{idx + 1}!**",
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
    return await build_setup_embed(tournament, guild)