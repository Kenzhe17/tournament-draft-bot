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
                    label=f"{cat_info['name']}",
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
            games_list.append(f"{idx}️⃣ {game.emoji} **{game.name}** (`{game.command}`) • [{mode}]")
            games_list.append(f"└ *{game.short_description}*")
        
        games_text = "\n".join(games_list)

        embed = discord.Embed(
            title=f"{category_info['emoji']}︱{category_info['name']} ({category})",
            description=f"""📝 *{category_info['description']}*
────────────────────────
📌 **Информация о категории:**
**Страница:** {page} из {total_pages}
**Игр в категории:** {len(games)}

🎮 **Игры на странице:**
{games_text}

ℹ️ *Выберите игру в меню ниже или используйте кнопки пагинации:*""",
            color=category_info['color'],
        )

        view = GamesCategoryView(self.user_id, self.guild_id, category, page)
        view.add_item(GameSelect(page_games, self.user_id, self.guild_id, category, page))
        
        # Кнопки навигации (всегда показываем 3 кнопки) - добавляем напрямую без ActionRow
        view.add_item(BackToMainMenuButton(self.user_id, self.guild_id))
        view.add_item(BackButton(self.user_id, self.guild_id, category, page, total_pages))
        view.add_item(ForwardButton(self.user_id, self.guild_id, category, page, total_pages))
        
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
                    label=f"{game.emoji}︱{game.name}",
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
        category_info = CATEGORIES.get(game.category, {"color": discord.Color.blue(), "emoji": "🎮", "name": "Категория"})
        
        # Статус игры (все в разработке)
        status_text = "🚧 В разработке"
        status_color = discord.Color.orange()

        # Режим игры
        if game.is_pvp and game.is_pve:
            mode_text = "PvP/PvE"
        elif game.is_pvp:
            mode_text = "PvP"
        else:
            mode_text = "PvE"

        embed = discord.Embed(
            title=f"{game.emoji}︱{game.name} ({game.command})",
            description=f"""📝 *{game.short_description}*
────────────────────────
📌 **Информация об игре:**
• **Режим:** [{mode_text}]
• **Команда:** `/play {game.command}`
• **Категория:** {category_info['emoji']} {category_info['name']}
• **Множитель:** `{game.multiplier}`
• **Мин. ставка:** `{game.min_bet} 🪙`
• **Макс. ставка:** `{game.max_bet} 🪙`
• **Статус:** {status_text} *В разработке*

────────────────────────
📖 **Правила и особенности:**
{game.how_to_play}
────────────────────────
ℹ️ *Нажмите кнопку ниже для запуска игры или вернитесь в меню*""",
            color=status_color,
        )

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
        
        # Кнопка запуска (disabled если игра в разработке)
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
        is_ready = game.status in ("ready", "available")
        super().__init__(
            label="▶️ Сыграть",
            style=discord.ButtonStyle.primary,
            custom_id=f"play_{game.id}",
            disabled=not is_ready
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
            label="⬅️ К категории",
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
        page = self.page  # Сохраняем текущую страницу

        # Получить игры для текущей страницы
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        page_games = games[start_idx:end_idx]

        # Создать список игр с нумерацией
        games_list = []
        for idx, game in enumerate(page_games, start=start_idx + 1):
            mode = "PvP/PvE" if game.is_pvp and game.is_pve else ("PvP" if game.is_pvp else "PvE")
            games_list.append(f"{idx}️⃣ {game.emoji} **{game.name}** (`{game.command}`) • [{mode}]")
            games_list.append(f"└ *{game.short_description}*")
        
        games_text = "\n".join(games_list)

        embed = discord.Embed(
            title=f"{category_info['emoji']}︱{category_info['name']} ({self.category})",
            description=f"""📝 *{category_info['description']}*
────────────────────────
📌 **Информация о категории:**
**Страница:** {page} из {total_pages}
**Игр в категории:** {len(games)}

🎮 **Игры на странице:**
{games_text}

ℹ️ *Выберите игру в меню ниже или используйте кнопки пагинации:*""",
            color=category_info['color'],
        )

        view = GamesCategoryView(self.user_id, self.guild_id, self.category, page)
        view.add_item(GameSelect(page_games, self.user_id, self.guild_id, self.category, page))
        
        # Кнопки навигации (всегда показываем 3 кнопки) - добавляем напрямую без ActionRow
        view.add_item(BackToMainMenuButton(self.user_id, self.guild_id))
        view.add_item(BackButton(self.user_id, self.guild_id, self.category, page, total_pages))
        view.add_item(ForwardButton(self.user_id, self.guild_id, self.category, page, total_pages))
        
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
            title="🎯 МИНИ-ИГРЫ| Главное меню",
            description=f"""👋 **Добро пожаловать, {interaction.user.display_name}!**
💳 **Ваш Баланс:** {balance:,} 🪙 

📂 **КАТЕГОРИИ**
🎲 **Игры на удачу** 
└ *Быстрые игры на риск: монетка, кубики, угадай число и др.*

🧠 **Викторины и головоломки**
└ *Интеллектуальные состязания, викторины и слова.*

🎰 **Казино и ставки** 
└ *Слоты, рулетка, баккара, лотерея и высокие ставки.*

ℹ️ *Выберите категорию в меню ниже для просмотра списка игр:*""",
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
                games_list.append(f"{idx}️⃣ {game.emoji} **{game.name}** (`{game.command}`) • [{mode}]")
                games_list.append(f"└ *{game.short_description}*")
            
            games_text = "\n".join(games_list)
            
            embed = discord.Embed(
                title=f"{category_info['emoji']}︱{category_info['name']} ({self.category})",
                description=f"""📝 *{category_info['description']}*
────────────────────────
📌 **Информация о категории:**
**Страница:** {new_page} из {self.total_pages}
**Игр в категории:** {len(games)}

🎮 **Игры на странице:**
{games_text}

ℹ️ *Выберите игру в меню ниже или используйте кнопки пагинации:*""",
                color=category_info['color'],
            )
            
            view = GamesCategoryView(self.user_id, self.guild_id, self.category, new_page)
            view.add_item(GameSelect(page_games, self.user_id, self.guild_id, self.category, new_page))
            
            # Кнопки навигации - добавляем напрямую без ActionRow
            view.add_item(BackToMainMenuButton(self.user_id, self.guild_id))
            view.add_item(BackButton(self.user_id, self.guild_id, self.category, new_page, self.total_pages))
            view.add_item(ForwardButton(self.user_id, self.guild_id, self.category, new_page, self.total_pages))
            
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
                games_list.append(f"{idx}️⃣ {game.emoji} **{game.name}** (`{game.command}`) • [{mode}]")
                games_list.append(f"└ *{game.short_description}*")
            
            games_text = "\n".join(games_list)
            
            embed = discord.Embed(
                title=f"{category_info['emoji']}︱{category_info['name']} ({self.category})",
                description=f"""📝 *{category_info['description']}*
────────────────────────
📌 **Информация о категории:**
**Страница:** {new_page} из {self.total_pages}
**Игр в категории:** {len(games)}

🎮 **Игры на странице:**
{games_text}

ℹ️ *Выберите игру в меню ниже или используйте кнопки пагинации:*""",
                color=category_info['color'],
            )
            
            view = GamesCategoryView(self.user_id, self.guild_id, self.category, new_page)
            view.add_item(GameSelect(page_games, self.user_id, self.guild_id, self.category, new_page))
            
            # Кнопки навигации - добавляем напрямую без ActionRow
            view.add_item(BackToMainMenuButton(self.user_id, self.guild_id))
            view.add_item(BackButton(self.user_id, self.guild_id, self.category, new_page, self.total_pages))
            view.add_item(ForwardButton(self.user_id, self.guild_id, self.category, new_page, self.total_pages))
            
            await interaction.response.edit_message(embed=embed, view=view)
