"""Main view for mini-games menu."""

import discord
from discord.ui import Button, View

from storage.minigame_store import minigame_store


class GamesMainView(View):
    """Main view for mini-games menu."""

    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="🎲 Игры на удачу", style=discord.ButtonStyle.primary, custom_id="games_luck")
    async def luck_games(self, interaction: discord.Interaction, button: Button) -> None:
        """Show luck games."""
        games = await minigame_store.get_available_games()
        luck_games = [g for g in games if g.category == "luck"]

        embed = discord.Embed(
            title="🎲 Игры на удачу",
            description="Игры на удачу с механикой ставок",
            color=discord.Color.dark_blue(),
        )

        for game in luck_games:
            embed.add_field(
                name=f"{game.name} ({game.min_bet}-{game.max_bet} 🪙)",
                value=f"{game.description}\nМножитель: {game.multiplier}x",
                inline=False,
            )

        view = GamesBackView()
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="🧠 Викторины", style=discord.ButtonStyle.primary, custom_id="games_quiz")
    async def quiz_games(self, interaction: discord.Interaction, button: Button) -> None:
        """Show quiz games."""
        games = await minigame_store.get_available_games()
        quiz_games = [g for g in games if g.category == "quiz"]

        if not quiz_games:
            await interaction.response.send_message(
                "🚧 Викторины в разработке", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🧠 Викторины",
            description="Викторины и головоломки",
            color=discord.Color.dark_purple(),
        )

        for game in quiz_games:
            embed.add_field(
                name=f"{game.name} ({game.min_bet}-{game.max_bet} 🪙)",
                value=f"{game.description}\nМножитель: {game.multiplier}x",
                inline=False,
            )

        view = GamesBackView()
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="🎰 Казино", style=discord.ButtonStyle.primary, custom_id="games_casino")
    async def casino_games(self, interaction: discord.Interaction, button: Button) -> None:
        """Show casino games."""
        games = await minigame_store.get_available_games()
        casino_games = [g for g in games if g.category == "casino"]

        if not casino_games:
            await interaction.response.send_message(
                "🚧 Казино в разработке", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🎰 Казино",
            description="Казино и ставки",
            color=discord.Color.gold(),
        )

        for game in casino_games:
            embed.add_field(
                name=f"{game.name} ({game.min_bet}-{game.max_bet} 🪙)",
                value=f"{game.description}\nМножитель: {game.multiplier}x",
                inline=False,
            )

        view = GamesBackView()
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="⚔️ PvP", style=discord.ButtonStyle.primary, custom_id="games_pvp")
    async def pvp_games(self, interaction: discord.Interaction, button: Button) -> None:
        """Show PvP games."""
        games = await minigame_store.get_available_games()
        pvp_games = [g for g in games if g.category == "pvp"]

        if not pvp_games:
            await interaction.response.send_message(
                "🚧 PvP игры в разработке", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="⚔️ PvP игры",
            description="Игры против других игроков",
            color=discord.Color.red(),
        )

        for game in pvp_games:
            embed.add_field(
                name=f"{game.name} ({game.min_bet}-{game.max_bet} 🪙)",
                value=f"{game.description}\nМножитель: {game.multiplier}x",
                inline=False,
            )

        view = GamesBackView()
        await interaction.response.edit_message(embed=embed, view=view)


class GamesBackView(View):
    """View with back button."""

    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="🔙 Назад", style=discord.ButtonStyle.secondary, custom_id="games_back")
    async def back_button(self, interaction: discord.Interaction, button: Button) -> None:
        """Go back to main menu."""
        embed = discord.Embed(
            title="🎮 Мини-игры",
            description="Выберите категорию игр и поставьте монеты!",
            color=discord.Color.dark_blue(),
        )

        view = GamesMainView()
        await interaction.response.edit_message(embed=embed, view=view)
