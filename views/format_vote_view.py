"""Persistent view кнопок голосования за формат (Format Vote).

Custom_id вида ``fv:{guild}:{channel}:{vote_id}:{fmt}`` —
``vote_id`` — уникальное поколение голосования конкретного матча.
Канал пула матчей переиспользуется между матчами, поэтому без
vote_id кнопки прошлого матча были бы неотличимы от кнопок нового;
с vote_id устаревшие кнопки отклоняются в handle_vote_click.

View регистрируется глобально (bot.add_view) для каждого
активного format_vote и восстанавливается в setup_hook.
"""

from __future__ import annotations

import discord


class FormatVoteButtonsView(discord.ui.View):
    """Две кнопки формата для одного голосования (persistent)."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        vote_id: str,
    ):
        super().__init__(timeout=None)
        for fmt, label in (
            ("brazil", "🇧🇷 Brazil"),
            ("kazakhstan", "🇰🇿 Kazakhstan"),
        ):
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=(
                    f"fv:{guild_id}:{channel_id}:{vote_id}:{fmt}"
                ),
            )

            async def _cb(interaction, _fmt=fmt):
                from services.format_vote import handle_vote_click

                await handle_vote_click(interaction, _fmt)

            btn.callback = _cb
            self.add_item(btn)


class LegacyFormatVoteView(discord.ui.View):
    """Persistent view для кнопок, опубликованных ДО введения
    vote_id: ``fv:{guild}:{channel}:{match_type}:{match_index}:{fmt}``.

    Регистрируется при restore для каждого активного
    format_vote — иначе после рестарта старые кнопки не имеют
    обработчика (discord.py роутит persistent views по
    точному custom_id). Callback ведёт в тот же
    handle_vote_click: legacy custom_id резолвится по
    (match_type, match_index) + проверке vote_message_id.
    """

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        match_type: str,
        match_index: int,
    ):
        super().__init__(timeout=None)
        for fmt, label in (
            ("brazil", "🇧🇷 Brazil"),
            ("kazakhstan", "🇰🇿 Kazakhstan"),
        ):
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=(
                    f"fv:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{fmt}"
                ),
            )

            async def _cb(interaction, _fmt=fmt):
                from services.format_vote import handle_vote_click

                await handle_vote_click(interaction, _fmt)

            btn.callback = _cb
            self.add_item(btn)
