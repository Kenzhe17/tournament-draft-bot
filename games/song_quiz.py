"""Угадай песню - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class SongQuizGame:
    """Логика игры Угадай песню."""

    def __init__(self):
        self.songs = [
            {"title": "Bohemian Rhapsody", "artist": "Queen", "year": 1975, "genre": "Рок"},
            {"title": "Smells Like Teen Spirit", "artist": "Nirvana", "year": 1991, "genre": "Гранж"},
            {"title": "Billie Jean", "artist": "Michael Jackson", "year": 1982, "genre": "Поп"},
            {"title": "Hotel California", "artist": "Eagles", "year": 1976, "genre": "Рок"},
            {"title": "Imagine", "artist": "John Lennon", "year": 1971, "genre": "Поп"},
            {"title": "Stairway to Heaven", "artist": "Led Zeppelin", "year": 1971, "genre": "Рок"},
            {"title": "Sweet Child O' Mine", "artist": "Guns N' Roses", "year": 1987, "genre": "Рок"},
            {"title": "Like a Rolling Stone", "artist": "Bob Dylan", "year": 1965, "genre": "Фолк"},
            {"title": "Purple Rain", "artist": "Prince", "year": 1984, "genre": "Поп"},
            {"title": "Wonderwall", "artist": "Oasis", "year": 1995, "genre": "Рок"},
        ]

    def get_random_song(self) -> dict:
        """Получить случайную песню."""
        return random.choice(self.songs)


class SongQuizModal(discord.ui.Modal, title="Угадай песню"):
    """Модал для ставки в Угадай песню."""

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
        game = SongQuizGame()
        song_data = game.get_random_song()

        # Создать view для игры
        from views.song_quiz_view import SongQuizView
        view = SongQuizView(self.guild_id, self.user_id, bet, song_data, game)

        await view.show_song(interaction)
