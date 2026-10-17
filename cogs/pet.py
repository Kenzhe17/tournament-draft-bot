"""Команды для управления питомцами."""

import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from config import replace_emojis, get_emoji
from storage.pet_store import pet_store, pet_catalog
from storage.db import get_pool
from models.pet import PetRarity

logger = logging.getLogger(__name__)


class PetCog(commands.Cog):
    """Команды питомцев."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    pet = app_commands.Group(name="pet", description="Управление питомцами")

    @pet.command(name="profile", description="Показать профиль питомца")
    async def pet_profile(self, interaction: discord.Interaction) -> None:
        """Показать профиль питомца."""
        await pet_profile_impl(interaction)

    @pet.command(name="release", description="Отпустить питомца на волю (удалить навсегда)")
    async def release(self, interaction: discord.Interaction) -> None:
        """Отпустить питомца."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            pet = await conn.fetchrow(
                "SELECT * FROM pets WHERE owner_1_id = $1 OR owner_2_id = $1",
                interaction.user.id
            )

        if not pet:
            await interaction.response.send_message(
                replace_emojis("❌ У вас нет питомца, которого можно было бы отпустить!"),
                ephemeral=True
            )
            return

        pet_dict = dict(pet)
        
        embed = discord.Embed(
            title=replace_emojis("ПОДТВЕРЖДЕНИЕ | ОТПУСТИТЬ ПИТОМЦА"),
            description=(
                f"Вы действительно хотите попрощаться с питомцем **{pet_dict['pet_name']}**?\n\n"
                f"⚠️ **ВНИМАНИЕ:**\n"
                f"{replace_emojis('white_dot')} Весь прогресс, уровень и способности **будут стерты**.\n"
                f"{replace_emojis('white_dot')} Вернуть питомца назад будет невозможно.\n\n"
                f"> *Это сообщение видите только вы.*"
            ),
            color=discord.Color.red()
        )

        view = PetDeleteConfirmView(owner_id=interaction.user.id, pet_row=pet_dict)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @pet.command(name="coowner", description="Предложить игроку стать со-опекуном питомца")
    @app_commands.describe(target="Игрок, которому предлагаете со-опекунство")
    async def coowner(self, interaction: discord.Interaction, target: discord.Member) -> None:
        """Предложить со-опекунство."""
        await interaction.response.defer()

        if target.id == interaction.user.id:
            await interaction.followup.send(replace_emojis("❌ Вы не можете предложить со-опекунство самому себе!"))
            return

        if target.bot:
            await interaction.followup.send(replace_emojis("❌ Боты не могут быть опекунами питомцев!"))
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            pet = await conn.fetchrow(
                "SELECT * FROM pets WHERE owner_1_id = $1 OR owner_2_id = $1",
                interaction.user.id
            )

            if not pet:
                await interaction.followup.send(replace_emojis("❌ У вас нет питомца!"))
                return

            pet_dict = dict(pet)

            if pet_dict["owner_1_id"] != interaction.user.id:
                await interaction.followup.send(replace_emojis("❌ Только главный опекун может приглашать со-опекунов!"))
                return

            if pet_dict.get("owner_2_id"):
                await interaction.followup.send(replace_emojis("❌ У вашего питомца уже есть второй опекун!"))
                return

            target_pet = await conn.fetchrow(
                "SELECT * FROM pets WHERE owner_1_id = $1 OR owner_2_id = $1",
                target.id
            )
            if target_pet:
                await interaction.followup.send(f"❌ У {target.mention} уже есть питомец!")
                return

        from models.pets_catalog import PETS_CATALOG
        pet_def = PETS_CATALOG.get(pet_dict["pet_type"], {})
        pet_display_name = pet_def.get("name", pet_dict["pet_name"])

        embed = discord.Embed(
            title=replace_emojis("ПРЕДЛОЖЕНИЕ СО-ОПЕКУНСТВА"),
            description=(
                f"{replace_emojis('a_sparkle')} {interaction.user.mention} предлагает {target.mention} стать со-опекуном питомца!\n\n"
                f"**Питомец:** **{pet_display_name}** (*{pet_dict['rarity']}*)\n"
                f"**Уровень:** `{pet_dict['level']} level`\n\n"
                f"📜 **Что даст со-опекунство?**\n"
                f"{replace_emojis('white_dot')} Вы сможете вместе кормить питомца через `/pet profile`.\n"
                f"{replace_emojis('white_dot')} Оба опекуна получают эффекты от активных пассивных способностей.\n\n"
                f"> {target.mention}, используйте кнопки ниже для ответа."
            ),
            color=discord.Color.purple()
        )

        view = CoOwnerInviteView(
            owner_id=interaction.user.id,
            target_id=target.id,
            pet_row=pet_dict
        )

        await interaction.followup.send(content=target.mention, embed=embed, view=view)

    @pet.command(name="coowner_remove", description="Убрать текущего со-опекуна питомца")
    async def coowner_remove(self, interaction: discord.Interaction) -> None:
        """Убрать со-опекуна."""
        await interaction.response.defer()

        pool = await get_pool()
        async with pool.acquire() as conn:
            pet = await conn.fetchrow(
                "SELECT * FROM pets WHERE owner_1_id = $1",
                interaction.user.id
            )

            if not pet:
                await interaction.followup.send(
                    replace_emojis("❌ У вас нет питомца или вы не являетесь главным опекуном!")
                )
                return

            pet_dict = dict(pet)
            old_coowner_id = pet_dict.get("owner_2_id")

            if not old_coowner_id:
                await interaction.followup.send(
                    f"❌ У вашего питомца **{pet_dict['pet_name']}** сейчас нет со-опекуна."
                )
                return

            await conn.execute(
                "UPDATE pets SET owner_2_id = NULL WHERE pet_id = $1",
                pet_dict["pet_id"]
            )

        old_coowner_mention = f"<@{old_coowner_id}>"

        embed = discord.Embed(
            title=replace_emojis("СО-ОПЕКУН УБРАН"),
            description=(
                f"{replace_emojis('a_sparkle')} Главный опекун {interaction.user.mention} прекратил совместную опеку с {old_coowner_mention} для питомца **{pet_dict['pet_name']}** (*{pet_dict['rarity']}*).\n\n"
                f"{replace_emojis('white_dot')} {old_coowner_mention} больше не имеет доступа к управлению и бонусам этого питомца.\n"
                f"{replace_emojis('white_dot')} Питомец снова находится под вашей единоличной опекой."
            ),
            color=discord.Color.red()
        )

        await interaction.followup.send(embed=embed)

    @pet.command(name="top", description="Топ питомцев по уровню")
    async def top(self, interaction: discord.Interaction) -> None:
        """Показать топ питомцев."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            pets = await conn.fetch(
                "SELECT pet_id, pet_name, pet_type, rarity, level, exp, owner_1_id, owner_2_id FROM pets ORDER BY level DESC, exp DESC LIMIT 50"
            )

            # Находим место текущего пользователя
            user_pet = await conn.fetchrow(
                "SELECT * FROM pets WHERE owner_1_id = $1 OR owner_2_id = $1",
                interaction.user.id
            )

            user_rank_info = None
            if user_pet:
                rank = await conn.fetchval(
                    """
                    SELECT COUNT(*) + 1
                    FROM pets
                    WHERE (level > $1) OR (level = $1 AND exp > $2)
                    """,
                    user_pet["level"], user_pet["exp"]
                )
                user_rank_info = f"**Ваше место:** **`#{rank}`** | **Ваш питомец:** **`Ур. {user_pet['level']}`** (`{user_pet['exp']} EXP`)"
            else:
                user_rank_info = "У вас пока нет питомца. Приобретите его в `/shop` → **Питомцы**!"

        if not pets:
            await interaction.response.send_message(
                replace_emojis("❌ Пока нет питомцев."),
                ephemeral=True
            )
            return

        from models.pets_catalog import PETS_CATALOG

        description = ""
        for i, pet in enumerate(pets, 1):
            pet_dict = dict(pet)
            pet_def = PETS_CATALOG.get(pet_dict["pet_type"], {})
            pet_display_name = pet_def.get("name", pet_dict["pet_name"])

            owner_mention = f"<@{pet_dict['owner_1_id']}>"
            if pet_dict["owner_2_id"]:
                owner_mention += f" & <@{pet_dict['owner_2_id']}>"

            medal = ""
            if i == 1:
                medal = get_emoji("gold_medal") or "🥇"
            elif i == 2:
                medal = get_emoji("silver_medal") or "🥈"
            elif i == 3:
                medal = get_emoji("bronze_medal") or "🥉"
            else:
                medal = f"`{i:2d}`"

            description += (
                f"{medal} **`Ур. {pet_dict['level']}`** — **{pet_display_name}** (*{pet_dict['rarity']}*)\n"
                f"{replace_emojis('white_arrow')} **Опекуны:** {owner_mention} — `{pet_dict['exp']:,} EXP`\n\n"
            )

        if user_rank_info:
            description += f"───\n{replace_emojis('white_dot')} {user_rank_info}"

        embed = discord.Embed(
            title=f"{get_emoji('a_star')} **ТОП ПИТОМЦЕВ**",
            description=description,
            color=discord.Color.from_rgb(69, 69, 69)
        )

        await interaction.response.send_message(embed=embed)

async def pet_profile_impl(interaction: discord.Interaction) -> None:
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


class PetDeleteConfirmView(discord.ui.View):
    """Интерактивное меню подтверждения удаления питомца."""
    def __init__(self, owner_id: int, pet_row: dict):
        super().__init__(timeout=60.0)
        self.owner_id = owner_id
        self.pet_row = pet_row

    @discord.ui.button(label="Да, отпустить на волю", style=discord.ButtonStyle.danger, custom_id="pet_delete:confirm")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                replace_emojis("❌ Вы не можете принимать решения за чужого питомца!"),
                ephemeral=True
            )
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM pets WHERE pet_id = $1", self.pet_row["pet_id"])

        await interaction.response.edit_message(
            content=replace_emojis("❌ Действие выполнено. Питомец удален."),
            embed=None,
            view=None
        )

        pet_name = self.pet_row["pet_name"]
        rarity = self.pet_row["rarity"]
        
        public_embed = discord.Embed(
            title=replace_emojis("ПИТОМЕЦ ОТПУЩЕН НА ВОЛЮ"),
            description=(
                f"{replace_emojis('a_sparkle')} Опекун {interaction.user.mention} снял ошейник и попрощался с питомцем **{pet_name}** (*{rarity}*).\n\n"
                f"{replace_emojis('white_dot')} Питомец радостно побежал в дикую природу к своим сородичам.\n"
                f"{replace_emojis('white_dot')} Слот освободился. Новый питомец ждет вас в магазине (`/shop`)."
            ),
            color=discord.Color.from_rgb(149, 165, 166)
        )
        
        await interaction.channel.send(embed=public_embed)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary, custom_id="pet_delete:cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                replace_emojis("❌ Это не ваша кнопка!"),
                ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content=replace_emojis("❌ Процесс отменен. Ваш питомец остался с вами! 🐾"),
            embed=None,
            view=None
        )


class CoOwnerInviteView(discord.ui.View):
    """Кнопки принятия/отклонения предложения стать со-опекуном."""
    def __init__(self, owner_id: int, target_id: int, pet_row: dict):
        super().__init__(timeout=120.0)
        self.owner_id = owner_id
        self.target_id = target_id
        self.pet_row = pet_row

    @discord.ui.button(label="Принять предложение", style=discord.ButtonStyle.success, custom_id="pet_coowner:accept")
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                replace_emojis("❌ Это предложение адресовано не вам!"),
                ephemeral=True
            )
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            existing_pet = await conn.fetchrow(
                "SELECT * FROM pets WHERE owner_1_id = $1 OR owner_2_id = $1",
                self.target_id
            )
            if existing_pet:
                await interaction.response.send_message(
                    replace_emojis("❌ Вы не можете стать со-опекуном, так как у вас уже есть питомец!"),
                    ephemeral=True
                )
                return

            await conn.execute(
                "UPDATE pets SET owner_2_id = $1 WHERE pet_id = $2",
                self.target_id, self.pet_row["pet_id"]
            )

        owner_mention = f"<@{self.owner_id}>"
        pet_name = self.pet_row["pet_name"]

        success_embed = discord.Embed(
            title=replace_emojis("НОВЫЙ СО-ОПЕКУН | УСПЕШНО"),
            description=(
                f"{replace_emojis('a_sparkle')} {interaction.user.mention} принял предложение и стал со-опекуном питомца **{pet_name}**!\n\n"
                f"**Первый опекун:** {owner_mention}\n"
                f"**Второй опекун:** {interaction.user.mention}\n\n"
                f"{replace_emojis('white_dot')} *Теперь вы оба можете кормить питомца и ухаживать за ним через `/pet profile`!*"
            ),
            color=discord.Color.green()
        )

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(embed=success_embed, view=self)

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.secondary, custom_id="pet_coowner:decline")
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                replace_emojis("❌ Это предложение адресовано не вам!"),
                ephemeral=True
            )
            return

        owner_mention = f"<@{self.owner_id}>"
        
        decline_embed = discord.Embed(
            title=replace_emojis("ПРЕДЛОЖЕНИЕ ОТКЛОНЕНО"),
            description=(
                f"{replace_emojis('white_dot')} {interaction.user.mention} отклонил предложение стать со-опекуном питомца **{self.pet_row['pet_name']}**.\n\n"
                f"> {owner_mention}, ваш питомец остаётся под вашей единоличной опекой."
            ),
            color=discord.Color.red()
        )

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(embed=decline_embed, view=self)


async def setup(bot: commands.Bot) -> None:
    """Настроить cog."""
    await bot.add_cog(PetCog(bot))
