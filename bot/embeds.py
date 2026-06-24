"""Построение Discord Embed-сообщений."""

from __future__ import annotations

import discord

from bot.models import DraftState, Tournament, TournamentPhase


async def captain_mention(guild: discord.Guild, captain_id: int) -> str:
    """Упоминание капитана или fallback."""
    member = guild.get_member(captain_id)
    if member:
        return member.mention
    return f"<@{captain_id}>"


async def captain_name(guild: discord.Guild, captain_id: int) -> str:
    member = guild.get_member(captain_id)
    if member:
        return member.display_name
    return f"Cap{captain_id}"


def _format_line(num: int, content: str) -> str:
    return f"{num}. {content if content else '*'}"


async def build_setup_embed(guild: discord.Guild, tournament: Tournament) -> discord.Embed:
    """Embed этапа настройки турнира."""
    embed = discord.Embed(title="🏆 Tournament Setup", color=discord.Color.gold())

    if tournament.captains:
        caps = " ".join([await captain_mention(guild, c) for c in tournament.captains])
    else:
        caps = "*"

    lines = [
        _format_line(1, caps),
        _format_line(2, " ".join(tournament.circles.get("2", []))),
        _format_line(3, " ".join(tournament.circles.get("3", []))),
        _format_line(4, " ".join(tournament.circles.get("4", []))),
    ]
    embed.description = "\n".join(lines)
    return embed


async def build_draft_embed(guild: discord.Guild, tournament: Tournament) -> discord.Embed:
    """Embed активного драфта."""
    draft = tournament.draft
    if not draft:
        return await build_setup_embed(guild, tournament)

    embed = discord.Embed(title="🎲 Порядок капитанов", color=discord.Color.blue())

    order_lines = []
    for i, cap_id in enumerate(draft.captain_order, start=1):
        name = await captain_name(guild, cap_id)
        order_lines.append(f"{i}. {name}")
    embed.description = "\n".join(order_lines)

    # Блок текущего круга
    circle = draft.current_circle
    circle_header = f"\n**Круг {circle}:**\n"
    pick_lines = []
    for cap_id in draft.captain_order:
        cap_display = await captain_name(guild, cap_id)
        pick = draft.picks.get(cap_id, {}).get(circle, "-")
        pick_lines.append(f"{cap_display} → {pick}")
    embed.add_field(name="\u200b", value=circle_header + "\n".join(pick_lines), inline=False)

    current = draft_engine_current_picker(draft)
    if current and tournament.phase == TournamentPhase.DRAFT:
        picker_name = await captain_name(guild, current)
        embed.add_field(name="Сейчас выбирает", value=picker_name, inline=False)

    if draft.auto_message:
        embed.add_field(name="\u200b", value=draft.auto_message, inline=False)

    return embed


def draft_engine_current_picker(draft: DraftState) -> int | None:
    from bot.draft_engine import get_current_picker
    return get_current_picker(draft)


async def build_teams_embed(guild: discord.Guild, tournament: Tournament) -> discord.Embed:
    """Embed финальных команд после драфта."""
    embed = discord.Embed(title="🏆 Команды", color=discord.Color.green())
    draft = tournament.draft
    if not draft or not draft.teams:
        return embed

    for team_num, roster in draft.teams.items():
        captain_id = int(roster[0])
        cap_name = await captain_name(guild, captain_id)
        members = [cap_name] + roster[1:]
        embed.add_field(
            name=f"Team {team_num}",
            value="\n".join(members),
            inline=True,
        )
    return embed


async def build_semifinals_embed(guild: discord.Guild, tournament: Tournament) -> discord.Embed:
    """Embed полуфиналов."""
    embed = discord.Embed(title="🏆 SEMIFINALS", color=discord.Color.purple())
    bracket = tournament.bracket
    if not bracket:
        return embed

    for match_num, (t1, t2) in enumerate(bracket.semifinal_pairs, start=1):
        embed.add_field(
            name=f"Match #{match_num}",
            value=f"Team {t1}\nVS\nTeam {t2}",
            inline=True,
        )
    return embed


async def build_final_embed(guild: discord.Guild, tournament: Tournament) -> discord.Embed:
    """Embed финала."""
    embed = discord.Embed(title="🏆 FINAL", color=discord.Color.red())
    bracket = tournament.bracket
    if not bracket or not bracket.final_pair:
        return embed

    t1, t2 = bracket.final_pair
    embed.description = "Winner Match #1\nVS\nWinner Match #2"
    embed.add_field(name="\u200b", value=f"Team {t1}\nVS\nTeam {t2}", inline=False)
    return embed


async def build_winner_embed(guild: discord.Guild, tournament: Tournament) -> discord.Embed:
    """Embed победителя турнира."""
    embed = discord.Embed(title="🏆 TOURNAMENT WINNER", color=discord.Color.gold())
    bracket = tournament.bracket
    draft = tournament.draft
    if not bracket or bracket.winner_team is None or not draft:
        return embed

    team_num = bracket.winner_team
    roster = draft.teams.get(team_num, [])
    if not roster:
        embed.description = f"🥇 Team {team_num}"
        return embed

    captain_id = int(roster[0])
    cap_name = await captain_name(guild, captain_id)
    members = [cap_name] + roster[1:]
    embed.description = f"🥇 Team {team_num}"
    embed.add_field(name="Состав команды", value="\n".join(members), inline=False)
    return embed


async def build_tournament_embed(guild: discord.Guild, tournament: Tournament) -> discord.Embed:
    """Универсальный билдер по текущей фазе."""
    phase = tournament.phase
    if phase == TournamentPhase.SETUP:
        return await build_setup_embed(guild, tournament)
    if phase == TournamentPhase.DRAFT:
        return await build_draft_embed(guild, tournament)
    if phase == TournamentPhase.TEAMS:
        return await build_teams_embed(guild, tournament)
    if phase == TournamentPhase.SEMIFINALS:
        return await build_semifinals_embed(guild, tournament)
    if phase == TournamentPhase.FINAL:
        return await build_final_embed(guild, tournament)
    if phase == TournamentPhase.COMPLETE:
        return await build_winner_embed(guild, tournament)
    return await build_setup_embed(guild, tournament)
