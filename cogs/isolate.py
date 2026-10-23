"""Команда экстренной изоляции сервера /isolate."""

import asyncio
import json
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID, DATA_DIR

logger = logging.getLogger(__name__)

# Файл для хранения snapshot прав
ISOLATION_STATE_FILE = DATA_DIR / "isolation_state.json"


class Isolate(commands.Cog):
    """Команда для экстренной изоляции сервера."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="isolate", description="Экстренная изоляция сервера")
    @app_commands.describe(status="on для включения, off для отключения")
    @app_commands.choices(status=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def isolate(
        self,
        interaction: discord.Interaction,
        status: str
    ) -> None:
        """Включить или отключить режим изоляции сервера."""
        
        # Проверка на владельца бота
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Эта команда доступна только владельцу бота.",
                ephemeral=True
            )
            return

        # Defer ответ, так как операция может занять время
        await interaction.response.defer(ephemeral=True)

        if status == "on":
            await self.enable_isolation(interaction)
        elif status == "off":
            await self.disable_isolation(interaction)

    async def enable_isolation(self, interaction: discord.Interaction) -> None:
        """Включить режим изоляции - скрыть все каналы."""
        
        # Проверка если уже включен
        if ISOLATION_STATE_FILE.exists():
            await interaction.followup.send(
                "⚠️ Режим изоляции уже активен.",
                ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send(
                "❌ Команда может быть использована только на сервере.",
                ephemeral=True
            )
            return

        # Сохранить текущие права (snapshot)
        try:
            snapshot = await self.save_permissions_snapshot(guild)
            
            # Применить изоляцию
            await self.apply_isolation(guild, snapshot)
            
            await interaction.followup.send(
                "✅ Сервер успешно изолирован. Все каналы скрыты.",
                ephemeral=True
            )
            logger.info(f"Изоляция включена на сервере {guild.id} пользователем {interaction.user.id}")
            
        except Exception as e:
            logger.error(f"Ошибка при включении изоляции: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Ошибка при включении изоляции: {str(e)}",
                ephemeral=True
            )
            # Удалить snapshot если произошла ошибка
            if ISOLATION_STATE_FILE.exists():
                ISOLATION_STATE_FILE.unlink()

    async def disable_isolation(self, interaction: discord.Interaction) -> None:
        """Отключить режим изоляции - восстановить права."""
        
        # Проверка если не включен
        if not ISOLATION_STATE_FILE.exists():
            await interaction.followup.send(
                "⚠️ Режим изоляции не активен или снимок прав не найден.",
                ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send(
                "❌ Команда может быть использована только на сервере.",
                ephemeral=True
            )
            return

        try:
            # Загрузить snapshot
            with open(ISOLATION_STATE_FILE, "r", encoding="utf-8") as f:
                snapshot = json.load(f)
            
            # Восстановить права
            await self.restore_permissions(guild, snapshot)
            
            # Удалить snapshot
            ISOLATION_STATE_FILE.unlink()
            
            await interaction.followup.send(
                "✅ Режим изоляции отключён. Права доступа к каналам восстановлены в исходное состояние.",
                ephemeral=True
            )
            logger.info(f"Изоляция отключена на сервере {guild.id} пользователем {interaction.user.id}")
            
        except Exception as e:
            logger.error(f"Ошибка при отключении изоляции: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Ошибка при отключении изоляции: {str(e)}",
                ephemeral=True
            )

    async def save_permissions_snapshot(self, guild: discord.Guild) -> dict:
        """Сохранить текущие права доступа для всех каналов и категорий."""
        
        snapshot = {
            "guild_id": guild.id,
            "channels": {},
            "categories": {}
        }

        # Сохранить права категорий
        for category in guild.categories:
            category_data = {}
            # Сохранить @everyone если есть overwrite
            if category.overwrites_for(guild.default_role).view_channel is not None:
                category_data[str(guild.default_role.id)] = {
                    "type": "role",
                    "view_channel": self._serialize_permission(category.overwrites_for(guild.default_role).view_channel)
                }
            # Сохранить остальные overwrites
            for target, overwrite in category.overwrites.items():
                if target.id == guild.default_role.id:
                    continue  # @everyone уже обработан
                category_data[str(target.id)] = {
                    "type": "role" if isinstance(target, discord.Role) else "member",
                    "view_channel": self._serialize_permission(overwrite.view_channel)
                }
            snapshot["categories"][str(category.id)] = category_data

        # Сохранить права каналов
        for channel in guild.channels:
            channel_data = {}
            # Сохранить @everyone если есть overwrite
            if channel.overwrites_for(guild.default_role).view_channel is not None:
                channel_data[str(guild.default_role.id)] = {
                    "type": "role",
                    "view_channel": self._serialize_permission(channel.overwrites_for(guild.default_role).view_channel)
                }
            # Сохранить остальные overwrites
            for target, overwrite in channel.overwrites.items():
                if target.id == guild.default_role.id:
                    continue  # @everyone уже обработан
                channel_data[str(target.id)] = {
                    "type": "role" if isinstance(target, discord.Role) else "member",
                    "view_channel": self._serialize_permission(overwrite.view_channel)
                }
            snapshot["channels"][str(channel.id)] = channel_data

        # Сохранить в файл
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(ISOLATION_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        logger.info(f"Snapshot сохранен: {len(snapshot['categories'])} категорий, {len(snapshot['channels'])} каналов")
        return snapshot

    def _serialize_permission(self, perm: bool | None) -> str:
        """Сериализовать значение права в строку."""
        if perm is True:
            return "allow"
        elif perm is False:
            return "deny"
        else:
            return "null"

    def _deserialize_permission(self, perm_str: str) -> bool | None:
        """Десериализовать строку в значение права."""
        if perm_str == "allow":
            return True
        elif perm_str == "deny":
            return False
        else:
            return None

    async def apply_isolation(self, guild: discord.Guild, snapshot: dict) -> None:
        """Применить изоляцию - запретить view_channel для всех."""
        
        # Обработать категории
        for category_id, overwrites in snapshot["categories"].items():
            category = guild.get_channel(int(category_id))
            if not category or not isinstance(category, discord.CategoryChannel):
                continue
            
            for target_id, data in overwrites.items():
                # Получить target (role или member)
                target = self._get_target(guild, target_id, data["type"])
                if not target:
                    continue
                
                # Если право было allow или null, установить deny
                old_perm = self._deserialize_permission(data["view_channel"])
                if old_perm in [True, None]:
                    # Установить явный deny
                    await category.set_permissions(
                        target,
                        view_channel=False
                    )
                    # Задержка для rate limits
                    await asyncio.sleep(0.3)

        # Обработать каналы
        for channel_id, overwrites in snapshot["channels"].items():
            channel = guild.get_channel(int(channel_id))
            if not channel:
                continue
            
            for target_id, data in overwrites.items():
                # Получить target (role или member)
                target = self._get_target(guild, target_id, data["type"])
                if not target:
                    continue
                
                # Если право было allow или null, установить deny
                old_perm = self._deserialize_permission(data["view_channel"])
                if old_perm in [True, None]:
                    # Установить явный deny
                    await channel.set_permissions(
                        target,
                        view_channel=False
                    )
                    # Задержка для rate limits
                    await asyncio.sleep(0.3)

        # Дополнительно: установить deny для @everyone во всех каналах без snapshot
        # Это гарантирует что каналы без явных overwrites тоже будут скрыты
        for channel in guild.channels:
            if str(channel.id) not in snapshot["channels"]:
                # Канал не был в snapshot - установить deny для @everyone
                await channel.set_permissions(
                    guild.default_role,
                    view_channel=False
                )
                await asyncio.sleep(0.3)

        logger.info(f"Изоляция применена к {len(snapshot['categories'])} категориям и {len(snapshot['channels'])} каналам")

    def _get_target(self, guild: discord.Guild, target_id: str, target_type: str) -> discord.Role | discord.Member | None:
        """Получить role или member по ID."""
        if target_type == "role":
            return guild.get_role(int(target_id))
        else:
            return guild.get_member(int(target_id))

    async def restore_permissions(self, guild: discord.Guild, snapshot: dict) -> None:
        """Восстановить права доступа из snapshot."""
        
        # Восстановить права категорий
        for category_id, overwrites in snapshot["categories"].items():
            category = guild.get_channel(int(category_id))
            if not category or not isinstance(category, discord.CategoryChannel):
                continue
            
            for target_id, data in overwrites.items():
                target = self._get_target(guild, target_id, data["type"])
                if not target:
                    continue
                
                # Восстановить исходное значение
                perm_value = self._deserialize_permission(data["view_channel"])
                if perm_value is None:
                    # Удалить overwrite (вернуть к наследованию)
                    await category.set_permissions(
                        target,
                        overwrite=None
                    )
                else:
                    # Установить исходное значение
                    await category.set_permissions(
                        target,
                        view_channel=perm_value
                    )
                # Задержка для rate limits
                await asyncio.sleep(0.3)

        # Восстановить права каналов
        for channel_id, overwrites in snapshot["channels"].items():
            channel = guild.get_channel(int(channel_id))
            if not channel:
                continue
            
            for target_id, data in overwrites.items():
                target = self._get_target(guild, target_id, data["type"])
                if not target:
                    continue
                
                # Восстановить исходное значение
                perm_value = self._deserialize_permission(data["view_channel"])
                if perm_value is None:
                    # Удалить overwrite (вернуть к наследованию)
                    await channel.set_permissions(
                        target,
                        overwrite=None
                    )
                else:
                    # Установить исходное значение
                    await channel.set_permissions(
                        target,
                        view_channel=perm_value
                    )
                # Задержка для rate limits
                await asyncio.sleep(0.3)

        logger.info(f"Права восстановлены для {len(snapshot['categories'])} категорий и {len(snapshot['channels'])} каналов")


async def setup(bot: commands.Bot) -> None:
    """Регистрация cog."""
    await bot.add_cog(Isolate(bot))
    logger.info("Cog 'isolate' загружен")
