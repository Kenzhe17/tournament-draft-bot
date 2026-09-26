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
                label="🧠 Викторины",
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
                label="🎮 Смешанные",
                value="mixed",
                description="Разные игровые механики",
                emoji="🎮"
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

        # Define category info with colors
        category_info = {
            "luck": ("🎲 Игры на удачу", "Игры на удачу с механикой ставок", discord.Color.dark_blue()),
            "quiz": ("🧠 Викторины", "Викторины и головоломки", discord.Color.dark_purple()),
            "casino": ("🎰 Казино", "Казино и ставки", discord.Color.gold()),
            "mixed": ("🎮 Смешанные", "Разные игровые механики", discord.Color.green()),
            "pvp": ("⚔️ PvP игры", "Игры против других игроков", discord.Color.red()),
        }

        title, description, color = category_info.get(category, ("Игры", "", discord.Color.blue()))

        if not category_games:
            await interaction.response.send_message(
                f"🚧 Категория '{title}' в разработке", ephemeral=True
            )
            return

        # Create games list without descriptions
        games_list = "\n".join([
            f"• **{game.name}** - `/{game.command_name}`" 
            for game in category_games
        ])

        embed = discord.Embed(
            title=title,
            description=f"{description}\n\n{games_list}",
            color=color,
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
                    description=f"Ставка: {game.min_bet}-{game.max_bet} 🪙",
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
        """Показать карточку игры."""
        command_name = self.values[0]
        games = await minigame_store.get_available_games()
        game = next((g for g in games if g.command_name == command_name), None)

        if not game:
            await interaction.response.send_message(
                f"❌ Игра не найдена.",
                ephemeral=True
            )
            return

        # Category colors
        category_colors = {
            "luck": discord.Color.dark_blue(),
            "quiz": discord.Color.dark_purple(),
            "casino": discord.Color.gold(),
            "mixed": discord.Color.green(),
            "pvp": discord.Color.red(),
        }

        color = category_colors.get(game.category, discord.Color.blue())

        # Player count display
        if game.is_pvp and game.is_pve:
            player_count = "1-2 игрока"
        elif game.is_pvp:
            player_count = "2 игрока"
        else:
            player_count = "1 игрок"

        embed = discord.Embed(
            title=f"{game.name}",
            description=game.description,
            color=color,
        )

        embed.add_field(name="💰 Ставка", value=f"{game.min_bet}-{game.max_bet} 🪙", inline=True)
        embed.add_field(name="🎲 Множитель", value=f"{game.multiplier}x", inline=True)
        embed.add_field(name="👥 Игроки", value=player_count, inline=True)
        embed.add_field(name="📌 Команда", value=f"`/{game.command_name}`", inline=True)
        embed.add_field(name="📊 Сложность", value=game.difficulty.capitalize(), inline=True)

        # Add play button
        view = GameCardView(command_name)
        await interaction.response.edit_message(embed=embed, view=view)


class GameCardView(View):
    """View with play button for game card."""

    def __init__(self, command_name: str):
        super().__init__(timeout=180)
        self.command_name = command_name
        self.add_item(PlayButton(command_name))
        self.add_item(BackButton())


class PlayButton(discord.ui.Button):
    """Button to play the game."""

    def __init__(self, command_name: str):
        super().__init__(
            label="▶️ Играть",
            style=discord.ButtonStyle.primary,
            custom_id=f"play_{command_name}"
        )
        self.command_name = command_name

    async def callback(self, interaction: discord.Interaction) -> None:
        """Execute the game command."""
        bot = interaction.client
        command = bot.tree.get_command(self.command_name)

        if command:
            # Execute the command
            await command.callback(interaction)
        else:
            await interaction.response.send_message(
                f"❌ Команда '{self.command_name}' не найдена.",
                ephemeral=True
            )


class BackButton(discord.ui.Button):
    """Button to go back to game list."""

    def __init__(self):
        super().__init__(
            label="🔙 Назад",
            style=discord.ButtonStyle.secondary,
            custom_id="card_back"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Go back to main menu."""
        embed = discord.Embed(
            title="🎮 Мини-игры",
            description="Выберите категорию игр и поставьте монеты!",
            color=discord.Color.dark_blue(),
        )

        view = GamesMainView()
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
