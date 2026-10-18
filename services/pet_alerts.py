"""Сервис для отправки уведомлений о питомцах."""

import logging
import discord

logger = logging.getLogger(__name__)

async def send_runaway_alert(bot, pet_dict: dict, guild: discord.Guild) -> None:
    """Отправить уведомление о побеге питомца."""
    try:
        hunger_channel = bot.get_channel(1551167853741219880)
        if hunger_channel:
            # Пинг всех опекунов
            mentions = f"<@{pet_dict['owner_1_id']}>"
            if pet_dict.get("owner_2_id"):
                mentions += f" <@{pet_dict['owner_2_id']}>"

            embed = discord.Embed(
                title="� ПИТОМЕЦ СБЕЖАЛ ОТ ГОЛОДА",
                description=f"<a:a_star:0000> К сожалению, опекуны {mentions} забросили своего питомца...\n\n<:white_arrow:0000> **Питомец:** **{pet_dict['pet_name']}** (*{pet_dict.get('rarity', 'Обычный')}*)\n<:white_arrow:0000> **Причина:** Критический уровень сытости (`0%`) в течение длительного времени.\n\n<:white_dot:0000> Питомец не выдержал голода, сорвал ошейник и убежал в тёмный лес к диким сородичам.\n<:white_dot:0000> Слот питомца освобождён. Будьте внимательнее к следующему питомцу в `/petshop`!\n\n> <:white_dot:0000> *История этого питомца завершена.*",
                color=0x960C0C
            )
            await hunger_channel.send(mentions, embed=embed)
            logger.info(f"Sent runaway alert for pet {pet_dict['pet_name']}")
    except Exception as e:
        logger.error(f"Failed to send runaway alert: {e}", exc_info=True)

async def send_hunger_alert(bot, pet_dict: dict, guild: discord.Guild) -> None:
    """Отправить уведомление о голоде питомца."""
    try:
        hunger_channel = bot.get_channel(1551167853741219880)
        if hunger_channel:
            # Пинг всех опекунов
            mentions = f"<@{pet_dict['owner_1_id']}>"
            if pet_dict.get("owner_2_id"):
                mentions += f" <@{pet_dict['owner_2_id']}>"

            embed = discord.Embed(
                title="⚠️ ВНИМАНИЕ | ПИТОМЕЦ ГОЛОДЕН",
                description=f"<a:a_sparkle:0000> Опекуны {mentions}, ваш питомец проголодался!\n\n<:white_arrow:0000> **Питомец:** **{pet_dict['pet_name']}**\n<:white_arrow:0000> **Текущая сытость:** **`{pet_dict['fullness']}%`** / `100%` 🍖\n\n<:white_dot:0000> Питомец чувствует слабость и требует ухода.\n<:white_dot:0000> Покормите его вовремя, иначе он сорвёт ошейник и сбежит на волю!\n\n> <:white_dot:0000> *Используйте профиль питомца (`/pet`), чтобы покормить.*",
                color=0xE69500
            )
            await hunger_channel.send(mentions, embed=embed)
            logger.info(f"Sent hunger alert for pet {pet_dict['pet_name']}")
    except Exception as e:
        logger.error(f"Failed to send hunger alert: {e}", exc_info=True)
