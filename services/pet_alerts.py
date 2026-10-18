"""Сервис для отправки уведомлений о питомцах."""

import logging
import discord

logger = logging.getLogger(__name__)

async def send_runaway_alert(bot, pet_dict: dict, guild: discord.Guild) -> None:
    """Отправить уведомление о побеге питомца."""
    try:
        from config import get_emoji
        
        hunger_channel = bot.get_channel(1551167853741219880)
        if hunger_channel:
            # Пинг всех опекунов
            mentions = f"<@{pet_dict['owner_1_id']}>"
            if pet_dict.get("owner_2_id"):
                mentions += f" <@{pet_dict['owner_2_id']}>"

            embed = discord.Embed(
                title=f"{get_emoji('a_star')} **ПИТОМЕЦ СБЕЖАЛ ОТ ГОЛОДА**",
                description=f"{get_emoji('a_sparkle')} К сожалению, опекуны {mentions} забросили своего питомца...\n\n{get_emoji('white_arrow')} **Питомец:** **{pet_dict['pet_name']}** (*{pet_dict.get('rarity', 'Обычный')}*)\n{get_emoji('white_arrow')} **Причина:** Критический уровень сытости (`0%`) в течение длительного времени.\n\n{get_emoji('white_dot')} Питомец не выдержал голода, сорвал ошейник и убежал в тёмный лес к диким сородичам.\n{get_emoji('white_dot')} Слот питомца освобождён. Будьте внимательнее к следующему питомцу в `/petshop`!\n\n> {get_emoji('white_dot')} *История этого питомца завершена.*",
                color=0x960C0C
            )
            await hunger_channel.send(mentions, embed=embed)
            logger.info(f"Sent runaway alert for pet {pet_dict['pet_name']}")
    except Exception as e:
        logger.error(f"Failed to send runaway alert: {e}", exc_info=True)

async def send_hunger_alert(bot, pet_dict: dict, guild: discord.Guild) -> None:
    """Отправить уведомление о голоде питомца."""
    try:
        from config import get_emoji
        
        hunger_channel = bot.get_channel(1551167853741219880)
        if hunger_channel:
            # Пинг всех опекунов
            mentions = f"<@{pet_dict['owner_1_id']}>"
            if pet_dict.get("owner_2_id"):
                mentions += f" <@{pet_dict['owner_2_id']}>"

            embed = discord.Embed(
                title=f"{get_emoji('a_star')} **ВНИМАНИЕ | ПИТОМЕЦ ГОЛОДЕН**",
                description=f"{get_emoji('a_sparkle')} Опекуны {mentions}, ваш питомец проголодался!\n\n{get_emoji('white_arrow')} **Питомец:** **{pet_dict['pet_name']}**\n{get_emoji('white_arrow')} **Текущая сытость:** **`{pet_dict['fullness']}%`** / `100%` 🍖\n\n{get_emoji('white_dot')} Питомец чувствует слабость и требует ухода.\n{get_emoji('white_dot')} Покормите его вовремя, иначе он сорвёт ошейник и сбежит на волю!\n\n> {get_emoji('white_dot')} *Используйте профиль питомца (`/pet`), чтобы покормить.*",
                color=0xE69500
            )
            await hunger_channel.send(mentions, embed=embed)
            logger.info(f"Sent hunger alert for pet {pet_dict['pet_name']}")
    except Exception as e:
        logger.error(f"Failed to send hunger alert: {e}", exc_info=True)
