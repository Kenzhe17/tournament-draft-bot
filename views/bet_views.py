"""Views for betting system."""

import discord
from discord.ui import View, Button, button
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot import TournamentBot


class MatchSelectView(View):
    """View for selecting a match to bet on."""
    
    def __init__(self, guild_id: int, tournament, matches: list[tuple[int, int]], match_type: str):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.matches = matches
        self.match_type = match_type
        
        # Add buttons for each match
        for i, (team_a, team_b) in enumerate(matches):
            team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
            team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
            captain_a = team_a_data.get("captain", f"П{team_a + 1}")
            captain_b = team_b_data.get("captain", f"П{team_b + 1}")
            name_a = tournament.team_names.get(team_a, captain_a)
            name_b = tournament.team_names.get(team_b, captain_b)
            
            button = Button(
                label=f"Игра #{i + 1}: {name_a} vs {name_b}",
                style=discord.ButtonStyle.primary,
                custom_id=f"match_{i}"
            )
            button.callback = self._create_match_callback(i, team_a, team_b, name_a, name_b)
            self.add_item(button)
    
    def _create_match_callback(self, match_index: int, team_a: int, team_b: int, name_a: str, name_b: str):
        """Create a callback for a match button."""
        async def callback(interaction: discord.Interaction):
            team_view = TeamSelectView(
                self.guild_id,
                self.tournament,
                match_index,
                team_a,
                team_b,
                name_a,
                name_b,
                self.match_type
            )
            await interaction.response.send_message(
                content="Выберите команду:",
                view=team_view,
                ephemeral=True
            )
        return callback


class TeamSelectView(View):
    """View for selecting a team to bet on."""
    
    def __init__(self, guild_id: int, tournament, match_index: int, team_a: int, team_b: int, name_a: str, name_b: str, match_type: str):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.tournament = tournament
        self.match_index = match_index
        self.team_a = team_a
        self.team_b = team_b
        self.name_a = name_a
        self.name_b = name_b
        self.match_type = match_type
        
        # Add buttons dynamically
        team_a_btn = Button(label=name_a, style=discord.ButtonStyle.primary)
        team_a_btn.callback = self.team_a_button
        self.add_item(team_a_btn)
        
        team_b_btn = Button(label=name_b, style=discord.ButtonStyle.primary)
        team_b_btn.callback = self.team_b_button
        self.add_item(team_b_btn)
    
    async def team_a_button(self, interaction: discord.Interaction):
        """Handle team A selection."""
        from views.bet_modal import BetAmountModal
        modal = BetAmountModal(
            self.guild_id,
            self.tournament,
            self.match_index,
            self.team_a,
            self.name_a,
            self.match_type
        )
        await interaction.response.send_modal(modal)
    
    async def team_b_button(self, interaction: discord.Interaction):
        """Handle team B selection."""
        from views.bet_modal import BetAmountModal
        modal = BetAmountModal(
            self.guild_id,
            self.tournament,
            self.match_index,
            self.team_b,
            self.name_b,
            self.match_type
        )
        await interaction.response.send_modal(modal)


class ViewBetsButton(Button):
    """Button to view detailed bets for a match."""
    
    def __init__(self, guild_id: int, tournament, matches: list[tuple[int, int]], match_type: str):
        super().__init__(
            label="📊 Посмотреть ставки",
            style=discord.ButtonStyle.secondary,
            custom_id="view_bets"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.matches = matches
        self.match_type = match_type
    
    async def callback(self, interaction: discord.Interaction):
        """Show detailed bets for all matches."""
        from storage.bet_store import bet_store
        
        bets_text = []
        for i, (team_a, team_b) in enumerate(self.matches):
            match_id = f"{self.match_type}_{i}"
            bets = await bet_store.get_bets_by_match(match_id)
            
            team_a_data = self.tournament.teams[team_a] if team_a < len(self.tournament.teams) else {}
            team_b_data = self.tournament.teams[team_b] if team_b < len(self.tournament.teams) else {}
            captain_a = team_a_data.get("captain", f"П{team_a + 1}")
            captain_b = team_b_data.get("captain", f"П{team_b + 1}")
            name_a = self.tournament.team_names.get(team_a, captain_a)
            name_b = self.tournament.team_names.get(team_b, captain_b)
            
            match_text = f"🔥 Игра #{i + 1}\n{name_a} vs {name_b}\n\n"
            
            # Group bets by team
            team_a_bets = [b for b in bets if b.team_name == name_a]
            team_b_bets = [b for b in bets if b.team_name == name_b]
            
            if team_a_bets:
                match_text += f"**{name_a}:**\n"
                for bet in team_a_bets:
                    match_text += f"• {bet.user_name} — {bet.amount} 🪙\n"
                match_text += "\n"
            
            if team_b_bets:
                match_text += f"**{name_b}:**\n"
                for bet in team_b_bets:
                    match_text += f"• {bet.user_name} — {bet.amount} 🪙\n"
                match_text += "\n"
            
            if not team_a_bets and not team_b_bets:
                match_text += "*Пока нет ставок*\n"
            
            bets_text.append(match_text)
        
        await interaction.response.send_message(
            content="\n".join(bets_text),
            ephemeral=True
        )


class CloseBettingButton(Button):
    """Button for admin to close betting."""
    
    def __init__(self, guild_id: int):
        super().__init__(
            label="🔒 Закрыть ставки",
            style=discord.ButtonStyle.danger,
            custom_id="close_betting"
        )
        self.guild_id = guild_id
    
    async def callback(self, interaction: discord.Interaction):
        """Close betting for the tournament."""
        from storage.json_store import store
        from utils.permissions import is_admin
        
        # Check if user is admin
        if not is_admin(interaction):
            await interaction.response.send_message(
                "❌ Только администратор может закрывать ставки.",
                ephemeral=True
            )
            return
        
        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Турнир не найден.",
                ephemeral=True
            )
            return
        
        tournament.betting_open = False
        store.set(tournament)
        
        await interaction.response.send_message(
            "✅ Ставки закрыты.",
            ephemeral=True
        )
        
        # Update the tournament message
        from bot import TournamentBot
        bot = interaction.client
        await bot.update_tournament_message(interaction.guild, tournament)


class BetButton(Button):
    """Button to open betting flow."""
    
    def __init__(self, guild_id: int, tournament, matches: list[tuple[int, int]], match_type: str):
        super().__init__(
            label="💰 Сделать ставку",
            style=discord.ButtonStyle.success,
            custom_id="place_bet"
        )
        self.guild_id = guild_id
        self.tournament = tournament
        self.matches = matches
        self.match_type = match_type
    
    async def callback(self, interaction: discord.Interaction):
        """Open match selection view."""
        if not self.tournament.betting_open:
            await interaction.response.send_message(
                "❌ Ставки закрыты.",
                ephemeral=True
            )
            return
        
        match_view = MatchSelectView(
            self.guild_id,
            self.tournament,
            self.matches,
            self.match_type
        )
        await interaction.response.send_message(
            content="Выберите матч:",
            view=match_view,
            ephemeral=True
        )
