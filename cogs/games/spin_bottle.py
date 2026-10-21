"""Spin Bottle game implementation - бутылочка для вызова друзей."""

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


class SpinBottleView(discord.ui.View):
    """View для игры Бутылочка."""

    def __init__(self, guild_id: int, user_id: int, bet: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.spun = False
        self.participants = set()  # Set of user IDs who joined
        self.participants_list = []  # List of member objects for display

    def create_embed(self, state: str = "menu", target: str = None, winner: str = None) -> discord.Embed:
        """Создать embed для разных состояний игры."""
        embed = discord.Embed(
            title=f"{replace_emojis('a_sparkle')} **БУТЫЛОЧКА | /spin_bottle**",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        if state == "menu":
            participants_text = ", ".join([f"<@{uid}>" for uid in self.participants]) if self.participants else "Нет участников"
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Информация",
                value=f"• Ставка: **{self.bet}** {get_emoji('money')}\n"
                      f"• Множитель: **2.0x**\n"
                      f"• PvP игра",
                inline=False
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Участники ({len(self.participants)})",
                value=participants_text,
                inline=False
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Правила",
                value="Нажмите 'Войти' чтобы участвовать. После этого нажмите 'Крутить' - бутылочка выберет случайного участника, который заберёт банк (2x).",
                inline=False
            )

        elif state == "result":
            if winner:
                embed.color = discord.Color.green()
                embed.add_field(
                    name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                    value=f"{replace_emojis('white_arrow')} Бутылочка указала на: **{target}**\n"
                          f"• Победитель: **{winner}**\n"
                          f"• Выигрыш: **{self.bet * 2}** {get_emoji('money')}!",
                    inline=False
                )
            else:
                embed.color = discord.Color.red()
                embed.add_field(
                    name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                    value=f"{replace_emojis('white_arrow')} Нет участников. Ставка возвращена.",
                    inline=False
                )

        return embed


class SpinBottleJoinButton(discord.ui.Button):
    """Кнопка вступить в игру."""

    def __init__(self, game_view: SpinBottleView):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Войти",
            custom_id="spin_bottle_join"
        )
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id in self.game_view.participants:
            await interaction.response.send_message("❌ Вы уже участвуете.", ephemeral=True)
            return

        # Добавить участника
        self.game_view.participants.add(interaction.user.id)
        self.game_view.participants_list.append(interaction.user)

        # Обновить embed
        embed = self.game_view.create_embed("menu")
        await interaction.response.edit_message(embed=embed)


class SpinBottleSpinButton(discord.ui.Button):
    """Кнопка крутить бутылочку."""

    def __init__(self, game_view: SpinBottleView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Крутить",
            custom_id="spin_bottle_spin"
        )
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Только инициатор может крутить.", ephemeral=True)
            return

        if self.game_view.spun:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        # Defer interaction
        await interaction.response.defer()

        # Отметить что бросок сделан
        self.game_view.spun = True

        # Проверить участников
        if not self.game_view.participants:
            # Вернуть ставку
            lock = get_user_lock(self.game_view.guild_id, self.game_view.user_id)
            async with lock:
                await user_balance_store.add_balance(
                    self.game_view.guild_id,
                    self.game_view.user_id,
                    self.game_view.bet
                )

            result_embed = self.game_view.create_embed("result")
            await interaction.edit_original_response(embed=result_embed, view=None)
            return

        # Выбрать случайного участника
        target = random.choice(self.game_view.participants_list)

        # Случайный победитель между инициатором и целью
        winner_user = random.choice([interaction.user, target])

        # Обновить state view
        for item in self.game_view.children:
            item.disabled = True

        # Обработать выигрыш
        if winner_user.id == self.game_view.user_id:
            winnings = self.game_view.bet * 2
            lock = get_user_lock(self.game_view.guild_id, self.game_view.user_id)
            async with lock:
                await user_balance_store.add_balance(
                    self.game_view.guild_id,
                    self.game_view.user_id,
                    winnings
                )
        else:
            # Цель выигрывает
            winnings = self.game_view.bet * 2
            lock = get_user_lock(self.game_view.guild_id, target.id)
            async with lock:
                await user_balance_store.add_balance(
                    self.game_view.guild_id,
                    target.id,
                    winnings
                )

        # Показать результат
        result_embed = self.game_view.create_embed("result", target.display_name, winner_user.display_name)

        # Добавить кнопку "Сыграть снова"
        result_view = discord.ui.View(timeout=None)
        result_view.add_item(SpinBottlePlayAgainButton(self.game_view.bet, self.game_view.user_id))

        await interaction.edit_original_response(embed=result_embed, view=result_view)


class SpinBottleCancelButton(discord.ui.Button):
    """Кнопка отмены игры."""

    def __init__(self, game_view: SpinBottleView):
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


class SpinBottlePlayAgainButton(discord.ui.Button):
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
        view = SpinBottleView(guild_id, user_id, self.bet)

        # Добавить кнопки
        view.add_item(SpinBottleJoinButton(view))
        view.add_item(SpinBottleSpinButton(view))
        view.add_item(SpinBottleCancelButton(view))

        embed = view.create_embed("menu")
        await interaction.response.edit_message(embed=embed, view=view)


class SpinBottleCog(commands.Cog):
    """Spin Bottle game cog."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="spin_bottle", description="Крутите бутылочку и бросайте вызов друзьям (2x)")
    @app_commands.describe(bet="Ставка в монетах (50-2,000)")
    async def spin_bottle(
        self,
        interaction: discord.Interaction,
        bet: int,
    ):
        """Запустить игру Бутылочка."""
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
        game_view = SpinBottleView(guild_id, user_id, bet)

        # Добавить кнопки
        game_view.add_item(SpinBottleJoinButton(game_view))
        game_view.add_item(SpinBottleSpinButton(game_view))
        game_view.add_item(SpinBottleCancelButton(game_view))

        embed = game_view.create_embed("menu")
        await interaction.response.send_message(embed=embed, view=game_view)


async def setup(bot: "TournamentBot"):
    """Setup the cog."""
    await bot.add_cog(SpinBottleCog(bot))
