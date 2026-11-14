"""Жизненный цикл завершённого турнира: архивация + итоговая карточка.

Единая точка для «освобождения слота канала» после COMPLETE:
1. Турнир ОБЯЗАТЕЛЬНО архивируется (tournament_archive) —
   при ошибке слот НЕ освобождается, вызывающий код сообщает
   организатору и может повторить.
2. Сообщение турнира переводится в итоговую CV2-карточку
   победителя (build_winner_embed, без кнопок) — старые
   компоненты регистрации/фазы не остаются.
3. Запись удаляется из активного store — канал свободен для
   нового турнира.

Используется и для восстановления пострадавших карточек:
restore_final_card() строит ту же итоговую карточку из
архивного снимка и редактирует исходное сообщение — без
пересчёта ELO/истории/наград.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from storage.json_store import store
from storage.tournament_archive import tournament_archive
from utils.design import compose
from utils.embeds import build_winner_embed

if TYPE_CHECKING:
    from models.tournament import Tournament

logger = logging.getLogger(__name__)


async def _winner_card_view(
    tournament: "Tournament", guild: discord.Guild
) -> discord.ui.LayoutView:
    """Итоговая CV2-карточка победителя (без кнопок)."""
    embed = await build_winner_embed(tournament, guild)
    return compose(embed, None)


async def render_final_card_to_message(
    message: discord.Message,
    tournament: "Tournament",
    guild: discord.Guild,
) -> bool:
    """Отредактировать сообщение в итоговую карточку победителя.

    True — карточка обновлена; False — ошибка Discord API
    (вызывающий решает: для архивации не критично, для
    восстановления — надо повторить).
    """
    try:
        await message.edit(
            view=await _winner_card_view(tournament, guild),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "final card edit failed tid=%s msg=%s: %s",
            getattr(tournament, "id", "?"),
            getattr(message, "id", "?"), exc,
        )
        return False


async def release_completed_tournament(
    guild: discord.Guild,
    tournament: "Tournament",
    *,
    channel: discord.abc.GuildChannel | None = None,
) -> bool:
    """Заархивировать завершённый турнир и освободить слот канала.

    Возвращает True — слот освобождён (запись удалена из store,
    сообщение переведено в итоговую карточку либо попытка
    выполнена). False — АРХИВ НЕ СОХРАНЁН: активная запись
    НЕ удалена, организатор должен повторить операцию.
    """
    # 1. Архив — обязательное условие освобождения слота.
    if not tournament_archive.archive(tournament):
        logger.error(
            "release_completed: архивирование не удалось для %s — "
            "слот канала НЕ освобождён",
            getattr(tournament, "id", "?"),
        )
        return False

    # 2. Итоговая карточка в исходном сообщении (не smart_view
    #    с None — он оставлял старый экран фазы).
    message_id = getattr(tournament, "message_id", 0)
    if message_id:
        try:
            if channel is None:
                channel = guild.get_channel(tournament.channel_id)
            if channel is None:
                channel = await guild.fetch_channel(
                    tournament.channel_id)
            message = await channel.fetch_message(message_id)
            await render_final_card_to_message(
                message, tournament, guild)
        except Exception as exc:  # noqa: BLE001
            # Карточку можно восстановить позже из архива —
            # архивация уже подтверждена, слот освобождаем.
            logger.warning(
                "release_completed: итоговая карточка не "
                "обновлена tid=%s: %s",
                getattr(tournament, "id", "?"), exc,
            )

    # 3. Освобождаем слот канала.
    store.delete(tournament.guild_id, tournament.channel_id)
    return True


async def restore_final_card(
    tournament_id: str,
    *,
    client: discord.Client | None = None,
) -> bool:
    """Восстановить итоговую карточку из архивного снимка.

    Только отображение: турнир НЕ пересохраняется в store,
    ELO/история/монеты/XP не трогаются. Возвращает True при
    успешном редактировании сообщения.
    """
    entry = tournament_archive.get(str(tournament_id))
    if entry is None:
        logger.warning("restore: турнир %s не найден в архиве",
                       tournament_id)
        return False
    data = entry.get("data") or {}
    try:
        from models.tournament import Tournament
        tournament = Tournament.from_dict(data)
    except Exception as exc:  # noqa: BLE001
        logger.error("restore: from_dict failed %s: %s",
                     tournament_id, exc)
        return False
    if not getattr(tournament, "message_id", 0):
        logger.warning("restore: у %s нет message_id", tournament_id)
        return False

    bot = client
    if bot is None:
        import bot as bot_module
        bot = getattr(bot_module, "bot", None)
    if bot is None:
        logger.error("restore: нет клиента Discord")
        return False

    guild = bot.get_guild(int(entry.get("guild_id") or 0))
    if guild is None:
        try:
            guild = await bot.fetch_guild(
                int(entry.get("guild_id") or 0))
        except Exception as exc:  # noqa: BLE001
            logger.error("restore: guild fetch failed: %s", exc)
            return False
    channel = guild.get_channel(int(entry.get("channel_id") or 0))
    if channel is None:
        try:
            channel = await guild.fetch_channel(
                int(entry.get("channel_id") or 0))
        except Exception as exc:  # noqa: BLE001
            logger.error("restore: channel fetch failed: %s", exc)
            return False
    try:
        message = await channel.fetch_message(tournament.message_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("restore: message fetch failed: %s", exc)
        return False
    return await render_final_card_to_message(message, tournament, guild)
