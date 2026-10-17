"""Команды для управления питомцами."""

import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from config import replace_emojis, get_emoji
from storage.pet_store import pet_store, pet_catalog
from models.pet import PetRarity

logger = logging.getLogger(__name__)


class PetCog(commands.Cog):
    """Команды питомцев."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="pet", description="Управление питомцами")
    @app_commands.describe(action="Действие")
    async def pet(
        self,
        interaction: discord.Interaction,
        action: str = "profile",
    ) -> None:
        """Команда управления питомцами."""
        if action == "profile":
            await self.pet_profile(interaction)
        else:
            await interaction.response.send_message(
                replace_emojis("❌ Неизвестное действие. Используйте: profile"),
                ephemeral=True
            )

    async def pet_profile(self, interaction: discord.Interaction) -> None:
        """Показать профиль питомца."""
        pet = await pet_store.get_user_pet(interaction.guild_id, interaction.user.id)

        if not pet:
            embed = discord.Embed(
                title=replace_emojis("🐾 Питомец"),
                description=(
                    f"{replace_emojis('sub_middle')} У вас нет питомца.\n"
                    f"{replace_emojis('sub_middle')} Используйте `/shop` → **Питомцы** для покупки."
                ),
                color=discord.Color.from_rgb(69, 69, 69)
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Проверить сытость и энергию
        is_dead, pet = await pet_store.check_fullness_and_energy(pet.pet_id)
        if is_dead:
            embed = discord.Embed(
                title=replace_emojis("💀 Питомец умер"),
                description=(
                    f"{replace_emojis('sub_middle')} Ваш питомец умер от голода.\n"
                    f"{replace_emojis('sub_middle')} Используйте `/shop` для нового питомца."
                ),
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Получить определение питомца
        pet_def = pet_catalog.get_pet(pet.pet_type.value)
        if not pet_def:
            await interaction.response.send_message(
                replace_emojis("❌ Ошибка загрузки данных питомца."),
                ephemeral=True
            )
            return

        # Цвет по редкости из каталога
        color = pet_def.get("color", discord.Color.from_rgb(69, 69, 69))

        # Формирование описания способностей
        ab1 = pet_def["abilities"][1]
        ab2 = pet_def["abilities"][2]

        # Способность 1
        icon_1 = "🔓" if pet.can_use_ability(ab1["unlock_level"]) else f"🔒 Ур. {ab1['unlock_level']}+"
        val_range = f"{ab1['min_val']}-{ab1['max_val']}"
        desc_1 = f"{icon_1} **Ур. {ab1['unlock_level']} | {ab1['name']}** - {ab1['desc_template'].format(val=val_range)}"

        # Способность 2
        icon_2 = "🔓" if pet.can_use_ability(ab2["unlock_level"]) else f"🔒 Ур. {ab2['unlock_level']}+"
        val_range_2 = f"{ab2['min_val']}-{ab2['max_val']}"
        desc_2 = f"{icon_2} **Ур. {ab2['unlock_level']} | {ab2['name']}** - {ab2['desc_template'].format(val=val_range_2)}"

        # Владельцы
        owner_info = f"<@{pet.owner_1_id}>"
        if pet.owner_2_id:
            owner_info += f" & <@{pet.owner_2_id}>"

        # Опыт
        max_exp = pet.get_exp_to_next_level()

        # Активные бонусы
        active_bonuses = []
        if pet.energy <= 0:
            active_bonuses_text = "⚠️ *Все пассивные бонусы заблокированы из-за отсутствия энергии! Покормите питомца.*"
        elif pet.level < 3:
            active_bonuses_text = "💤 *Пока нет активных бонусов. Достигните 3-го уровня, чтобы разблокировать первую способность!*"
        else:
            if pet.can_use_ability(ab1["unlock_level"]):
                active_bonuses.append(f"🔥 **[{ab1['name']}]:** `{ab1['desc_template'].format(val=val_range)}`")
            if pet.can_use_ability(ab2["unlock_level"]):
                active_bonuses.append(f"🔥 **[{ab2['name']}]:** `{ab2['desc_template'].format(val=val_range_2)}`")
            active_bonuses_text = "\n".join(f"• {bonus}" for bonus in active_bonuses) if active_bonuses else "💤 *Пока нет активных бонусов.*"

        # Статус сытости
        status_warning = ""
        if pet.fullness <= 30:
            status_warning = "\n\n⚠️ **КРИТИЧЕСКОЕ СОСТОЯНИЕ!** Питомец умирает от голода!"
        elif pet.fullness > 70:
            status_warning = "\n\n✨ *Питомец сыт и восстанавливает энергию!*"

        embed = discord.Embed(
            title=f"ПРОФИЛЬ ПИТОМЦА | {pet.pet_name.upper()}",
            description=(
                f"**Вид:** **{pet_def['name']}** (*{pet_def['rarity']}*)\n"
                f"**Уровень / Возраст:** `{pet.level} level` ({pet.level} лет)\n"
                f"**Опекуны:** {owner_info}\n\n"
                f"**Состояние:**\n"
                f"🍖 **Сытость:** `{pet.fullness}%` / `100%`\n"
                f"⚡ **Энергия:** `{pet.energy}%` / `100%`\n"
                f"✨ **Опыт:** `{pet.exp}` / `{max_exp} EXP`\n\n"
                f"⚡ **Текущие активные бонусы:**\n"
                f"{active_bonuses_text}\n\n"
                f"📜 **Все способности:**\n"
                f"• {desc_1}\n"
                f"• {desc_2}"
                f"{status_warning}"
            ),
            color=color
        )

        if pet_def.get("image_url"):
            embed.set_thumbnail(url=pet_def["image_url"])

        # Создать View с кнопками
        view = PetProfileView(pet, pet_def)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class PetProfileView(discord.ui.View):
    """View для профиля питомца."""

    def __init__(self, pet, pet_def):
        super().__init__(timeout=180)
        self.pet = pet
        self.pet_def = pet_def
        self.add_item(FeedPetButton(pet.pet_id))
        self.add_item(RenamePetButton(pet.pet_id))


class FeedPetButton(discord.ui.Button):
    """Кнопка кормления питомца."""

    def __init__(self, pet_id: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Покормить (100 🪙)",
            custom_id=f"feed_pet:{pet_id}"
        )
        self.pet_id = pet_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать кормление."""
        from storage.user_balance_store import user_balance_store

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < 100:
            await interaction.response.send_message(
                replace_emojis("❌ Недостаточно монет. Нужно: 100 🪙"),
                ephemeral=True
            )
            return

        # Списать деньги
        await user_balance_store.subtract_balance(interaction.guild_id, interaction.user.id, 100)

        # Покормить (обычная еда: +20 сытость, +5 энергия, +5 EXP)
        success, message = await pet_store.feed_pet(
            self.pet_id,
            fullness_gain=20,
            energy_gain=5,
            exp_gain=5
        )

        if success:
            embed = discord.Embed(
                title=replace_emojis("✅ Питомец покормлен"),
                description=(
                    f"{replace_emojis('sub_middle')} **Сытость:** +20\n"
                    f"{replace_emojis('sub_middle')} **Энергия:** +5\n"
                    f"{replace_emojis('sub_middle')} **Опыт:** +5"
                ),
                color=discord.Color.green()
            )
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            # Вернуть деньги
            await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, 100)
            await interaction.response.send_message(
                replace_emojis(f"❌ {message}"),
                ephemeral=True
            )


class RenamePetButton(discord.ui.Button):
    """Кнопка переименования питомца."""

    def __init__(self, pet_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Переименовать",
            custom_id=f"rename_pet:{pet_id}"
        )
        self.pet_id = pet_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать модальное окно для переименования."""
        await interaction.response.send_modal(RenamePetModal(self.pet_id))


class RenamePetModal(discord.ui.Modal):
    """Модальное окно переименования питомца."""

    def __init__(self, pet_id: int):
        super().__init__(title="Переименовать питомца")
        self.pet_id = pet_id

        self.name_input = discord.ui.TextInput(
            label="Новое имя",
            placeholder="Введите имя питомца",
            max_length=32,
            required=True
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Обработать отправку формы."""
        new_name = self.name_input.value.strip()

        if not new_name:
            await interaction.response.send_message(
                replace_emojis("❌ Имя не может быть пустым."),
                ephemeral=True
            )
            return

        await pet_store.rename_pet(self.pet_id, new_name)

        await interaction.response.send_message(
            replace_emojis(f"✅ Питомец переименован в {new_name}"),
            ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    """Настроить cog."""
    await bot.add_cog(PetCog(bot))
