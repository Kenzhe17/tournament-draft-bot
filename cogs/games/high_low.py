"""High-Low game implementation - угадай выше или ниже."""

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


def get_instruction_embed(command: str, bet: int) -> discord.Embed:
    """Создать embed с инструкцией как начать игру."""
    return discord.Embed(
        title=f"{replace_emojis('a_star')} **КАК НАЧАТЬ ИГРУ**",
        description=f"{replace_emojis('a_sparkle')} Для участия в играх необходимо создать персональную ветку.\n\n{replace_emojis('white_arrow')} **Шаг 1. Перейдите в игровой канал:**\nПерейдите в канал <#1561707634938351728>\n\n{replace_emojis('white_arrow')} **Шаг 2. Откройте игровое пространство:**\nОтправьте команду `/thread` для создания личной ветки.\n\n{replace_emojis('white_arrow')} **Шаг 3. Запустите нужную игру внутри ветки:**\n`/{command} bet:{bet}`\n\n{replace_emojis('white_arrow')} **Шаг 4. Завершение сессии:**\nПосле окончания игры обязательно отправьте команду `/delete` прямо внутри ветки, чтобы удалить её.\n\n> {replace_emojis('white_dot')} *Скопируйте нужную команду, подставьте свои значения и отправьте в чат ветки.*",
        color=15105570
    )


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

    def create_embed(self, state: str = "playing", result: str = None) -> discord.Embed:
        """Создать embed для разных состояний игры."""
        embed = discord.Embed(
            title=f"{replace_emojis('a_sparkle')} **HIGH-LOW | /high_low**",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        if state == "playing":
            multiplier = 1.5 ** self.wins
            current_prize = int(self.bet * multiplier)

            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Информация",
                value=f"• Текущее число: **{self.current_number}**\n"
                      f"• Раунд: **{self.round}/{self.max_rounds}**\n"
                      f"• Побед: **{self.wins}**\n"
                      f"• Текущий приз: **{current_prize}** {get_emoji('money')}\n"
                      f"• Множитель: **{multiplier:.2f}x**",
                inline=False
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Правила",
                value="Угадайте будет следующее число выше или ниже текущего. Каждая правильная догадка увеличивает множитель на 1.5x!",
                inline=False
            )

        elif state == "won":
            final_prize = int(self.bet * (1.5 ** self.wins))
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                value=f"Вы прошли все раунды!\n"
                      f"• Побед: **{self.wins}/{self.max_rounds}**\n"
                      f"• Выигрыш: **{final_prize}** {get_emoji('money')}!",
                inline=False
            )

        elif state == "lost":
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                value=f"😢 Вы проиграли на раунде {self.round}!\n"
                      f"• Побед: **{self.wins}**\n"
                      f"• Проигрыш: **{self.bet}** {get_emoji('money')}.",
                inline=False
            )

        return embed

    async def handle_guess(self, interaction: discord.Interaction, guess: str) -> None:
        """Обработать догадку."""
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
            result_embed = self.create_embed("lost")
            result_view = discord.ui.View(timeout=None)
            result_view.add_item(HighLowPlayAgainButton(self.bet, self.user_id))
            await interaction.edit_original_response(embed=result_embed, view=result_view)
        elif self.round > self.max_rounds:
            # Победа
            self.game_over = True
            final_prize = int(self.bet * (1.5 ** self.wins))

            lock = get_user_lock(self.guild_id, self.user_id)
            async with lock:
                await user_balance_store.add_balance(
                    self.guild_id,
                    self.user_id,
                    final_prize
                )

            result_embed = self.create_embed("won")
            result_view = discord.ui.View(timeout=None)
            result_view.add_item(HighLowPlayAgainButton(self.bet, self.user_id))
            await interaction.edit_original_response(embed=result_embed, view=result_view)
        else:
            # Продолжить игру
            updated_embed = self.create_embed("playing")
            await interaction.edit_original_response(embed=updated_embed)


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

        # Создать embed с инструкцией
        embed = get_instruction_embed("high_low", self.bet)

        await interaction.response.edit_message(embed=embed, view=None)


class HighLowCog(commands.Cog):
    """High-Low game cog."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="high_low", description="Угадай выше или ниже (множитель до 7.6x)")
    @app_commands.describe(bet="Ставка в монетах (10-10,000)")
    async def high_low(
        self,
        interaction: discord.Interaction,
        bet: int,
    ):
        """Запустить игру High-Low."""
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
        game_view = HighLowView(guild_id, user_id, bet)

        # Добавить кнопки
        game_view.add_item(HighLowHigherButton(game_view))
        game_view.add_item(HighLowLowerButton(game_view))
        game_view.add_item(HighLowCancelButton(game_view))

        embed = game_view.create_embed("playing")
        await interaction.response.send_message(embed=embed, view=game_view)


async def setup(bot: "TournamentBot"):
    """Setup the cog."""
    await bot.add_cog(HighLowCog(bot))
