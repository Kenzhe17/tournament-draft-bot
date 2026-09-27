"""Enhanced view for mini-games menu with categories and game cards."""

import discord
from discord.ui import Button, View, Select
from typing import Optional

from games_config import (
    CATEGORIES,
    get_game_by_id,
    get_games_by_category,
    get_all_games,
)
from storage.user_balance_store import user_balance_store


class GamesMainView(View):
    """Main view for mini-games menu with user balance."""

    def __init__(self, user_id: int, guild_id: int):
        super().__init__(timeout=180)  # 3 минуты
        self.user_id = user_id
        self.guild_id = guild_id
        self.add_item(GameCategorySelect(user_id, guild_id))

    async def get_balance(self) -> int:
        """Получить баланс пользователя."""
        return await user_balance_store.get_balance(self.guild_id, self.user_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Проверка: только пользователь который вызвал /games может нажимать."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Это меню может использовать только тот, кто его вызвал.",
                ephemeral=True
            )
            return False
        return True


class GameCategorySelect(Select):
    """Выпадающее меню выбора категории игр."""

    def __init__(self, user_id: int, guild_id: int):
        self.user_id = user_id
        self.guild_id = guild_id
        
        options = []
        for cat_id, cat_info in CATEGORIES.items():
            options.append(
                discord.SelectOption(
                    label=cat_info["name"],
                    value=cat_id,
                    description=cat_info["description"],
                    emoji=cat_info["emoji"]
                )
            )
        
        super().__init__(
            placeholder="Выберите категорию игр...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать выбор категории."""
        category = self.values[0]
        category_info = CATEGORIES[category]
        games = get_games_by_category(category)

        # Создать список игр со статусами
        games_list = []
        for game in games:
            status_emoji = "🟢" if game.status == "available" else "🚧"
            status_text = "Доступна" if game.status == "available" else "В разработке"
            games_list.append(f"{status_emoji} **{game.name}** - {status_text}")
            games_list.append(f"   └ {game.short_description}")
        
        games_text = "\n".join(games_list)

        embed = discord.Embed(
            title=f"{category_info['emoji']} {category_info['name']}",
            description=f"{category_info['description']}\n\n{games_text}",
            color=category_info['color'],
        )

        view = GamesCategoryView(self.user_id, self.guild_id, category)
        view.add_item(GameSelect(games, self.user_id, self.guild_id))
        await interaction.response.edit_message(embed=embed, view=view)


class GamesCategoryView(View):
    """View для экрана категории."""

    def __init__(self, user_id: int, guild_id: int, category: str):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.guild_id = guild_id
        self.category = category
        self.add_item(BackToMainMenuButton(user_id, guild_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Проверка: только пользователь который вызвал /games может нажимать."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Это меню может использовать только тот, кто его вызвал.",
                ephemeral=True
            )
            return False
        return True


class GameSelect(Select):
    """Выпадающее меню для выбора игры."""

    def __init__(self, games, user_id: int, guild_id: int):
        self.user_id = user_id
        self.guild_id = guild_id
        
        options = []
        for game in games:
            status_emoji = "🟢" if game.status == "available" else "🚧"
            options.append(
                discord.SelectOption(
                    label=f"{status_emoji} {game.name}",
                    value=game.id,
                    description=game.short_description,
                    emoji=game.emoji
                )
            )

        super().__init__(
            placeholder="Выберите игру для просмотра...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать карточку игры."""
        game_id = self.values[0]
        game = get_game_by_id(game_id)

        if not game:
            await interaction.response.send_message(
                "❌ Игра не найдена",
                ephemeral=True
            )
            return

        # Создать карточку игры
        category_info = CATEGORIES.get(game.category, {"color": discord.Color.blue()})
        
        # Статус игры
        if game.status == "available":
            status_text = "🟢 Доступна"
            status_color = discord.Color.green()
        else:
            status_text = "🚧 В разработке"
            status_color = discord.Color.orange()

        # Количество игроков
        if game.is_pvp and game.is_pve:
            player_count = "1-2 игрока"
        elif game.is_pvp:
            player_count = "2 игрока"
        else:
            player_count = "1 игрок"

        embed = discord.Embed(
            title=f"{game.emoji} {game.name}",
            description=game.description,
            color=status_color,
        )

        embed.add_field(name="📊 Статус", value=status_text, inline=True)
        embed.add_field(name="👥 Игроки", value=player_count, inline=True)
        embed.add_field(name="📊 Сложность", value=game.difficulty.capitalize(), inline=True)
        embed.add_field(name="💰 Мин. ставка", value=f"{game.min_bet} 🪙", inline=True)
        embed.add_field(name="💰 Макс. ставка", value=f"{game.max_bet} 🪙", inline=True)
        embed.add_field(name="🎲 Множитель", value=f"{game.multiplier}x", inline=True)

        embed.add_field(name="📌 Команда", value=f"`/{game.command}`", inline=False)
        embed.add_field(name="📖 Как играть", value=game.how_to_play, inline=False)

        view = GameCardView(self.user_id, self.guild_id, game, game.category)
        await interaction.response.edit_message(embed=embed, view=view)


class GameCardView(View):
    """View с кнопками для карточки игры."""

    def __init__(self, user_id: int, guild_id: int, game, category: str):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.guild_id = guild_id
        self.game = game
        self.category = category
        
        # Кнопка запуска (только если игра доступна)
        if game.status == "available":
            self.add_item(PlayButton(game, user_id, guild_id))
        
        self.add_item(BackToCategoryButton(user_id, guild_id, category))
        self.add_item(BackToMainMenuButton(user_id, guild_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Проверка: только пользователь который вызвал /games может нажимать."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Это меню может использовать только тот, кто его вызвал.",
                ephemeral=True
            )
            return False
        return True


class PlayButton(Button):
    """Кнопка запуска игры."""

    def __init__(self, game, user_id: int, guild_id: int):
        super().__init__(
            label="▶️ Запустить",
            style=discord.ButtonStyle.primary,
            custom_id=f"play_{game.id}"
        )
        self.game = game
        self.user_id = user_id
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Запустить игру."""
        bot = interaction.client
        command = bot.tree.get_command(self.game.command)

        if command:
            # Execute the command (public message)
            await command.callback(interaction)
        else:
            await interaction.response.send_message(
                f"🚧 Игра '{self.game.name}' пока в разработке",
                ephemeral=True
            )


class BackToCategoryButton(Button):
    """Кнопка возврата к списку игр в категории."""

    def __init__(self, user_id: int, guild_id: int, category: str):
        super().__init__(
            label="◀️ Назад",
            style=discord.ButtonStyle.secondary,
            custom_id=f"back_to_category_{category}"
        )
        self.user_id = user_id
        self.guild_id = guild_id
        self.category = category

    async def callback(self, interaction: discord.Interaction) -> None:
        """Вернуться к списку игр в категории."""
        category_info = CATEGORIES[self.category]
        games = get_games_by_category(self.category)

        # Создать список игр со статусами
        games_list = []
        for game in games:
            status_emoji = "🟢" if game.status == "available" else "🚧"
            status_text = "Доступна" if game.status == "available" else "В разработке"
            games_list.append(f"{status_emoji} **{game.name}** - {status_text}")
            games_list.append(f"   └ {game.short_description}")
        
        games_text = "\n".join(games_list)

        embed = discord.Embed(
            title=f"{category_info['emoji']} {category_info['name']}",
            description=f"{category_info['description']}\n\n{games_text}",
            color=category_info['color'],
        )

        view = GamesCategoryView(self.user_id, self.guild_id, self.category)
        view.add_item(GameSelect(games, self.user_id, self.guild_id))
        await interaction.response.edit_message(embed=embed, view=view)


class BackToMainMenuButton(Button):
    """Кнопка возврата в главное меню."""

    def __init__(self, user_id: int, guild_id: int):
        super().__init__(
            label="🏠 В главное меню",
            style=discord.ButtonStyle.secondary,
            custom_id="back_to_main"
        )
        self.user_id = user_id
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Вернуться в главное меню."""
        # Получить баланс для главного экрана
        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        
        embed = discord.Embed(
            title="🎮 Мини-игры",
            description=f"Выберите категорию игр и поставьте монеты!\n\n💰 Ваш баланс: {balance:,} 🪙",
            color=discord.Color.dark_blue(),
        )

        view = GamesMainView(self.user_id, self.guild_id)
        await interaction.response.edit_message(embed=embed, view=view)
