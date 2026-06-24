"""Построение Discord Embed-сообщений для турнира."""

from __future__ import annotations

import discord

from models.tournament import Tournament, TournamentPhase


def _captain_mentions(tournament: Tournament) -> str:
    """Строка с упоминаниями всех капитанов."""
    if not tournament.captains:
        return "*"
    return " ".join(f"<@{cid}>" for cid in tournament.captains)


def _circle_line(players: list[str]) -> str:
    """Строка игроков круга или пустой слот."""
    if not players:
        return ""
    return " ".join(players)


async def build_setup_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed настройки турнира."""
    embed = discord.Embed(
        title="🏆 Tournament Setup",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="1.",
        value=_captain_mentions(tournament) if tournament.captains else "*",
        inline=False,
    )
    embed.add_field(name="2.", value=_circle_line(tournament.circle2) or "*", inline=False)
    embed.add_field(name="3.", value=_circle_line(tournament.circle3) or "*", inline=False)
    embed.add_field(name="4.", value=_circle_line(tournament.circle4) or "*", inline=False)
    return embed


async def _captain_display_name(guild: discord.Guild, captain_id: int) -> str:
    """Имя капитана для отображения."""
    member = guild.get_member(captain_id)
    if member is None:
        try:
            member = await guild.fetch_member(captain_id)
        except discord.HTTPException:
            return f"Captain#{captain_id}"
    return member.display_name


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
        cap_id = tournament.captains[cap_idx]
        name = await _captain_display_name(guild, cap_id)
        order_lines.append(f"{i + 1}. {name}")
    embed.description = "\n".join(order_lines)

    # Таблица выборов по текущему и пройденным кругам
    for circle in (2, 3, 4):
        lines = []
        for pos in range(4):
            cap_idx = tournament.captain_order[pos]
            cap_id = tournament.captains[cap_idx]
            name = await _captain_display_name(guild, cap_id)
            pick = tournament.picks.get(str(pos), {}).get(str(circle))
            if circle > tournament.current_circle or not pick:
                pick = "-"
            lines.append(f"{name} → {pick}")

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
        cap_id = tournament.captains[tournament.captain_order[picker_pos]]
        name = await _captain_display_name(guild, cap_id)
        embed.add_field(
            name="Сейчас выбирает",
            value=name,
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
        title="🏆 Команды",
        color=discord.Color.green(),
    )

    for i, team in enumerate(tournament.teams):
        cap_id = team["captain_id"]
        cap_name = await _captain_display_name(guild, cap_id)
        members = [
            cap_name,
            team.get("circle2", ""),
            team.get("circle3", ""),
            team.get("circle4", ""),
        ]
        embed.add_field(
            name=f"Team {i + 1}",
            value="\n".join(members),
            inline=True,
        )

    return embed


async def build_semifinals_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed полуфиналов."""
    embed = discord.Embed(
        title="🏆 SEMIFINALS",
        color=discord.Color.orange(),
    )

    for i, (team_a, team_b) in enumerate(tournament.semifinal_matches):
        name_a = f"Team {team_a + 1}"
        name_b = f"Team {team_b + 1}"
        embed.add_field(
            name=f"Match #{i + 1}",
            value=f"{name_a}\n**VS**\n{name_b}",
            inline=True,
        )

    return embed


async def build_final_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed финала."""
    team_a = tournament.final_teams[0]
    team_b = tournament.final_teams[1]
    embed = discord.Embed(
        title="🏆 FINAL",
        color=discord.Color.red(),
    )
    embed.description = (
        f"Winner Match #1 — **Team {team_a + 1}**\n"
        f"**VS**\n"
        f"Winner Match #2 — **Team {team_b + 1}**"
    )
    return embed


async def build_winner_embed(
    tournament: Tournament, guild: discord.Guild
) -> discord.Embed:
    """Embed победителя турнира."""
    idx = tournament.winner_team_index
    if idx is None:
        return discord.Embed(title="🏆 TOURNAMENT WINNER", color=discord.Color.gold())

    team = tournament.teams[idx]
    cap_name = await _captain_display_name(guild, team["captain_id"])
    roster = [
        cap_name,
        team.get("circle2", ""),
        team.get("circle3", ""),
        team.get("circle4", ""),
    ]

    embed = discord.Embed(
        title="🏆 TOURNAMENT WINNER",
        description=f"🥇 **Team {idx + 1}**",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Состав команды",
        value="\n".join(roster),
        inline=False,
    )
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
    if phase == TournamentPhase.SEMIFINALS:
        return await build_semifinals_embed(tournament, guild)
    if phase == TournamentPhase.FINAL:
        return await build_final_embed(tournament, guild)
    if phase == TournamentPhase.COMPLETE:
        return await build_winner_embed(tournament, guild)
    return await build_setup_embed(tournament, guild)
