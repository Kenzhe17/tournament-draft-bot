"""Storage для системы питомцев."""

import logging
import random
import asyncpg
import discord
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

from models.pet import (
    Pet, PetType, PetRarity,
    EXP_REQUIREMENTS, RARITY_PRICES, ENERGY_COSTS
)

if TYPE_CHECKING:
    pass

# Импорт централизованного каталога
from models.pets_catalog import PETS_CATALOG, MYSTERY_EGG_DATA

# Вероятности выпадения из таинственного яйца
EGG_DROP_RATES = {
    "nothing": 0.80,
    "common": 0.12,
    "uncommon": 0.05,
    "epic": 0.024,
    "legendary": 0.006,
}

# Питомцы по редкости для случайного выбора
PETS_BY_RARITY = {
    "common": ["golden_fox", "scout_fox", "treasury_owl"],
    "uncommon": ["raccoon_robber", "raven_smuggler"],
    "epic": ["mimic_collector", "guardian_turtle"],
    "legendary": ["arena_wolf"],
}


class PetCatalog:
    """Каталог питомцев."""

    def __init__(self) -> None:
        self._pets = PETS_CATALOG

    def get_pet(self, pet_type: str) -> dict | None:
        """Получить определение питомца по типу."""
        return self._pets.get(pet_type)

    def get_all_pets(self) -> list[dict]:
        """Получить всех питомцев."""
        return list(self._pets.values())

    def get_pets_by_rarity(self, rarity: PetRarity) -> list[dict]:
        """Получить питомцев по редкости."""
        return [pet for pet in self._pets.values() if pet["rarity_code"] == rarity.value]


class PetStore:
    """Хранилище питомцев."""

    def __init__(self) -> None:
        self._use_db = True  # Использовать PostgreSQL
        self._catalog = PetCatalog()

    async def get_user_pet(self, guild_id: int, user_id: int) -> Pet | None:
        """Получить активного питомца пользователя."""
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            try:
                # Найти питомца пользователя (как owner_1 или owner_2)
                row = await conn.fetchrow(
                    "SELECT * FROM pets WHERE owner_1_id = $1 OR owner_2_id = $1",
                    user_id
                )
            except asyncpg.UndefinedTableError:
                # Таблица ещё не создана
                return None

            if not row:
                return None

            return self._row_to_pet(row)

    async def create_pet(
        self,
        owner_1_id: int,
        owner_2_id: int | None,
        pet_type: str,
        pet_name: str,
    ) -> Pet:
        """Создать нового питомца."""
        from storage.db import get_pool

        pet_def = self._catalog.get_pet(pet_type)
        if not pet_def:
            raise ValueError(f"Unknown pet type: {pet_type}")

        now = datetime.now(timezone.utc)

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Создать питомца
                row = await conn.fetchrow(
                    """
                    INSERT INTO pets (
                        owner_1_id, owner_2_id, pet_type, rarity, pet_name,
                        level, exp, fullness, energy,
                        last_fed_at, last_energy_update_at, created_at, hunger_started_at, hunger_warning_sent, runaway_alert_sent
                    ) VALUES ($1, $2, $3, $4, $5, 1, 0, 100, 100, $6, $6, $6, NULL, FALSE, FALSE)
                    RETURNING *
                    """,
                    owner_1_id, owner_2_id, pet_type, pet_def["rarity_code"],
                    pet_name, now
                )

                pet = self._row_to_pet(row)

        return pet

    async def delete_pet(self, pet_id: int) -> None:
        """Удалить питомца (каскадно удалит слоты)."""
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM pets WHERE pet_id = $1", pet_id)

    async def update_pet(self, pet: Pet) -> None:
        """Обновить данные питомца."""
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE pets SET
                    pet_name = $2,
                    level = $3,
                    exp = $4,
                    fullness = $5,
                    energy = $6,
                    last_fed_at = $7,
                    last_energy_update_at = $8,
                    protected_item_id = $9,
                    hunger_started_at = $10,
                    hunger_warning_sent = $11,
                    runaway_alert_sent = $12
                WHERE pet_id = $1
                """,
                pet.pet_id, pet.pet_name, pet.level, pet.exp,
                pet.fullness, pet.energy, pet.last_fed_at,
                pet.last_energy_update_at, pet.protected_item_id, pet.hunger_started_at, pet.hunger_warning_sent, pet.runaway_alert_sent
            )

    async def add_exp(self, pet_id: int, exp: int, max_daily: int = 50) -> int:
        """Добавить опыт питомцу с учётом дневного лимита."""
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Получить текущий питомец
            row = await conn.fetchrow("SELECT * FROM pets WHERE pet_id = $1", pet_id)
            if not row:
                return 0

            pet = self._row_to_pet(row)

            # Проверить дневной лимит (упрощённо - можно добавить таблицу для отслеживания)
            new_exp = min(pet.exp + exp, pet.exp + max_daily)

            # Возраст = exp // 50 + 1 (каждые 50 опыта = +1 возраст)
            new_level = new_exp // 50 + 1

            # Обновить
            await conn.execute(
                "UPDATE pets SET exp = $2, level = $3 WHERE pet_id = $1",
                pet_id, new_exp, new_level
            )

            return new_exp

    async def check_fullness_and_energy(self, pet_id: int, guild_id: int = None, bot=None) -> tuple[bool, Pet | None]:
        """Проверить сытость и энергию питомца. Возвращает (is_dead, pet)."""
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM pets WHERE pet_id = $1", pet_id)
            if not row:
                return False, None

            pet = self._row_to_pet(row)
            now = datetime.now(timezone.utc)

            # Сохраняем значения ДО изменений для проверок уведомлений
            old_fullness = pet.fullness
            old_energy = pet.energy
            was_full = pet.fullness > 0
            had_runaway_alert = pet.runaway_alert_sent

            # Рассчитать прошедшее время с последнего обновления
            hours_since_fed = (now - pet.last_fed_at).total_seconds() / 3600
            hours_since_energy = (now - pet.last_energy_update_at).total_seconds() / 3600

            # Пассивное уменьшение энергии (2 в час)
            energy_loss = int(hours_since_energy * 2)
            pet.energy = max(0, pet.energy - energy_loss)

            # Восстановление энергии если сытость > 70 (10 в час)
            if pet.fullness > 70:
                energy_gain = int(hours_since_energy * 10)
                pet.energy = min(100, pet.energy + energy_gain)

            # Пассивное уменьшение еды
            # Если энергии нет, еда уходит в 2 раза быстрее
            fullness_multiplier = 2 if pet.energy == 0 else 1
            fullness_loss = int(hours_since_fed / 6 * 5 * fullness_multiplier)  # 5 каждые 6 часов (или 10 если нет энергии)
            pet.fullness = max(0, pet.fullness - fullness_loss)

            # Проверить когда сытость достигла 0
            if pet.fullness == 0 and pet.hunger_started_at is None:
                pet.hunger_started_at = now

            # Если сытость 0 - автоматический побег
            if pet.fullness == 0:
                logger.info(f"Pet {pet.pet_name} fullness reached 0, was_full={was_full}, had_runaway_alert={had_runaway_alert}, guild_id={guild_id}, bot={bot is not None}")
                
                # Отправить уведомление о побеге с новым дизайном (только один раз)
                if not had_runaway_alert and guild_id and bot:
                    try:
                        logger.info(f"Sending runaway alert for pet {pet.pet_name}")
                        from config import get_emoji
                        from models.pets_catalog import PETS_CATALOG
                        import os
                        
                        hunger_channel = bot.get_channel(1551167853741219880)
                        logger.info(f"Got runaway channel: {hunger_channel}")
                        if hunger_channel:
                            # Пинг всех опекунов
                            mentions = f"<@{pet.owner_1_id}>"
                            if pet.owner_2_id:
                                mentions += f" <@{pet.owner_2_id}>"

                            # Получить изображение питомца
                            catalog_data = PETS_CATALOG.get(pet.pet_type.value, {})
                            image_url = catalog_data.get("image_url")
                            local_image_path = f"assets/pets/{pet.pet_type.value}.png"

                            embed = discord.Embed(
                                title=f"{get_emoji('a_star')} **ПИТОМЕЦ СБЕЖАЛ ОТ ГОЛОДА**",
                                description=f"{get_emoji('a_sparkle')} К сожалению, опекуны {mentions} забросили своего питомца...\n\n{get_emoji('white_arrow')} **Питомец:** **{pet.pet_name}** (*{pet.rarity.value if hasattr(pet, 'rarity') else 'Обычный'}*)\n{get_emoji('white_arrow')} **Причина:** Критический уровень сытости (`0%`) в течение длительного времени.\n\n{get_emoji('white_dot')} Питомец не выдержал голода, сорвал ошейник и убежал в тёмный лес к диким сородичам.\n{get_emoji('white_dot')} Слот питомца освобождён. Будьте внимательнее к следующему питомцу в `/petshop`!\n\n> {get_emoji('white_dot')} *История этого питомца завершена.*",
                                color=0x960C0C
                            )
                            
                            # Добавить изображение справа сверху
                            if os.path.exists(local_image_path):
                                filename = os.path.basename(local_image_path)
                                file = discord.File(local_image_path, filename=filename)
                                embed.set_thumbnail(url=f"attachment://{filename}")
                                await hunger_channel.send(mentions, embed=embed, file=file)
                            elif image_url:
                                embed.set_thumbnail(url=image_url)
                                await hunger_channel.send(mentions, embed=embed)
                            else:
                                await hunger_channel.send(mentions, embed=embed)
                            logger.info(f"Sent runaway alert for pet {pet.pet_name}")
                    except Exception as e:
                        logger.error(f"Failed to send runaway notification: {e}", exc_info=True)
                
                await self.delete_pet(pet_id)
                return True, None

            # Уведомление при достижении сытости <= 30
            logger.info(f"Checking hunger alert: pet={pet.pet_name}, fullness={pet.fullness}, old_fullness={old_fullness}, hunger_warning_sent={pet.hunger_warning_sent}, guild_id={guild_id}, bot={bot is not None}")
            if pet.fullness <= 30 and not pet.hunger_warning_sent and guild_id and bot:
                try:
                    logger.info(f"Pet {pet.pet_name} at {pet.fullness}% fullness, sending warning")
                    from config import get_emoji
                    from models.pets_catalog import PETS_CATALOG
                    import os
                    
                    hunger_channel = bot.get_channel(1551167853741219880)
                    logger.info(f"Got hunger channel: {hunger_channel}")
                    if hunger_channel:
                        # Пинг всех опекунов
                        mentions = f"<@{pet.owner_1_id}>"
                        if pet.owner_2_id:
                            mentions += f" <@{pet.owner_2_id}>"

                        # Получить изображение питомца
                        catalog_data = PETS_CATALOG.get(pet.pet_type.value, {})
                        image_url = catalog_data.get("image_url")
                        local_image_path = f"assets/pets/{pet.pet_type.value}.png"
                        
                        embed = discord.Embed(
                            title=f"{get_emoji('a_star')} **ВНИМАНИЕ | ПИТОМЕЦ ГОЛОДЕН**",
                            description=f"{get_emoji('a_sparkle')} Опекуны {mentions}, ваш питомец проголодался!\n\n{get_emoji('white_arrow')} **Питомец:** **{pet.pet_name}**\n{get_emoji('white_arrow')} **Текущая сытость:** **`{pet.fullness}%`** / `100%` 🍖\n\n{get_emoji('white_dot')} Питомец чувствует слабость и требует ухода.\n{get_emoji('white_dot')} Покормите его вовремя, иначе он сорвёт ошейник и сбежет на волю!\n\n> {get_emoji('white_dot')} *Используйте профиль питомца (`/pet`), чтобы покормить.*",
                            color=0xE69500
                        )
                        
                        # Добавить изображение справа сверху
                        if os.path.exists(local_image_path):
                            filename = os.path.basename(local_image_path)
                            file = discord.File(local_image_path, filename=filename)
                            embed.set_thumbnail(url=f"attachment://{filename}")
                            await hunger_channel.send(mentions, embed=embed, file=file)
                        elif image_url:
                            embed.set_thumbnail(url=image_url)
                            await hunger_channel.send(mentions, embed=embed)
                        else:
                            await hunger_channel.send(mentions, embed=embed)
                        logger.info(f"Successfully sent hunger warning for pet {pet.pet_name}")
                        pet.hunger_warning_sent = True
                    else:
                        logger.error(f"Could not get hunger channel 1551167853741219880")
                except Exception as e:
                    logger.error(f"Failed to send hunger warning for pet {pet.pet_name}: {e}", exc_info=True)

            # Обновить таймстампы
            pet.last_fed_at = now
            # НЕ обновляем last_energy_update_at здесь - это делается только при кормлении
            # чтобы энергия восстанавливалась пассивно при каждом просмотре профиля

            await self.update_pet(pet)

            return False, pet

    async def feed_pet(
        self,
        pet_id: int,
        fullness_gain: int,
        energy_gain: int,
        exp_gain: int,
    ) -> tuple[bool, str]:
        """Покормить питомца. Возвращает (success, message)."""
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Проверить жив ли питомец
            is_dead, pet = await self.check_fullness_and_energy(pet_id)
            if is_dead or not pet:
                return False, "Питомец умер"

            # Обновить сытость и энергию
            pet.fullness = min(100, pet.fullness + fullness_gain)
            pet.energy = min(100, pet.energy + energy_gain)
            pet.last_fed_at = datetime.now(timezone.utc)
            pet.last_energy_update_at = datetime.now(timezone.utc)  # Обновляем при кормлении
            
            # Сбросить флаг предупреждения если сытость поднялась выше 30
            if pet.fullness > 30:
                pet.hunger_warning_sent = False

            await self.update_pet(pet)

            # Добавить опыт
            await self.add_exp(pet_id, exp_gain)

            return True, "Питомец покормлен"

    async def use_energy(self, pet_id: int, amount: int) -> bool:
        """Списать энергию питомца. Возвращает True если достаточно энергии."""
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM pets WHERE pet_id = $1", pet_id)
            if not row:
                return False

            pet = self._row_to_pet(row)

            if pet.energy < amount:
                return False

            pet.energy -= amount
            # НЕ обновляем last_energy_update_at при использовании энергии
            # чтобы энергия восстанавливалась пассивно
            await self.update_pet(pet)
            return True

    async def consume_energy_for_ability(self, guild_id: int, user_id: int, action: str, bot=None) -> bool:
        """Списать энергию и сытость за использование способности.

        action может быть: 'case_open', 'pay', 'rob', 'rob_defense', 'elo_match'
        Возвращает True если энергии хватило и она была списана.
        
        Способности блокируются если:
        - Сытость < 30%
        - Энергия < требуемой
        """
        from models.pet import ENERGY_COSTS

        energy_cost = ENERGY_COSTS.get(action, 0)
        if energy_cost == 0:
            return True  # Без затрат энергии

        pet = await self.get_user_pet(guild_id, user_id)
        if not pet:
            return False
        
        # Блокировка способностей если сытость < 30 или энергии недостаточно
        if pet.fullness < 30:
            logger.info(f"Ability blocked: pet {pet.pet_name} fullness {pet.fullness}% < 30%")
            return False
        if pet.energy <= 0:
            logger.info(f"Ability blocked: pet {pet.pet_name} energy {pet.energy} <= 0")
            return False

        # Списать энергию
        success = await self.use_energy(pet.pet_id, energy_cost)
        if not success:
            return False

        # Добавить опыт за использование способности
        await self.add_exp(pet.pet_id, 3)  # 3 опыта за использование способности

        # Списать сытость (2 единицы за использование способности)
        from storage.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE pets SET fullness = GREATEST(0, fullness - 2) WHERE pet_id = $1",
                pet.pet_id
            )

        # Проверить сытость после использования способности для уведомлений
        if bot:
            await self.check_fullness_and_energy(pet.pet_id, guild_id, bot)

        return True

    async def rename_pet(self, pet_id: int, new_name: str) -> bool:
        """Переименовать питомца."""
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE pets SET pet_name = $2 WHERE pet_id = $1",
                pet_id, new_name
            )
            return True

    async def set_protected_item(self, pet_id: int, item_id: str | None) -> bool:
        """Установить защищаемый предмет."""
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE pets SET protected_item_id = $2 WHERE pet_id = $1",
                pet_id, item_id
            )
            return True

    async def is_item_protected(self, pet_id: int, item_id: str) -> bool:
        """Проверить защищён ли предмет данным питомцем."""
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT protected_item_id FROM pets WHERE pet_id = $1",
                pet_id
            )
            if row and row["protected_item_id"] == item_id:
                return True
        return False

    async def has_pet_slot(self, user_id: int) -> bool:
        """Проверить есть ли свободный слот у пользователя."""
        from storage.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            try:
                # Проверяем есть ли у пользователя питомец (как owner_1 или owner_2)
                row = await conn.fetchrow(
                    "SELECT pet_id FROM pets WHERE owner_1_id = $1 OR owner_2_id = $1",
                    user_id
                )
                return row is None
            except asyncpg.UndefinedTableError:
                # Таблица ещё не создана - считаем что слот свободен
                return True

    async def open_mystery_egg(self) -> dict:
        """Открыть таинственное яйцо. Возвращает результат."""
        roll = random.random()

        cumulative = 0.0
        for result, rate in EGG_DROP_RATES.items():
            cumulative += rate
            if roll < cumulative:
                if result == "nothing":
                    return {"type": "nothing", "pet": None}
                else:
                    # Выбрать случайного питомца этой редкости
                    pets = PETS_BY_RARITY.get(result, [])
                    if pets:
                        pet_type = random.choice(pets)
                        pet_def = self._catalog.get_pet(pet_type)
                        return {"type": "pet", "pet": pet_def}
                    return {"type": "nothing", "pet": None}

        return {"type": "nothing", "pet": None}

    def _row_to_pet(self, row) -> Pet:
        """Преобразовать строку БД в объект Pet."""
        return Pet(
            pet_id=row["pet_id"],
            owner_1_id=row["owner_1_id"],
            owner_2_id=row["owner_2_id"],
            pet_type=PetType(row["pet_type"]),
            rarity=PetRarity(row["rarity"]),
            pet_name=row["pet_name"],
            level=row["level"],
            exp=row["exp"],
            fullness=row["fullness"],
            energy=row["energy"],
            last_fed_at=row["last_fed_at"],
            last_energy_update_at=row["last_energy_update_at"],
            created_at=row["created_at"],
            protected_item_id=row["protected_item_id"],
            hunger_started_at=row.get("hunger_started_at"),
            hunger_warning_sent=row.get("hunger_warning_sent", False),
            runaway_alert_sent=row.get("runaway_alert_sent", False),
        )


# Глобальные экземпляры
pet_catalog = PetCatalog()
pet_store = PetStore()
