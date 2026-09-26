"""View для Угадай фильм."""

import discord
from storage.user_balance_store import user_balance_store
from storage.minigame_store import minigame_store


class MovieQuizView(discord.ui.View):
    """View для Угадай фильм."""

    def __init__(self, guild_id: int, user_id: int, bet: int, movie_data: dict, game):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.movie_data = movie_data
        self.game = game

    async def show_movie(self, interaction: discord.Interaction) -> None:
        """Показать фильм."""
        year = self.movie_data["year"]
        genre = self.movie_data["genre"]
        actor = self.movie_data["actor"]

        # Получить 4 варианта ответа
        options = [self.movie_data["title"]]
        other_movies = [m for m in self.game.movies if m["title"] != self.movie_data["title"]]
        random.shuffle(other_movies)
        options.extend([m["title"] for m in other_movies[:3]])
        random.shuffle(options)

        embed = discord.Embed(
            title="🎬 Угадай фильм",
            description=f"**Ставка:** {self.bet} 🪙\n**Множитель:** 3x\n\n**Год:** {year}\n**Жанр:** {genre}\n**Актёр:** {actor}",
            color=discord.Color.pink()
        )

        view = MovieChoiceView(self.guild_id, self.user_id, self.bet, self.movie_data, options)

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class MovieChoiceView(discord.ui.View):
    """View для выбора фильма."""

    def __init__(self, guild_id: int, user_id: int, bet: int, movie_data: dict, options: list[str]):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.movie_data = movie_data
        self.options = options

        # Добавить кнопки для каждого фильма
        for i, option in enumerate(options):
            self.add_item(MovieButton(option, i, self))

    async def handle_selection(self, interaction: discord.Interaction, selected_movie: str) -> None:
        """Обработать выбор фильма."""
        correct_movie = self.movie_data["title"]

        won = (selected_movie == correct_movie)
        multiplier = 3.0

        if won:
            winnings = int(self.bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="✅ Правильно!",
                description=f"**Правильный ответ:** {correct_movie}\n**Ваш ответ:** {selected_movie}\n\n**Выигрыш:** {winnings} 🪙 ({multiplier}x)",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="❌ Неправильно!",
                description=f"**Правильный ответ:** {correct_movie}\n**Ваш ответ:** {selected_movie}\n\n**Потеря:** {self.bet} 🪙",
                color=discord.Color.red()
            )

        # Обновить статистику
        game_id = "movie_quiz"
        minigame_stats = await minigame_store.get_player_stats(self.guild_id, self.user_id)
        existing_stats = [s for s in minigame_stats if s.get("game_id") == game_id]

        if existing_stats:
            stats = existing_stats[0]
            stats["games_played"] = stats.get("games_played", 0) + 1
            if won:
                stats["games_won"] = stats.get("games_won", 0) + 1
            stats["total_bet"] = stats.get("total_bet", 0) + self.bet
            stats["total_won"] = stats.get("total_won", 0) + winnings if won else 0
            stats["net_profit"] = stats.get("net_profit", 0) + (winnings - self.bet) if won else -self.bet
        else:
            stats = {
                "game_id": game_id,
                "games_played": 1,
                "games_won": 1 if won else 0,
                "total_bet": self.bet,
                "total_won": winnings if won else 0,
                "net_profit": winnings - self.bet if won else -self.bet
            }
            minigame_stats.append(stats)

        await minigame_store.update_player_stats(self.guild_id, self.user_id, game_id, stats)

        await interaction.response.edit_message(embed=embed, view=None)


class MovieButton(discord.ui.Button):
    """Кнопка для фильма."""

    def __init__(self, movie: str, index: int, view: MovieChoiceView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=movie,
            custom_id=f"movie_{index}"
        )
        self.movie = movie
        self.view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать нажатие на фильм."""
        await self.view.handle_selection(interaction, self.movie)
