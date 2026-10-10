"""Lucky Coins Event Module - Spontaneous coin drops in chat."""

import asyncio
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Set

import discord
from discord import app_commands
from discord.ext import commands

from config import get_emoji, BOT_OWNER_ID
from storage.user_balance_store import user_balance_store

if TYPE_CHECKING:
    from bot import TournamentBot


# Custom check for bot owner
def is_bot_owner():
    def predicate(interaction: discord.Interaction):
        return interaction.user.id == BOT_OWNER_ID
    return app_commands.check(predicate)


# Глобальный словарь для отслеживания количества сообщений по каналам: {channel_id: message_count}
CHANNEL_MESSAGE_COUNTERS: dict[int, int] = {}

# Глобальный словарь для отслеживания последнего ивента: {channel_id: last_event_time}
LAST_EVENT_TIME: dict[int, datetime] = {}


# ==========================================
# Вспомогательные функции работы с БД
# ==========================================

async def add_balance(guild_id: int, user_id: int, amount: int) -> None:
    """Зачисление монет пользователю."""
    if amount <= 0:
        return
    await user_balance_store.add_balance(guild_id, user_id, amount)


# ==========================================
# UI Компоненты Ивента
# ==========================================

# 1. Режим «На скорость» (Speed Drop)
class SpeedDropView(discord.ui.View):
    def __init__(self, reward_amount: int, event_id: str):
        super().__init__(timeout=60.0)
        self.reward_amount = reward_amount
        self.event_id = event_id
        self.claimed = False

    @discord.ui.button(
        label="Забрать монеты!",
        style=discord.ButtonStyle.green,
        custom_id="speed_coin_drop_btn"  # Will be dynamically updated
    )
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if self.claimed:
            await interaction.followup.send(
                f"{get_emoji('white_dot')} Кто-то оказался быстрее вас!",
                ephemeral=True
            )
            return

        self.claimed = True
        self.stop()

        # Выплата победителю
        await add_balance(interaction.guild_id, interaction.user.id, self.reward_amount)

        result_embed = discord.Embed(
            title=f"{get_emoji('a_star')} **ИВЕНТ ЗАВЕРШЁН | НАГРАДА ЗАБРАНА**",
            description=(
                f"{get_emoji('a_sparkle')} **Счастливчик:** {interaction.user.mention}\n"
                f"{get_emoji('white_arrow')} **Выигрыш:** **`{self.reward_amount:,}`** {get_emoji('money')}\n\n"
                f"{get_emoji('white_dot')} *Монеты зачислены на ваш баланс. Проверить: `/balance`*"
            ),
            color=0x2ECC71
        )
        await interaction.edit_original_response(view=None)
        await interaction.followup.send(embed=result_embed)


# 2. Режим «Розыгрыш / 60 секунд» (Raffle Drop)
class RaffleDropView(discord.ui.View):
    def __init__(self, reward_amount: int, event_id: str):
        super().__init__(timeout=60.0)
        self.reward_amount = reward_amount
        self.event_id = event_id
        self.participants: Set[int] = set()  # Store user IDs instead of User objects

    def build_embed(self) -> discord.Embed:
        return discord.Embed(
            title=f"{get_emoji('a_star')} **СПОНТАННЫЙ ИВЕНТ | ЛОТЕРЕЯ МОНЕТ**",
            description=(
                f"{get_emoji('a_sparkle')} В чате разыгрывается мешок с монетами!\n"
                f"{get_emoji('white_arrow')} Награда: **`{self.reward_amount:,}`** {get_emoji('money')}\n"
                f"{get_emoji('white_arrow')} Участников: **`{len(self.participants)}`**\n\n"
                f"{get_emoji('white_dot')} *Победитель решится случайно через 60 секунд!*"
            ),
            color=0xF1C40F
        )

    @discord.ui.button(
        label="Участвовать в розыгрыше",
        style=discord.ButtonStyle.blurple,
        custom_id="raffle_coin_drop_btn"  # Will be dynamically updated
    )
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if interaction.user.id in self.participants:
            await interaction.followup.send(
                f"{get_emoji('white_dot')} Вы уже участвуете в этом розыгрыше!",
                ephemeral=True
            )
            return

        self.participants.add(interaction.user.id)
        await interaction.edit_original_response(embed=self.build_embed(), view=self)


# ==========================================
# Cog Модуль Автоматического Спавна
# ==========================================

class CoinDropCog(commands.Cog):
    def __init__(self, bot: "TournamentBot"):
        self.bot = bot
        self.target_channel_id = 1200125075156910181

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Игнорируем сообщения ботов и системные сообщения
        if message.author.bot or not message.guild:
            return

        # Проверяем нужный канал
        if message.channel.id != self.target_channel_id:
            return

        channel_id = message.channel.id

        # Проверяем 30-минутный cooldown перед счётом
        last_event = LAST_EVENT_TIME.get(channel_id)
        if last_event:
            time_since_last = datetime.now() - last_event
            if time_since_last < timedelta(minutes=30):
                return  # В кулдауне, не считаем сообщения

        # Считаем сообщения
        CHANNEL_MESSAGE_COUNTERS[channel_id] = CHANNEL_MESSAGE_COUNTERS.get(channel_id, 0) + 1

        # Каждые 100 сообщений запускаем ивент
        if CHANNEL_MESSAGE_COUNTERS[channel_id] >= 100:
            CHANNEL_MESSAGE_COUNTERS[channel_id] = 0  # Сбрасываем счетчик
            await self.trigger_drop(message.channel)

    async def trigger_drop(self, channel: discord.TextChannel):
        """Логика генерации случайного ивента (сумма 100-500, шанс 50/50)."""
        # Записываем время ивента
        LAST_EVENT_TIME[channel.id] = datetime.now()

        reward = random.randint(100, 500)
        is_speed_mode = random.choice([True, False])

        # Генерируем уникальный ID для ивента (чтобы избежать конфликтов кнопок)
        event_id = str(datetime.now().timestamp())

        if is_speed_mode:
            # === РЕЖИМ 1: НА СКОРОСТЬ ===
            view = SpeedDropView(reward, event_id)
            # Обновляем custom_id кнопки чтобы был уникальным
            for item in view.children:
                item.custom_id = f"speed_coin_drop_{event_id}"

            embed = discord.Embed(
                title=f"{get_emoji('a_star')} **СПОНТАННЫЙ ИВЕНТ | БЫСТРЫЕ МОНЕТЫ**",
                description=(
                    f"{get_emoji('a_sparkle')} В чате обнаружен мешок с монетами!\n"
                    f"{get_emoji('white_arrow')} Нажмите кнопку как можно быстрее — кто успеет первым, тот забрал куш.\n\n"
                    f"{get_emoji('white_dot')} Награда: **`{reward:,}`** {get_emoji('money')} | *Режим: На скорость*"
                ),
                color=0xF1C40F
            )
            await channel.send(embed=embed, view=view)

        else:
            # === РЕЖИМ 2: РОЗЫГРЫШ (60 СЕКУНД) ===
            view = RaffleDropView(reward, event_id)
            # Обновляем custom_id кнопки чтобы был уникальным
            for item in view.children:
                item.custom_id = f"raffle_coin_drop_{event_id}"

            embed = view.build_embed()
            msg = await channel.send(embed=embed, view=view)

            # Ожидание 60 секунд для сбора участников
            await asyncio.sleep(60.0)

            if not view.participants:
                no_p_embed = discord.Embed(
                    title=f"{get_emoji('a_star')} **ЛОТЕРЕЯ ЗАВЕРШЕНА | НИКОГО**",
                    description=f"{get_emoji('white_dot')} Никто не успел принять участие за 60 секунд. Мешок с {get_emoji('money')} сгорел!",
                    color=0xED4245
                )
                try:
                    await msg.edit(view=None)
                    await msg.reply(embed=no_p_embed)
                except discord.HTTPException:
                    pass
            else:
                winner_id = random.choice(list(view.participants))
                winner = channel.guild.get_member(winner_id)
                if not winner:
                    # Если пользователь не найден, выбираем другого
                    view.participants.remove(winner_id)
                    if view.participants:
                        winner_id = random.choice(list(view.participants))
                        winner = channel.guild.get_member(winner_id)

                if winner:
                    await add_balance(channel.guild.id, winner.id, reward)

                    result_embed = discord.Embed(
                        title=f"{get_emoji('a_star')} **ЛОТЕРЕЯ ЗАВЕРШЕНА | ПОБЕДИТЕЛЬ**",
                        description=(
                            f"{get_emoji('a_sparkle')} **Победитель:** {winner.mention}\n"
                            f"{get_emoji('white_arrow')} **Выигрыш:** **`{reward:,}`** {get_emoji('money')}\n"
                            f"{get_emoji('white_arrow')} **Всего участников:** `{len(view.participants)}`\n\n"
                            f"{get_emoji('white_dot')} *Монеты зачислены на ваш баланс. Проверить: `/balance`*"
                        ),
                        color=0x2ECC71
                    )
                    try:
                        await msg.edit(view=None)
                        await msg.reply(embed=result_embed)
                    except discord.HTTPException:
                        pass

    # Админ-команда для принудительного вызова ивента
    @app_commands.command(name="forcedrop", description="Принудительно запустить ивент 'Счастливые монеты' (Только владелец бота)")
    @is_bot_owner()
    async def force_drop(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.trigger_drop(interaction.channel)
        await interaction.followup.send(f"{get_emoji('white_dot')} Ивент успешно вызван принудительно!", ephemeral=True)


async def setup(bot: "TournamentBot"):
    await bot.add_cog(CoinDropCog(bot))
