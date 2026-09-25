"""View для Словесные цепочки."""

import discord
from storage.user_balance_store import user_balance_store
from storage.minigame_store import minigame_store


class WordChainView(discord.ui.View):
    """View для Словесные цепочки."""

    def __init__(self, guild_id: int, user_id: int, bet: int, letter: str, game):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.letter = letter
        self.game = game
        self.round = 1
        self.max_rounds = 5

    @discord.ui.button(label="Ответить", style=discord.ButtonStyle.primary, custom_id="word_chain_answer")
    async def answer_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Показать модал для ввода ответа."""
        await interaction.response.send_modal(WordChainAnswerModal(self.guild_id, self.user_id, self.bet, self.letter, self.game, self.round, self.max_rounds))


class WordChainAnswerModal(discord.ui.Modal, title="Ваше слово"):
    """Модал для ввода слова."""

    def __init__(self, guild_id: int, user_id: int, bet: int, letter: str, game, round: int, max_rounds: int):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.letter = letter
        self.game = game
        self.round = round
        self.max_rounds = max_rounds

        self.word_input = discord.ui.TextInput(
            label="Слово",
            placeholder="Введите слово",
            required=True
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Проверить слово."""
        user_word = self.word_input.value.lower().strip()

        # Проверить, начинается ли слово с нужной буквы
        is_valid = self.game.check_word(user_word, self.letter)

        if not is_valid:
            embed = discord.Embed(
                title="❌ Неверное слово!",
                description=f"Слово должно начинаться на букву '{self.letter.upper()}'",
                color=discord.Color.red()
            )

            # Проигрыш
            game_id = "word_chain"
            minigame_stats = await minigame_store.get_player_stats(self.guild_id, self.user_id)
            existing_stats = [s for s in minigame_stats if s.get("game_id") == game_id]

            if existing_stats:
                stats = existing_stats[0]
                stats["games_played"] = stats.get("games_played", 0) + 1
                stats["total_bet"] = stats.get("total_bet", 0) + self.bet
                stats["net_profit"] = stats.get("net_profit", 0) - self.bet
            else:
                stats = {
                    "game_id": game_id,
                    "games_played": 1,
                    "games_won": 0,
                    "total_bet": self.bet,
                    "total_won": 0,
                    "net_profit": -self.bet
                }
                minigame_stats.append(stats)

            await minigame_store.update_player_stats(self.guild_id, self.user_id, game_id, stats)

            await interaction.response.edit_message(embed=embed, view=None)
            return

        # Проверить последний символ слова для следующего раунда
        last_letter = user_word[-1] if user_word else self.letter

        if self.round >= self.max_rounds:
            # Все раунды пройдены - победа
            winnings = int(self.bet * 2.0)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="✅ Победа!",
                description=f"**Вы прошли все {self.max_rounds} раундов!**\n**Выигрыш:** {winnings} 🪙 (2x)",
                color=discord.Color.green()
            )

            # Обновить статистику
            game_id = "word_chain"
            minigame_stats = await minigame_store.get_player_stats(self.guild_id, self.user_id)
            existing_stats = [s for s in minigame_stats if s.get("game_id") == game_id]

            if existing_stats:
                stats = existing_stats[0]
                stats["games_played"] = stats.get("games_played", 0) + 1
                stats["games_won"] = stats.get("games_won", 0) + 1
                stats["total_bet"] = stats.get("total_bet", 0) + self.bet
                stats["total_won"] = stats.get("total_won", 0) + winnings
                stats["net_profit"] = stats.get("net_profit", 0) + (winnings - self.bet)
            else:
                stats = {
                    "game_id": game_id,
                    "games_played": 1,
                    "games_won": 1,
                    "total_bet": self.bet,
                    "total_won": winnings,
                    "net_profit": winnings - self.bet
                }
                minigame_stats.append(stats)

            await minigame_store.update_player_stats(self.guild_id, self.user_id, game_id, stats)

            await interaction.response.edit_message(embed=embed, view=None)
        else:
            # Следующий раунд
            self.round += 1
            next_letter = last_letter

            embed = discord.Embed(
                title="✅ Правильно!",
                description=f"**Ваше слово:** {user_word}\n**Следующая буква:** {next_letter.upper()}",
                color=discord.Color.green()
            )

            embed.add_field(
                name="📊 Раунд",
                value=f"{self.round}/{self.max_rounds}",
                inline=False
            )

            # Обновить view
            view = WordChainView(self.guild_id, self.user_id, self.bet, next_letter, self.game)
            view.round = self.round
            view.max_rounds = self.max_rounds

            await interaction.response.edit_message(embed=embed, view=view)
