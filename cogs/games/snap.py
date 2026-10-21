"""Snap game implementation - карточная игра на удвоение или фолд."""

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


class SnapView(discord.ui.View):
    """View для игры Снэп."""

    def __init__(self, guild_id: int, user_id: int, bet: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.played = False

    def create_embed(self, state: str = "menu", player_card: int = None, dealer_card: int = None) -> discord.Embed:
        """Создать embed для разных состояний игры."""
        embed = discord.Embed(
            title=f"{replace_emojis('a_sparkle')} **СНЭП | /snap**",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        if state == "menu":
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Информация",
                value=f"• Ставка: **{self.bet}** {get_emoji('money')}\n"
                      f"• Множитель: **2.0x**\n"
                      f"• PvE игра",
                inline=False
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Правила",
                value="Сравните вашу карту с картой дилера. Если ваша карта выше, вы выиграете 2x ставку!",
                inline=False
            )

        elif state == "result":
            card_names = {11: "J", 12: "Q", 13: "K", 14: "A"}
            player_card_name = card_names.get(player_card, str(player_card))
            dealer_card_name = card_names.get(dealer_card, str(dealer_card))

            if player_card > dealer_card:
                winnings = self.bet * 2
                embed.color = discord.Color.green()
                embed.add_field(
                    name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                    value=f"🃏 Ваша карта: **{player_card_name}**\n"
                          f"🃏 Карта дилера: **{dealer_card_name}**\n"
                          f"• Поздравляем! Вы выиграли **{winnings}** {get_emoji('money')}!",
                    inline=False
                )
            else:
                embed.color = discord.Color.red()
                embed.add_field(
                    name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                    value=f"🃏 Ваша карта: **{player_card_name}**\n"
                          f"🃏 Карта дилера: **{dealer_card_name}**\n"
                          f"• К сожалению, вы проиграли **{self.bet}** {get_emoji('money')}.",
                    inline=False
                )

        return embed


class SnapPlayButton(discord.ui.Button):
    """Кнопка играть."""

    def __init__(self, game_view: SnapView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Играть",
            custom_id="snap_play"
        )
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game_view.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        if self.game_view.played:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
            return

        # Defer interaction
        await interaction.response.defer()

        # Отметить что игра сделана
        self.game_view.played = True

        # Сгенерировать карты (2-14, где 11=J, 12=Q, 13=K, 14=A)
        player_card = random.randint(2, 14)
        dealer_card = random.randint(2, 14)

        # Обновить state view
        for item in self.game_view.children:
            item.disabled = True

        # Обработать выигрыш/проигрыш
        if player_card > dealer_card:
            winnings = self.game_view.bet * 2
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
        result_embed = self.game_view.create_embed("result", player_card, dealer_card)

        # Добавить кнопку "Сыграть снова"
        result_view = discord.ui.View(timeout=None)
        result_view.add_item(SnapPlayAgainButton(self.game_view.bet, self.game_view.user_id))

        await interaction.edit_original_response(embed=result_embed, view=result_view)


class SnapCancelButton(discord.ui.Button):
    """Кнопка отмены игры."""

    def __init__(self, game_view: SnapView):
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

        self.game_view.played = True
        await interaction.response.edit_message(
            content="❌ Игра отменена. Ставка возвращена.",
            embed=None,
            view=None
        )


class SnapPlayAgainButton(discord.ui.Button):
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
        view = SnapView(guild_id, user_id, self.bet)

        # Добавить кнопки
        view.add_item(SnapPlayButton(view))
        view.add_item(SnapCancelButton(view))

        embed = view.create_embed("menu")
        await interaction.response.edit_message(embed=embed, view=view)


class SnapCog(commands.Cog):
    """Snap game cog."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="snap", description="Карточная игра на удвоение или фолд (2x)")
    @app_commands.describe(bet="Ставка в монетах (50-2,000)")
    async def snap(
        self,
        interaction: discord.Interaction,
        bet: int,
    ):
        """Запустить игру Снэп."""
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
        game_view = SnapView(guild_id, user_id, bet)

        # Добавить кнопки
        game_view.add_item(SnapPlayButton(game_view))
        game_view.add_item(SnapCancelButton(game_view))

        embed = game_view.create_embed("menu")
        await interaction.response.send_message(embed=embed, view=game_view)


async def setup(bot: "TournamentBot"):
    """Setup the cog."""
    await bot.add_cog(SnapCog(bot))
