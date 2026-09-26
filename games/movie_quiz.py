"""Угадай фильм - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class MovieQuizGame:
    """Логика игры Угадай фильм."""

    def __init__(self):
        self.movies = [
            {"title": "Матрица", "year": 1999, "genre": "Фантастика", "actor": "Киану Ривз"},
            {"title": "Аватар", "year": 2009, "genre": "Фантастика", "actor": "Сэм Уортингтон"},
            {"title": "Титаник", "year": 1997, "genre": "Драма", "actor": "Леонардо Ди Каприо"},
            {"title": "Интерстеллар", "year": 2014, "genre": "Фантастика", "actor": "Мэттью Макконахи"},
            {"title": "Начало", "year": 2010, "genre": "Фантастика", "actor": "Леонардо Ди Каприо"},
            {"title": "Бэтмен: Начало", "year": 2005, "genre": "Боевик", "actor": "Кристиан Бэйл"},
            {"title": "Железный человек", "year": 2008, "genre": "Боевик", "actor": "Роберт Дауни мл."},
            {"title": "Властелин колец", "year": 2001, "genre": "Фэнтези", "actor": "Элайджа Вуд"},
            {"title": "Гарри Поттер", "year": 2001, "genre": "Фэнтези", "actor": "Дэниел Рэдклифф"},
            {"title": "Звёздные войны", "year": 1977, "genre": "Фантастика", "actor": "Марк Хэмилл"},
        ]

    def get_random_movie(self) -> dict:
        """Получить случайный фильм."""
        return random.choice(self.movies)


class MovieQuizModal(discord.ui.Modal, title="Угадай фильм"):
    """Модал для ставки в Угадай фильм."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id

        self.bet = discord.ui.TextInput(
            label="Ставка (🪙)",
            placeholder="Введите сумму ставки",
            min_length=1,
            max_length=10,
            required=True
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Начать игру с указанной ставкой."""
        try:
            bet = int(self.bet.value)
        except ValueError:
            await interaction.response.send_message(
                "❌ Ставка должна быть числом!",
                ephemeral=True
            )
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        if balance < bet:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. У вас: {balance} 🪙",
                ephemeral=True
            )
            return

        # Проверить лимиты ставок
        if bet < MIN_BET or bet > MAX_BET:
            await interaction.response.send_message(
                f"❌ Ставка должна быть между {MIN_BET} и {MAX_BET} 🪙",
                ephemeral=True
            )
            return

        # Списать ставку
        await user_balance_store.subtract_balance(self.guild_id, self.user_id, bet)

        # Создать сессию игры
        game = MovieQuizGame()
        movie_data = game.get_random_movie()

        # Создать view для игры
        from views.movie_quiz_view import MovieQuizView
        view = MovieQuizView(self.guild_id, self.user_id, bet, movie_data, game)

        await view.show_movie(interaction)
