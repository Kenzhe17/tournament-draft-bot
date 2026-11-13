"""Информационные кнопки турнирной сетки: «📜 Правила» и «🎮 Формат».

Показываются на фазах QUALIFIERS / SEMIFINALS / FINAL внутри
существующего Components V2 View. Кнопки не меняют состояние —
отвечают ephemeral-карточками. Состояние читается заново из
storage.json_store при каждом нажатии (persistent safe).
"""

from __future__ import annotations

import discord

from storage.json_store import store
from utils.design import text_card


class TournamentRulesButton(discord.ui.Button):
    """📜 Правила — ephemeral карточка с _MATCH_RULES_TEXT."""

    def __init__(self, guild_id: int, channel_id: int) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="📜 Правила",
            custom_id=f"tinfo:{guild_id}:{channel_id}:rules",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from services.match_announcer import _MATCH_RULES_TEXT

        await interaction.response.send_message(
            view=text_card(_MATCH_RULES_TEXT, title="📜 ПРАВИЛА МАТЧА"),
            ephemeral=True,
        )


class TournamentFormatButton(discord.ui.Button):
    """🎮 Формат — ephemeral карточка с выбранным форматом."""

    def __init__(self, guild_id: int, channel_id: int) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="🎮 Формат",
            custom_id=f"tinfo:{guild_id}:{channel_id}:format",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        ruleset = (
            (getattr(tournament, "ruleset", "") or "").upper()
            if tournament is not None else ""
        )
        if not ruleset:
            text = "Формат ещё не определён"
        else:
            from services.format_vote import _FORMATS
            from services.rules_vote import ruleset_label

            key = ruleset.lower()
            fmt = _FORMATS.get(key, {})
            label = ruleset_label(ruleset)
            rules = fmt.get("rules", "")
            text = f"**{label}**\n\n{rules}" if rules else f"**{label}**"
        await interaction.response.send_message(
            view=text_card(text, title="🎮 ФОРМАТ МАТЧА"),
            ephemeral=True,
        )


def add_tournament_info_buttons(view, guild_id: int, channel_id: int) -> None:
    """Добавить «Правила»/«Формат» в конец View (макс. 25 кнопок)."""
    if len(view.children) + 2 > 25:
        return
    view.add_item(TournamentRulesButton(guild_id, channel_id))
    view.add_item(TournamentFormatButton(guild_id, channel_id))
