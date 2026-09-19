"""Обновление главного сообщения турнира."""

from __future__ import annotations

import logging

import discord

from bot.embeds import build_tournament_embed
from bot.models import Tournament, TournamentPhase
from bot.storage import storage
from bot.views.bracket import BracketView, FinalView, GenerateMatchesView
from bot.views.draft_select import DraftSelectView

logger = logging.getLogger(__name__)


def get_view_for_tournament(tournament: Tournament) -> discord.ui.View | None:
    """Вернуть View с кнопками/меню для текущей фазы."""
    if tournament.phase == TournamentPhase.DRAFT and tournament.draft:
        available = _get_select_options(tournament)
        if available:
            return DraftSelectView(tournament.guild_id, available)
        return None
    if tournament.phase == TournamentPhase.TEAMS:
        return GenerateMatchesView(tournament.guild_id)
    if tournament.phase == TournamentPhase.SEMIFINALS and tournament.bracket:
        return BracketView(tournament.guild_id, tournament.bracket)
    if tournament.phase == TournamentPhase.FINAL and tournament.bracket:
        return FinalView(tournament.guild_id, tournament.bracket)
    return None


def _get_select_options(tournament: Tournament) -> list[str]:
    from bot.draft_engine import get_available_players, get_current_picker

    if not tournament.draft:
        return []
    if get_current_picker(tournament.draft) is None:
        return []
    return get_available_players(tournament, tournament.draft.current_circle)


async def update_tournament_message(
    bot: discord.Client,
    guild: discord.Guild,
    tournament: Tournament,
) -> None:
    """Редактировать единственное сообщение турнира."""
    if not tournament.message_id or not tournament.channel_id:
        return

    channel = guild.get_channel(tournament.channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        logger.warning("Канал турнира не найден: %s", tournament.channel_id)
        return

    try:
        message = await channel.fetch_message(tournament.message_id)
    except discord.NotFound:
        logger.warning("Сообщение турнира не найдено: %s", tournament.message_id)
        return
    except discord.HTTPException as exc:
        logger.error("Ошибка загрузки сообщения: %s", exc)
        return

    embed = await build_tournament_embed(guild, tournament)
    view = get_view_for_tournament(tournament)
    try:
        await message.edit(embed=embed, view=view)
    except discord.HTTPException as exc:
        logger.error("Ошибка редактирования сообщения: %s", exc)


async def refresh_tournament(bot: discord.Client, guild_id: int) -> Tournament | None:
    """Загрузить турнир из хранилища и обновить сообщение."""
    tournament = storage.get(guild_id)
    if not tournament:
        return None
    guild = bot.get_guild(guild_id)
    if guild:
        await update_tournament_message(bot, guild, tournament)
    return tournament
