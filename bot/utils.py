"""Вспомогательные функции."""

from __future__ import annotations

import asyncio

import discord


async def send_ephemeral_and_delete(
    interaction: discord.Interaction,
    content: str,
    delay: float = 3.0,
) -> None:
    """Отправить ephemeral-ответ и удалить через delay секунд."""
    await interaction.response.send_message(content, ephemeral=True)
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass
