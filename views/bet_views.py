from config import replace_emojis, get_emoji
"""Views for betting system."""

import discord
from utils.design import compose, text_card
from discord.ui import View, Button, Select
from discord import SelectOption
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot import TournamentBot


def _match_names(tournament, team_a: int, team_b: int) -> tuple[str, str]:
    team_a_data = (
        tournament.teams[team_a] if team_a < len(tournament.teams) else {}
    )
    team_b_data = (
        tournament.teams[team_b] if team_b < len(tournament.teams) else {}
    )
    captain_a = team_a_data.get("captain", f"П{team_a + 1}")
    captain_b = team_b_data.get("captain", f"П{team_b + 1}")
    name_a = tournament.team_names.get(team_a, captain_a)
    name_b = tournament.team_names.get(team_b, captain_b)
    return name_a, name_b


class MatchSelectView(View):
    """Выбор матча для ставки — один Select вместо ряда кнопок."""

    def __init__(self, guild_id: int, channel_id: int, tournament,
                 matches: list[tuple[int, int]], match_type: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.matches = matches
        self.match_type = match_type

        options = []
        for i, (team_a, team_b) in enumerate(matches[:25]):
            name_a, name_b = _match_names(tournament, team_a, team_b)
            label = f"Матч #{i + 1} — {name_a} vs {name_b}"
            options.append(SelectOption(
                label=label[:100], value=str(i),
                description=f"{name_a[:45]} vs {name_b[:45]}",
            ))
        if options:
            select = Select(
                placeholder="Выберите матч",
                options=options,
                custom_id="bet_match_select",
                min_values=1,
                max_values=1,
            )
            async def _cb(interaction, _s=select):
                await self._on_select(interaction, _s)
            select.callback = _cb
            self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction,
                         select: Select) -> None:
        try:
            idx = int(select.values[0])
        except (ValueError, IndexError):
            await interaction.response.send_message(
                "Некорректный выбор.", ephemeral=True)
            return
        if idx >= len(self.matches):
            await interaction.response.send_message(
                "Матч не найден.", ephemeral=True)
            return
        team_a, team_b = self.matches[idx]
        name_a, name_b = _match_names(self.tournament, team_a, team_b)

        # Актуальные коэффициенты для карточки выбора команды
        odds_a = odds_b = 1.9
        try:
            from storage.bet_store import bet_store
            match_id = (
                f"{self.tournament.id}_{self.match_type}_{idx}"
            )
            odds_data = await bet_store.get_current_odds_sync(match_id)
            if odds_data:
                odds_a = odds_data.team_a_odds
                odds_b = odds_data.team_b_odds
        except Exception:
            pass

        team_view = TeamSelectView(
            self.guild_id, self.channel_id, self.tournament,
            idx, team_a, team_b, name_a, name_b,
            self.match_type, odds_a, odds_b,
        )
        await interaction.response.send_message(
            view=compose(
                text=(
                    f"### Матч #{idx + 1}\n"
                    f"**{name_a}** `{odds_a:.2f}x` vs "
                    f"**{name_b}** `{odds_b:.2f}x`\n"
                    "-# Выберите команду."
                ),
                view=team_view,
            ),
            ephemeral=True,
        )


class TeamSelectView(View):
    """Выбор команды — две компактные secondary-кнопки."""

    def __init__(self, guild_id: int, channel_id: int, tournament,
                 match_index: int, team_a: int, team_b: int,
                 name_a: str, name_b: str, match_type: str,
                 odds_a: float = 1.9, odds_b: float = 1.9):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.match_index = match_index
        self.team_a = team_a
        self.team_b = team_b
        self.name_a = name_a
        self.name_b = name_b
        self.match_type = match_type
        self.odds_a = odds_a
        self.odds_b = odds_b

        btn_a = Button(
            label=name_a[:80], style=discord.ButtonStyle.secondary)
        btn_a.callback = self.team_a_button
        self.add_item(btn_a)

        btn_b = Button(
            label=name_b[:80], style=discord.ButtonStyle.secondary)
        btn_b.callback = self.team_b_button
        self.add_item(btn_b)

    async def team_a_button(self, interaction: discord.Interaction):
        """Handle team A selection."""
        # Проверка: игрок не может ставить против своей команды
        user_team_index = self.tournament.get_player_team_index(interaction.user.id)
        if user_team_index is not None and user_team_index == self.team_b:
            await interaction.response.send_message(
                "Вы не можете ставить против своей команды.",
                ephemeral=True
            )
            return

        from views.bet_modal import BetAmountModal
        modal = BetAmountModal(
            self.guild_id,
            self.tournament,
            self.match_type,
            self.match_index,
            self.name_a,  # Team name, not index
            self.name_a,
            self.name_b
        )
        await interaction.response.send_modal(modal)

    async def team_b_button(self, interaction: discord.Interaction):
        """Handle team B selection."""
        # Проверка: игрок не может ставить против своей команды
        user_team_index = self.tournament.get_player_team_index(interaction.user.id)
        if user_team_index is not None and user_team_index == self.team_a:
            await interaction.response.send_message(
                "Вы не можете ставить против своей команды.",
                ephemeral=True
            )
            return

        from views.bet_modal import BetAmountModal
        modal = BetAmountModal(
            self.guild_id,
            self.tournament,
            self.match_type,
            self.match_index,
            self.name_b,  # Team name, not index
            self.name_a,
            self.name_b
        )
        await interaction.response.send_modal(modal)


class ViewBetsButton(Button):
    """Button to view detailed bets for a match."""

    def __init__(self, guild_id: int, channel_id: int, tournament, matches: list[tuple[int, int]], match_type: str):
        super().__init__(
            label="Посмотреть ставки",
            style=discord.ButtonStyle.secondary,
            custom_id="view_bets"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.matches = matches
        self.match_type = match_type

    async def callback(self, interaction: discord.Interaction):
        """Show detailed bets for all matches."""
        from storage.bet_store import bet_store

        bets_text = []
        for i, (team_a, team_b) in enumerate(self.matches):
            match_id = f"{self.tournament.id}_{self.match_type}_{i}"
            bets = await bet_store.get_bets_by_match(self.tournament.id, match_id)

            name_a, name_b = _match_names(self.tournament, team_a, team_b)

            # Get odds for this match
            current_odds = await bet_store.get_current_odds_sync(match_id)
            if current_odds:
                odds_a = current_odds.team_a_odds
                odds_b = current_odds.team_b_odds
            else:
                odds_a = 1.9
                odds_b = 1.9

            match_text = (
                f"Матч #{i + 1}: {name_a} `{odds_a:.2f}x` vs "
                f"{name_b} `{odds_b:.2f}x`\n\n"
            )

            # Group bets by team (using team_index)
            team_a_bets = [b for b in bets if b.team_index == 0]
            team_b_bets = [b for b in bets if b.team_index == 1]

            # Calculate totals
            team_a_total = sum(b.amount for b in team_a_bets)
            team_b_total = sum(b.amount for b in team_b_bets)

            match_text += f"`Ставки:`\n"

            if team_a_bets:
                users_with_amounts = [f"<@{b.user_id}> ({b.amount:,} {get_emoji('money')})" for b in team_a_bets]
                users_str = ", ".join(users_with_amounts)
                match_text += f">  **{name_a}**: {team_a_total:,} {get_emoji('money')} - {users_str}\n"
            else:
                match_text += f">  **{name_a}**: *Нет ставок*\n"

            if team_b_bets:
                users_with_amounts = [f"<@{b.user_id}> ({b.amount:,} {get_emoji('money')})" for b in team_b_bets]
                users_str = ", ".join(users_with_amounts)
                match_text += f">  **{name_b}**: {team_b_total:,} {get_emoji('money')} - {users_str}\n"
            else:
                match_text += f">  **{name_b}**: *Нет ставок*\n"

            bets_text.append(match_text)

        await interaction.response.send_message(
            view=text_card("\n".join(bets_text)),
            ephemeral=True,
        )


class ToggleBettingButton(Button):
    """Button for admin to toggle betting open/close."""

    def __init__(self, guild_id: int, channel_id: int, betting_open: bool):
        label = "Закрыть ставки" if betting_open else "Открыть ставки"
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id="toggle_betting"
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        """Toggle betting for the tournament."""
        try:
            from storage.json_store import store
            from utils.permissions import can_manage_channel_tournament

            # Check if user is org
            if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
                await interaction.response.send_message(
                    replace_emojis("Только организаторы (роль 'org') могут менять статус ставок."),
                    ephemeral=True
                )
                return

            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("Турнир не найден."),
                    ephemeral=True
                )
                return

            # Toggle betting status
            tournament.betting_open = not tournament.betting_open
            status = "открыты" if tournament.betting_open else "закрыты"
            store.set(tournament)

            await interaction.response.send_message(
                replace_emojis("Ставки {status}."),
                ephemeral=True
            )

            # Update the tournament message
            from bot import TournamentBot
            bot = interaction.client
            await bot.update_tournament_message(interaction.guild, tournament)
        except Exception as e:
            import logging
            logging.error(f"Error toggling betting: {e}", exc_info=True)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        replace_emojis(f"Ошибка при изменении статуса ставок: {str(e)}"),
                        ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        replace_emojis(f"Ошибка при изменении статуса ставок: {str(e)}"),
                        ephemeral=True
                    )
            except:
                pass


class BetButton(Button):
    """Button to open betting flow."""

    def __init__(self, guild_id: int, channel_id: int, tournament, matches: list[tuple[int, int]], match_type: str):
        # Check if betting is open for this phase
        is_open = tournament.is_betting_open() and tournament.betting_phase == match_type
        super().__init__(
            label="Сделать ставку",
            style=discord.ButtonStyle.primary,
            custom_id="place_bet",
            disabled=not is_open
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament = tournament
        self.matches = matches
        self.match_type = match_type

    async def callback(self, interaction: discord.Interaction):
        """Open match selection view."""
        if not self.tournament.is_betting_open() or self.tournament.betting_phase != self.match_type:
            await interaction.response.send_message(
                replace_emojis("Ставки закрыты."),
                ephemeral=True
            )
            return

        match_view = MatchSelectView(
            self.guild_id,
            self.channel_id,
            self.tournament,
            self.matches,
            self.match_type
        )
        await interaction.response.send_message( view=compose(text="Выберите матч для ставки:", view=match_view),
            ephemeral=True
        )
