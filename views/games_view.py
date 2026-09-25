"""Main view for mini-games menu."""

import discord
from discord.ui import Button, View

from storage.minigame_store import minigame_store


class GamesMainView(View):
    """Main view for mini-games menu."""

    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(GameCategorySelect())


class GameCategorySelect(discord.ui.Select):
    """Выпадающее меню выбора категории игр."""

    def __init__(self):
        options = [
            discord.SelectOption(
                label="🎲 Игры на удачу",
                value="luck",
                description="Игры на удачу с механикой ставок",
                emoji="🎲"
            ),
            discord.SelectOption(
                label="� Викторины",
                value="quiz",
                description="Викторины и головоломки",
                emoji="🧠"
            ),
            discord.SelectOption(
                label="🎰 Казино",
                value="casino",
                description="Казино и ставки",
                emoji="🎰"
            ),
            discord.SelectOption(
                label="⚔️ PvP",
                value="pvp",
                description="Игры против других игроков",
                emoji="⚔️"
            ),
        ]
        super().__init__(
            placeholder="Выберите категорию игр...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать выбор категории."""
        category = self.values[0]
        games = await minigame_store.get_available_games()
        category_games = [g for g in games if g.category == category]

        # Define category info
        category_info = {
            "luck": ("🎲 Игры на удачу", "Игры на удачу с механикой ставок", discord.Color.dark_blue()),
            "quiz": ("🧠 Викторины", "Викторины и головоломки", discord.Color.dark_purple()),
            "casino": ("🎰 Казино", "Казино и ставки", discord.Color.gold()),
            "pvp": ("⚔️ PvP игры", "Игры против других игроков", discord.Color.red()),
        }

        title, description, color = category_info.get(category, ("Игры", "", discord.Color.blue()))

        if not category_games:
            await interaction.response.send_message(
                f"🚧 Категория '{title}' в разработке", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
        )

        for game in category_games:
            embed.add_field(
                name=f"{game.name} ({game.min_bet}-{game.max_bet} 🪙)",
                value=f"{game.description}\nМножитель: {game.multiplier}x",
                inline=False,
            )

        view = GamesBackView()
        view.add_item(GameLaunchSelect(category_games))
        await interaction.response.edit_message(embed=embed, view=view)


class GameLaunchSelect(discord.ui.Select):
    """Выпадающее меню для запуска игры."""

    def __init__(self, games):
        options = []
        for game in games:
            command_name = getattr(game, 'command_name', game.id)
            options.append(
                discord.SelectOption(
                    label=game.name,
                    value=command_name,
                    description=f"{game.description} ({game.min_bet}-{game.max_bet} 🪙)",
                    emoji="🎮"
                )
            )

        super().__init__(
            placeholder="Выберите игру для запуска...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Запустить выбранную игру."""
        command_name = self.values[0]

        # Get the command
        bot = interaction.client
        command = bot.tree.get_command(command_name)

        if command:
            # Execute the command
            await command.callback(interaction)
        else:
            await interaction.response.send_message(
                f"❌ Команда '{command_name}' не найдена.",
                ephemeral=True
            )


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
