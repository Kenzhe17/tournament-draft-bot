"""View для игры в угадай число."""

import discord
from discord import app_commands
from discord.ui import Modal, TextInput, View, Button, button
from games.guess_number import GuessNumberGame
from storage.redis_client import set_minigame_session, get_minigame_session, delete_minigame_session
from utils.logger import log_game_play, log_balance_change, log_error


class NumberBetModal(Modal, title="🎲 Угадай число"):
    """Modal для ввода ставки и первого числа."""

    bet = TextInput(label="Ставка (монеты)", placeholder="Введите сумму ставки", min_length=1, max_length=10)
    guess = TextInput(label="Ваше число (1-100)", placeholder="Введите число от 1 до 100", min_length=1, max_length=3)

    def __init__(self, guild_id: int, user_id: int) -> None:
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Обработать отправку формы."""
        from storage.user_balance_store import user_balance_store
        from games.guess_number import GuessNumberGame

        try:
            bet = int(self.bet.value)
            guess = int(self.guess.value)
        except ValueError:
            await interaction.response.send_message("❌ Введите корректные числа!", ephemeral=True)
            return

        if guess < 1 or guess > 100:
            await interaction.response.send_message("❌ Число должно быть от 1 до 100!", ephemeral=True)
            return

        if bet < 20:
            await interaction.response.send_message("❌ Минимальная ставка: 20 🪙", ephemeral=True)
            return

        if bet > 500:
            await interaction.response.send_message("❌ Максимальная ставка: 500 🪙", ephemeral=True)
            return

        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        if balance < bet:
            await interaction.response.send_message("❌ Недостаточно монет!", ephemeral=True)
            return

        # Списать ставку
        await user_balance_store.add_balance(self.guild_id, self.user_id, -bet)
        log_balance_change(self.guild_id, self.user_id, -bet, f"guess_number bet")

        # Создать игру
        game = GuessNumberGame()
        # Сохранить сессию в Redis
        await set_minigame_session(game.session_id, game.get_state())
        result, message = game.make_guess(guess)

        # Создать embed
        embed = discord.Embed(
            title="🎲 Угадай число",
            description=message,
            color=discord.Color.blue() if result == "correct" else discord.Color.orange()
        )
        embed.add_field(name="Ставка", value=f"{bet} 🪙", inline=True)
        embed.add_field(name="Попыток осталось", value=str(game.attempts_left), inline=True)

        # Если игра окончена
        if game.game_over:
            if game.won:
                winnings = int(bet * game.get_multiplier())
                await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)
                log_balance_change(self.guild_id, self.user_id, winnings, f"guess_number win")
                log_game_play(self.guild_id, self.user_id, "guess_number", bet, "win", winnings)
                embed.add_field(name="Выигрыш", value=f"{winnings} 🪙", inline=False)
                embed.set_footer(text=f"Множитель: {game.get_multiplier()}x")
            else:
                log_game_play(self.guild_id, self.user_id, "guess_number", bet, "lose", 0)
                embed.add_field(name="Потеряно", value=f"{bet} 🪙", inline=False)

            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            # Продолжить игру
            view = GuessNumberGameView(self.guild_id, self.user_id, bet, game)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class GuessNumberGameView(View):
    """View для продолжения игры в угадай число."""

    def __init__(self, guild_id: int, user_id: int, bet: int, game: GuessNumberGame) -> None:
        super().__init__(timeout=300)  # 5 минут на игру
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.game = game

    @button(label="Сделать попытку", style=discord.ButtonStyle.primary, emoji="🎯")
    async def make_guess(self, interaction: discord.Interaction, button: Button) -> None:
        """Открыть modal для ввода числа."""
        modal = NumberGuessModal(self.guild_id, self.user_id, self.bet, self.game)
        await interaction.response.send_modal(modal)

    @button(label="Сдаться", style=discord.ButtonStyle.danger, emoji="🏳️")
    async def give_up(self, interaction: discord.Interaction, button: Button) -> None:
        """Сдаться и потерять ставку."""
        self.game.game_over = True
        embed = discord.Embed(
            title="🎲 Угадай число",
            description=f"😢 Вы сдались! Загаданное число было {self.game.secret_number}.",
            color=discord.Color.red()
        )
        embed.add_field(name="Потеряно", value=f"{self.bet} 🪙", inline=False)
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)


class NumberGuessModal(Modal, title="🎯 Сделать попытку"):
    """Modal для ввода числа."""

    guess = TextInput(label="Ваше число (1-100)", placeholder="Введите число от 1 до 100", min_length=1, max_length=3)

    def __init__(self, guild_id: int, user_id: int, bet: int, game: GuessNumberGame) -> None:
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.game = game

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Обработать отправку формы."""
        from storage.user_balance_store import user_balance_store

        try:
            guess = int(self.guess.value)
        except ValueError:
            await interaction.response.send_message("❌ Введите корректное число!", ephemeral=True)
            return

        if guess < 1 or guess > 100:
            await interaction.response.send_message("❌ Число должно быть от 1 до 100!", ephemeral=True)
            return

        result, message = self.game.make_guess(guess)

        # Создать embed
        embed = discord.Embed(
            title="🎲 Угадай число",
            description=message,
            color=discord.Color.blue() if result == "correct" else discord.Color.orange()
        )
        embed.add_field(name="Ставка", value=f"{self.bet} 🪙", inline=True)
        embed.add_field(name="Попыток осталось", value=str(self.game.attempts_left), inline=True)

        # Если игра окончена
        if self.game.game_over:
            if self.game.won:
                winnings = int(self.bet * self.game.get_multiplier())
                await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)
                embed.add_field(name="Выигрыш", value=f"{winnings} 🪙", inline=False)
                embed.set_footer(text=f"Множитель: {self.game.get_multiplier()}x")
            else:
                embed.add_field(name="Потеряно", value=f"{self.bet} 🪙", inline=False)

            await interaction.response.edit_message(embed=embed, view=None)
        else:
            # Продолжить игру
            view = GuessNumberGameView(self.guild_id, self.user_id, self.bet, self.game)
            await interaction.response.edit_message(embed=embed, view=view)
