"""Embed builders for matchmaking."""

from __future__ import annotations

import discord
from models.matchmaking import Matchmaking, MatchmakingPhase


async def build_matchmaking_embed(matchmaking: Matchmaking, guild: discord.Guild) -> discord.Embed:
    """Построить embed для текущей фазы matchmaking."""
    phase = matchmaking.phase

    if phase == MatchmakingPhase.SETUP:
        return await build_setup_embed(matchmaking)
    elif phase == MatchmakingPhase.DRAFT:
        return await build_draft_embed(matchmaking)
    elif phase == MatchmakingPhase.TEAMS:
        return await build_teams_embed(matchmaking)
    elif phase == MatchmakingPhase.READY_CHECK:
        return await build_ready_check_embed(matchmaking)
    elif phase == MatchmakingPhase.IN_PROGRESS:
        return await build_in_progress_embed(matchmaking)
    elif phase == MatchmakingPhase.COMPLETE:
        return await build_complete_embed(matchmaking)
    else:
        return discord.Embed(title="❌ Unknown Phase", color=discord.Color.red())


async def build_setup_embed(matchmaking: Matchmaking) -> discord.Embed:
    """Embed для фазы регистрации."""
    player_count = len(matchmaking.players)

    embed = discord.Embed(
        title="🎮 Matchmaking Lobby",
        color=discord.Color.blue()
    )

    if matchmaking.is_full:
        embed.description = "🎉 **Match Found!**\n\n8/8 игроков собрано."
    else:
        embed.description = f"Поиск игры:\n{player_count}/8 игроков"

    # Список игроков
    players_text = ""
    for i in range(8):
        if i < len(matchmaking.players):
            players_text += f"{i + 1}. {matchmaking.players[i]}\n"
        else:
            players_text += f"{i + 1}.\n"

    embed.add_field(name="Players:", value=players_text, inline=False)

    return embed


async def build_draft_embed(matchmaking: Matchmaking) -> discord.Embed:
    """Embed для фазы драфта."""
    embed = discord.Embed(
        title="🎲 Драфт",
        color=discord.Color.purple()
    )

    embed.description = f"Капитан {matchmaking.team1_captain if matchmaking.draft_picker == 0 else matchmaking.team2_captain} выбирает."

    # Доступные игроки
    available_text = "\n".join([f"{i + 1}. {name}" for i, name in enumerate(matchmaking.available_players)])
    embed.add_field(name="Доступные игроки:", value=available_text, inline=False)

    # Текущие команды
    team1_text = "\n".join(matchmaking.team1_players)
    team2_text = "\n".join(matchmaking.team2_players)

    embed.add_field(name=f"{matchmaking.team1_name}:", value=team1_text, inline=True)
    embed.add_field(name=f"{matchmaking.team2_name}:", value=team2_text, inline=True)

    return embed


async def build_teams_embed(matchmaking: Matchmaking) -> discord.Embed:
    """Embed для фазы настройки команд."""
    embed = discord.Embed(
        title="⚙️ Настройка команд",
        color=discord.Color.orange()
    )

    embed.description = "Капитаны могут изменить название команды и нажать Ready когда будут готовы."

    team1_text = "\n".join(matchmaking.team1_players)
    team2_text = "\n".join(matchmaking.team2_players)

    embed.add_field(
        name=f"{matchmaking.team1_name} {'✅' if matchmaking.team1_ready else '⏳'}",
        value=team1_text,
        inline=True
    )
    embed.add_field(
        name=f"{matchmaking.team2_name} {'✅' if matchmaking.team2_ready else '⏳'}",
        value=team2_text,
        inline=True
    )

    return embed


async def build_ready_check_embed(matchmaking: Matchmaking) -> discord.Embed:
    """Embed для проверки готовности."""
    embed = discord.Embed(
        title="✅ Проверка готовности",
        color=discord.Color.green()
    )

    embed.description = "Обе команды готовы! Матч скоро начнется."

    team1_text = "\n".join(matchmaking.team1_players)
    team2_text = "\n".join(matchmaking.team2_players)

    embed.add_field(name=f"{matchmaking.team1_name}:", value=team1_text, inline=True)
    embed.add_field(name=f"{matchmaking.team2_name}:", value=team2_text, inline=True)

    return embed


async def build_in_progress_embed(matchmaking: Matchmaking) -> discord.Embed:
    """Embed для матча в процессе."""
    embed = discord.Embed(
        title="⚔️ Матч идет",
        color=discord.Color.red()
    )

    embed.description = "Матч в процессе..."

    team1_text = "\n".join(matchmaking.team1_players)
    team2_text = "\n".join(matchmaking.team2_players)

    embed.add_field(name=f"{matchmaking.team1_name}:", value=team1_text, inline=True)
    embed.add_field(name=f"{matchmaking.team2_name}:", value=team2_text, inline=True)

    return embed


async def build_complete_embed(matchmaking: Matchmaking) -> discord.Embed:
    """Embed для завершенного матча."""
    embed = discord.Embed(
        title="🏆 Матч завершен",
        color=discord.Color.gold()
    )

    team1_text = "\n".join(matchmaking.team1_players)
    team2_text = "\n".join(matchmaking.team2_players)

    embed.add_field(name=f"{matchmaking.team1_name}:", value=team1_text, inline=True)
    embed.add_field(name=f"{matchmaking.team2_name}:", value=team2_text, inline=True)

    return embed
