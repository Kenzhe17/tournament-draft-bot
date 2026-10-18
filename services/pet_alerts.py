"""Сервис для отправки уведомлений о питомцах."""

import logging
import discord

logger = logging.getLogger(__name__)

async def send_runaway_alert(bot, pet_dict: dict, guild: discord.Guild) -> None:
    """Отправить уведомление о побеге питомца."""
    try:
        hunger_channel = bot.get_channel(1551167853741219880)
        if hunger_channel:
            embed = discord.Embed(
                title="🐾 Питомец сбежал",
                description=f"Питомец **{pet_dict['pet_name']}** сбежал от голода.",
                color=discord.Color.orange()
            )
            await hunger_channel.send(embed=embed)
            logger.info(f"Sent runaway alert for pet {pet_dict['pet_name']}")
    except Exception as e:
        logger.error(f"Failed to send runaway alert: {e}", exc_info=True)

async def send_hunger_alert(bot, pet_dict: dict, guild: discord.Guild) -> None:
    """Отправить уведомление о голоде питомца."""
    try:
        hunger_channel = bot.get_channel(1551167853741219880)
        if hunger_channel:
            embed = discord.Embed(
                title="⚠️ Питомец голоден",
                description=f"Питомец **{pet_dict['pet_name']}** голодает! Сытость: **{pet_dict['fullness']}%**\n\nПокормите его, иначе он сбежит!",
                color=discord.Color.yellow()
            )
            await hunger_channel.send(embed=embed)
            logger.info(f"Sent hunger alert for pet {pet_dict['pet_name']}")
    except Exception as e:
        logger.error(f"Failed to send hunger alert: {e}", exc_info=True)
