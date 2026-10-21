"""Dice Roll game implementation - бросок кубика 1-6."""

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


class DiceRollView(discord.ui.View):
    """View для игры Кубик."""

    def __init__(self, guild_id: int, user_id: int, bet: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.rolled = False

    def create_embed(self, state: str = "menu", result: int = None) -> discord.Embed:
        """Создать embed для разных состояний игры."""
        embed = discord.Embed(
            title=f"{replace_emojis('a_sparkle')} **КУБИК | /dice_roll**",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        if state == "menu":
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Информация",
                value=f"• Ставка: **{self.bet}** {get_emoji('money')}\n"
                      f"• Множитель: **6.0x**\n"
                      f"• Шанс: **1/6** (16.67%)",
                inline=False
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Правила",
                value="Выберите число от 1 до 6. Если выпадет ваше число, вы выиграете 6x ставку!",
                inline=False
            )

        elif state == "result":
            result_text = f"{replace_emojis('white_arrow')} Выпало: **{result}**"
            if result == self.user_choice:
                winnings = self.bet * 6
                embed.color = discord.Color.green()
                embed.add_field(
                    name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                    value=f"{result_text}\n"
                          f"• Ваш выбор: **{self.user_choice}**\n"
                          f"• Поздравляем! Вы выиграли **{winnings}** {get_emoji('money')}!",
                    inline=False
                )
            else:
                embed.color = discord.Color.red()
                embed.add_field(
                    name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                    value=f"{result_text}\n"
                          f"• Ваш выбор: **{self.user_choice}**\n"
                          f"• К сожалению, вы проиграли **{self.bet}** {get_emoji('money')}.",
                    inline=False
                )

        return embed


class DiceRollButton(discord.ui.Button):
    """Кнопка выбора числа."""

    def __init__(self, number: int, game_view: DiceRollView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=str(number),
            custom_id=f"dice_{number}"
        )
        self.number = number
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.rolled:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        # Defer interaction
        await interaction.response.defer()

        # Отметить что бросок сделан
        self.game_view.rolled = True
        self.game_view.user_choice = self.number

        # Сгенерировать результат
        result = random.randint(1, 6)

        # Обновить state view
        for item in self.game_view.children:
            item.disabled = True

        # Обработать выигрыш/проигрыш
        if result == self.number:
            winnings = self.game_view.bet * 6
            lock = get_user_lock(self.game_view.guild_id, self.game_view.user_id)
            async with lock:
                await user_balance_store.add_balance(
                    self.game_view.guild_id,
                    self.game_view.user_id,
                    winnings
                )
        else:
            # Ставка уже списана при создании игры
            pass

        # Показать результат
        result_embed = self.game_view.create_embed("result", result)

        # Добавить кнопку "Сыграть снова"
        result_view = discord.ui.View(timeout=None)
        result_view.add_item(DiceRollPlayAgainButton(self.game_view.bet, self.game_view.user_id))

        await interaction.edit_original_response(embed=result_embed, view=result_view)


class DiceRollCancelButton(discord.ui.Button):
    """Кнопка отмены игры."""

    def __init__(self, game_view: DiceRollView):
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

        self.game_view.rolled = True
        await interaction.response.edit_message(
            content="❌ Игра отменена. Ставка возвращена.",
            embed=None,
            view=None
        )


class DiceRollPlayAgainButton(discord.ui.Button):
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

        # Создать игру
        game_view = DiceRollView(guild_id, user_id, self.bet)

        # Добавить кнопки
        for i in range(1, 7):
            game_view.add_item(DiceRollButton(i, game_view))
        game_view.add_item(DiceRollCancelButton(game_view))

        embed = game_view.create_embed("menu")
        await interaction.response.edit_message(embed=embed, view=game_view)


class DiceRollCog(commands.Cog):
    """Dice Roll game cog."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="dice_roll", description="Бросок кубика 1-6 (6x множитель)")
    @app_commands.describe(bet="Ставка в монетах (10-10,000)")
    async def dice_roll(
        self,
        interaction: discord.Interaction,
        bet: int,
    ):
        """Запустить игру Кубик."""
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

        # Создать игру
        game_view = DiceRollView(guild_id, user_id, bet)

        # Добавить кнопки
        for i in range(1, 7):
            game_view.add_item(DiceRollButton(i, game_view))
        game_view.add_item(DiceRollCancelButton(game_view))

        embed = game_view.create_embed("menu")
        await interaction.response.send_message(embed=embed, view=game_view)


async def setup(bot: "TournamentBot"):
    """Setup the cog."""
    await bot.add_cog(DiceRollCog(bot))
