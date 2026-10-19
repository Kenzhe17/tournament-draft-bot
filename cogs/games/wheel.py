"""Wheel of Fortune game implementation - колесо фортуны."""

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


# Призы колеса с новыми шансами
WHEEL_PRIZES = [
    {"name": "0x Ставка", "multiplier": 0, "emoji": "�", "weight": 40},
    {"name": "0.5x Ставка", "multiplier": 0.5, "emoji": "�", "weight": 25},
    {"name": "1.5x Ставка", "multiplier": 1.5, "emoji": "�", "weight": 15},
    {"name": "2x Ставка", "multiplier": 2, "emoji": "�", "weight": 10},
    {"name": "3x Ставка", "multiplier": 3, "emoji": "🎁", "weight": 5},
    {"name": "5x Ставка", "multiplier": 5, "emoji": "💎", "weight": 3},
    {"name": "7x Ставка", "multiplier": 7, "emoji": "🔥", "weight": 1},
    {"name": "10x Ставка", "multiplier": 10, "emoji": "🌟", "weight": 1},
]


def get_weighted_prize():
    """Выбрать приз с учётом весов."""
    weighted_prizes = []
    for prize in WHEEL_PRIZES:
        weighted_prizes.extend([prize] * prize["weight"])
    return random.choice(weighted_prizes)


class WheelView(discord.ui.View):
    """View для игры Колесо фортуны."""

    def __init__(self, guild_id: int, user_id: int, bet: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.spun = False

    def create_embed(self, state: str = "menu", prize: dict = None) -> discord.Embed:
        """Создать embed для разных состояний игры."""
        embed = discord.Embed(
            title=f"{replace_emojis('a_sparkle')} **КОЛЕСО ФОРТУНЫ | /wheel**",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        if state == "menu":
            prizes_text = "\n".join([
                f"{p['emoji']} {p['name']} - {p['weight']}%"
                for p in WHEEL_PRIZES
            ])
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Информация",
                value=f"• Ставка: **{self.bet}** {get_emoji('money')}",
                inline=False
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Возможные призы",
                value=prizes_text,
                inline=False
            )

        elif state == "result":
            winnings = int(self.bet * prize["multiplier"])
            prize_emoji = prize["emoji"]
            prize_name = prize["name"]

            if winnings > 0:
                embed.add_field(
                    name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                    value=f"{prize_emoji} **{prize_name}**\n"
                          f"• Вы выиграли **{winnings}** {get_emoji('money')}!",
                    inline=False
                )
            else:
                embed.add_field(
                    name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                    value=f"{prize_emoji} **{prize_name}**\n"
                          f"• К сожалению, вы проиграли **{self.bet}** {get_emoji('money')}.",
                    inline=False
                )

        return embed


class WheelSpinButton(discord.ui.Button):
    """Кнопка вращения колеса."""

    def __init__(self, game_view: WheelView):
        super().__init__(style=discord.ButtonStyle.primary, label="🎰 Вращать")
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.spun:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        # Defer interaction
        await interaction.response.defer()

        # Отметить что вращение сделано
        self.game_view.spun = True

        # Сгенерировать результат с учётом весов
        prize = get_weighted_prize()

        # Обновить state view
        for item in self.game_view.children:
            item.disabled = True

        # Обработать выигрыш/проигрыш
        winnings = int(self.game_view.bet * prize["multiplier"])
        if winnings > 0:
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
        result_embed = self.game_view.create_embed("result", prize)

        # Добавить кнопку "Сыграть снова"
        result_view = discord.ui.View(timeout=None)
        result_view.add_item(WheelPlayAgainButton(self.game_view.bet))

        await interaction.edit_original_response(embed=result_embed, view=result_view)


class WheelCancelButton(discord.ui.Button):
    """Кнопка отмены игры."""

    def __init__(self, game_view: WheelView):
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


class WheelPlayAgainButton(discord.ui.Button):
    """Кнопка сыграть снова."""

    def __init__(self, bet: int):
        super().__init__(style=discord.ButtonStyle.primary, label="Сыграть снова")
        self.bet = bet

    async def callback(self, interaction: discord.Interaction) -> None:
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
        game_view = WheelView(guild_id, user_id, self.bet)

        # Добавить кнопки
        game_view.add_item(WheelSpinButton(game_view))
        game_view.add_item(WheelCancelButton(game_view))

        embed = game_view.create_embed("menu")
        await interaction.response.edit_message(embed=embed, view=game_view)


class WheelCog(commands.Cog):
    """Wheel of Fortune game cog."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="wheel", description="Колесо фортуны (множители от 0x до 10x)")
    @app_commands.describe(bet="Ставка в монетах (10-10,000)")
    async def wheel(
        self,
        interaction: discord.Interaction,
        bet: int,
    ):
        """Запустить игру Колесо фортуны."""
        # Проверить что в треде
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "❌ Игры можно запускать только в игровых тредах. Используйте `/thread` для создания треда.",
                ephemeral=True
            )
            return

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
        game_view = WheelView(guild_id, user_id, bet)

        # Добавить кнопки
        game_view.add_item(WheelSpinButton(game_view))
        game_view.add_item(WheelCancelButton(game_view))

        embed = game_view.create_embed("menu")
        await interaction.response.send_message(embed=embed, view=game_view)


async def setup(bot: "TournamentBot"):
    """Setup the cog."""
    await bot.add_cog(WheelCog(bot))
