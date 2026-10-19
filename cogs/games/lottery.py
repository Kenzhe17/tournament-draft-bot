"""Lottery game implementation - лотерея."""

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


# Активные лотереи по гильдиям
lotteries = {}  # guild_id -> LotteryState


class LotteryState:
    """Состояние лотереи."""

    def __init__(self, guild_id: int, ticket_price: int, max_tickets: int):
        self.guild_id = guild_id
        self.ticket_price = ticket_price
        self.max_tickets = max_tickets
        self.tickets_sold = 0
        self.participants = {}  # user_id -> ticket_count
        self.active = True
        self.winner = None


class LotteryBuyButton(discord.ui.Button):
    """Кнопка покупки билета."""

    def __init__(self, lottery_view: LotteryView):
        super().__init__(style=discord.ButtonStyle.primary, label="Купить билет")
        self.lottery_view = lottery_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.lottery_view.lottery.active:
            await interaction.response.send_message("❌ Лотерея завершена.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        user_id = interaction.user.id
        lottery = self.lottery_view.lottery

        # Проверить что есть билеты
        if lottery.tickets_sold >= lottery.max_tickets:
            await interaction.response.send_message("❌ Все билеты проданы!", ephemeral=True)
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(guild_id, user_id)
        if balance < lottery.ticket_price:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. У вас: {balance}",
                ephemeral=True
            )
            return

        # Списать деньги
        lock = get_user_lock(guild_id, user_id)
        async with lock:
            balance = await user_balance_store.get_balance(guild_id, user_id)
            if balance < lottery.ticket_price:
                await interaction.response.send_message(
                    f"❌ Недостаточно монет. У вас: {balance}",
                    ephemeral=True
                )
                return

            await user_balance_store.subtract_balance(guild_id, user_id, lottery.ticket_price)

        # Добавить билет
        lottery.tickets_sold += 1
        lottery.participants[user_id] = lottery.participants.get(user_id, 0) + 1

        await interaction.response.send_message(
            f"✅ Вы купили билет! Теперь у вас {lottery.participants[user_id]} билет(ов).",
            ephemeral=True
        )

        # Обновить embed
        updated_embed = self.lottery_view.create_embed("active")
        await interaction.edit_original_response(embed=updated_embed)

        # Проверить если все билеты проданы
        if lottery.tickets_sold >= lottery.max_tickets:
            await self.lottery_view.draw_winner(interaction)


class LotteryCancelButton(discord.ui.Button):
    """Кнопка отмены лотереи."""

    def __init__(self, lottery_view: LotteryView):
        super().__init__(style=discord.ButtonStyle.danger, label="Отменить")
        self.lottery_view = lottery_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.lottery_view.lottery.active:
            await interaction.response.send_message("❌ Лотерея завершена.", ephemeral=True)
            return

        # Вернуть деньги всем участникам
        lottery = self.lottery_view.lottery
        for user_id, ticket_count in lottery.participants.items():
            refund = ticket_count * lottery.ticket_price
            lock = get_user_lock(lottery.guild_id, user_id)
            async with lock:
                await user_balance_store.add_balance(lottery.guild_id, user_id, refund)

        lottery.active = False
        del lotteries[lottery.guild_id]

        await interaction.response.edit_message(
            content="❌ Лотерея отменена. Все деньги возвращены участникам.",
            embed=None,
            view=None
        )


class LotteryView(discord.ui.View):
    """View для игры Лотерея."""

    def __init__(self, lottery_state: LotteryState):
        super().__init__(timeout=300)
        self.lottery = lottery_state

    def create_embed(self, state: str = "active", winner: int = None) -> discord.Embed:
        """Создать embed для разных состояний лотереи."""
        embed = discord.Embed(
            title=f"{replace_emojis('a_sparkle')} **ЛОТЕРЕЯ | /lottery**",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        if state == "active":
            prize_pool = self.lottery.tickets_sold * self.lottery.ticket_price
            remaining = self.lottery.max_tickets - self.lottery.tickets_sold

            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Информация",
                value=f"• Цена билета: **{self.lottery.ticket_price}** {get_emoji('money')}\n"
                      f"• Продано билетов: **{self.lottery.tickets_sold}/{self.lottery.max_tickets}**\n"
                      f"• Призовой фонд: **{prize_pool}** {get_emoji('money')}\n"
                      f"• Осталось билетов: **{remaining}**",
                inline=False
            )
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Правила",
                value="Купите билет(ы) для участия. Когда все билеты будут проданы, будет выбран случайный победитель!",
                inline=False
            )

        elif state == "won":
            prize_pool = self.lottery.tickets_sold * self.lottery.ticket_price
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                value=f"🎉 Победитель: <@{winner}>\n"
                      f"• Выигрыш: **{prize_pool}** {get_emoji('money')}\n"
                      f"• Продано билетов: **{self.lottery.tickets_sold}**",
                inline=False
            )

        return embed

    async def draw_winner(self, interaction: discord.Interaction) -> None:
        """Выбрать победителя."""
        if not self.lottery.participants:
            await interaction.edit_original_response(
                content="❌ Нет участников. Деньги возвращены.",
                embed=None,
                view=None
            )
            self.lottery.active = False
            del lotteries[self.lottery.guild_id]
            return

        # Создать взвешенный список участников (больше билетов = больше шанс)
        weighted_participants = []
        for user_id, ticket_count in self.lottery.participants.items():
            weighted_participants.extend([user_id] * ticket_count)

        # Выбрать победителя
        winner = random.choice(weighted_participants)
        self.lottery.winner = winner
        self.lottery.active = False

        # Выплатить приз
        prize_pool = self.lottery.tickets_sold * self.lottery.ticket_price
        lock = get_user_lock(self.lottery.guild_id, winner)
        async with lock:
            await user_balance_store.add_balance(self.lottery.guild_id, winner, prize_pool)

        # Показать результат
        result_embed = self.create_embed("won", winner)
        await interaction.edit_original_response(embed=result_embed, view=None)

        del lotteries[self.lottery.guild_id]


class LotteryCog(commands.Cog):
    """Lottery game cog."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="lottery", description="Лотерея - покупайте билеты и выигрывайте!")
    @app_commands.describe(ticket_price="Цена билета в монетах (10-1000)")
    @app_commands.describe(max_tickets="Максимальное количество билетов (10-100)")
    async def lottery(
        self,
        interaction: discord.Interaction,
        ticket_price: int = 100,
        max_tickets: int = 50,
    ):
        """Запустить лотерею."""
        # Проверить что в треде
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "❌ Игры можно запускать только в игровых тредах. Используйте `/thread` для создания треда.",
                ephemeral=True
            )
            return

        guild_id = interaction.guild_id

        # Проверить если уже есть активная лотерея
        if guild_id in lotteries and lotteries[guild_id].active:
            await interaction.response.send_message(
                "❌ В этой гильдии уже есть активная лотерея!",
                ephemeral=True
            )
            return

        # Проверить параметры
        if ticket_price < 10 or ticket_price > 1000:
            await interaction.response.send_message(
                "❌ Цена билета должна быть от 10 до 1,000 монет.",
                ephemeral=True
            )
            return

        if max_tickets < 10 or max_tickets > 100:
            await interaction.response.send_message(
                "❌ Количество билетов должно быть от 10 до 100.",
                ephemeral=True
            )
            return

        # Создать лотерею
        lottery_state = LotteryState(guild_id, ticket_price, max_tickets)
        lotteries[guild_id] = lottery_state

        # Создать view
        lottery_view = LotteryView(lottery_state)
        lottery_view.add_item(LotteryBuyButton(lottery_view))
        lottery_view.add_item(LotteryCancelButton(lottery_view))

        embed = lottery_view.create_embed("active")
        await interaction.response.send_message(embed=embed, view=lottery_view)


async def setup(bot: "TournamentBot"):
    """Setup the cog."""
    await bot.add_cog(LotteryCog(bot))
