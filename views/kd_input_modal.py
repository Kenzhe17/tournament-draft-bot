"""Modal for inputting K/D statistics for match players."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from models.tournament import Tournament


class KDInputModal(discord.ui.Modal):
    """Modal for inputting Kills/Deaths for a player."""

    def __init__(self, guild_id: int, player_name: str, circle: int, team_index: int, tournament: Tournament | None = None):
        super().__init__(title=f"Статистика: {player_name}")
        self.guild_id = guild_id
        self.player_name = player_name  # Keep original name for data storage
        self.circle = circle
        self.team_index = team_index

        self.kills_input = discord.ui.TextInput(
            label="Kills",
            placeholder="Количество убийств",
            required=True,
            max_length=3,
        )
        self.add_item(self.kills_input)

        self.deaths_input = discord.ui.TextInput(
            label="Deaths",
            placeholder="Количество смертей",
            required=True,
            max_length=3,
        )
        self.add_item(self.deaths_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            kills = int(self.kills_input.value)
            deaths = int(self.deaths_input.value)
        except ValueError:
            await interaction.response.send_message(
                "❌ Пожалуйста, введите числовые значения.",
                ephemeral=True
            )
            return

        if kills < 0 or deaths < 0:
            await interaction.response.send_message(
                "❌ Значения не могут быть отрицательными.",
                ephemeral=True
            )
            return

        # Store the K/D data temporarily (will be used when all players are entered)
        # For now, we'll use a simple approach: store in a temporary dict
        from storage.json_store import store
        tournament = store.get(self.guild_id)
        if tournament:
            if not hasattr(tournament, 'temp_kd_data'):
                tournament.temp_kd_data = {}
            
            tournament.temp_kd_data[self.player_name] = {
                'kills': kills,
                'deaths': deaths,
                'circle': self.circle,
                'team_index': self.team_index,
            }
            store.set(tournament)

        await interaction.response.send_message(
            f"✅ Статистика для {self.display_name}: {kills}/{deaths}",
            ephemeral=True
        )


class TeamKDInputModal(discord.ui.Modal):
    """Modal for inputting K/D for all players in a team."""

    def __init__(self, guild_id: int, team_index: int, team_name: str, players: list[tuple[str, int]], tournament: Tournament | None = None):
        """
        Args:
            guild_id: Server ID
            team_index: Team index (0 or 1)
            team_name: Team name for display
            players: List of (player_name, circle) tuples
            tournament: Tournament object for game nickname lookup
        """
        super().__init__(title=f"Статистика: {team_name}")
        self.guild_id = guild_id
        self.team_index = team_index
        self.players = players
        self.tournament = tournament

        # Create input fields for each player
        for player_name, circle in players:
            # Use a shorter label for the input
            label = f"{player_name} (K/D)"
            # Create a single text input for K/D format like "8 2"
            kd_input = discord.ui.TextInput(
                label=label,
                placeholder="Формат: kills deaths (например: 8 2)",
                required=True,
                max_length=10,
            )
            # Store player info in the custom_id for later retrieval
            kd_input.custom_id = f"{player_name}|{circle}"
            self.add_item(kd_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Process K/D input for all players in the team."""
        from storage.json_store import store
        
        tournament = store.get(self.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Турнир не найден.",
                ephemeral=True
            )
            return

        if not hasattr(tournament, 'temp_kd_data'):
            tournament.temp_kd_data = {}

        # Parse K/D for each player
        for item in self.children:
            if isinstance(item, discord.ui.TextInput):
                player_name, circle = item.custom_id.split('|')
                circle = int(circle)
                kd_str = item.value.strip()

                try:
                    # Parse format "kills deaths"
                    parts = kd_str.split()
                    if len(parts) != 2:
                        raise ValueError
                    
                    kills = int(parts[0].strip())
                    deaths = int(parts[1].strip())

                    if kills < 0 or deaths < 0:
                        raise ValueError

                    tournament.temp_kd_data[player_name] = {
                        'kills': kills,
                        'deaths': deaths,
                        'circle': circle,
                        'team_index': self.team_index,
                    }
                except (ValueError, IndexError):
                    await interaction.response.send_message(
                        f"❌ Неверный формат для {player_name}. Используйте формат: kills deaths (например: 8 2)",
                        ephemeral=True
                    )
                    return

        store.set(tournament)

        await interaction.response.send_message(
            f"✅ Статистика команды сохранена!",
            ephemeral=True
        )
