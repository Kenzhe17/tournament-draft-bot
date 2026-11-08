"""High-Low game implementation - угадай выше или ниже."""

import asyncio
import logging
import random
from typing import TYPE_CHECKING

import discord
from utils.design import compose, text_card
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


class HighLowView(discord.ui.View):
    """View для игры High-Low."""

    def __init__(self, guild_id: int, user_id: int, bet: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.current_number = random.randint(1, 100)
        self.round = 1
        self.max_rounds = 5
        self.wins = 0
        self.game_over = False

    async def on_timeout(self) -> None:
        """Вернуть ставку, если игра не завершена."""
        if self.game_over:
            return
        self.game_over = True  # до возврата — защита от двойного refund
        try:
            lock = get_user_lock(self.guild_id, self.user_id)
            async with lock:
                await user_balance_store.add_balance(self.guild_id, self.user_id, self.bet)
            logger.info(f"HighLow timeout: refunded {self.bet} to {self.user_id}")
        except Exception as e:
            logger.error(f"HighLow on_timeout refund failed: {e}")

    def create_embed(self, state: str = "playing", result: str = None) -> discord.Embed:
        """Создать embed для разных состояний игры."""
        embed = discord.Embed(
            title=f" **HIGH-LOW | /high_low**",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        if state == "playing":
            multiplier = 1.3 ** self.wins
            current_prize = int(self.bet * multiplier)

            embed.add_field(
                name=f"  Информация",
                value=f"• Текущее число: **{self.current_number}**\n"
                      f"• Раунд: **{self.round}/{self.max_rounds}**\n"
                      f"• Побед: **{self.wins}**\n"
                      f"• Текущий приз: **{current_prize}** {get_emoji('money')}\n"
                      f"• Множитель: **{multiplier:.2f}x**",
                inline=False
            )
            embed.add_field(
                name=f"  Правила",
                value="Угадайте будет следующее число выше или ниже текущего. Каждая правильная догадка увеличивает множитель на 1.3x!",
                inline=False
            )

        elif state == "won":
            final_prize = int(self.bet * (1.3 ** self.wins))
            embed.color = discord.Color.green()
            embed.add_field(
                name=f"  Результат",
                value=f" Вы прошли все раунды!\n"
                      f"• Побед: **{self.wins}/{self.max_rounds}**\n"
                      f"• Выигрыш: **{final_prize}** {get_emoji('money')}!",
                inline=False
            )

        elif state == "lost":
            embed.color = discord.Color.red()
            embed.add_field(
                name=f"  Результат",
                value=f" Вы проиграли на раунде {self.round}!\n"
                      f"• Побед: **{self.wins}**\n"
                      f"• Проигрыш: **{self.bet}** {get_emoji('money')}.",
                inline=False
            )

        return embed

    async def handle_guess(self, interaction: discord.Interaction, guess: str) -> None:
        """Обработать догадку."""
        if getattr(self, "_resolving", False):
            await interaction.response.send_message("⏳ Дождитесь результата предыдущего хода.", ephemeral=True)
            return
        self._resolving = True
        try:
            # Defer interaction
            await interaction.response.defer()

            # Сгенерировать следующее число
            next_number = random.randint(1, 100)

            # Проверить результат
            correct = False
            if guess == "higher" and next_number > self.current_number:
                correct = True
            elif guess == "lower" and next_number < self.current_number:
                correct = True
            elif next_number == self.current_number:
                # Равные числа = автоматическая победа
                correct = True

            if correct:
                self.wins += 1
                await interaction.followup.send(f"✅ Правильно! Следующее число: {next_number}", ephemeral=True)
            else:
                self.game_over = True
                await interaction.followup.send(f"❌ Неправильно! Следующее число: {next_number}", ephemeral=True)

            # Обновить текущее число
            self.current_number = next_number
            self.round += 1

            # Проверить конец игры
            if self.game_over:
                # Проигрыш
                from services.game_stats import record_game
                await record_game(self.guild_id, "high_low", self.bet, 0)
                result_embed = self.create_embed("lost")
                result_view = discord.ui.View(timeout=None)
                result_view.add_item(HighLowPlayAgainButton(self.bet, self.user_id))
                await interaction.edit_original_response( view=compose(result_embed, result_view))
            elif self.round > self.max_rounds:
                # Победа
                self.game_over = True
                final_prize = int(self.bet * (1.3 ** self.wins))

                lock = get_user_lock(self.guild_id, self.user_id)
                async with lock:
                    await user_balance_store.add_balance(
                        self.guild_id,
                        self.user_id,
                        final_prize
                    )
                from services.game_stats import record_game
                await record_game(self.guild_id, "high_low", self.bet, final_prize)

                result_embed = self.create_embed("won")
                result_view = discord.ui.View(timeout=None)
                result_view.add_item(HighLowPlayAgainButton(self.bet, self.user_id))
                await interaction.edit_original_response( view=compose(result_embed, result_view))
            else:
                # Продолжить игру
                updated_embed = self.create_embed("playing")
                await interaction.edit_original_response( view=compose(updated_embed, self))
        finally:
            self._resolving = False


class HighLowHigherButton(discord.ui.Button):
    """Кнопка выбора 'Выше'."""

    def __init__(self, game_view: HighLowView):
        super().__init__(style=discord.ButtonStyle.success, label="⬆️ Выше")
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.game_over:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        await self.game_view.handle_guess(interaction, "higher")


class HighLowLowerButton(discord.ui.Button):
    """Кнопка выбора 'Ниже'."""

    def __init__(self, game_view: HighLowView):
        super().__init__(style=discord.ButtonStyle.danger, label="⬇️ Ниже")
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.game_over:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        await self.game_view.handle_guess(interaction, "lower")


class HighLowCancelButton(discord.ui.Button):
    """Кнопка отмены игры."""

    def __init__(self, game_view: HighLowView):
        super().__init__(style=discord.ButtonStyle.secondary, label="Отмена")
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.game_over or getattr(self.game_view, "_resolving", False):
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        self.game_view.game_over = True  # до возврата — защита от гонки с on_timeout
        # Вернуть ставку
        lock = get_user_lock(self.game_view.guild_id, self.game_view.user_id)
        async with lock:
            await user_balance_store.add_balance(
                self.game_view.guild_id,
                self.game_view.user_id,
                self.game_view.bet
            )
        await interaction.response.edit_message( view=text_card("❌ Игра отменена. Ставка возвращена.")
        )


class HighLowPlayAgainButton(discord.ui.Button):
    """Кнопка сыграть снова."""

    def __init__(self, bet: int, user_id: int):
        super().__init__(style=discord.ButtonStyle.primary, label="Сыграть снова")
        self.bet = bet
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        # Защита от повторного нажатия (двойное списание ставки)
        if getattr(self, "_activated", False):
            await interaction.response.send_message("⏳ Игра уже запущена.", ephemeral=True)
            return
        self._activated = True

        guild_id = interaction.guild_id
        user_id = interaction.user.id

        # Проверить баланс
        balance = await user_balance_store.get_balance(guild_id, user_id)
        if balance < self.bet:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. У вас: {balance}",
                ephemeral=True
            )
            self._activated = False
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
                self._activated = False
                return

            await user_balance_store.subtract_balance(guild_id, user_id, self.bet)

        # Создать игру
        game_view = HighLowView(guild_id, user_id, self.bet)

        # Добавить кнопки
        game_view.add_item(HighLowHigherButton(game_view))
        game_view.add_item(HighLowLowerButton(game_view))
        game_view.add_item(HighLowCancelButton(game_view))

        embed = game_view.create_embed("playing")
        await interaction.response.edit_message( view=compose(embed, game_view))


from cogs.groups import games_group, bind_commands


class HighLowCog(commands.Cog):
    """High-Low game cog."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        bind_commands(games_group, self, ["high_low"])

    @games_group.command(name="high_low", description="Угадай выше или ниже (множитель до 3.7x)")
    @app_commands.describe(bet="Ставка в монетах (10-10,000)")
    async def high_low(
        self,
        interaction: discord.Interaction,
        bet: int,
    ):
        """Запустить игру High-Low."""
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
            self._activated = False
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
                self._activated = False
                return

            await user_balance_store.subtract_balance(guild_id, user_id, bet)

        # Создать игру
        game_view = HighLowView(guild_id, user_id, bet)

        # Добавить кнопки
        game_view.add_item(HighLowHigherButton(game_view))
        game_view.add_item(HighLowLowerButton(game_view))
        game_view.add_item(HighLowCancelButton(game_view))

        embed = game_view.create_embed("playing")
        await interaction.response.send_message( view=compose(embed, game_view))


async def setup(bot: "TournamentBot"):
    """Setup the cog."""
    await bot.add_cog(HighLowCog(bot))
