"""Craps game implementation - игра в кости на линии Pass / Don't Pass."""

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


class CrapsView(discord.ui.View):
    """View для игры Крэпс."""

    def __init__(self, guild_id: int, user_id: int, bet: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.rolled = False

    def create_embed(self, state: str = "menu", result: int = None, dice1: int = None, dice2: int = None) -> discord.Embed:
        """Создать embed для разных состояний игры."""
        embed = discord.Embed(
            title=f"{replace_emojis('a_sparkle')} **КРЭПС | /craps**",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        if state == "menu":
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Информация",
                value=f"• Ставка: **{self.bet}** {get_emoji('money')}\n"
                      f"• Множитель: **1x - 2x**\n"
                      f"• Линия: **Pass / Don't Pass**",
                inline=False
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Правила",
                value="Бросьте 2 кубика:\n"
                      "• **Pass Line**: Победа если сумма 7 или 11 (2x), проигрыш если 2, 3 или 12\n"
                      "• **Don't Pass**: Победа если сумма 2, 3 или 12 (2x), проигрыш если 7 или 11",
                inline=False
            )

        elif state == "result":
            result_text = f"{replace_emojis('white_arrow')} Выпало: **{dice1} + {dice2} = {result}**"
            won = False
            winnings = 0

            if self.choice == "pass":
                if result in [7, 11]:
                    won = True
                    winnings = self.bet * 2
                elif result in [2, 3, 12]:
                    won = False
                else:
                    # Other numbers - return bet (push)
                    winnings = self.bet
                    won = True

            elif self.choice == "dont_pass":
                if result in [2, 3, 12]:
                    won = True
                    winnings = self.bet * 2
                elif result in [7, 11]:
                    won = False
                else:
                    # Other numbers - return bet (push)
                    winnings = self.bet
                    won = True

            if won:
                embed.color = discord.Color.green()
                if winnings == self.bet:
                    result_msg = f"• Ваш выбор: **{self.choice}**\n• Push - ставка возвращена."
                else:
                    result_msg = f"• Ваш выбор: **{self.choice}**\n• Поздравляем! Вы выиграли **{winnings}** {get_emoji('money')}!"
                embed.add_field(
                    name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                    value=f"{result_text}\n{result_msg}",
                    inline=False
                )
            else:
                embed.color = discord.Color.red()
                embed.add_field(
                    name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                    value=f"{result_text}\n"
                          f"• Ваш выбор: **{self.choice}**\n"
                          f"• К сожалению, вы проиграли **{self.bet}** {get_emoji('money')}.",
                    inline=False
                )

        return embed


class CrapsPassButton(discord.ui.Button):
    """Кнопка выбора Pass Line."""

    def __init__(self, game_view: CrapsView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Pass Line (7/11)",
            custom_id="craps_pass"
        )
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.rolled:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        await self._roll(interaction, "pass")


class CrapsDontPassButton(discord.ui.Button):
    """Кнопка выбора Don't Pass."""

    def __init__(self, game_view: CrapsView):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Don't Pass (2/3/12)",
            custom_id="craps_dont_pass"
        )
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.rolled:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        await self._roll(interaction, "dont_pass")


async def _roll(self, interaction: discord.Interaction, choice: str):
    """Общий метод для броска костей."""
    game_view = self.game_view

    # Defer interaction
    await interaction.response.defer()

    # Отметить что бросок сделан
    game_view.rolled = True
    game_view.choice = choice

    # Сгенерировать результат (2 кубика)
    dice1 = random.randint(1, 6)
    dice2 = random.randint(1, 6)
    result = dice1 + dice2

    # Обновить state view
    for item in game_view.children:
        item.disabled = True

    # Обработать выигрыш/проигрыш
    won = False
    winnings = 0

    if choice == "pass":
        if result in [7, 11]:
            won = True
            winnings = game_view.bet * 2
        elif result in [2, 3, 12]:
            won = False
        else:
            # Other numbers - return bet (push)
            winnings = game_view.bet
            won = True

    elif choice == "dont_pass":
        if result in [2, 3, 12]:
            won = True
            winnings = game_view.bet * 2
        elif result in [7, 11]:
            won = False
        else:
            # Other numbers - return bet (push)
            winnings = game_view.bet
            won = True

    if won:
        lock = get_user_lock(game_view.guild_id, game_view.user_id)
        async with lock:
            await user_balance_store.add_balance(
                game_view.guild_id,
                game_view.user_id,
                winnings
            )

    # Показать результат
    result_embed = game_view.create_embed("result", result, dice1, dice2)

    # Добавить кнопку "Сыграть снова"
    result_view = discord.ui.View(timeout=None)
    result_view.add_item(CrapsPlayAgainButton(game_view.bet, game_view.user_id))

    await interaction.edit_original_response(embed=result_embed, view=result_view)


# Присоединить метод к классам
CrapsPassButton._roll = _roll
CrapsDontPassButton._roll = _roll


class CrapsCancelButton(discord.ui.Button):
    """Кнопка отмены игры."""

    def __init__(self, game_view: CrapsView):
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


class CrapsPlayAgainButton(discord.ui.Button):
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
        view = CrapsView(guild_id, user_id, self.bet)

        # Добавить кнопки
        view.add_item(CrapsPassButton(view))
        view.add_item(CrapsDontPassButton(view))
        view.add_item(CrapsCancelButton(view))

        embed = view.create_embed("menu")
        await interaction.response.edit_message(embed=embed, view=view)


class CrapsCog(commands.Cog):
    """Craps game cog."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="craps", description="Игра в кости на линии Pass / Don't Pass (2x)")
    @app_commands.describe(bet="Ставка в монетах (50-2,000)")
    async def craps(
        self,
        interaction: discord.Interaction,
        bet: int,
    ):
        """Запустить игру Крэпс."""
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        # Проверить ставку
        if bet < 50 or bet > 2000:
            await interaction.response.send_message(
                "❌ Ставка должна быть от 50 до 2,000 монет.",
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
        game_view = CrapsView(guild_id, user_id, bet)

        # Добавить кнопки
        game_view.add_item(CrapsPassButton(game_view))
        game_view.add_item(CrapsDontPassButton(game_view))
        game_view.add_item(CrapsCancelButton(game_view))

        embed = game_view.create_embed("menu")
        await interaction.response.send_message(embed=embed, view=game_view)


async def setup(bot: "TournamentBot"):
    """Setup the cog."""
    await bot.add_cog(CrapsCog(bot))
