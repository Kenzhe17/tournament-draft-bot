"""Enhanced view for mini-games menu with categories and game cards."""
from config import replace_emojis

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
                replace_emojis("⚪ Это меню вызвал другой игрок. Введите `/games` для открытия своего меню!"),
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
            # Используем кастомные эмодзи
            emoji_map = {"luck": "dice", "quiz": "a_star", "casino": "game"}
            emoji = replace_emojis(emoji_map.get(cat_id, "game"))
            options.append(
                discord.SelectOption(
                    label=f"{cat_info['name']}",
                    value=cat_id
                )
            )
        
        super().__init__(
            placeholder=replace_emojis("sub_directory Выберите категорию игр..."),
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

        # Создать список игр
        games_list = []
        for game in page_games:
            mode = "PvP/PvE" if game.is_pvp and game.is_pve else ("PvP" if game.is_pvp else "PvE")
            games_list.append(f"{replace_emojis('sub_middle')} **{game.name}** • `[{mode}]`")
            games_list.append(f"{replace_emojis('sub_middle')} {game.short_description}")
            games_list.append("")  # Пустая строка между играми

        games_text = "\n".join(games_list)

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {category_info['name'].upper()} | Страница {page} из {total_pages}",
            description=f"{replace_emojis('white_arrow')} {category_info['description']}\n\n{replace_emojis('white_dot')} **Информация о категории:**\n{replace_emojis('a_dot_smaller')} Страница: {page} из {total_pages}\n{replace_emojis('sub_directory')} Игр в категории: {len(games)}\n\n{replace_emojis('⚪')} **Игры на странице:**\n{games_text}\n\n{replace_emojis('a_dot_smaller')} Выберите игру в меню ниже или используйте кнопки пагинации",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

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
                replace_emojis("⚪ Это меню вызвал другой игрок. Введите `/games` для открытия своего меню!"),
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
            placeholder=replace_emojis("🎮 Выберите игру для просмотра..."),
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
                replace_emojis("⚪ Игра не найдена"),
                ephemeral=True
            )
            return

        # Создать карточку игры
        category_info = CATEGORIES.get(game.category, {"color": discord.Color.blue(), "emoji": "🎮", "name": "Категория"})

        # Статус игры (все в разработке)
        status_text = "В разработке"

        # Режим игры
        if game.is_pvp and game.is_pve:
            mode_text = "PvP/PvE"
        elif game.is_pvp:
            mode_text = "PvP"
        else:
            mode_text = "PvE"

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {game.name.upper()} | Игровой режим",
            description=f"{replace_emojis('white_arrow')} {game.short_description}\n\n{replace_emojis('⚪')} **Информация об игре:**\n{replace_emojis('sub_middle')} Режим: `[{mode_text}]`\n{replace_emojis('sub_middle')} Команда: `/{game.command}`\n{replace_emojis('sub_middle')} Категория: {category_info['name']}\n{replace_emojis('sub_middle')} Множитель: {game.multiplier}x\n{replace_emojis('sub_middle')} Мин. ставка: {game.min_bet} {replace_emojis('money')}\n{replace_emojis('sub_middle')} Макс. ставка: {game.max_bet:,} {replace_emojis('money')}\n{replace_emojis('sub_directory')} Статус: {status_text}\n\n{replace_emojis('⚪')} **Правила и особенности:**\n{replace_emojis('sub_directory')} {game.how_to_play}\n\n{replace_emojis('a_dot_smaller')} Нажмите кнопку ниже для запуска игры или вернитесь в меню",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

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
                replace_emojis("⚪ Это меню вызвал другой игрок. Введите `/games` для открытия своего меню!"),
                ephemeral=True
            )
            return False
        return True


class PlayButton(Button):
    """Кнопка запуска игры."""

    def __init__(self, game, user_id: int, guild_id: int):
        is_ready = game.status in ("ready", "available")
        super().__init__(
            label=replace_emojis("▶️ Сыграть"),
            style=discord.ButtonStyle.primary,
            custom_id=f"play_{game.id}",
            disabled=not is_ready
        )
        self.game = game
        self.user_id = user_id
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать гайд как играть."""
        # Build guide message with parameter attributes
        pve_command = f"/{self.game.command} bet:100"
        pvp_command = f"/{self.game.command} bet:100 opponent:@"
        
        embed = discord.Embed(
            title=f"{replace_emojis('📖')} Как начать игру {self.game.name}",
            description="",
            color=discord.Color.blue()
        )

        embed.add_field(
            name=replace_emojis("🤖 Игра с ботом (PvE)"),
            value=pve_command,
            inline=False
        )

        embed.add_field(
            name=replace_emojis("⚔️ Дуэль с игроком (PvP)"),
            value=pvp_command,
            inline=False
        )

        embed.set_footer(text=replace_emojis("💡 Скопируйте команду, замените значения и отправьте её в чат."))
        
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BackToCategoryButton(Button):
    """Кнопка возврата к списку игр в категории."""

    def __init__(self, user_id: int, guild_id: int, category: str, page: int = 1):
        super().__init__(
            label=replace_emojis("arrow_left К категории"),
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

        # Создать список игр
        games_list = []
        for game in page_games:
            mode = "PvP/PvE" if game.is_pvp and game.is_pve else ("PvP" if game.is_pvp else "PvE")
            games_list.append(f"{replace_emojis('sub_middle')} **{game.name}** • `[{mode}]`")
            games_list.append(f"{replace_emojis('sub_middle')} {game.short_description}")
            games_list.append("")  # Пустая строка между играми

        games_text = "\n".join(games_list)

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} {category_info['name'].upper()} | Страница {page} из {total_pages}",
            description=f"{replace_emojis('white_arrow')} {category_info['description']}\n\n{replace_emojis('white_dot')} **Информация о категории:**\n{replace_emojis('a_dot_smaller')} Страница: {page} из {total_pages}\n{replace_emojis('a_dot_smaller')} Игр в категории: {len(games)}\n\n{replace_emojis('white_dot')} **Игры на странице:**\n{games_text}\n\n{replace_emojis('a_dot_smaller')} Выберите игру в меню ниже или используйте кнопки пагинации",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

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
            label=replace_emojis("🏠 В главное меню"),
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
            title=f"{replace_emojis('a_star')} МИНИ-ИГРЫ | Главное меню",
            description=f"{replace_emojis('white_arrow')} {interaction.user.mention}\n\n{replace_emojis('white_dot')} **Информация:**\n{replace_emojis('a_dot_smaller')} Ваш баланс: {balance:,} {replace_emojis('money')}\n\n{replace_emojis('white_dot')} **Категории:**\n{replace_emojis('a_dot_smaller')} **Игры на удачу**\n{replace_emojis('a_dot_smaller')} Быстрые игры на риск: монетка, кубики, угадай число и др.\n{replace_emojis('a_dot_smaller')} **Викторины и головоломки**\n{replace_emojis('a_dot_smaller')} Интеллектуальные состязания, викторины и слова.\n{replace_emojis('a_dot_smaller')} **Казино и ставки**\n{replace_emojis('a_dot_smaller')} Слоты, рулетка, баккара, лотерея и высокие ставки.\n\n{replace_emojis('a_dot_smaller')} Выберите категорию в меню ниже для просмотра списка игр",
            color=discord.Color.from_rgb(69, 69, 69)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        view = GamesMainView(self.user_id, self.guild_id)
        await interaction.response.edit_message(embed=embed, view=view)


class BackButton(Button):
    """Кнопка назад для пагинации."""

    def __init__(self, user_id: int, guild_id: int, category: str, page: int, total_pages: int):
        super().__init__(
            label=replace_emojis("arrow_left Назад"),
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
            for game in page_games:
                mode = "PvP/PvE" if game.is_pvp and game.is_pve else ("PvP" if game.is_pvp else "PvE")
                games_list.append(f"{replace_emojis('sub_middle')} **{game.name}** • `[{mode}]`")
                games_list.append(f"{replace_emojis('sub_middle')} {game.short_description}")
                games_list.append("")  # Пустая строка между играми

            games_text = "\n".join(games_list)

            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} {category_info['name'].upper()} | Страница {new_page} из {self.total_pages}",
                description=f"{replace_emojis('white_arrow')} {category_info['description']}\n\n{replace_emojis('white_dot')} **Информация о категории:**\n{replace_emojis('a_dot_smaller')} Страница: {new_page} из {self.total_pages}\n{replace_emojis('sub_directory')} Игр в категории: {len(games)}\n\n{replace_emojis('⚪')} **Игры на странице:**\n{games_text}\n\n{replace_emojis('a_dot_smaller')} Выберите игру в меню ниже или используйте кнопки пагинации",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)

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
            label=f"{replace_emojis('arrow_right')} Вперёд",
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
            for game in page_games:
                mode = "PvP/PvE" if game.is_pvp and game.is_pve else ("PvP" if game.is_pvp else "PvE")
                games_list.append(f"{replace_emojis('sub_middle')} **{game.name}** • `[{mode}]`")
                games_list.append(f"{replace_emojis('sub_middle')} {game.short_description}")
                games_list.append("")  # Пустая строка между играми

            games_text = "\n".join(games_list)

            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} {category_info['name'].upper()} | Страница {new_page} из {self.total_pages}",
                description=f"{replace_emojis('white_arrow')} {category_info['description']}\n\n{replace_emojis('white_dot')} **Информация о категории:**\n{replace_emojis('a_dot_smaller')} Страница: {new_page} из {self.total_pages}\n{replace_emojis('sub_directory')} Игр в категории: {len(games)}\n\n{replace_emojis('⚪')} **Игры на странице:**\n{games_text}\n\n{replace_emojis('a_dot_smaller')} Выберите игру в меню ниже или используйте кнопки пагинации",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)

            view = GamesCategoryView(self.user_id, self.guild_id, self.category, new_page)
            view.add_item(GameSelect(page_games, self.user_id, self.guild_id, self.category, new_page))

            # Кнопки навигации - добавляем напрямую без ActionRow
            view.add_item(BackToMainMenuButton(self.user_id, self.guild_id))
            view.add_item(BackButton(self.user_id, self.guild_id, self.category, new_page, self.total_pages))
            view.add_item(ForwardButton(self.user_id, self.guild_id, self.category, new_page, self.total_pages))

            await interaction.response.edit_message(embed=embed, view=view)
