"""Persistent view кнопок голосования за формат (Format Vote).

Custom_id вида ``fv:{guild}:{channel}:{tournament}:{match_type}:
{match_index}:{fmt}`` — tournament_id обязателен: каналы
MM_MATCH_CHANNEL_POOL переиспользуются, без него кнопки разных
матчей в одном канале делили бы custom_id и lock-ключи.
— dispatch не зависит от сообщения, поэтому view регистрируется
глобально (bot.add_view) и восстанавливается в setup_hook для
каждого матча с состоянием format_vote.
"""

from __future__ import annotations

import discord


class FormatVoteButtonsView(discord.ui.View):
    """Две кнопки формата для одного матча (persistent, timeout=None)."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament_id,
        match_type: str,
        match_index: int,
    ):
        super().__init__(timeout=None)
        from services.format_vote import _custom_id

        for fmt, label in (
            ("brazil", "🇧🇷 Brazil"),
            ("kazakhstan", "🇰🇿 Kazakhstan"),
        ):
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=_custom_id(
                    guild_id, channel_id, tournament_id,
                    match_type, match_index, fmt,
                ),
            )

            async def _cb(interaction, _fmt=fmt):
                from services.format_vote import handle_vote_click

                await handle_vote_click(interaction, _fmt)

            btn.callback = _cb
            self.add_item(btn)
