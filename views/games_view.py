"""Enhanced view for mini-games menu with categories and game cards."""

import discord
from discord.ui import Button, View, Select
from typing import Optional

from storage.games_config import (
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
        self.add_item(CloseButton())

    async def get_balance(self) -> int:
        """Получить баланс пользователя."""
        return await user_balance_store.get_balance(self.guild_id, self.user_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Проверка: только пользователь который вызвал /games может нажимать."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Это меню вызвал другой игрок. Введите `/games` для открытия своего меню!",
                ephemeral=True
            )
            return False
        return True


class CloseButton(Button):
    """Кнопка закрытия меню."""

    def __init__(self):
        super().__init__(
            label="✖ Закрыть",
            style=discord.ButtonStyle.danger,
            custom_id="close_menu"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Закрыть меню (удалить сообщение)."""
        await interaction.response.delete_message()


class GameCategorySelect(Select):
    """Выпадающее меню выбора категории игр."""

    def __init__(self, user_id: int, guild_id: int):
        self.user_id = user_id
        self.guild_id = guild_id
        
        options = []
        for cat_id, cat_info in CATEGORIES.items():
            games = get_games_by_category(cat_id)
            # Используем простые эмодзи для Discord API
            emoji_map = {"luck": "🎲", "quiz": "🧠", "casino": "🎰"}
            emoji = emoji_map.get(cat_id, "🎮")
            options.append(
                discord.SelectOption(
                    label=f"{cat_info['name']} ({len(games)})",
                    value=cat_id,
                    emoji=emoji
                )
            )
        
        super().__init__(
            placeholder="📁 Выберите категорию игр...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать выбор категории."""
        category = self.values[0]
        category_info = CATEGORIES[category]
        games = get_games_by_category(category)

        # Пагинация: 4 игры на страницу
        per_page = 4
        total_pages = (len(games) + per_page - 1) // per_page
        page = 1

        # Получить игры для текущей страницы
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        page_games = games[start_idx:end_idx]

        # Создать список игр с нумерацией
        games_list = []
        for idx, game in enumerate(page_games, start=start_idx + 1):
            mode = "PvP/PvE" if game.is_pvp and game.is_pve else ("PvP" if game.is_pvp else "PvE")
            games_list.append(f"{idx}️⃣ {game.emoji} **{game.name}** `[{mode}]` • 🚧 *В разработке*")
            games_list.append(f"└ *{game.short_description}*")
        
        games_text = "\n".join(games_list)

        embed = discord.Embed(
            title=f"{category_info['emoji']}︱{category_info['name']} `[Стр. {page}/{total_pages}]`",
            description=f"> 📝 *{category_info['description']}*\n> ────────────────────────\n\n{games_text}\n\n────────────────────────\n*Выберите игру в меню ниже или переключите страницу:*",
            color=category_info['color'],
        )

        view = GamesCategoryView(self.user_id, self.guild_id, category, page)
        view.add_item(GameSelect(page_games, self.user_id, self.guild_id, category, page))
        view.add_item(BackToMainMenuButton(self.user_id, self.guild_id))
        
        # Кнопки пагинации
        if total_pages > 1:
            row = discord.ui.ActionRow()
            row.add_item(BackButton(self.user_id, self.guild_id, category, page, total_pages))
            row.add_item(ForwardButton(self.user_id, self.guild_id, category, page, total_pages))
            view.add_item(row)
        
        await interaction.response.edit_message(embed=embed, view=view)


class GamesCategoryView(View):
    """View для экрана категории с пагинацией."""

    def __init__(self, user_id: int, guild_id: int, category: str, page: int = 1):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.guild_id = guild_id
        self.category = category
        self.page = page
        self.per_page = 4

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Проверка: только пользователь который вызвал /games может нажимать."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Это меню вызвал другой игрок. Введите `/games` для открытия своего меню!",
                ephemeral=True
            )
            return False
        return True


class GameSelect(Select):
    """Выпадающее меню для выбора игры."""

    def __init__(self, games, user_id: int, guild_id: int, category: str, page: int):
        self.user_id = user_id
        self.guild_id = guild_id
        self.category = category
        self.page = page
        
        options = []
        for game in games:
            options.append(
                discord.SelectOption(
                    label=f"{game.name} (В разработке)",
                    value=game.id
                )
            )

        super().__init__(
            placeholder="🎮 Выберите игру для просмотра...",
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
        
        # Статус игры (все в разработке)
        status_text = "🚧 В разработке"
        status_color = discord.Color.orange()

        # Режим игры
        if game.is_pvp and game.is_pve:
            mode_text = "PvP / PvE"
        elif game.is_pvp:
            mode_text = "PvP"
        else:
            mode_text = "PvE"

        embed = discord.Embed(
            title=f"{game.emoji} Игровой профиль: {game.name}",
            description=game.description,
            color=status_color,
        )

        embed.add_field(name="📝 Описание", value=game.short_description, inline=False)
        embed.add_field(name="📖 Как играть", value=game.how_to_play, inline=False)
        embed.add_field(name="⚔️ Режим", value=mode_text, inline=True)
        embed.add_field(name="⚙️ Команда", value=f"`/play {game.command}`", inline=True)
        embed.add_field(name="📌 Статус", value=status_text, inline=True)
        embed.add_field(name="💰 Мин. ставка", value=f"{game.min_bet} 🪙", inline=True)
        embed.add_field(name="💰 Макс. ставка", value=f"{game.max_bet} 🪙", inline=True)
        embed.add_field(name="🎲 Множитель", value=f"{game.multiplier}x", inline=True)

        view = GameCardView(self.user_id, self.guild_id, game, game.category, self.page)
        await interaction.response.edit_message(embed=embed, view=view)


class GameCardView(View):
    """View с кнопками для карточки игры."""

    def __init__(self, user_id: int, guild_id: int, game, category: str, page: int = 1):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.guild_id = guild_id
        self.game = game
        self.category = category
        self.page = page
        
        # Кнопка запуска (только если игра доступна)
        if game.status in ("ready", "available"):
            self.add_item(PlayButton(game, user_id, guild_id))
        
        self.add_item(BackToCategoryButton(user_id, guild_id, category, page))
        self.add_item(BackToMainMenuButton(user_id, guild_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Проверка: только пользователь который вызвал /games может нажимать."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ Это меню вызвал другой игрок. Введите `/games` для открытия своего меню!",
                ephemeral=True
            )
            return False
        return True


class PlayButton(Button):
    """Кнопка запуска игры."""

    def __init__(self, game, user_id: int, guild_id: int):
        super().__init__(
            label="▶️ Сыграть",
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

    def __init__(self, user_id: int, guild_id: int, category: str, page: int = 1):
        super().__init__(
            label="◀️ Назад к категории",
            style=discord.ButtonStyle.secondary,
            custom_id=f"back_to_category_{category}"
        )
        self.user_id = user_id
        self.guild_id = guild_id
        self.category = category
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        """Вернуться к списку игр в категории."""
        category_info = CATEGORIES[self.category]
        games = get_games_by_category(self.category)

        # Пагинация: 4 игры на страницу
        per_page = 4
        total_pages = (len(games) + per_page - 1) // per_page
        page = 1

        # Получить игры для текущей страницы
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        page_games = games[start_idx:end_idx]

        # Создать список игр с нумерацией
        games_list = []
        for idx, game in enumerate(page_games, start=start_idx + 1):
            mode = "PvP/PvE" if game.is_pvp and game.is_pve else ("PvP" if game.is_pvp else "PvE")
            games_list.append(f"{idx}️⃣ {game.emoji} **{game.name}** `[{mode}]` • 🚧 *В разработке*")
            games_list.append(f"└ *{game.short_description}*")
        
        games_text = "\n".join(games_list)

        embed = discord.Embed(
            title=f"{category_info['emoji']}︱{category_info['name']} `[Стр. {page}/{total_pages}]`",
            description=f"> 📝 *{category_info['description']}*\n> ────────────────────────\n\n{games_text}\n\n────────────────────────\n*Выберите игру в меню ниже или переключите страницу:*",
            color=category_info['color'],
        )

        view = GamesCategoryView(self.user_id, self.guild_id, self.category, page)
        view.add_item(GameSelect(page_games, self.user_id, self.guild_id, self.category, page))
        view.add_item(BackToMainMenuButton(self.user_id, self.guild_id))
        
        # Кнопки пагинации
        if total_pages > 1:
            row = discord.ui.ActionRow()
            row.add_item(BackButton(self.user_id, self.guild_id, self.category, page, total_pages))
            row.add_item(ForwardButton(self.user_id, self.guild_id, self.category, page, total_pages))
            view.add_item(row)
        
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
        from storage.games_config import get_all_games
        
        # Получить баланс для главного экрана
        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        total_games = len(get_all_games())
        
        # Создать описание категорий
        categories_text = ""
        for cat_id, cat_info in CATEGORIES.items():
            games = get_games_by_category(cat_id)
            # Короткие описания для минимализма
            short_desc = {
                "luck": "Монетка, Кубик, КНБ, Бутылочка, Эмодзи...",
                "quiz": "Математика, Словесные цепочки...",
                "casino": "Рулетка, Слоты, Лотерея, Баккара, High-Low..."
            }
            categories_text += f"{cat_info['emoji']} **{cat_info['name']}** `({len(games)})`\n> *{short_desc.get(cat_id, cat_info['description'])}*\n\n"
        
        embed = discord.Embed(
            title="🎮︱Игровой Центр",
            description=f"""👋 **Приветствуем в игровом хабе!**
💵 **Баланс:** {balance:,} 🪙
🎯 **Доступно игр:** {total_games}

📁 **КАТЕГОРИИ**

{categories_text}────────────────────────
*Выберите категорию в меню ниже, чтобы начать играть.*""",
            color=0x2F3136,  # Тёмно-фиолетовый
        )

        view = GamesMainView(self.user_id, self.guild_id)
        await interaction.response.edit_message(embed=embed, view=view)


class BackButton(Button):
    """Кнопка назад для пагинации."""

    def __init__(self, user_id: int, guild_id: int, category: str, page: int, total_pages: int):
        super().__init__(
            label="◀️ Назад",
            style=discord.ButtonStyle.primary,
            custom_id=f"back_page_{category}_{page}",
            disabled=page == 1
        )
        self.user_id = user_id
        self.guild_id = guild_id
        self.category = category
        self.page = page
        self.total_pages = total_pages

    async def callback(self, interaction: discord.Interaction) -> None:
        """Перейти на предыдущую страницу."""
        if self.page > 1:
            new_page = self.page - 1
            category_info = CATEGORIES[self.category]
            games = get_games_by_category(self.category)
            
            per_page = 4
            start_idx = (new_page - 1) * per_page
            end_idx = start_idx + per_page
            page_games = games[start_idx:end_idx]
            
            games_list = []
            for idx, game in enumerate(page_games, start=start_idx + 1):
                mode = "PvP/PvE" if game.is_pvp and game.is_pve else ("PvP" if game.is_pvp else "PvE")
                games_list.append(f"{idx}️⃣ {game.emoji} **{game.name}** `[{mode}]` • 🚧 *В разработке*")
                games_list.append(f"└ *{game.short_description}*")
            
            games_text = "\n".join(games_list)
            
            embed = discord.Embed(
                title=f"{category_info['emoji']}︱{category_info['name']} `[Стр. {new_page}/{self.total_pages}]`",
                description=f"> 📝 *{category_info['description']}*\n> ────────────────────────\n\n{games_text}\n\n────────────────────────\n*Выберите игру в меню ниже или переключите страницу:*",
                color=category_info['color'],
            )
            
            view = GamesCategoryView(self.user_id, self.guild_id, self.category, new_page)
            view.add_item(GameSelect(page_games, self.user_id, self.guild_id, self.category, new_page))
            view.add_item(BackToMainMenuButton(self.user_id, self.guild_id))
            
            row = discord.ui.ActionRow()
            row.add_item(BackButton(self.user_id, self.guild_id, self.category, new_page, self.total_pages))
            row.add_item(ForwardButton(self.user_id, self.guild_id, self.category, new_page, self.total_pages))
            view.add_item(row)
            
            await interaction.response.edit_message(embed=embed, view=view)


class ForwardButton(Button):
    """Кнопка вперёд для пагинации."""

    def __init__(self, user_id: int, guild_id: int, category: str, page: int, total_pages: int):
        super().__init__(
            label="Вперёд ▶️",
            style=discord.ButtonStyle.primary,
            custom_id=f"forward_page_{category}_{page}",
            disabled=page == total_pages
        )
        self.user_id = user_id
        self.guild_id = guild_id
        self.category = category
        self.page = page
        self.total_pages = total_pages

    async def callback(self, interaction: discord.Interaction) -> None:
        """Перейти на следующую страницу."""
        if self.page < self.total_pages:
            new_page = self.page + 1
            category_info = CATEGORIES[self.category]
            games = get_games_by_category(self.category)
            
            per_page = 4
            start_idx = (new_page - 1) * per_page
            end_idx = start_idx + per_page
            page_games = games[start_idx:end_idx]
            
            games_list = []
            for idx, game in enumerate(page_games, start=start_idx + 1):
                mode = "PvP/PvE" if game.is_pvp and game.is_pve else ("PvP" if game.is_pvp else "PvE")
                games_list.append(f"{idx}️⃣ {game.emoji} **{game.name}** `[{mode}]` • 🚧 *В разработке*")
                games_list.append(f"└ *{game.short_description}*")
            
            games_text = "\n".join(games_list)
            
            embed = discord.Embed(
                title=f"{category_info['emoji']}︱{category_info['name']} `[Стр. {new_page}/{self.total_pages}]`",
                description=f"> 📝 *{category_info['description']}*\n> ────────────────────────\n\n{games_text}\n\n────────────────────────\n*Выберите игру в меню ниже или переключите страницу:*",
                color=category_info['color'],
            )
            
            view = GamesCategoryView(self.user_id, self.guild_id, self.category, new_page)
            view.add_item(GameSelect(page_games, self.user_id, self.guild_id, self.category, new_page))
            view.add_item(BackToMainMenuButton(self.user_id, self.guild_id))
            
            row = discord.ui.ActionRow()
            row.add_item(BackButton(self.user_id, self.guild_id, self.category, new_page, self.total_pages))
            row.add_item(ForwardButton(self.user_id, self.guild_id, self.category, new_page, self.total_pages))
            view.add_item(row)
            
            await interaction.response.edit_message(embed=embed, view=view)
