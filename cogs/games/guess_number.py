"""Guess Number game implementation - угадай число от 1 до 100."""

import asyncio
import logging
import random
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from storage.user_balance_store import user_balance_store
from utils.embeds import replace_emojis
from config import get_emoji

if TYPE_CHECKING:
    from main import TournamentBot

logger = logging.getLogger(__name__)


def get_user_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    """Get or create a lock for user operations."""
    if not hasattr(get_user_lock, "_locks"):
        get_user_lock._locks = {}
    key = (guild_id, user_id)
    if key not in get_user_lock._locks:
        get_user_lock._locks[key] = asyncio.Lock()
    return get_user_lock._locks[key]


class GuessNumberView(discord.ui.View):
    """View для игры Угадай число."""

    def __init__(self, guild_id: int, user_id: int, bet: int, secret_number: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.secret_number = secret_number
        self.attempts = 0
        self.max_attempts = 5
        self.game_over = False
        self.min_range = 1
        self.max_range = 100

    def create_embed(self, state: str = "playing", result: str = None) -> discord.Embed:
        """Создать embed для разных состояний игры."""
        embed = discord.Embed(
            title=f"{replace_emojis('a_sparkle')} **УГАДАЙ ЧИСЛО | /guess_number**",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        if state == "playing":
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Информация",
                value=f"• Ставка: **{self.bet}** {get_emoji('money')}\n"
                      f"• Диапазон: **{self.min_range} - {self.max_range}**\n"
                      f"• Попытки: **{self.attempts}/{self.max_attempts}**\n"
                      f"• Множитель: **5.0x** (при угадывании)",
                inline=False
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Правила",
                value="Угадайте число от 1 до 100. У вас есть 5 попыток. Используйте бинарный поиск для максимального шанса!",
                inline=False
            )

        elif state == "won":
            winnings = self.bet * 5
            embed.color = discord.Color.green()
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                value=f"{replace_emojis('white_arrow')} Вы угадали число **{self.secret_number}**!\n"
                      f"• Попыток: **{self.attempts}**\n"
                      f"• Выигрыш: **{winnings}** {get_emoji('money')} (5x)",
                inline=False
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                value=f"• Секретное число: **{self.secret_number}**\n"
                      f"• Попыток: **{self.attempts}**\n"
                      f"• Поздравляем! Вы выиграли **{winnings}** {get_emoji('money')}!",
                inline=False
            )

        elif state == "lost":
            embed.color = discord.Color.red()
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                value=f"{replace_emojis('white_arrow')} Вы не угадали число **{self.secret_number}**.\n"
                      f"• Попыток: **{self.attempts}**\n"
                      f"• Проигрыш: **{self.bet}** {get_emoji('money')}.",
                inline=False
            )

        return embed


class GuessNumberInput(discord.ui.Modal):
    """Modal для ввода числа."""

    def __init__(self, game_view: GuessNumberView):
        super().__init__(title="Угадай число")
        self.game_view = game_view

        self.number_input = discord.ui.TextInput(
            label="Введите число",
            placeholder=f"От {game_view.min_range} до {game_view.max_range}",
            required=True,
            max_length=3
        )
        self.add_item(self.number_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.game_over:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        # Defer interaction
        await interaction.response.defer()

        # Получить число
        try:
            guess = int(self.number_input.value)
        except ValueError:
            await interaction.followup.send("❌ Введите корректное число.", ephemeral=True)
            return

        # Проверить диапазон
        if guess < self.game_view.min_range or guess > self.game_view.max_range:
            await interaction.followup.send(
                f"❌ Число должно быть от {self.game_view.min_range} до {self.game_view.max_range}.",
                ephemeral=True
            )
            return

        # Увеличить попытки
        self.game_view.attempts += 1

        # Проверить результат
        if guess == self.game_view.secret_number:
            # Победа
            self.game_view.game_over = True
            winnings = int(self.game_view.bet * 1.25)

            lock = get_user_lock(self.game_view.guild_id, self.game_view.user_id)
            async with lock:
                await user_balance_store.add_balance(
                    self.game_view.guild_id,
                    self.game_view.user_id,
                    winnings
                )

            result_embed = self.game_view.create_embed("won")
            result_view = discord.ui.View(timeout=None)
            result_view.add_item(GuessNumberPlayAgainButton(self.game_view.bet, self.game_view.user_id))

            await interaction.edit_original_response(embed=result_embed, view=result_view)

        elif self.game_view.attempts >= self.game_view.max_attempts:
            # Проигрыш
            self.game_view.game_over = True

            result_embed = self.game_view.create_embed("lost")
            result_view = discord.ui.View(timeout=None)
            result_view.add_item(GuessNumberPlayAgainButton(self.game_view.bet, self.game_view.user_id))

            await interaction.edit_original_response(embed=result_embed, view=result_view)

        else:
            # Подсказка
            if guess < self.game_view.secret_number:
                hint = f"Больше! ({guess} - {self.game_view.max_range})"
                self.game_view.min_range = guess + 1
            else:
                hint = f"Меньше! ({self.game_view.min_range} - {guess})"
                self.game_view.max_range = guess - 1

            await interaction.followup.send(f"💡 {hint}", ephemeral=True)

            # Обновить embed
            updated_embed = self.game_view.create_embed("playing")
            await interaction.edit_original_response(embed=updated_embed)


class GuessNumberGuessButton(discord.ui.Button):
    """Кнопка для ввода числа."""

    def __init__(self, game_view: GuessNumberView):
        super().__init__(style=discord.ButtonStyle.primary, label="Угадать")
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.game_over:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        # Показать modal
        modal = GuessNumberInput(self.game_view)
        await interaction.response.send_modal(modal)


class GuessNumberCancelButton(discord.ui.Button):
    """Кнопка отмены игры."""

    def __init__(self, game_view: GuessNumberView):
        super().__init__(style=discord.ButtonStyle.danger, label="Отмена")
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        # Вернуть ставку
        lock = get_user_lock(self.game_view.guild_id, self.game_view.user_id)
        async with lock:
            await user_balance_store.add_balance(
                self.game_view.guild_id,
                self.game_view.user_id,
                self.game_view.bet
            )

        self.game_view.game_over = True
        await interaction.response.edit_message(
            content="❌ Игра отменена. Ставка возвращена.",
            embed=None,
            view=None
        )


class GuessNumberPlayAgainButton(discord.ui.Button):
    """Кнопка сыграть снова."""

    def __init__(self, bet: int, user_id: int):
        super().__init__(style=discord.ButtonStyle.primary, label="Сыграть снова")
        self.bet = bet
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        user_id = interaction.user.id

        # Проверить баланс
        balance = await user_balance_store.get_balance(guild_id, user_id)
        if balance < self.bet:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. У вас: {balance}",
                ephemeral=True
            )
            return

        # Списать ставку
        lock = get_user_lock(guild_id, user_id)
        async with lock:
            balance = await user_balance_store.get_balance(guild_id, user_id)
            if balance < self.bet:
                await interaction.response.send_message(
                    f"❌ Недостаточно монет. У вас: {balance}",
                    ephemeral=True
                )
                return

            await user_balance_store.subtract_balance(guild_id, user_id, self.bet)

        # Сгенерировать новое число
        secret_number = random.randint(1, 100)

        # Создать новую игру
        game_view = GuessNumberView(guild_id, user_id, self.bet, secret_number)

        # Добавить кнопки
        game_view.add_item(GuessNumberGuessButton(game_view))
        game_view.add_item(GuessNumberCancelButton(game_view))

        embed = game_view.create_embed("playing")
        await interaction.response.edit_message(embed=embed, view=game_view)


class GuessNumberCog(commands.Cog):
    """Guess Number game cog."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="guess_number", description="Угадай число от 1 до 100 (1.25x множитель)")
    @app_commands.describe(bet="Ставка в монетах (10-10,000)")
    async def guess_number(
        self,
        interaction: discord.Interaction,
        bet: int,
    ):
        """Запустить игру Угадай число."""
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        # Проверить ставку
        if bet < 10 or bet > 10000:
            await interaction.response.send_message(
                "❌ Ставка должна быть от 10 до 10,000 монет.",
                ephemeral=True
            )
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(guild_id, user_id)
        if balance < bet:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. У вас: {balance}",
                ephemeral=True
            )
            return

        # Списать ставку
        lock = get_user_lock(guild_id, user_id)
        async with lock:
            balance = await user_balance_store.get_balance(guild_id, user_id)
            if balance < bet:
                await interaction.response.send_message(
                    f"❌ Недостаточно монет. У вас: {balance}",
                    ephemeral=True
                )
                return

            await user_balance_store.subtract_balance(guild_id, user_id, bet)

        # Сгенерировать секретное число
        secret_number = random.randint(1, 100)

        # Создать игру
        game_view = GuessNumberView(guild_id, user_id, bet, secret_number)

        # Добавить кнопки
        game_view.add_item(GuessNumberGuessButton(game_view))
        game_view.add_item(GuessNumberCancelButton(game_view))

        embed = game_view.create_embed("playing")
        await interaction.response.send_message(embed=embed, view=game_view)


async def setup(bot: "TournamentBot"):
    """Setup the cog."""
    await bot.add_cog(GuessNumberCog(bot))
