"""Roulette game implementation - классическая рулетка."""

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


class RouletteView(discord.ui.View):
    """View для игры Рулетка."""

    def __init__(self, guild_id: int, user_id: int, bet: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.spun = False

    def create_embed(self, state: str = "menu", result: int = None, choice: str = None) -> discord.Embed:
        """Создать embed для разных состояний игры."""
        embed = discord.Embed(
            title=f"{replace_emojis('a_sparkle')} **РУЛЕТКА | /roulette**",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        if state == "menu":
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Информация",
                value=f"• Ставка: **{self.bet}** {get_emoji('money')}\n"
                      f"• Множитель: **2x - 35x**\n"
                      f"• Диапазон: **0-36**",
                inline=False
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Правила",
                value="Выберите вариант ставки:\n"
                      "• **Красный/Чёрный** - 2x\n"
                      "• **Чёт/Нечёт** - 2x\n"
                      "• **Точное число** - 35x",
                inline=False
            )

        elif state == "result":
            result_text = f"{replace_emojis('white_arrow')} Выпало: **{result}**"
            is_red = result in [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]
            is_black = result in [2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35]
            is_even = result % 2 == 0 and result != 0

            won = False
            winnings = 0

            if choice == "red" and is_red:
                won = True
                winnings = self.bet * 2
            elif choice == "black" and is_black:
                won = True
                winnings = self.bet * 2
            elif choice == "even" and is_even:
                won = True
                winnings = self.bet * 2
            elif choice == "odd" and not is_even and result != 0:
                won = True
                winnings = self.bet * 2
            elif choice.isdigit() and int(choice) == result:
                won = True
                winnings = self.bet * 35

            if won:
                embed.color = discord.Color.green()
                embed.add_field(
                    name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                    value=f"{result_text}\n"
                          f"• Ваш выбор: **{choice}**\n"
                          f"• Поздравляем! Вы выиграли **{winnings}** {get_emoji('money')}!",
                    inline=False
                )
            else:
                embed.color = discord.Color.red()
                embed.add_field(
                    name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                    value=f"{result_text}\n"
                          f"• Ваш выбор: **{choice}**\n"
                          f"• К сожалению, вы проиграли **{self.bet}** {get_emoji('money')}.",
                    inline=False
                )

        return embed


class RouletteRedButton(discord.ui.Button):
    """Кнопка выбора красного."""

    def __init__(self, game_view: RouletteView):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Красный (2x)",
            custom_id="roulette_red"
        )
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.spun:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        await self._spin(interaction, "red")


class RouletteBlackButton(discord.ui.Button):
    """Кнопка выбора чёрного."""

    def __init__(self, game_view: RouletteView):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Чёрный (2x)",
            custom_id="roulette_black"
        )
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.spun:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        await self._spin(interaction, "black")


class RouletteEvenButton(discord.ui.Button):
    """Кнопка выбора чёт."""

    def __init__(self, game_view: RouletteView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Чёт (2x)",
            custom_id="roulette_even"
        )
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.spun:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        await self._spin(interaction, "even")


class RouletteOddButton(discord.ui.Button):
    """Кнопка выбора нечёт."""

    def __init__(self, game_view: RouletteView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Нечёт (2x)",
            custom_id="roulette_odd"
        )
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.spun:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        await self._spin(interaction, "odd")


class RouletteSpinButton(discord.ui.Button):
    """Кнопка для выбора точного числа (через модал)."""

    def __init__(self, game_view: RouletteView):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Точное число (35x)",
            custom_id="roulette_number"
        )
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.spun:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        # Показать модал для ввода числа
        modal = RouletteNumberModal(self.game_view)
        await interaction.response.send_modal(modal)


class RouletteNumberModal(discord.ui.Modal, title="Выберите число"):
    """Модал для ввода числа."""

    def __init__(self, game_view: RouletteView):
        super().__init__()
        self.game_view = game_view
        self.number = discord.ui.TextInput(
            label="Число (0-36)",
            placeholder="0-36",
            required=True,
            max_length=2
        )
        self.add_item(self.number)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            num = int(self.number.value)
            if num < 0 or num > 36:
                await interaction.response.send_message("❌ Число должно быть от 0 до 36.", ephemeral=True)
                return
            await self._spin(interaction, str(num))
        except ValueError:
            await interaction.response.send_message("❌ Некорректное число.", ephemeral=True)


async def _spin(self, interaction: discord.Interaction, choice: str):
    """Общий метод для вращения рулетки."""
    game_view = self.game_view

    # Defer interaction
    await interaction.response.defer()

    # Отметить что вращение сделано
    game_view.spun = True

    # Сгенерировать результат
    result = random.randint(0, 36)

    # Обновить state view
    for item in game_view.children:
        item.disabled = True

    # Обработать выигрыш/проигрыш
    is_red = result in [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]
    is_black = result in [2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35]
    is_even = result % 2 == 0 and result != 0

    won = False
    winnings = 0

    if choice == "red" and is_red:
        won = True
        winnings = game_view.bet * 2
    elif choice == "black" and is_black:
        won = True
        winnings = game_view.bet * 2
    elif choice == "even" and is_even:
        won = True
        winnings = game_view.bet * 2
    elif choice == "odd" and not is_even and result != 0:
        won = True
        winnings = game_view.bet * 2
    elif choice.isdigit() and int(choice) == result:
        won = True
        winnings = game_view.bet * 35

    if won:
        lock = get_user_lock(game_view.guild_id, game_view.user_id)
        async with lock:
            await user_balance_store.add_balance(
                game_view.guild_id,
                game_view.user_id,
                winnings
            )

    # Показать результат
    result_embed = game_view.create_embed("result", result, choice)

    # Добавить кнопку "Сыграть снова"
    result_view = discord.ui.View(timeout=None)
    result_view.add_item(RoulettePlayAgainButton(game_view.bet, game_view.user_id))

    await interaction.edit_original_response(embed=result_embed, view=result_view)


# Присоединить метод к классам
RouletteRedButton._spin = _spin
RouletteBlackButton._spin = _spin
RouletteEvenButton._spin = _spin
RouletteOddButton._spin = _spin
RouletteNumberModal._spin = _spin


class RouletteCancelButton(discord.ui.Button):
    """Кнопка отмены игры."""

    def __init__(self, game_view: RouletteView):
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

        self.game_view.spun = True
        await interaction.response.edit_message(
            content="❌ Игра отменена. Ставка возвращена.",
            embed=None,
            view=None
        )


class RoulettePlayAgainButton(discord.ui.Button):
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

        # Создать новую игру
        view = RouletteView(guild_id, user_id, self.bet)

        # Добавить кнопки
        view.add_item(RouletteRedButton(view))
        view.add_item(RouletteBlackButton(view))
        view.add_item(RouletteEvenButton(view))
        view.add_item(RouletteOddButton(view))
        view.add_item(RouletteSpinButton(view))
        view.add_item(RouletteCancelButton(view))

        embed = view.create_embed("menu")
        await interaction.response.edit_message(embed=embed, view=view)


class RouletteCog(commands.Cog):
    """Roulette game cog."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="roulette", description="Классическая рулетка (2x-35x множитель)")
    @app_commands.describe(bet="Ставка в монетах (50-5,000)")
    async def roulette(
        self,
        interaction: discord.Interaction,
        bet: int,
    ):
        """Запустить игру Рулетка."""
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        # Проверить ставку
        if bet < 50 or bet > 5000:
            await interaction.response.send_message(
                "❌ Ставка должна быть от 50 до 5,000 монет.",
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

        # Создать игру
        game_view = RouletteView(guild_id, user_id, bet)

        # Добавить кнопки
        game_view.add_item(RouletteRedButton(game_view))
        game_view.add_item(RouletteBlackButton(game_view))
        game_view.add_item(RouletteEvenButton(game_view))
        game_view.add_item(RouletteOddButton(game_view))
        game_view.add_item(RouletteSpinButton(game_view))
        game_view.add_item(RouletteCancelButton(game_view))

        embed = game_view.create_embed("menu")
        await interaction.response.send_message(embed=embed, view=game_view)


async def setup(bot: "TournamentBot"):
    """Setup the cog."""
    await bot.add_cog(RouletteCog(bot))
