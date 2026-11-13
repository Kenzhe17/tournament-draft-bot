"""Persistent view кнопок голосования за правила (BRAZIL/KAZAKHSTAN).

Custom_id вида ``rv:{guild}:{channel}:{fmt}`` — dispatch не зависит
от сообщения; view регистрируется глобально (bot.add_view) и
восстанавливается в setup_hook для каждого турнира с rules_vote.
"""

from __future__ import annotations

import discord


class RulesVoteButtonsView(discord.ui.View):
    """Две кнопки правил для одного турнира (persistent)."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(timeout=None)
        for fmt, label in (
            ("brazil", "BRAZIL"),
            ("kazakhstan", "KAZAKHSTAN"),
        ):
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=f"rv:{guild_id}:{channel_id}:{fmt}",
            )

            async def _cb(interaction, _fmt=fmt):
                from services.rules_vote import handle_rules_vote_click

                await handle_rules_vote_click(interaction, _fmt)

            btn.callback = _cb
            self.add_item(btn)
