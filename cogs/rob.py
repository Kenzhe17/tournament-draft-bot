"""Модуль ограбления - /rob команда."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from storage.user_balance_store import user_balance_store
from storage.shop_store import inventory_store, shop_store
from models.shop_item import CosmeticRarity
from config import replace_emojis, get_emoji

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

# Cooldown storage: {guild_id: {user_id: datetime}}
_rob_cooldowns: dict[int, dict[int, datetime]] = {}


class RobView(discord.ui.View):
    """View для интерфейса ограбления."""

    def __init__(self, guild_id: int, robber_id: int, victim_id: int, victim_name: str):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.robber_id = robber_id
        self.victim_id = victim_id
        self.victim_name = victim_name

    def build_embed(self, status: str = "Взлом замка...") -> discord.Embed:
        """Построить embed для ограбления."""
        embed = discord.Embed(
            title=f"{get_emoji('a_sparkle')} **ОГРАБЛЕНИЕ | /rob**",
            color=0x4539717
        )
        embed.set_thumbnail(url="https://cdn.discordapp.com/embed/avatars/0.png")

        embed.add_field(
            name=f"{get_emoji('white_dot')} {get_emoji('white_arrow')} Информация о деле",
            value=f"• Жертва: <@{self.victim_id}>\n• Грабитель: <@{self.robber_id}>\n• Шанс успеха: **50%**\n• Лимит кражи: **до 100% вашего баланса**\n• Статус: **{status}** {get_emoji('white_dots')}",
            inline=False
        )

        embed.add_field(
            name=f"{get_emoji('white_dot')} {get_emoji('white_arrow')} Условия и риски",
            value="При успехе вы заберёте монеты или предмет (с авто-продажей за 30% на Чёрном рынке). При провале вы выплатите жертве штраф до 70% своего баланса.",
            inline=False
        )

        return embed


class RobStartButton(discord.ui.Button):
    """Кнопка для начала ограбления."""

    def __init__(self, guild_id: int, robber_id: int, victim_id: int, victim_name: str, game_view: RobView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Совершить налёт",
            custom_id="rob_start"
        )
        self.guild_id = guild_id
        self.robber_id = robber_id
        self.victim_id = victim_id
        self.victim_name = victim_name
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать нажатие кнопки начала ограбления."""
        # Проверка cooldown
        now = datetime.now()
        if self.guild_id not in _rob_cooldowns:
            _rob_cooldowns[self.guild_id] = {}
        
        if self.robber_id in _rob_cooldowns[self.guild_id]:
            last_rob = _rob_cooldowns[self.guild_id][self.robber_id]
            if now - last_rob < timedelta(hours=3):
                remaining = timedelta(hours=3) - (now - last_rob)
                hours, remainder = divmod(remaining.seconds, 3600)
                minutes, _ = divmod(remainder, 60)
                await interaction.response.edit_message(
                    content=f"{replace_emojis('❌')} Кулдаун! Попробуйте через {hours}ч {minutes}мин.",
                    view=None
                )
                return

        # Получить балансы
        robber_balance = await user_balance_store.get_balance(self.guild_id, self.robber_id)
        victim_balance = await user_balance_store.get_balance(self.guild_id, self.victim_id)

        if robber_balance <= 0:
            await interaction.response.edit_message(
                content=f"{replace_emojis('❌')} У вас нет монет для ограбления!",
                view=None
            )
            return

        if victim_balance <= 0:
            await interaction.response.edit_message(
                content=f"{replace_emojis('❌')} У жертвы нет монет!",
                view=None
            )
            return

        # Проверка инвентаря жертвы
        victim_inventory = inventory_store.get_player_inventory(self.guild_id, self.victim_id)
        
        # Определение доступной редкости по балансу грабителя
        available_rarities = []
        if robber_balance >= 200:
            available_rarities.append(CosmeticRarity.BASIC)
        if robber_balance >= 500:
            available_rarities.append(CosmeticRarity.PREMIUM)
        if robber_balance >= 1000:
            available_rarities.append(CosmeticRarity.ELITE)
        if robber_balance >= 2000:
            available_rarities.append(CosmeticRarity.SPECIAL)

        # Проверка наличия предметов нужной редкости
        stealable_items = []
        for cosmetic in victim_inventory:
            item = shop_store.get_item(cosmetic.item_id)
            if item and item.rarity in available_rarities:
                stealable_items.append(item)

        has_items = len(stealable_items) > 0
        use_item_steal = has_items and random.random() < 0.4  # 40% шанс кражи предмета

        # Бросок на успех/провал
        success = random.random() < 0.5  # 50% шанс

        if success:
            if use_item_steal:
                # Кража предмета
                stolen_item = random.choice(stealable_items)
                inventory_store.remove_item(self.guild_id, self.victim_id, stolen_item.id)
                
                # Продажа за 30% стоимости
                sale_price = int(stolen_item.price * 0.3)
                await user_balance_store.add_balance(self.guild_id, self.robber_id, sale_price)
                
                result_text = f"{replace_emojis('✅')} **УСПЕХ!** Вы украли предмет **{stolen_item.name}** и продали его на Чёрном рынке за **{sale_price}** монет!"
            else:
                # Кража монет
                percent = random.uniform(0.1, 0.5)  # 10% - 50%
                potential = int(victim_balance * percent)
                stolen = min(potential, robber_balance)  # Кап: максимум баланс грабителя
                
                await user_balance_store.subtract_balance(self.guild_id, self.victim_id, stolen)
                await user_balance_store.add_balance(self.guild_id, self.robber_id, stolen)
                
                result_text = f"{replace_emojis('✅')} **УСПЕХ!** Вы украли **{stolen}** монет ({int(percent * 100)}% от баланса жертвы)!"
        else:
            # Провал - штраф
            percent = random.uniform(0.1, 0.5)  # 10% - 50%
            potential = int(victim_balance * percent)
            penalty = min(potential, int(robber_balance * 0.7))  # Максимум 70% от баланса грабителя
            
            await user_balance_store.subtract_balance(self.guild_id, self.robber_id, penalty)
            await user_balance_store.add_balance(self.guild_id, self.victim_id, penalty)
            
            result_text = f"{replace_emojis('❌')} **ПРОВАЛ!** Вас поймали! Вы выплатили жертве штраф **{penalty}** монет."

        # Установить cooldown
        _rob_cooldowns[self.guild_id][self.robber_id] = now

        await interaction.response.edit_message(
            content=result_text,
            embed=None,
            view=None
        )


class RobCog(commands.Cog):
    """Ког с командой ограбления."""

    def __init__(self, bot: TournamentBot):
        self.bot = bot

    @app_commands.command(name="rob", description="Ограбить другого пользователя")
    @app_commands.describe(user="Пользователь, которого хотите ограбить")
    async def rob(self, interaction: discord.Interaction, user: discord.User) -> None:
        """Команда ограбления."""
        if user.id == interaction.user.id:
            await interaction.response.send_message(
                f"{replace_emojis('❌')} Нельзя ограбить самого себя!",
                ephemeral=True
            )
            return

        if user.bot:
            await interaction.response.send_message(
                f"{replace_emojis('❌')} Нельзя ограбить бота!",
                ephemeral=True
            )
            return

        # Создать view
        view = RobView(interaction.guild_id, interaction.user.id, user.id, user.display_name)
        
        # Добавить кнопку
        view.add_item(RobStartButton(interaction.guild_id, interaction.user.id, user.id, user.display_name, view))
        
        # Кнопка возврата в главное меню
        back_button = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Главное меню",
            custom_id="games_menu_back"
        )
        view.add_item(back_button)

        embed = view.build_embed()

        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot: TournamentBot) -> None:
    """Загрузить ког."""
    await bot.add_cog(RobCog(bot))
    logger.info("Rob cog loaded")
