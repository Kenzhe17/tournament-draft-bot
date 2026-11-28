"""BLACKJACK 2.0 — точка входа /games blackjack и оркестрация.

Публичный контракт (совместимость с Dockerfile smoke-import и
cogs/tournament.py): ``start_blackjack(interaction)``.

Поток: ephemeral меню ставки (правила видны до списания) →
подтверждение (op_id — идемпотентность) → атомарное списание +
раздача в PG → одно публичное CV2-сообщение с PNG-столом.
Ходы/settlement — в views/blackjack_view.py и storage/blackjack_store.py.

Доставка имеет явный state machine (delivery_state):
    pending → sending → delivered | unknown | failed
Refund выполняется ТОЛЬКО при доказанной недоставке
('pending' — HTTP не вызывался; 'failed' — reconcile не нашёл
сообщение с маркером ``ref <round_id>`` в истории канала).
Неоднозначная доставка ('sending'/'unknown') никогда не
рефандится автоматически.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

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

# Сколько ждать после создания раздачи, прежде чем считать
# 'pending'/'sending'/'unknown' кандидатами на reconcile. Даёт
# запас на штатную доставку и медленный Discord API.
DELIVERY_GRACE_SECONDS = 120


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
    """Fail-CLOSED: при ошибке настроек ставки ЗАПРЕЩЕНЫ
    (бесплатная игра остаётся). Иначе админ-отключение могло бы
    молча не сработать."""
    try:
        from services.settings_service import settings_service
        return bool(settings_service.peek(guild_id, "economy.blackjack_bets_enabled"))
    except Exception:
        logger.warning("blackjack bets_enabled check failed — "
                       "ставки запрещены до восстановления настроек",
                       exc_info=True)
        return False


def _minigames_enabled(guild_id: int) -> bool:
    try:
        from services.settings_service import settings_service
        return bool(settings_service.peek(guild_id, "feature.minigames_enabled"))
    except Exception:
        logger.warning("blackjack minigames_enabled check failed",
                       exc_info=True)
        return False


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
    публичного сообщения предыдущей раздачи для edit вместо нового.

    PG-запросы перед ответом → defer(ephemeral) при необходимости,
    затем followup. Работает и из slash, и из deferred component
    interaction (тогда defer пропускается)."""
    from views.blackjack_view import BetMenuView

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    async def _note(text: str) -> None:
        await interaction.followup.send(
            text, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    try:
        active = await blackjack_store.get_active(guild_id, user_id)
    except Exception:
        active = None
        logger.info("blackjack get_active failed (non-fatal)", exc_info=True)
    if active is not None:
        await _note("У вас уже есть активная раздача — завершите её.")
        return
    try:
        balance = await user_balance_store.get_balance(guild_id, user_id)
    except Exception:
        logger.exception("blackjack balance read failed")
        await _note("Не удалось прочитать баланс. Попробуйте позже.")
        return
    view = BetMenuView(guild_id, user_id, channel_id, balance, reuse=reuse)
    await interaction.followup.send(
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
    op_id: str | None = None,
) -> None:
    """Подтверждённая ставка → атомарное списание + раздача + доставка.

    ``op_id`` делает подтверждение идемпотентным: повторный вызов с
    тем же op_id (двойной клик, retry) возвращает существующий round_id
    без повторного списания — включая случай мгновенного natural,
    когда partial unique index уже не защищает от второй раздачи.
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
            except Exception:
                logger.debug("blackjack fail-notify failed", exc_info=True)

    if stake < 0 or stake > 10**9:
        await _fail("Некорректная ставка.")
        return
    lo, hi = _bet_limits(guild_id)
    if stake > 0:
        if not _bets_enabled(guild_id):
            await _fail("Ставки сейчас отключены. "
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
        round_id, created = await blackjack_store.create_round(
            guild_id, user_id, channel_id, stake, game, op_id=op_id)
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
    if not created:
        # идемпотентный повтор подтверждения
        if stored.message_id is not None:
            await _fail("Раздача уже создана — смотрите сообщение выше.")
            return
        if stored.status not in ("active", "settled"):
            await _fail("Эта раздача уже завершена.")
            return
        if stored.delivery_state in ("sending", "unknown", "delivered"):
            # первая попытка ещё в полёте или доставка подтверждена
            # без message_id — не создаём второе публичное сообщение
            await _fail("Раздача уже отправляется — смотрите канал.")
            return
        # delivery_state pending/failed → повторная попытка доставки
        logger.info("blackjack op replay: redelivering %s", round_id)

    view, png = build_game_view(stored)

    # ---------- доставка с явными состояниями ----------
    # Pre-send: всё, что может доказуемо НЕ дойти до Discord.
    channel = None
    target_msg = None
    try:
        if reuse is not None:
            ch_id, msg_id = reuse
            channel = interaction.client.get_channel(ch_id)
            if channel is None:
                channel = await interaction.client.fetch_channel(ch_id)
            target_msg = await channel.fetch_message(msg_id)
        kwargs = {"view": view, "allowed_mentions": discord.AllowedMentions.none()}
        if reuse is not None:
            if png is not None:
                kwargs["attachments"] = [png]
        else:
            kwargs["ephemeral"] = False
            kwargs["wait"] = True
            if png is not None:
                kwargs["files"] = [png]
    except Exception:
        # Discord ещё не вызывался → доставка доказуемо не состоялась
        logger.warning("blackjack pre-send failed for %s", round_id,
                       exc_info=True)
        try:
            await blackjack_store.mark_delivery(round_id, "failed")
        except Exception:
            logger.exception("blackjack mark_delivery(failed) failed")
        await _refund_and_fail(interaction, round_id, _fail)
        return

    # Граница отправки: с этого момента исход HTTP-вызова неизвестен.
    try:
        await blackjack_store.mark_delivery(round_id, "sending")
    except Exception:
        # Не отправляем без зафиксированного состояния — иначе
        # возможна доставка при 'pending' → ложный refund.
        logger.exception("blackjack mark_delivery(sending) failed %s",
                         round_id)
        await _fail("Не удалось создать раздачу. Попробуйте позже.")
        return

    delivered = False
    try:
        if reuse is not None:
            await target_msg.edit(**kwargs)
            sent_id = target_msg.id
        else:
            msg = await interaction.followup.send(**kwargs)
            sent_id = msg.id if msg is not None else None
        if sent_id is not None:
            delivered = True
    except Exception:
        logger.warning("blackjack deal delivery failed for %s",
                       round_id, exc_info=True)

    if delivered:
        try:
            await blackjack_store.mark_message(round_id, sent_id)
        except Exception:
            # Ответ Discord потерян ПОСЛЕ отправки — delivery
            # доказан msg.id: фиксируем 'delivered' без message_id,
            # refund запрещён, reconcile найдёт сообщение по маркеру.
            logger.exception("blackjack mark_message failed %s", round_id)
            try:
                await blackjack_store.mark_delivery(round_id, "delivered")
            except Exception:
                logger.exception("blackjack mark_delivery(delivered) failed")
    else:
        try:
            await blackjack_store.mark_delivery(round_id, "unknown")
        except Exception:
            logger.exception("blackjack mark_delivery(unknown) failed")
        # 'unknown' → НЕ рефандим: сообщение могло дойти.
        # Reconcile в sweeper найдёт маркер или докажет отсутствие.
        settled_note = ""
        try:
            cur = await blackjack_store.get_round(round_id)
            if cur is not None and cur.status == "settled":
                settled_note = " Раздача уже рассчитана."
        except Exception:
            logger.debug("blackjack settled-check failed", exc_info=True)
        await _fail(
            "Не удалось подтвердить доставку раздачи. "
            "Статус проверяется — средства не потеряны." + settled_note)
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


async def _refund_and_fail(interaction, round_id: str, fail) -> None:
    """Доказанная недоставка ('failed'/'pending') → idempotent refund."""
    try:
        refunded = await blackjack_store.refund_undelivered(round_id)
    except Exception:
        refunded = False
        logger.exception("blackjack refund failed for %s", round_id)
    await fail(
        "Не удалось отправить раздачу. "
        + ("Ставка возвращена." if refunded else
           "Статус раздачи проверяется — средства не потеряны."))


# ------------------------------------------------------------------
# Тайм-аут / доставка / recovery
# ------------------------------------------------------------------

_DELIVERY_MARKER_PREFIX = "ref "


def _msg_has_marker(msg: discord.Message, round_id: str) -> bool:
    """CV2-сообщение содержит маркер ``-# ref <round_id>`` в components."""
    try:
        blob = json.dumps(
            [c.to_dict() for c in (msg.components or [])],
            ensure_ascii=False,
        )
    except Exception:  # noqa: BLE001 — to_dict может падать на чужих типах
        return False
    return f"{_DELIVERY_MARKER_PREFIX}{round_id}" in blob


async def _find_delivery_message(
    client: discord.Client, stored
) -> discord.Message | None:
    """Найти публичное сообщение раздачи по маркеру в истории канала."""
    channel = client.get_channel(stored.channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(stored.channel_id)
        except Exception:  # noqa: BLE001 — канал удалён/недоступен
            return None
    since = (stored.created_at or datetime.now(UTC)) - timedelta(seconds=10)
    try:
        async for msg in channel.history(
            limit=500, after=since, oldest_first=True
        ):
            if _msg_has_marker(msg, stored.round_id):
                return msg
    except Exception:
        logger.info("blackjack marker scan failed for %s",
                    stored.round_id, exc_info=True)
    return None


async def reconcile_undelivered(client: discord.Client) -> int:
    """Сверка недоставленных раздач старше DELIVERY_GRACE_SECONDS.

    - 'pending' — HTTP к Discord не вызывался → доказанная
      недоставка → refund ('failed' обрабатывается так же).
    - 'sending'/'unknown' — ищем маркер в истории канала:
      найден → delivered; не найден → 'failed' → refund.
      Неоднозначность разрешается доказательством, а не угадыванием.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=DELIVERY_GRACE_SECONDS)
    try:
        rounds = await blackjack_store.list_undelivered(cutoff)
    except Exception:
        logger.warning("blackjack list_undelivered failed", exc_info=True)
        return 0
    handled = 0
    for stored in rounds:
        try:
            if stored.delivery_state in ("pending", "failed"):
                # 'pending': send не вызывался; 'failed': уже доказано
                if await blackjack_store.refund_undelivered(stored.round_id):
                    handled += 1
                continue
            if stored.delivery_state == "delivered":
                # отправка была, id потерян → только поиск по маркеру
                msg = await _find_delivery_message(client, stored)
                if msg is not None:
                    try:
                        await blackjack_store.mark_message(
                            stored.round_id, msg.id)
                        handled += 1
                    except Exception:
                        logger.exception("blackjack reconcile mark failed")
                continue
            # sending | unknown → reconcile по маркеру
            msg = await _find_delivery_message(client, stored)
            if msg is not None:
                try:
                    await blackjack_store.mark_message(
                        stored.round_id, msg.id)
                    handled += 1
                    logger.info("blackjack reconcile: delivery confirmed "
                                "for %s (msg %s)", stored.round_id, msg.id)
                except Exception:
                    logger.exception("blackjack reconcile mark failed")
            else:
                # Канал недоступен → НЕ доказываем отсутствие:
                # оставляем 'unknown' до следующего прохода.
                channel = client.get_channel(stored.channel_id)
                if channel is None:
                    try:
                        channel = await client.fetch_channel(
                            stored.channel_id)
                    except Exception:  # noqa: BLE001 — канал недоступен
                        channel = None
                if channel is not None:
                    await blackjack_store.mark_delivery(
                        stored.round_id, "failed")
                    if await blackjack_store.refund_undelivered(
                            stored.round_id):
                        handled += 1
        except Exception:
            logger.warning("blackjack reconcile failed for %s",
                           stored.round_id, exc_info=True)
    return handled


async def sweep_once() -> int:
    """Один проход: просроченные раздачи → авто-Stand + settle
    + обновление публичного сообщения; затем reconcile доставки."""
    from views.blackjack_view import build_game_view

    try:
        settled = await blackjack_store.expire_due()
    except Exception:
        logger.warning("blackjack expire_due failed", exc_info=True)
        settled = []
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
    if client is not None:
        try:
            await reconcile_undelivered(client)
        except Exception:
            logger.warning("blackjack reconcile pass failed", exc_info=True)
    return len(settled)


async def recover_active(client: discord.Client) -> int:
    """После рестарта: перечитать активные раздачи, обновить сообщения
    и сверить недоставленные. Идемпотентно — безопасен при повторном
    запуске."""
    global _client
    _client = client
    try:
        active = await blackjack_store.list_active()
    except Exception:
        logger.warning("blackjack recovery list failed", exc_info=True)
        active = []
    from views.blackjack_view import build_game_view
    n = 0
    for stored in active:
        try:
            if stored.message_id is None:
                # доставка не подтверждена — reconcile/тайм-аут решат
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
    # сверка доставки — отдельный проход, не зависит от list_active
    try:
        await reconcile_undelivered(client)
    except Exception:
        logger.warning("blackjack recover reconcile failed", exc_info=True)
    return n


_client: discord.Client | None = None


async def blackjack_sweeper_loop(interval: float = 20.0) -> None:
    """Фоновый цикл тайм-аута и reconcile (запускается из bot.setup_hook)."""
    import asyncio
    while True:
        try:
            await sweep_once()
        except Exception:
            logger.warning("blackjack sweeper iteration failed", exc_info=True)
        await asyncio.sleep(interval)
