"""View для Виселицы."""

import discord
from storage.user_balance_store import user_balance_store
from storage.minigame_store import minigame_store


class HangmanView(discord.ui.View):
    """View для Виселицы."""

    def __init__(self, guild_id: int, user_id: int, bet: int, word: str, guessed_letters: set[str]):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.word = word
        self.guessed_letters = guessed_letters
        self.mistakes = 0
        self.max_mistakes = 6

        # Add letter buttons
        russian_alphabet = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
        for letter in russian_alphabet:
            self.add_item(LetterButton(letter, self))

    def update_embed(self) -> discord.Embed:
        """Обновить embed с текущим состоянием."""
        game = self.__class__.__getattribute__(HangmanGame, None)
        if game is None:
            from games.hangman import HangmanGame
            game = HangmanGame()

        masked_word = game.get_masked_word(self.word, self.guessed_letters)

        embed = discord.Embed(
            title="🎯 Виселица",
            description=f"**Ставка:** {self.bet} 🪙\n**Множитель:** 2x\n\n**Слово:**\n{masked_word}",
            color=discord.Color.purple()
        )

        embed.add_field(
            name="📝 Ошибки",
            value=f"{self.mistakes}/{self.max_mistakes}",
            inline=False
        )

        embed.add_field(
            name="🔤 Угаданные буквы",
            value=", ".join(sorted(self.guessed_letters)) if self.guessed_letters else "Нет",
            inline=False
        )

        return embed


class LetterButton(discord.ui.Button):
    """Кнопка для буквы."""

    def __init__(self, letter: str, view: HangmanView):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=letter.upper(),
            custom_id=f"letter_{letter}"
        )
        self.letter = letter
        self.view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать нажатие на букву."""
        view = self.view

        # Добавить букву в угаданные
        view.guessed_letters.add(self.letter)

        # Проверить, есть ли буква в слове
        if self.letter in view.word:
            # Буква есть в слове
            if all(letter in view.guessed_letters for letter in view.word):
                # Все буквы угаданы - победа!
                winnings = int(view.bet * 2.0)
                await user_balance_store.add_balance(view.guild_id, view.user_id, winnings)

                embed = discord.Embed(
                    title="✅ Победа!",
                    description=f"**Слово:** {view.word}\n**Выигрыш:** {winnings} 🪙 (2x)",
                    color=discord.Color.green()
                )

                # Обновить статистику
                game_id = "hangman"
                minigame_stats = await minigame_store.get_player_stats(view.guild_id, view.user_id)
                existing_stats = [s for s in minigame_stats if s.get("game_id") == game_id]

                if existing_stats:
                    stats = existing_stats[0]
                    stats["games_played"] = stats.get("games_played", 0) + 1
                    stats["games_won"] = stats.get("games_won", 0) + 1
                    stats["total_bet"] = stats.get("total_bet", 0) + view.bet
                    stats["total_won"] = stats.get("total_won", 0) + winnings
                    stats["net_profit"] = stats.get("net_profit", 0) + (winnings - view.bet)
                else:
                    stats = {
                        "game_id": game_id,
                        "games_played": 1,
                        "games_won": 1,
                        "total_bet": view.bet,
                        "total_won": winnings,
                        "net_profit": winnings - view.bet
                    }
                    minigame_stats.append(stats)

                await minigame_store.update_player_stats(view.guild_id, view.user_id, game_id, stats)

                await interaction.response.edit_message(embed=embed, view=None)
            else:
                # Продолжить игру
                embed = view.update_embed()
                # Отключить эту кнопку
                self.disabled = True
                self.style = discord.ButtonStyle.success
                await interaction.response.edit_message(embed=embed, view=view)
        else:
            # Буквы нет в слове - ошибка
            view.mistakes += 1

            if view.mistakes >= view.max_mistakes:
                # Проигрыш
                embed = discord.Embed(
                    title="❌ Проигрыш!",
                    description=f"**Слово:** {view.word}\n**Потеря:** {view.bet} 🪙",
                    color=discord.Color.red()
                )

                # Обновить статистику
                game_id = "hangman"
                minigame_stats = await minigame_store.get_player_stats(view.guild_id, view.user_id)
                existing_stats = [s for s in minigame_stats if s.get("game_id") == game_id]

                if existing_stats:
                    stats = existing_stats[0]
                    stats["games_played"] = stats.get("games_played", 0) + 1
                    stats["total_bet"] = stats.get("total_bet", 0) + view.bet
                    stats["net_profit"] = stats.get("net_profit", 0) - view.bet
                else:
                    stats = {
                        "game_id": game_id,
                        "games_played": 1,
                        "games_won": 0,
                        "total_bet": view.bet,
                        "total_won": 0,
                        "net_profit": -view.bet
                    }
                    minigame_stats.append(stats)

                await minigame_store.update_player_stats(view.guild_id, view.user_id, game_id, stats)

                await interaction.response.edit_message(embed=embed, view=None)
            else:
                # Продолжить игру
                embed = view.update_embed()
                # Отключить эту кнопку
                self.disabled = True
                self.style = discord.ButtonStyle.danger
                await interaction.response.edit_message(embed=embed, view=view)
