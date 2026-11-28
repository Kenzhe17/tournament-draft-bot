"""BLACKJACK 2.0 — точка входа /games blackjack и оркестрация.

Публичный контракт (совместимость с Dockerfile smoke-import и
cogs/tournament.py): ``start_blackjack(interaction)``.

Поток: ephemeral меню ставки (правила видны до списания) →
подтверждение → атомарное списание + раздача в PG → одно публичное
CV2-сообщение с PNG-столом. Ходы/settlement — в views/blackjack_view.py
и storage/blackjack_store.py.
"""

from __future__ import annotations

import logging

import discord

from config import GAMES_CHANNEL_ID, guild_allowed
from games.blackjack_engine import BET_MAX, BET_MIN, Round
from storage.blackjack_store import (
    ActiveGameExists,
    InsufficientFunds,
    blackjack_store,
)
from storage.user_balance_store import user_balance_store
from utils.design import DesignView

logger = logging.getLogger(__name__)


def _bet_limits(guild_id: int) -> tuple[int, int]:
    """Лимиты ставок из Control Center (economy.blackjack_*), fallback 10–10000."""
    try:
        from services.settings_service import settings_service
        lo = int(settings_service.peek(guild_id, "economy.blackjack_min_bet"))
        hi = int(settings_service.peek(guild_id, "economy.blackjack_max_bet"))
        if 0 <= lo <= hi <= 10**9:
            return lo, hi
    except Exception:
        logger.debug("blackjack bet limits fallback", exc_info=True)
    return BET_MIN, BET_MAX


def _bets_enabled(guild_id: int) -> bool:
    try:
        from services.settings_service import settings_service
        return bool(settings_service.peek(guild_id, "economy.blackjack_bets_enabled"))
    except Exception:  # noqa: BLE001 — флаг недоступен → ставки разрешены по умолчанию
        return True


def _minigames_enabled(guild_id: int) -> bool:
    try:
        from services.settings_service import settings_service
        return bool(settings_service.peek(guild_id, "feature.minigames_enabled"))
    except Exception:  # noqa: BLE001 — флаг недоступен → игра разрешена по умолчанию
        return True


async def _precheck(interaction: discord.Interaction) -> bool:
    """guild/channel/feature-флаги. False → уже отвечено ephemeral."""
    if interaction.guild_id is None or not guild_allowed(interaction.guild_id):
        await interaction.response.send_message(
            "Блэкджек доступен на сервере r!z3.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        return False
    if GAMES_CHANNEL_ID and interaction.channel_id != GAMES_CHANNEL_ID:
        await interaction.response.send_message(
            f"Чтобы сыграть в блэкджек, перейдите в <#{GAMES_CHANNEL_ID}>.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        return False
    if not _minigames_enabled(interaction.guild_id):
        await interaction.response.send_message(
            "Этот раздел временно отключён.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        return False
    return True


async def start_blackjack(interaction: discord.Interaction) -> None:
    """Slash-команда /games blackjack и кнопка «Сыграть» в /games list."""
    if not await _precheck(interaction):
        return
    await show_bet_menu(
        interaction,
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        channel_id=interaction.channel_id,
    )


async def show_bet_menu(
    interaction: discord.Interaction,
    *,
    guild_id: int,
    user_id: int,
    channel_id: int,
    reuse: tuple[int, int] | None = None,
) -> None:
    """Ephemeral меню ставки. ``reuse`` — (channel_id, message_id)
    публичного сообщения предыдущей раздачи для edit вместо нового."""
    from views.blackjack_view import BetMenuView

    try:
        active = await blackjack_store.get_active(guild_id, user_id)
    except Exception:
        active = None
        logger.info("blackjack get_active failed (non-fatal)", exc_info=True)
    if active is not None:
        await interaction.response.send_message(
            "У вас уже есть активная раздача — завершите её.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return
    balance = await user_balance_store.get_balance(guild_id, user_id)
    view = BetMenuView(guild_id, user_id, channel_id, balance, reuse=reuse)
    await interaction.response.send_message(
        view=view, ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none())


async def deal_round(
    interaction: discord.Interaction,
    *,
    guild_id: int,
    user_id: int,
    channel_id: int,
    stake: int,
    reuse: tuple[int, int] | None = None,
) -> None:
    """Подтверждённая ставка → атомарное списание + раздача + публичное сообщение.

    Вызывается из ConfirmStakeView._confirm (ephemeral-кнопка).
    """
    from views.blackjack_view import build_game_view

    # Сразу ack на ephemeral-кнопку — дальше работа может занять >3с.
    try:
        await interaction.response.edit_message(
            view=DesignView.simple(title="BLACKJACK", body="Создаём раздачу…"),
            allowed_mentions=discord.AllowedMentions.none())
    except discord.InteractionResponded:
        pass

    async def _fail(text: str) -> None:
        view = DesignView.simple(title="BLACKJACK", body=text)
        try:
            await interaction.edit_original_response(
                view=view, allowed_mentions=discord.AllowedMentions.none())
        except Exception:  # noqa: BLE001 — любой сбой Discord → пробуем followup
            try:
                await interaction.followup.send(
                    text, ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none())
            except Exception:  # уведомление о ошибке недоступно
                logger.debug("blackjack fail-notify failed", exc_info=True)

    if stake < 0 or stake > 10**9:
        await _fail("Некорректная ставка.")
        return
    lo, hi = _bet_limits(guild_id)
    if stake > 0:
        if not _bets_enabled(guild_id):
            await _fail("Ставки сейчас отключены администратором. "
                        "Доступна бесплатная игра.")
            return
        if stake < lo or stake > hi:
            await _fail(f"Ставка должна быть от {lo:,} до {hi:,}.")
            return

    if not _minigames_enabled(guild_id):
        await _fail("Этот раздел временно отключён.")
        return

    game = Round.deal()
    try:
        round_id = await blackjack_store.create_round(
            guild_id, user_id, channel_id, stake, game)
    except InsufficientFunds:
        balance = await user_balance_store.get_balance(guild_id, user_id)
        await _fail(f"Недостаточно монет. У вас: **{balance:,}**, "
                    f"ставка: **{stake:,}**.")
        return
    except ActiveGameExists:
        await _fail("У вас уже есть активная раздача.")
        return
    except Exception:
        logger.exception("blackjack create_round failed")
        await _fail("Не удалось создать раздачу. Попробуйте позже.")
        return

    stored = await blackjack_store.get_round(round_id)
    if stored is None:
        await _fail("Ошибка состояния раздачи.")
        return
    view, png = build_game_view(stored)

    delivered = False
    try:
        if reuse is not None:
            ch_id, msg_id = reuse
            channel = interaction.client.get_channel(ch_id)
            if channel is None:
                channel = await interaction.client.fetch_channel(ch_id)
            msg = await channel.fetch_message(msg_id)
            # message_id фиксируем ДО edit: при неоднозначной доставке
            # (ответ Discord потерян) состояние = «раздача видна» →
            # refund запрещён, исход решит sweeper/тайм-аут.
            try:
                await blackjack_store.mark_message(round_id, msg_id)
            except Exception:
                logger.warning("blackjack mark_message failed %s",
                               round_id, exc_info=True)
            kwargs = {"view": view,
                      "allowed_mentions": discord.AllowedMentions.none()}
            if png is not None:
                kwargs["attachments"] = [png]
            await msg.edit(**kwargs)
        else:
            kwargs = {"view": view, "ephemeral": False,
                      "allowed_mentions": discord.AllowedMentions.none(),
                      "wait": True}
            if png is not None:
                kwargs["files"] = [png]
            msg = await interaction.followup.send(**kwargs)
            if msg is not None:
                try:
                    await blackjack_store.mark_message(round_id, msg.id)
                except Exception:
                    logger.warning("blackjack mark_message failed %s",
                                   round_id, exc_info=True)
        delivered = True
    except Exception:
        logger.warning("blackjack deal delivery failed", exc_info=True)

    if not delivered:
        # Технический сбой до/при доставке: idempotent refund по
        # доказанному состоянию (ставка списана, message_id IS NULL).
        try:
            refunded = await blackjack_store.refund_undelivered(round_id)
        except Exception:
            refunded = False
            logger.exception("blackjack refund failed for %s", round_id)
        await _fail(
            "Не удалось отправить раздачу. "
            + ("Ставка возвращена." if refunded else
               "Статус раздачи проверяется — средства не потеряны."))
        return

    # ephemeral-подтверждение → короткий статус
    try:
        await interaction.edit_original_response(
            view=DesignView.simple(
                title="BLACKJACK",
                body="Раздача началась — карта выше. Удачи!"),
            allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        logger.debug("blackjack deal-status edit failed", exc_info=True)


# ------------------------------------------------------------------
# Тайм-аут / recovery
# ------------------------------------------------------------------

async def sweep_once() -> int:
    """Один проход тайм-аута: просроченные раздачи → авто-Stand + settle
    + обновление публичного сообщения. Возвращает число обработанных."""
    from views.blackjack_view import build_game_view

    try:
        settled = await blackjack_store.expire_due()
    except Exception:
        logger.warning("blackjack expire_due failed", exc_info=True)
        return 0
    client = _client
    for stored in settled:
        try:
            if client is None or stored.message_id is None:
                continue
            channel = client.get_channel(stored.channel_id)
            if channel is None:
                channel = await client.fetch_channel(stored.channel_id)
            msg = await channel.fetch_message(stored.message_id)
            view, png = build_game_view(stored)
            kwargs = {"view": view,
                      "allowed_mentions": discord.AllowedMentions.none()}
            if png is not None:
                kwargs["attachments"] = [png]
            await msg.edit(**kwargs)
        except Exception:
            logger.info("blackjack sweep edit skipped %s", stored.round_id,
                        exc_info=True)
    return len(settled)


async def recover_active(client: discord.Client) -> int:
    """После рестарта: перечитать активные раздачи и обновить сообщения
    (таймер/состояние), чтобы кнопки и текст соответствовали БД."""
    global _client
    _client = client
    try:
        active = await blackjack_store.list_active()
    except Exception:
        logger.warning("blackjack recovery list failed", exc_info=True)
        return 0
    from views.blackjack_view import build_game_view
    n = 0
    for stored in active:
        try:
            if stored.message_id is None:
                # раздача создана, но сообщение не доставлено —
                # до тайм-аута sweep закроет; refund только до message_id
                continue
            channel = client.get_channel(stored.channel_id)
            if channel is None:
                channel = await client.fetch_channel(stored.channel_id)
            msg = await channel.fetch_message(stored.message_id)
            view, png = build_game_view(stored)
            kwargs = {"view": view,
                      "allowed_mentions": discord.AllowedMentions.none()}
            if png is not None:
                kwargs["attachments"] = [png]
            await msg.edit(**kwargs)
            n += 1
        except Exception:
            logger.info("blackjack recover edit skipped %s",
                        stored.round_id, exc_info=True)
    if n:
        logger.info("blackjack: восстановлено %d активных раздач", n)
    return n


_client: discord.Client | None = None


async def blackjack_sweeper_loop(interval: float = 20.0) -> None:
    """Фоновый цикл тайм-аута (запускается из bot.setup_hook)."""
    import asyncio
    while True:
        try:
            await sweep_once()
        except Exception:
            logger.warning("blackjack sweeper iteration failed", exc_info=True)
        await asyncio.sleep(interval)
