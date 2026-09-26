"""View для Угадай песню."""

import discord
from storage.user_balance_store import user_balance_store
from storage.minigame_store import minigame_store


class SongQuizView(discord.ui.View):
    """View для Угадай песню."""

    def __init__(self, guild_id: int, user_id: int, bet: int, song_data: dict, game):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.song_data = song_data
        self.game = game

    async def show_song(self, interaction: discord.Interaction) -> None:
        """Показать песню."""
        year = self.song_data["year"]
        genre = self.song_data["genre"]
        artist = self.song_data["artist"]

        # Получить 4 варианта ответа
        options = [self.song_data["title"]]
        other_songs = [s for s in self.game.songs if s["title"] != self.song_data["title"]]
        random.shuffle(other_songs)
        options.extend([s["title"] for s in other_songs[:3]])
        random.shuffle(options)

        embed = discord.Embed(
            title="🎵 Угадай песню",
            description=f"**Ставка:** {self.bet} 🪙\n**Множитель:** 3x\n\n**Год:** {year}\n**Жанр:** {genre}\n**Исполнитель:** {artist}",
            color=discord.Color.magenta()
        )

        view = SongChoiceView(self.guild_id, self.user_id, self.bet, self.song_data, options)

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class SongChoiceView(discord.ui.View):
    """View для выбора песни."""

    def __init__(self, guild_id: int, user_id: int, bet: int, song_data: dict, options: list[str]):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.song_data = song_data
        self.options = options

        # Добавить кнопки для каждой песни
        for i, option in enumerate(options):
            self.add_item(SongButton(option, i, self))

    async def handle_selection(self, interaction: discord.Interaction, selected_song: str) -> None:
        """Обработать выбор песни."""
        correct_song = self.song_data["title"]

        won = (selected_song == correct_song)
        multiplier = 3.0

        if won:
            winnings = int(self.bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="✅ Правильно!",
                description=f"**Правильный ответ:** {correct_song}\n**Ваш ответ:** {selected_song}\n\n**Выигрыш:** {winnings} 🪙 ({multiplier}x)",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="❌ Неправильно!",
                description=f"**Правильный ответ:** {correct_song}\n**Ваш ответ:** {selected_song}\n\n**Потеря:** {self.bet} 🪙",
                color=discord.Color.red()
            )

        # Обновить статистику
        game_id = "song_quiz"
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


class SongButton(discord.ui.Button):
    """Кнопка для песни."""

    def __init__(self, song: str, index: int, view: SongChoiceView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=song,
            custom_id=f"song_{index}"
        )
        self.song = song
        self.view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать нажатие на песню."""
        await self.view.handle_selection(interaction, self.song)
