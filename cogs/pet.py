"""Команды для управления питомцами."""

import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from config import replace_emojis, get_emoji
from storage.pet_store import pet_store, pet_catalog
from storage.db import get_pool
from models.pet import PetRarity
from models.pets_catalog import PETS_CATALOG

logger = logging.getLogger(__name__)


async def build_pet_profile_embed(pet_row: dict, guild: discord.Guild) -> Tuple[discord.Embed, Optional[discord.File]]:
    """Формирует компактный профиль питомца с аватаркой в правом верхнем углу (thumbnail)."""
    pet_type = pet_row["pet_type"]
    catalog_data = PETS_CATALOG.get(pet_type, {})

    pet_name = pet_row["pet_name"]
    pet_title = catalog_data.get("name", pet_name)
    color = catalog_data.get("color", 0xF1C40F)
    image_path = catalog_data.get("image_path", f"assets/pets/{pet_type}.png")

    # Опекуны
    owner1 = guild.get_member(pet_row["owner_1_id"])
    owner1_mention = owner1.mention if owner1 else f"<@{pet_row['owner_1_id']}>"

    owner2_mention = ""
    if pet_row.get("owner_2_id"):
        owner2 = guild.get_member(pet_row["owner_2_id"])
        owner2_mention = f" & {owner2.mention if owner2 else f'<@{pet_row[\"owner_2_id\"]}>'}"

    age = pet_row["level"]
    fullness = pet_row["fullness"]
    energy = pet_row["energy"]
    exp = pet_row["exp"]
    max_exp = age * 50

    # Сборка пассивных способностей
    abilities_list = []
    if catalog_data:
        for ab_key in (1, 2):
            ab = catalog_data["abilities"][ab_key]
            val = ab["max_val"]  # Всегда максимальное значение
            desc_val = ab["desc_template"].format(val=f"{val:.1f}")

            if energy <= 0:
                abilities_list.append(f"**{ab['name']}:** *(Отключено: 0% энергии)*")
            else:
                abilities_list.append(f"**{ab['name']}:** {desc_val}")

    abilities_formatted = "\n".join(f"{get_emoji('white_dot')} {item}" for item in abilities_list)

    # Статус сытости
    status_text = f"\n\n{get_emoji('a_sparkle')} *Питомец сыт и восстанавливает энергию!*"
    if fullness <= 0:
        status_text = f"\n\n{get_emoji('white_dot')} **КРИТИЧЕСКОЕ СОСТОЯНИЕ!** Питомец умирает от голода!"
    elif fullness <= 30:
        status_text = f"\n\n{get_emoji('white_dot')} **ПЛОХОЕ СОСТОЯНИЕ!** Питомец может уйти из-за голода!"

    embed = discord.Embed(
        title=f"{get_emoji('a_star')} **ПРОФИЛЬ ПИТОМЦА | {pet_name.upper()}**",
        description=(
            f"{get_emoji('a_sparkle')} **Имя:** **{pet_name}**\n"
            f"{get_emoji('white_arrow')} **Вид:** **{pet_title}**\n"
            f"{get_emoji('white_arrow')} **Возраст:** `{age} лет`\n"
            f"{get_emoji('white_arrow')} **Опекуны:** {owner1_mention}{owner2_mention}\n\n"
            f"**Состояние:**\n"
            f"{get_emoji('white_dot')} 🍖 **Сытость:** `{fullness}%` / `100%`\n"
            f"{get_emoji('white_dot')} ⚡ **Энергия:** `{energy}%` / `100%`\n"
            f"{get_emoji('white_dot')} ✨ **Опыт:** `{exp}` / `{max_exp} EXP`\n\n"
            f"**Пассивные эффекты:**\n"
            f"{abilities_formatted}"
            f"{status_text}"
        ),
        color=color
    )

    # Переносим арт питомца в верхний правый угол (thumbnail)
    file = None
    if os.path.exists(image_path):
        filename = os.path.basename(image_path)
        file = discord.File(image_path, filename=filename)
        embed.set_thumbnail(url=f"attachment://{filename}")

    return embed, file


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

    @pet.command(name="test", description="Тестовый режим: получить питомца с максимальным уровнем")
    @app_commands.describe(pet_type="Тип питомца (например: golden_fox, scout_fox)")
    async def test(self, interaction: discord.Interaction, pet_type: str) -> None:
        """Тестовый режим для проверки способностей."""
        from config import BOT_OWNER_ID, ORG_ROLE_ID

        # Проверяем владельца или организатора
        has_permission = (
            interaction.user.id == BOT_OWNER_ID or
            interaction.user.id == self.bot.owner_id or
            any(role.id == ORG_ROLE_ID for role in interaction.user.roles)
        )

        if not has_permission:
            await interaction.response.send_message(
                replace_emojis("❌ Только владелец бота или организатор могут использовать эту команду."),
                ephemeral=True
            )
            return

        # Проверяем, есть ли уже питомец
        pool = await get_pool()
        async with pool.acquire() as conn:
            existing_pet = await conn.fetchrow(
                "SELECT * FROM pets WHERE owner_1_id = $1 OR owner_2_id = $1",
                interaction.user.id
            )
            if existing_pet:
                await conn.execute("DELETE FROM pets WHERE pet_id = $1", existing_pet["pet_id"])

        # Проверяем, существует ли тип питомца
        if pet_type not in PETS_CATALOG:
            await interaction.response.send_message(
                replace_emojis(f"❌ Неизвестный тип питомца: {pet_type}"),
                ephemeral=True
            )
            return

        # Создаём питомца с максимальным уровнем
        pet_data = PETS_CATALOG[pet_type]
        try:
            pet = await pet_store.create_pet(
                owner_1_id=interaction.user.id,
                owner_2_id=None,
                pet_type=pet_type,
                pet_name=pet_data["name"]
            )

            # Устанавливаем возраст для теста
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE pets SET level = 1, exp = 50, fullness = 100, energy = 100 WHERE pet_id = $1",
                    pet.pet_id
                )

            await interaction.response.send_message(
                replace_emojis(f"✅ Тестовый питомец **{pet_data['name']}** создан!"),
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                replace_emojis(f"❌ Ошибка при создании тестового питомца: {str(e)}"),
                ephemeral=True
            )

    @pet.command(name="catalog", description="Показать каталог всех питомцев и их способностей")
    async def catalog(self, interaction: discord.Interaction) -> None:
        """Показать каталог всех питомцев с навигацией по страницам."""
        from models.pets_catalog import PETS_CATALOG

        pet_list = list(PETS_CATALOG.items())
        view = PetCatalogView(pet_list, interaction.user.id)
        
        # Откладываем ответ перед редактированием
        await interaction.response.defer()
        
        # Показать первого питомца
        await view.show_pet(interaction, 0)

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
                user_rank_info = f"**Ваше место:** **`#{rank}`** | **Ваш питомец:** **{user_pet['pet_name']}** — **`Ур. {user_pet['level']}`** (`{user_pet['exp']} EXP`)"
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
                medal = f"{i}."

            description += (
                f"{medal} **`Ур. {pet_dict['level']}`** — **{pet_dict['pet_name']}** (*{pet_dict['rarity']}*)\n"
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


class PetCatalogView(discord.ui.View):
    """View для навигации по каталогу питомцев."""

    def __init__(self, pet_list, user_id):
        super().__init__(timeout=180)
        self.pet_list = pet_list
        self.user_id = user_id
        self.current_index = 0
        
        self.refresh_buttons()

    def refresh_buttons(self):
        """Обновляет состояние кнопок навигации."""
        # Remove existing navigation buttons
        for item in self.children[:]:
            if isinstance(item, discord.ui.Button):
                self.remove_item(item)

        # Previous button
        if self.current_index > 0:
            prev_button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="◀",
                custom_id="catalog_prev"
            )
            prev_button.callback = self.prev_callback
            self.add_item(prev_button)
        else:
            disabled_button = discord.ui.Button(
                label="◀",
                style=discord.ButtonStyle.primary,
                disabled=True
            )
            self.add_item(disabled_button)

        # Next button
        if self.current_index < len(self.pet_list) - 1:
            next_button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="▶",
                custom_id="catalog_next"
            )
            next_button.callback = self.next_callback
            self.add_item(next_button)
        else:
            disabled_button = discord.ui.Button(
                label="▶",
                style=discord.ButtonStyle.primary,
                disabled=True
            )
            self.add_item(disabled_button)

    async def show_pet(self, interaction, index):
        """Показать страницу с питомцем."""
        pet_id, pet_data = self.pet_list[index]
        
        # Собрать описание способностей
        abilities_text = ""
        abilities = pet_data.get("abilities", {})
        for ab_key in (1, 2):
            if ab_key in abilities:
                ab = abilities[ab_key]
                desc_template = ab.get("desc_template", "")
                desc = desc_template.replace("{val}", f"{ab.get('max_val', 0)}")
                abilities_text += f"{get_emoji('white_arrow')} **{ab['name']}:** {desc}\n"
        
        embed = discord.Embed(
            title=f"{get_emoji('a_star')} **КАТАЛОГ ПИТОМЦЕВ**",
            description=(
                f"{get_emoji('a_sparkle')} **{pet_data['name']}** (*{pet_data['rarity']}*)\n\n"
                f"{get_emoji('white_dot')} **ID:** `{pet_id}`\n"
                f"{get_emoji('white_dot')} **Класс:** `{pet_data['rarity']}`\n"
                f"{get_emoji('white_dot')} **Стоимость:** `{pet_data['price']:,}` {get_emoji('money')}\n\n"
                f"{get_emoji('white_dot')} **Описание:**\n{pet_data['description']}\n\n"
                f"{get_emoji('white_dot')} **Способности:**\n{abilities_text}"
            ),
            color=pet_data.get("color", discord.Color.from_rgb(69, 69, 69))
        )
        
        # Добавить изображение
        image_url = pet_data.get("image_url")
        if image_url:
            embed.set_image(url=image_url)
        
        # Обновить кнопки
        self.refresh_buttons()
        
        # Всегда редактируем сообщение (для кнопок навигации)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def prev_callback(self, interaction: discord.Interaction):
        """Предыдущий питомец."""
        if self.current_index > 0:
            self.current_index -= 1
            await self.show_pet(interaction, self.current_index)

    async def next_callback(self, interaction: discord.Interaction):
        """Следующий питомец."""
        if self.current_index < len(self.pet_list) - 1:
            self.current_index += 1
            await self.show_pet(interaction, self.current_index)


async def pet_profile_impl(interaction: discord.Interaction) -> None:
    """Показать профиль питомца."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        pet_row = await conn.fetchrow(
            "SELECT * FROM pets WHERE owner_1_id = $1 OR owner_2_id = $1",
            interaction.user.id
        )

    if not pet_row:
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

    pet_dict = dict(pet_row)

    # Проверить сытость и энергию
    is_dead, pet = await pet_store.check_fullness_and_energy(pet_dict["pet_id"], interaction.guild_id, interaction.client)
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

    embed, file = await build_pet_profile_embed(pet_dict, interaction.guild)

    # Создать View с кнопками
    view = PetProfileView(pet, pet_dict, interaction)
    if file:
        msg = await interaction.response.send_message(embed=embed, view=view, file=file)
    else:
        msg = await interaction.response.send_message(embed=embed, view=view)

    # Сохраняем сообщение в view
    if msg:
        view.message = msg
        view.rename_button.message = msg


class PetProfileView(discord.ui.View):
    """View для профиля питомца."""

    def __init__(self, pet, pet_dict, interaction: discord.Interaction):
        super().__init__(timeout=180)
        self.pet = pet
        self.pet_dict = pet_dict
        self.interaction = interaction
        self.message = None  # Will be set after response
        self.rename_button = RenamePetButton(pet.pet_id, pet_dict, None)
        self.add_item(FeedPetButton(pet.pet_id, pet_dict))
        self.add_item(self.rename_button)

        # Добавить кнопку защиты предмета для Черепахи-хранителя (с возраста 1, так как способность открыта)
        if pet.pet_type.value == "guardian_turtle" and pet.level >= 1:
            self.add_item(ProtectItemButton(pet.pet_id, pet_dict, interaction))

    async def on_timeout(self) -> None:
        """Remove buttons on timeout."""
        if self.message:
            await self.message.edit(view=None)


class FeedPetButton(discord.ui.Button):
    """Кнопка кормления питомца."""

    def __init__(self, pet_id: int, pet_dict: dict):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Покормить (30 🪙)",
            custom_id=f"feed_pet:{pet_id}"
        )
        self.pet_id = pet_id
        self.pet_dict = pet_dict

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать кормление."""
        # Проверить, является ли пользователь опекуном
        if interaction.user.id not in [self.pet_dict["owner_1_id"], self.pet_dict.get("owner_2_id")]:
            await interaction.response.send_message(
                replace_emojis("❌ Только опекуны могут кормить питомца!"),
                ephemeral=True
            )
            return

        # Проверить сытость
        if self.pet_dict["fullness"] >= 100:
            await interaction.response.send_message(
                replace_emojis("❌ Питомец уже сыт! Сытость: 100%"),
                ephemeral=True
            )
            return

        from storage.user_balance_store import user_balance_store

        # Проверить баланс
        balance = await user_balance_store.get_balance(interaction.guild_id, interaction.user.id)
        if balance < 30:
            await interaction.response.send_message(
                replace_emojis("❌ Недостаточно монет. Нужно: 30 🪙"),
                ephemeral=True
            )
            return

        # Списать деньги
        await user_balance_store.subtract_balance(interaction.guild_id, interaction.user.id, 30)

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
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            # Вернуть деньги
            await user_balance_store.add_balance(interaction.guild_id, interaction.user.id, 30)
            await interaction.response.send_message(
                replace_emojis(f"❌ {message}"),
                ephemeral=True
            )


class RenamePetButton(discord.ui.Button):
    """Кнопка переименования питомца."""

    def __init__(self, pet_id: int, pet_dict: dict, message: discord.Message):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Переименовать",
            custom_id=f"rename_pet:{pet_id}"
        )
        self.pet_id = pet_id
        self.pet_dict = pet_dict
        self.message = message

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать модальное окно для переименования."""
        # Проверить, является ли пользователь опекуном
        if interaction.user.id not in [self.pet_dict["owner_1_id"], self.pet_dict.get("owner_2_id")]:
            await interaction.response.send_message(
                replace_emojis("❌ Только опекуны могут переименовывать питомца!"),
                ephemeral=True
            )
            return

        await interaction.response.send_modal(RenamePetModal(self.pet_id, self.message, self.pet_dict))


class RenamePetModal(discord.ui.Modal):
    """Модальное окно переименования питомца."""

    def __init__(self, pet_id: int, message: discord.Message, pet_dict: dict):
        super().__init__(title="Переименовать питомца")
        self.pet_id = pet_id
        self.message = message
        self.pet_dict = pet_dict

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

        # Обновляем исходное сообщение с новым именем
        self.pet_dict["pet_name"] = new_name
        new_embed, new_file = await build_pet_profile_embed(self.pet_dict, interaction.guild)

        # Обновляем сообщение профиля через сохранённый message объект
        if self.message:
            try:
                if new_file:
                    await self.message.edit(embed=new_embed, attachments=[new_file])
                else:
                    await self.message.edit(embed=new_embed)
            except Exception as e:
                logger.error(f"Failed to edit pet profile message: {e}")

        await interaction.response.send_message(
            replace_emojis(f"✅ Питомец переименован в {new_name}"),
            ephemeral=True
        )


class ProtectItemButton(discord.ui.Button):
    """Кнопка защиты предмета (для Черепахи-хранителя)."""

    def __init__(self, pet_id: int, pet_dict: dict, interaction: discord.Interaction):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="🛡️ Защитить предмет",
            custom_id=f"protect_item:{pet_id}"
        )
        self.pet_id = pet_id
        self.pet_dict = pet_dict
        self.interaction = interaction

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать меню выбора предмета для защиты."""
        # Проверить, является ли пользователь опекуном
        if interaction.user.id not in [self.pet_dict["owner_1_id"], self.pet_dict.get("owner_2_id")]:
            await interaction.response.send_message(
                replace_emojis("❌ Только опекуны могут использовать защиту предмета!"),
                ephemeral=True
            )
            return

        # Получить инвентарь пользователя
        from storage.shop_store import inventory_store
        inventory = inventory_store.get_user_inventory(self.interaction.guild_id, interaction.user.id)

        if not inventory:
            await interaction.response.send_message(
                replace_emojis("❌ У вас нет предметов для защиты!"),
                ephemeral=True
            )
            return

        # Создать селект-меню
        from storage.shop_store import shop_store
        options = []
        for cosmetic in inventory:
            item = shop_store.get_item(cosmetic.item_id)
            if item:
                # Проверить, не защищён ли этот предмет другим опекуном
                from storage.pet_store import pet_store
                is_protected = await pet_store.is_item_protected(self.pet_id, item.id)
                if is_protected:
                    continue

                options.append(
                    discord.SelectOption(
                        label=item.name[:25],
                        description=f"{item.rarity.value}",
                        value=item.id
                    )
                )

        if not options:
            await interaction.response.send_message(
                replace_emojis("❌ Все предметы уже защищены!"),
                ephemeral=True
            )
            return

        view = ProtectItemSelectView(self.pet_id, options, self.interaction)
        await interaction.response.send_message(
            "Выберите предмет для защиты:",
            view=view,
            ephemeral=True
        )


class ProtectItemSelectView(discord.ui.View):
    """View для выбора предмета для защиты."""

    def __init__(self, pet_id: int, options: list, interaction: discord.Interaction):
        super().__init__(timeout=180)
        self.pet_id = pet_id
        self.interaction = interaction

        select = discord.ui.Select(
            placeholder="Выберите предмет...",
            options=options[:25]  # Discord limit is 25
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        """Обработка выбора предмета."""
        if not interaction.data:
            return

        selected_item_id = interaction.data["values"][0]

        # Установить защищаемый предмет
        from storage.pet_store import pet_store
        success = await pet_store.set_protected_item(self.pet_id, selected_item_id)

        if success:
            # Получить информацию о предмете
            from storage.shop_store import shop_store
            item = shop_store.get_item(selected_item_id)

            await interaction.response.edit_message(
                content=replace_emojis(f"✅ Предмет **{item.name}** теперь защищён питомцем!"),
                view=None
            )
        else:
            await interaction.response.edit_message(
                content=replace_emojis("❌ Не удалось защитить предмет."),
                view=None
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
        pet_type = self.pet_row["pet_type"]

        # Получить изображение питомца из каталога
        catalog_data = PETS_CATALOG.get(pet_type)
        image_url = None
        if catalog_data:
            image_url = catalog_data.get("image_url")

        public_embed = discord.Embed(
            title=replace_emojis("ПИТОМЕЦ ОТПУЩЕН НА ВОЛЮ"),
            description=(
                f"{replace_emojis('a_sparkle')} Опекун {interaction.user.mention} снял ошейник и попрощался с питомцем **{pet_name}**.\n\n"
                f"{replace_emojis('white_dot')} Питомец радостно побежал в дикую природу к своим сородичам.\n"
                f"{replace_emojis('white_dot')} Слот освободился. Новый питомец ждет вас в магазине (`/shop`)."
            ),
            color=discord.Color.from_rgb(149, 165, 166)
        )

        # Прикрепить изображение как аватарку
        file = None
        local_image_path = f"assets/pets/{pet_type}.png"
        if os.path.exists(local_image_path):
            filename = os.path.basename(local_image_path)
            file = discord.File(local_image_path, filename=filename)
            public_embed.set_thumbnail(url=f"attachment://{filename}")
        elif image_url:
            public_embed.set_thumbnail(url=image_url)

        if file:
            await interaction.followup.send(embed=public_embed, file=file)
        else:
            await interaction.followup.send(embed=public_embed)

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
        # Получаем эмодзи
        emoji_dot = get_emoji("white_dot")
        emoji_star = get_emoji("a_star")
        emoji_sparkle = get_emoji("a_sparkle")
        emoji_arrow = get_emoji("white_arrow")

        # Проверка: нажать может только тот, кого пригласили
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(f"{emoji_dot} Это предложение адресовано не вам!", ephemeral=True)
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Проверяем, не появился ли у целевого пользователя уже свой питомец
            existing_pet = await conn.fetchrow(
                "SELECT * FROM pets WHERE owner_1_id = $1 OR owner_2_id = $1",
                self.target_id
            )
            if existing_pet:
                await interaction.response.send_message(
                    f"{emoji_dot} Вы не можете стать со-опекуном, так как у вас уже есть питомец или вы являетесь со-опекуном другого!",
                    ephemeral=True
                )
                return

            # Обновляем запись в БД (записываем второго владельца)
            await conn.execute(
                "UPDATE pets SET owner_2_id = $1 WHERE pet_id = $2",
                self.target_id, self.pet_row["pet_id"]
            )

        owner_mention = f"<@{self.owner_id}>"
        pet_name = self.pet_row["pet_name"]

        success_embed = discord.Embed(
            title=f"{emoji_star} **НОВЫЙ СО-ОПЕКУН | УСПЕШНО**",
            description=(
                f"{emoji_sparkle} {interaction.user.mention} принял предложение и стал со-опекуном питомца **{pet_name}**!\n\n"
                f"{emoji_arrow} **Первый опекун:** {owner_mention}\n"
                f"{emoji_arrow} **Второй опекун:** {interaction.user.mention}\n\n"
                f"{emoji_dot} *Теперь вы оба можете кормить питомца и ухаживать за ним через `/pet profile`!*"
            ),
            color=0x2ECC71  # Пастельно-зеленый
        )

        # Отключаем кнопки и обновляем публичное сообщение
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(embed=success_embed, view=self)

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.secondary, custom_id="pet_coowner:decline")
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        emoji_dot = get_emoji("white_dot")
        emoji_star = get_emoji("a_star")

        if interaction.user.id != self.target_id:
            await interaction.response.send_message(f"{emoji_dot} Это предложение адресовано не вам!", ephemeral=True)
            return

        owner_mention = f"<@{self.owner_id}>"

        decline_embed = discord.Embed(
            title=f"{emoji_star} **ПРЕДЛОЖЕНИЕ ОТКЛОНЕНО**",
            description=(
                f"{emoji_dot} {interaction.user.mention} отклонил предложение стать со-опекуном питомца **{self.pet_row['pet_name']}**.\n\n"
                f"> {owner_mention}, ваш питомец остаётся под вашей единоличной опекой."
            ),
            color=0xE74C3C
        )

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(embed=decline_embed, view=self)


async def setup(bot: commands.Bot) -> None:
    """Setup function for the pet cog."""
    await bot.add_cog(PetCog(bot))
