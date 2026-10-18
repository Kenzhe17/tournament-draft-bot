"""Сервис для отправки уведомлений о питомцах."""

import discord
from data.pets_catalog import (
    EMOJI_STAR,
    EMOJI_SPARKLE,
    EMOJI_ARROW,
    EMOJI_DOT
)

PET_ALERT_CHANNEL_ID = 1551167853741219880


async def send_hunger_alert(bot: discord.Client, pet_row: dict, guild: discord.Guild):
    """Отправляет предупреждение о голоде питомца с упоминанием всех опекунов."""
    channel = bot.get_channel(PET_ALERT_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(PET_ALERT_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden):
            return

    pet_name = pet_row["pet_name"]
    fullness = pet_row["fullness"]

    # Формируем упоминания главного опекуна и со-опекуна (если он есть)
    owner1 = guild.get_member(pet_row["owner_1_id"])
    owner1_mention = owner1.mention if owner1 else f"<@{pet_row['owner_1_id']}>"
    
    owner2_mention = ""
    if pet_row.get("owner_2_id"):
        owner2 = guild.get_member(pet_row["owner_2_id"])
        if owner2:
            owner2_mention = f" & {owner2.mention}"
        else:
            owner2_mention = f" & <@{pet_row['owner_2_id']}>"

    all_owners = f"{owner1_mention}{owner2_mention}"

    embed = discord.Embed(
        title=f"{EMOJI_STAR} **ВНИМАНИЕ | ПИТОМЕЦ ГОЛОДЕН**",
        description=(
            f"{EMOJI_SPARKLE} Опекуны {all_owners}, ваш питомец проголодался!\n\n"
            f"{EMOJI_ARROW} **Питомец:** **{pet_name}**\n"
            f"{EMOJI_ARROW} **Текущая сытость:** **`{fullness}%`** / `100%` 🍖\n\n"
            f"{EMOJI_DOT} Питомец чувствует слабость и требует ухода.\n"
            f"{EMOJI_DOT} Покормите его вовремя, иначе он сорвёт ошейник и сбежит на волю!\n\n"
            f"> <:white_dot:0000> *Используйте профиль питомца (`/pet`), чтобы покормить.*"
        ),
        color=0xE67E22
    )

    # Отправляем сообщение в канал с публичным пингом опекунов
    await channel.send(content=f"⚠️ {all_owners}", embed=embed)


async def send_runaway_alert(bot: discord.Client, pet_row: dict, guild: discord.Guild):
    """Отправляет публичное уведомление о побеге питомца из-за 0% сытости."""
    channel = bot.get_channel(PET_ALERT_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(PET_ALERT_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden):
            return

    pet_name = pet_row["pet_name"]
    rarity = pet_row.get("rarity", "Обычный")

    # Формируем упоминания опекунов
    owner1 = guild.get_member(pet_row["owner_1_id"])
    owner1_mention = owner1.mention if owner1 else f"<@{pet_row['owner_1_id']}>"
    
    owner2_mention = ""
    if pet_row.get("owner_2_id"):
        owner2 = guild.get_member(pet_row["owner_2_id"])
        if owner2:
            owner2_mention = f" & {owner2.mention}"
        else:
            owner2_mention = f" & <@{pet_row['owner_2_id']}>"

    all_owners = f"{owner1_mention}{owner2_mention}"

    embed = discord.Embed(
        title=f"{EMOJI_STAR} **ПИТОМЕЦ СБЕЖАЛ ОТ ГОЛОДА**",
        description=(
            f"{EMOJI_SPARKLE} К сожалению, опекуны {all_owners} забросили своего питомца...\n\n"
            f"{EMOJI_ARROW} **Питомец:** **{pet_name}** (*{rarity}*)\n"
            f"{EMOJI_ARROW} **Причина:** Критический уровень сытости (`0%`) в течение длительного времени.\n\n"
            f"{EMOJI_DOT} Питомец не выдержал голода, сорвал ошейник и убежал в тёмный лес к диким сородичам.\n"
            f"{EMOJI_DOT} Слот питомца освобождён. Будьте внимательнее к следующему питомцу в `/shop`!\n\n"
            f"> <:white_dot:0000> *История этого питомца завершена.*"
        ),
        color=0x95A5A6  # Нейтральный пастельно-серый цвет ухода
    )

    # Публичная отправка в канал с пингом опекунов
    await channel.send(content=f"💨 {all_owners}", embed=embed)
