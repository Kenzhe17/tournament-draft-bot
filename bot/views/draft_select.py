"""Select Menu для выбора игроков в драфте."""

from __future__ import annotations

import discord

from bot.config import VIEW_DRAFT_SELECT
from bot.draft_engine import (
    apply_pick,
    get_auto_captain_index,
    get_available_players,
    get_current_picker,
    step_after_manual_pick,
)
from bot.embeds import captain_name
from bot.models import TournamentPhase
from bot.storage import storage


class PlayerSelect(discord.ui.Select):
    """Выпадающий список доступных игроков."""

    def __init__(self, guild_id: int, options: list[str]) -> None:
        self.guild_id = guild_id
        select_options = [
            discord.SelectOption(label=name[:100], value=name)
            for name in options[:25]
        ]
        super().__init__(
            placeholder="Выберите игрока...",
            min_values=1,
            max_values=1,
            options=select_options,
            custom_id=f"{VIEW_DRAFT_SELECT}:{guild_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return

        tournament = storage.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.DRAFT or not tournament.draft:
            await interaction.response.send_message("❌ Драфт не активен.", ephemeral=True)
            return

        draft = tournament.draft
        current_picker = get_current_picker(draft)
        if current_picker is None:
            await interaction.response.send_message("❌ Сейчас не ваш ход.", ephemeral=True)
            return

        if interaction.user.id != current_picker:
            picker_name = await captain_name(interaction.guild, current_picker)
            await interaction.response.send_message(
                f"❌ Сейчас выбирает {picker_name}",
                ephemeral=True,
            )
            return

        player = self.values[0]
        available = get_available_players(tournament, draft.current_circle)
        if player not in available:
            await interaction.response.send_message("❌ Игрок уже выбран.", ephemeral=True)
            return

        apply_pick(draft, current_picker, player)

        # Имя капитана для авто-назначения в конце круга
        auto_idx = get_auto_captain_index(draft.current_circle)
        auto_cap_id = draft.captain_order[auto_idx]
        auto_cap_name = await captain_name(interaction.guild, auto_cap_id)

        completed = step_after_manual_pick(draft, tournament, auto_cap_name)

        if completed or draft.teams:
            tournament.phase = TournamentPhase.TEAMS

        storage.save(tournament)

        from bot.message_manager import update_tournament_message

        await interaction.response.defer()
        await update_tournament_message(interaction.client, interaction.guild, tournament)


class DraftSelectView(discord.ui.View):
    """View с Select Menu для драфта (persistent)."""

    def __init__(self, guild_id: int, options: list[str]) -> None:
        super().__init__(timeout=None)
        self.add_item(PlayerSelect(guild_id, options))
