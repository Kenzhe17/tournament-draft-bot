"""View для игры в угадай эмодзи."""

import discord
from discord import app_commands
from discord.ui import Modal, TextInput, View, Button, button
from games.guess_emoji import GuessEmojiGame


class EmojiBetModal(Modal, title="🎭 Угадай эмодзи"):
    """Modal для ввода ставки и эмодзи."""

    bet = TextInput(label="Ставка (монеты)", placeholder="Введите сумму ставки", min_length=1, max_length=10)
    guess = TextInput(label="Ваш эмодзи", placeholder="Введите эмодзи (например: 🐕)", min_length=1, max_length=5)

    def __init__(self, guild_id: int, user_id: int) -> None:
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Обработать отправку формы."""
        from storage.user_balance_store import user_balance_store

        try:
            bet = int(self.bet.value)
            guess = self.guess.value.strip()
        except ValueError:
            await interaction.response.send_message("❌ Введите корректную ставку!", ephemeral=True)
            return

        if not guess or len(guess) > 5:
            await interaction.response.send_message("❌ Введите эмодзи (1-5 символов)!", ephemeral=True)
            return

        if bet < 15:
            await interaction.response.send_message("❌ Минимальная ставка: 15 🪙", ephemeral=True)
            return

        if bet > 300:
            await interaction.response.send_message("❌ Максимальная ставка: 300 🪙", ephemeral=True)
            return

        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        if balance < bet:
            await interaction.response.send_message("❌ Недостаточно монет!", ephemeral=True)
            return

        # Списать ставку
        await user_balance_store.add_balance(self.guild_id, self.user_id, -bet)

        # Создать игру
        game = GuessEmojiGame()
        result, message = game.make_guess(guess)

        # Создать embed
        embed = discord.Embed(
            title="🎭 Угадай эмодзи",
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
                embed.add_field(name="Выигрыш", value=f"{winnings} 🪙", inline=False)
                embed.set_footer(text=f"Множитель: {game.get_multiplier()}x")
            else:
                embed.add_field(name="Потеряно", value=f"{bet} 🪙", inline=False)

            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            # Продолжить игру
            view = GuessEmojiGameView(self.guild_id, self.user_id, bet, game)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class GuessEmojiGameView(View):
    """View для продолжения игры в угадай эмодзи."""

    def __init__(self, guild_id: int, user_id: int, bet: int, game: GuessEmojiGame) -> None:
        super().__init__(timeout=300)  # 5 минут на игру
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.game = game

    @button(label="Сделать попытку", style=discord.ButtonStyle.primary, emoji="🎯")
    async def make_guess(self, interaction: discord.Interaction, button: Button) -> None:
        """Открыть modal для ввода эмодзи."""
        modal = EmojiGuessModal(self.guild_id, self.user_id, self.bet, self.game)
        await interaction.response.send_modal(modal)

    @button(label="Сдаться", style=discord.ButtonStyle.danger, emoji="🏳️")
    async def give_up(self, interaction: discord.Interaction, button: Button) -> None:
        """Сдаться и потерять ставку."""
        self.game.game_over = True
        embed = discord.Embed(
            title="🎭 Угадай эмодзи",
            description=f"😢 Вы сдались! Загаданный эмодзи был {self.game.secret_emoji}.",
            color=discord.Color.red()
        )
        embed.add_field(name="Потеряно", value=f"{self.bet} 🪙", inline=False)
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)


class EmojiGuessModal(Modal, title="🎯 Сделать попытку"):
    """Modal для ввода эмодзи."""

    guess = TextInput(label="Ваш эмодзи", placeholder="Введите эмодзи (например: 🐕)", min_length=1, max_length=5)

    def __init__(self, guild_id: int, user_id: int, bet: int, game: GuessEmojiGame) -> None:
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.game = game

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Обработать отправку формы."""
        from storage.user_balance_store import user_balance_store

        guess = self.guess.value.strip()

        if not guess or len(guess) > 5:
            await interaction.response.send_message("❌ Введите эмодзи (1-5 символов)!", ephemeral=True)
            return

        result, message = self.game.make_guess(guess)

        # Создать embed
        embed = discord.Embed(
            title="🎭 Угадай эмодзи",
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
            view = GuessEmojiGameView(self.guild_id, self.user_id, self.bet, self.game)
            await interaction.response.edit_message(embed=embed, view=view)
