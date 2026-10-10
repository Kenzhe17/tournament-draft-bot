"""Модуль ограбления - /rob и /robgroup команды."""

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
from storage.user_bank_store import user_bank_store
from models.shop_item import CosmeticRarity
from config import replace_emojis, get_emoji, BOT_OWNER_ID

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

# Cooldown storage for /rob: {guild_id: {user_id: datetime}}
_rob_cooldowns: dict[int, dict[int, datetime]] = {}

# Cooldown storage for /robgroup: {guild_id: {user_id: datetime}}
_robgroup_cooldowns: dict[int, dict[int, datetime]] = {}

# Group robbery lobbies: {message_id: {"leader_id": int, "victim_id": int, "members": set[int], "start_time": datetime}}
_robgroup_lobbies: dict[int, dict] = {}

# User locks for concurrent operations
_user_locks: dict[int, asyncio.Lock] = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    """Get or create a lock for a user."""
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]


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
            title=f"{get_emoji('a_star')} **ОГРАБЛЕНИЕ** | `/rob`",
            description=(
                f"{get_emoji('a_sparkle')} **Жертва:** <@{self.victim_id}>\n"
                f"{get_emoji('white_arrow')} **Грабитель:** <@{self.robber_id}>\n"
                f"{get_emoji('white_arrow')} **Шанс:** `50%` | **Лимит:** до `100%` баланса\n"
            ),
            color=0x2B2D31
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
        # Проверка cooldown (пропускаем для владельца бота)
        now = datetime.now()
        if self.guild_id not in _rob_cooldowns:
            _rob_cooldowns[self.guild_id] = {}

        if self.robber_id != BOT_OWNER_ID:  # Нет cooldown для владельца
            if self.robber_id in _rob_cooldowns[self.guild_id]:
                last_rob = _rob_cooldowns[self.guild_id][self.robber_id]
                if now - last_rob < timedelta(hours=2):
                    remaining = timedelta(hours=2) - (now - last_rob)
                    hours, remainder = divmod(remaining.seconds, 3600)
                    minutes, _ = divmod(remainder, 60)
                    await interaction.response.send_message(
                        content=f"{replace_emojis('❌')} Кулдаун! Попробуйте через {hours}ч {minutes}мин.",
                        ephemeral=True
                    )
                    await interaction.message.delete()
                    return

        # Получить балансы
        robber_balance = await user_balance_store.get_balance(self.guild_id, self.robber_id)
        victim_balance = await user_balance_store.get_balance(self.guild_id, self.victim_id)
        victim_bank = await user_bank_store.get_bank_balance(self.guild_id, self.victim_id)

        # Грабить можно только наличные (без денег в сейфе)
        victim_cash = victim_balance  # user_balance это наличные на руках

        if robber_balance <= 100:
            await interaction.response.send_message(
                content=f"{replace_emojis('❌')} У вас недостаточно {get_emoji('money')} для ограбления! Нужно минимум 100.",
                ephemeral=True
            )
            await interaction.message.delete()
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
        use_item_steal = has_items and random.random() < 0.5  # 50% шанс кражи предмета

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

                # Build success embed
                embed = discord.Embed(
                    title=f"{get_emoji('a_star')} **УСПЕШНОЕ ОГРАБЛЕНИЕ | ТРОФЕЙ!**",
                    description=(
                        f"{get_emoji('a_sparkle')} **Грабитель:** <@{self.robber_id}>\n"
                        f"{get_emoji('white_arrow')} **Жертва:** <@{self.victim_id}>\n"
                        f"{get_emoji('white_dot')} **Предмет:** {stolen_item.name}\n"
                        f"{get_emoji('white_dot')} **Продан за:** `{sale_price}` {get_emoji('money')}\n"
                    ),
                    color=0x2B2D31
                )

                await interaction.response.edit_message(embed=embed, view=None)
            else:
                # Кража монет
                if victim_balance == 0:
                    # У жертвы нет монет - считаем ограбление неуспешным
                    embed = discord.Embed(
                        title=f"{get_emoji('a_star')} **ОГРАБЛЕНИЕ НЕУДАЧНО** | `/rob`",
                        description=(
                            f"{get_emoji('a_sparkle')} **Грабитель:** <@{self.robber_id}>\n"
                            f"{get_emoji('white_arrow')} **Жертва:** <@{self.victim_id}>\n"
                            f"{get_emoji('white_dot')} **Результат:** У жертвы 0 {get_emoji('money')}\n"
                        ),
                        color=0x2B2D31
                    )
                    
                    await interaction.response.edit_message(embed=embed, view=None)
                    # Не ставим кулдаун при неуспешном ограблении из-за отсутствия монет
                    return
                else:
                    # Кража монет
                    percent = random.uniform(0.1, 0.5)  # 10% - 50%
                    potential = int(victim_balance * percent)
                    stolen = min(potential, robber_balance)  # Кап: максимум баланс грабителя

                    await user_balance_store.subtract_balance(self.guild_id, self.victim_id, stolen)
                    await user_balance_store.add_balance(self.guild_id, self.robber_id, stolen)

                    # Build success embed
                    embed = discord.Embed(
                        title=f"{get_emoji('a_star')} **УСПЕШНОЕ ОГРАБЛЕНИЕ** | `/rob`",
                        description=(
                            f"{get_emoji('a_sparkle')} **Грабитель:** <@{self.robber_id}>\n"
                            f"{get_emoji('white_arrow')} **Жертва:** <@{self.victim_id}>\n"
                            f"{get_emoji('white_dot')} **Украдено:** `{stolen}` {get_emoji('money')}\n"
                        ),
                        color=0x2B2D31
                    )

                    await interaction.response.edit_message(embed=embed, view=None)
        else:
            # Провал - штраф
            percent = random.uniform(0.1, 0.5)  # 10% - 50%
            potential = int(victim_balance * percent)
            penalty = min(potential, int(robber_balance * 0.7))  # Максимум 70% от баланса грабителя

            await user_balance_store.subtract_balance(self.guild_id, self.robber_id, penalty)
            await user_balance_store.add_balance(self.guild_id, self.victim_id, penalty)

            # Build failure embed
            embed = discord.Embed(
                title=f"{get_emoji('a_star')} **ОГРАБЛЕНИЕ ПРОВАЛЕНО** | `/rob`",
                description=(
                    f"{get_emoji('a_sparkle')} **Грабитель:** <@{self.robber_id}>\n"
                    f"{get_emoji('white_arrow')} **Жертва:** <@{self.victim_id}>\n"
                    f"{get_emoji('white_dot')} **Штраф:** `{penalty}` {get_emoji('money')}\n"
                    f"{get_emoji('white_dot')} **Тюрьма:** 2 часа\n"
                ),
                color=0x2B2D31
            )

            await interaction.response.edit_message(embed=embed, view=None)

        # Установить cooldown только если поймали (провал)
        if not success and self.robber_id != BOT_OWNER_ID:
            _rob_cooldowns[self.guild_id][self.robber_id] = now + timedelta(hours=2)


class RobCog(commands.Cog):
    """Ког с командой ограбления."""

    def __init__(self, bot: TournamentBot):
        self.bot = bot

    @app_commands.command(name="rob", description="Ограбить другого пользователя")
    @app_commands.describe(user="Пользователь, которого хотите ограбить")
    @app_commands.default_permissions()
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

    @app_commands.command(name="robgroup", description="Ограбить другого пользователя группой")
    @app_commands.describe(user="Пользователь, которого хотите ограбить")
    @app_commands.default_permissions()
    async def robgroup(self, interaction: discord.Interaction, user: discord.User) -> None:
        """Команда группового ограбления."""
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
        view = RobGroupView(interaction.guild_id, interaction.user.id, user.id, user.display_name)
        
        # Добавить кнопки
        view.add_item(RobGroupJoinButton(interaction.guild_id, view))
        view.add_item(RobGroupStartButton(interaction.guild_id, view))
        view.add_item(RobGroupCancelButton(interaction.guild_id, view))

        embed = view.build_embed()
        message = await interaction.response.send_message(embed=embed, view=view)
        
        # Сохранить message_id и original_message для лобби
        original_response = await message.original_response()
        view.message_id = original_response.id
        view.original_message = original_response
        _robgroup_lobbies[view.message_id] = {
            "leader_id": interaction.user.id,
            "victim_id": user.id,
            "members": view.members,
            "start_time": view.start_time
        }


class RobGroupView(discord.ui.View):
    """View для группового ограбления."""

    def __init__(self, guild_id: int, leader_id: int, victim_id: int, victim_name: str):
        super().__init__(timeout=120)  # 2 минуты таймаут
        self.guild_id = guild_id
        self.leader_id = leader_id
        self.victim_id = victim_id
        self.victim_name = victim_name
        self.members: set[int] = {leader_id}  # Лидер + участники
        self.start_time = datetime.now()
        self.message_id = 0
        self.original_message = None

    async def on_timeout(self) -> None:
        """Обработать таймаут лобби."""
        if self.message_id in _robgroup_lobbies:
            del _robgroup_lobbies[self.message_id]
        
        if self.original_message:
            try:
                await self.original_message.edit(
                    content=f"{replace_emojis('❌')} Время на сбор истекло! Лобби отменено.",
                    embed=None,
                    view=None
                )
            except:
                pass

    def build_embed(self) -> discord.Embed:
        """Построить embed для группового ограбления."""
        # Рассчитать шанс успеха: 50% для 1 участника, +6% за каждого следующего
        member_count = len(self.members)
        base_chance = 50  # 50% для 1 участника
        bonus = (member_count - 1) * 6  # +6% за каждого дополнительного участника
        success_chance = min(base_chance + bonus, 90)  # Максимум 90%

        # Оставшееся время
        remaining = timedelta(seconds=120) - (datetime.now() - self.start_time)
        seconds = int(remaining.total_seconds())
        minutes, secs = divmod(seconds, 60)
        time_str = f"{minutes:02d}:{secs:02d}"

        # Состав банды
        members_list = ", ".join([f"<@{uid}>" for uid in list(self.members)[:6]])
        if len(self.members) > 6:
            members_list += f" (+{len(self.members) - 6})"

        embed = discord.Embed(
            title=f"{get_emoji('a_star')} **ГРУППОВОЕ ОГРАБЛЕНИЕ** | `/robgroup`",
            description=(
                f"{get_emoji('a_sparkle')} **Цель:** <@{self.victim_id}>\n"
                f"{get_emoji('white_arrow')} **Лидер:** <@{self.leader_id}>\n"
                f"{get_emoji('white_arrow')} **Шанс:** `{success_chance}%` | **Сбор:** `{time_str}`\n"
                f"{get_emoji('white_dot')} **Банда ({len(self.members)}/6):** {members_list}\n\n"
            ),
            color=0x2B2D31
        )
        return embed


class RobGroupJoinButton(discord.ui.Button):
    """Кнопка для вступления в банду."""

    def __init__(self, guild_id: int, game_view: RobGroupView):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Вступить в банду",
            custom_id="robgroup_join"
        )
        self.guild_id = guild_id
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать вступление в банду."""
        user_id = interaction.user.id

        async with get_user_lock(user_id):
            # Проверка cooldown для robgroup
            now = datetime.now()
            if self.guild_id not in _robgroup_cooldowns:
                _robgroup_cooldowns[self.guild_id] = {}

            if user_id != BOT_OWNER_ID:  # Нет cooldown для владельца
                if user_id in _robgroup_cooldowns[self.guild_id]:
                    last_rob = _robgroup_cooldowns[self.guild_id][user_id]
                    if now - last_rob < timedelta(hours=2):
                        remaining = timedelta(hours=2) - (now - last_rob)
                        hours, remainder = divmod(remaining.seconds, 3600)
                        minutes, _ = divmod(remainder, 60)
                        await interaction.response.send_message(
                            content=f"{replace_emojis('❌')} Кулдаун! Попробуйте через {hours}ч {minutes}мин.",
                            ephemeral=True
                        )
                        return

            # Проверка: уже в банде
            if user_id in self.game_view.members:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Вы уже в банде!",
                    ephemeral=True
                )
                return

            # Проверка: нельзя участвовать в ограблении себя
            if user_id == self.game_view.victim_id:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Нельзя участвовать в ограблении самого себя!",
                    ephemeral=True
                )
                return

            # Проверка: лимит участников
            if len(self.game_view.members) >= 6:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Банда полна (максимум 6 участников)!",
                    ephemeral=True
                )
                return

            # Проверка: входной барьер (30% от баланса лидера)
            leader_balance = await user_balance_store.get_balance(self.guild_id, self.game_view.leader_id)
            user_balance = await user_balance_store.get_balance(self.guild_id, user_id)
            required_balance = int(leader_balance * 0.3)

            if user_balance < required_balance:
                await interaction.response.send_message(
                    f"{replace_emojis('❌')} Недостаточно {get_emoji('money')}! Требуется минимум {required_balance} (30% от баланса лидера).",
                    ephemeral=True
                )
                return

            # Добавить в банду
            self.game_view.members.add(user_id)

            # Обновить embed
            embed = self.game_view.build_embed()
            await interaction.response.edit_message(embed=embed)


class RobGroupStartButton(discord.ui.Button):
    """Кнопка для начала штурма."""

    def __init__(self, guild_id: int, game_view: RobGroupView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Начать штурм",
            custom_id="robgroup_start"
        )
        self.guild_id = guild_id
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать начало штурма."""
        # Проверка: только лидер
        if interaction.user.id != self.game_view.leader_id:
            await interaction.response.send_message(
                f"{replace_emojis('❌')} Только лидер может начать штурм!",
                ephemeral=True
            )
            return

        # Проверка: минимум 2 участника
        if len(self.game_view.members) < 2:
            await interaction.response.send_message(
                f"{replace_emojis('❌')} Нужно минимум 2 участника для штурма!",
                ephemeral=True
            )
            return

        # Выполнить ограбление
        await self.execute_robbery(interaction)

    async def execute_robbery(self, interaction: discord.Interaction) -> None:
        """Выполнить групповое ограбление."""
        guild_id = self.game_view.guild_id
        victim_id = self.game_view.victim_id
        members = list(self.game_view.members)
        leader_id = self.game_view.leader_id

        # Проверка cooldown для всех участников (пропускаем владельца бота)
        now = datetime.now()
        if guild_id not in _robgroup_cooldowns:
            _robgroup_cooldowns[guild_id] = {}

        for member_id in members:
            if member_id != BOT_OWNER_ID:  # Нет cooldown для владельца
                if member_id in _robgroup_cooldowns[guild_id]:
                    last_rob = _robgroup_cooldowns[guild_id][member_id]
                    if now - last_rob < timedelta(hours=2):
                        remaining = timedelta(hours=2) - (now - last_rob)
                        hours, remainder = divmod(remaining.seconds, 3600)
                        minutes, _ = divmod(remainder, 60)
                        await interaction.response.send_message(
                            content=f"{replace_emojis('❌')} Кулдаун для участника <@{member_id}>! Попробуйте через {hours}ч {minutes}мин.",
                            ephemeral=True
                        )
                        await interaction.message.delete()
                        return

        # Получить балансы
        victim_balance = await user_balance_store.get_balance(guild_id, victim_id)
        victim_bank = await user_bank_store.get_bank_balance(guild_id, victim_id)

        # Проверить что у всех участников баланс > 100
        for member_id in members:
            member_balance = await user_balance_store.get_balance(guild_id, member_id)
            if member_balance <= 100:
                await interaction.response.send_message(
                    content=f"{replace_emojis('❌')} У участника <@{member_id}> недостаточно {get_emoji('money')} для ограбления! Нужно минимум 100.",
                    ephemeral=True
                )
                await interaction.message.delete()
                return

        # Рассчитать групповой кап
        total_balance = 0
        for member_id in members:
            balance = await user_balance_store.get_balance(guild_id, member_id)
            total_balance += balance

        group_cap = total_balance // len(members)  # Средний баланс

        # Рассчитать шанс успеха: 50% для 1 участника, +6% за каждого следующего
        member_count = len(members)
        base_chance = 50
        bonus = (member_count - 1) * 6
        success_chance = min(base_chance + bonus, 90) / 100

        # Проверка инвентаря жертвы (по балансу лидера)
        leader_balance = await user_balance_store.get_balance(guild_id, leader_id)
        victim_inventory = inventory_store.get_player_inventory(guild_id, victim_id)

        available_rarities = []
        if leader_balance >= 200:
            available_rarities.append(CosmeticRarity.BASIC)
        if leader_balance >= 500:
            available_rarities.append(CosmeticRarity.PREMIUM)
        if leader_balance >= 1000:
            available_rarities.append(CosmeticRarity.ELITE)
        if leader_balance >= 2000:
            available_rarities.append(CosmeticRarity.SPECIAL)

        stealable_items = []
        for cosmetic in victim_inventory:
            item = shop_store.get_item(cosmetic.item_id)
            if item and item.rarity in available_rarities:
                stealable_items.append(item)

        has_items = len(stealable_items) > 0
        use_item_steal = has_items and random.random() < 0.5

        # Бросок на успех/провал
        success = random.random() < success_chance

        if success:
            if use_item_steal:
                # Кража предмета
                stolen_item = random.choice(stealable_items)
                inventory_store.remove_item(guild_id, victim_id, stolen_item.id)

                # Продажа за 30% стоимости
                sale_price = int(stolen_item.price * 0.3)
                share = sale_price // len(members)
                total_stolen = sale_price

                # Выплатить всем участникам
                for member_id in members:
                    await user_balance_store.add_balance(guild_id, member_id, share)
            else:
                # Кража монет
                if victim_balance == 0:
                    # У жертвы нет монет - считаем ограбление неуспешным
                    party_list = "\n".join([f"{get_emoji('white_dot')} <@{mid}>" for mid in members])
                    embed = discord.Embed(
                        title=f"{get_emoji('a_star')} **ГРУППОВОЕ ОГРАБЛЕНИЕ НЕУДАЧНО** | `/robgroup`",
                        description=(
                            f"{get_emoji('a_sparkle')} **Цель:** <@{victim_id}>\n"
                            f"{get_emoji('white_arrow')} **Результат:** У жертвы 0 {get_emoji('money')}\n"
                            f"{get_emoji('white_dot')} **Банда ({len(members)}):**\n{party_list}\n"
                        ),
                        color=0x2B2D31
                    )
                    
                    await interaction.response.edit_message(embed=embed, view=None)
                    # Не ставим кулдаун при неуспешном ограблении из-за отсутствия монет
                    return
                else:
                    # Кража монет
                    percent = random.uniform(0.1, 0.5)
                    potential = int(victim_balance * percent)
                    loot = min(potential, group_cap)

                    await user_balance_store.subtract_balance(guild_id, victim_id, loot)

                    # Разделить поровну
                    share = loot // len(members)
                    total_stolen = loot

                    # Выплатить всем участникам
                    for member_id in members:
                        await user_balance_store.add_balance(guild_id, member_id, share)
        else:
            # Провал - штраф
            percent = random.uniform(0.1, 0.5)
            potential = int(victim_balance * percent)
            total_penalty = min(potential, int(group_cap * 0.7))

            # Круговая порука
            base_share = total_penalty // len(members)
            collected = 0

            for member_id in members:
                async with get_user_lock(member_id):
                    balance = await user_balance_store.get_balance(guild_id, member_id)
                    to_pay = min(base_share, balance)
                    await user_balance_store.subtract_balance(guild_id, member_id, to_pay)
                    collected += to_pay

            # Если есть дефицит, списать со случайного платежеспособного
            deficit = total_penalty - collected
            while deficit > 0:
                solvent_members = []
                for member_id in members:
                    balance = await user_balance_store.get_balance(guild_id, member_id)
                    if balance > 0:
                        solvent_members.append(member_id)

                if not solvent_members:
                    break

                chosen = random.choice(solvent_members)
                async with get_user_lock(chosen):
                    balance = await user_balance_store.get_balance(guild_id, chosen)
                    to_pay = min(deficit, balance)
                    await user_balance_store.subtract_balance(guild_id, chosen, to_pay)
                    collected += to_pay
                    deficit -= to_pay

            # Выплатить жертве
            await user_balance_store.add_balance(guild_id, victim_id, collected)

        # Удалить лобби
        if self.game_view.message_id in _robgroup_lobbies:
            del _robgroup_lobbies[self.game_view.message_id]

        # Build success embed
        if success:
            party_list = "\n".join([f"{get_emoji('white_dot')} <@{mid}>" for mid in members])
            if use_item_steal:
                stolen_amount = sale_price
            else:
                stolen_amount = loot
            embed = discord.Embed(
                title=f"{get_emoji('a_star')} **ГРУППОВОЕ ОГРАБЛЕНИЕ УСПЕХ** | `/robgroup`",
                description=(
                    f"{get_emoji('a_sparkle')} **Цель:** <@{victim_id}>\n"
                    f"{get_emoji('white_arrow')} **Украдено:** `{stolen_amount}` {get_emoji('money')}\n"
                    f"{get_emoji('white_arrow')} **Доля каждого:** `{share}` {get_emoji('money')}\n"
                    f"{get_emoji('white_dot')} **Банда ({len(members)}):**\n{party_list}\n"
                ),
                color=0x2B2D31
            )
            
            await interaction.response.edit_message(view=None)
            await interaction.followup.send(embed=embed)
        else:
            # Failure embed
            party_list = "\n".join([f"{get_emoji('white_dot')} <@{mid}>" for mid in members])
            embed = discord.Embed(
                title=f"{get_emoji('a_star')} **ГРУППОВОЕ ОГРАБЛЕНИЕ ПРОВАЛ** | `/robgroup`",
                description=(
                    f"{get_emoji('a_sparkle')} **Цель:** <@{victim_id}>\n"
                    f"{get_emoji('white_arrow')} **Штраф:** `{collected}` {get_emoji('money')}\n"
                    f"{get_emoji('white_arrow')} **Тюрьма:** 2 часа\n"
                    f"{get_emoji('white_dot')} **Банда ({len(members)}):**\n{party_list}\n"
                ),
                color=0x2B2D31
            )
            
            await interaction.response.edit_message(view=None)
            await interaction.followup.send(embed=embed)
            
            # Установить cooldown для всех участников только если поймали (провал)
            for member_id in members:
                if member_id != BOT_OWNER_ID:
                    _robgroup_cooldowns[guild_id][member_id] = now + timedelta(hours=2)


class RobGroupCancelButton(discord.ui.Button):
    """Кнопка для отмены лобби."""

    def __init__(self, guild_id: int, game_view: RobGroupView):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Отмена",
            custom_id="robgroup_cancel"
        )
        self.guild_id = guild_id
        self.game_view = game_view

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать отмену лобби."""
        # Проверка: только лидер
        if interaction.user.id != self.game_view.leader_id:
            await interaction.response.send_message(
                f"{replace_emojis('❌')} Только лидер может отменить лобби!",
                ephemeral=True
            )
            return

        # Удалить лобби
        if self.game_view.message_id in _robgroup_lobbies:
            del _robgroup_lobbies[self.game_view.message_id]

        await interaction.response.edit_message(
            content=f"{replace_emojis('❌')} Лобби отменено.",
            embed=None,
            view=None
        )


async def setup(bot: TournamentBot) -> None:
    """Загрузить ког."""
    await bot.add_cog(RobCog(bot))
    logger.info("Rob cog loaded")
